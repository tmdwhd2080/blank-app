# -*- coding: utf-8 -*-
"""
ETF iNAV-lag directional strategy: collect iNAV + LOB and log ENTER/EXIT signals.

Default target:
    457990  PLUS 태양광&ESS

Thesis: an ETF LP requotes mechanically toward iNAV and takes no directional view.
The published iNAV is delayed, so when the basket trends we can predict iNAV's
near-term direction and trade ahead of the LP's repricing, then sell back into the
LP as it catches up. This is a directional (not market-neutral) lag-momentum trade,
so exits/stops are half the strategy.

How the two feeds are used (per tick):
    0G ETF NAV   -> direction model: time-normalized velocity, acceleration, a
                    rising-streak, age, and a forward-projected fair value
                    (fair_now = nav_last * (1 + v_per_sec * min(age, cap))).
    0D orderbook -> confirmation + execution: is the LP still lagging (ask1 <
                    fair_now), does order flow agree (OFI / ask-retreat), is the
                    touch executable (ask1 size, spread); bid1 is the exit ref.

Entry (FLAT->LONG): eligibility gates + ONE alpha score (no double-counting):
    eligibility: iNAV fresh (age<=max) & executable (ask1 size>=min, spread<=max)
                 & not chasing (ask1 <= fair*(1+max_overpay)) & iNAV rising
                 (streak>=K & v>=vel_entry) & not in cooldown.
    alpha:       alpha_score >= threshold  (carries flow / edge / depth / accel).
Exit (LONG->FLAT), any rule:
    bid1 >= entry fair_now (converge_tp) | velocity flips (reversal_stop) |
    velocity ~0 & not losing (exhaust_tp) | hard stop | time stop.

This is a collector/signal logger, not an order executor. It subscribes to Kiwoom
WebSocket real-time feeds (0D orderbook depth, 0G ETF NAV) and saves raw values so
field mapping can be audited later.

Example:
    python trading/etf_inav_lob_alpha_collect.py --code 457990 --start 08:55 --end 15:30
    python trading/etf_inav_lob_alpha_collect.py --code 457990 --duration-min 10
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import math
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any

import requests
import websockets

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from trading.config import ConfigError, KiwoomConfig, load_config
from trading.kiwoom.auth import TokenManager


log = logging.getLogger(__name__)


ORDERBOOK_TYPE = "0D"
ETF_NAV_TYPE = "0G"
TRADE_TYPE = "0B"          # 주식체결 (trade prints) — needed to model limit-order fills
DEFAULT_CODES = ("457990",)

# Kiwoom stock orderbook depth FIDs. These match the classic OpenAPI real type
# layout and are also used by the REST/WebSocket real-time values payload.
QUOTE_TIME_FID = "21"
ASK_PRICE_FIDS = {level: str(40 + level) for level in range(1, 11)}
BID_PRICE_FIDS = {level: str(50 + level) for level in range(1, 11)}
ASK_SIZE_FIDS = {level: str(60 + level) for level in range(1, 11)}
BID_SIZE_FIDS = {level: str(70 + level) for level in range(1, 11)}

# ETF NAV real-time FID names can vary across docs/wrappers. The script stores
# raw 0G values and lets --nav-fid override this auto-detection.
NAV_CANDIDATE_FIDS = (
    "36",
    "37",
    "131",
    "132",
    "250",
    "251",
    "nav",
    "iNAV",
    "inav",
    "estimated_nav",
)

# Stock trade (체결) FIDs. The maker fill model needs the trade PRICE; a resting
# buy limit fills when sell-initiated prints reach it. FID guesses follow the
# classic OpenAPI layout and may need a `probe` to confirm — raw 0B values are
# always stored so the mapping can be re-parsed offline.
TRADE_PRICE_FIDS = ("10",)   # 체결가(현재가)
TRADE_QTY_FIDS = ("15",)     # 체결량 (sign may encode aggressor side)
TRADE_TIME_FID = "20"        # 체결시간 HHMMSS


def _num(value: Any, *, abs_value: bool = False) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        out = float(text)
    except ValueError:
        return None
    if abs_value:
        out = abs(out)
    if not math.isfinite(out):
        return None
    return out


def _int(value: Any, *, abs_value: bool = True) -> int | None:
    out = _num(value, abs_value=abs_value)
    if out is None:
        return None
    return int(out)


def _bps(numerator: float, denominator: float | None) -> float:
    if not denominator:
        return 0.0
    return numerator / denominator * 10000.0


def _clip(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _parse_hhmm(value: str) -> dtime:
    hour, minute = value.split(":")
    return dtime(int(hour), int(minute))


def _clean_code(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text.startswith("A") and len(text) == 7:
        text = text[1:]
    return text


def parse_trade(values: dict[str, Any]) -> dict[str, Any]:
    """Parse a 0B trade print -> {trade_price, trade_qty, trade_sign, trade_time}.

    trade_sign keeps the FID-15 sign if present (+buy / -sell aggressor), but the
    backtest does not rely on it: it infers the aggressor by comparing trade_price
    to the prevailing book (price <= bid => sell-initiated => fills a resting bid).
    """
    price = None
    for fid in TRADE_PRICE_FIDS:
        price = _num(values.get(fid), abs_value=True)
        if price:
            break
    qty_signed = None
    for fid in TRADE_QTY_FIDS:
        qty_signed = _num(values.get(fid))
        if qty_signed is not None:
            break
    sign = 0
    if qty_signed is not None:
        sign = 1 if qty_signed > 0 else (-1 if qty_signed < 0 else 0)
    return {
        "trade_price": price,
        "trade_qty": abs(qty_signed) if qty_signed is not None else None,
        "trade_sign": sign,
        "trade_time": str(values.get(TRADE_TIME_FID, "")),
    }


def _parse_codes(args: argparse.Namespace) -> list[str]:
    raw: list[str] = []
    if args.codes:
        raw.extend(part.strip() for part in args.codes.split(","))
    if args.code:
        for group in args.code:
            raw.extend(group)

    seen: set[str] = set()
    codes: list[str] = []
    for value in raw or list(DEFAULT_CODES):
        code = _clean_code(value)
        if not code or code in seen:
            continue
        seen.add(code)
        codes.append(code)
    return codes


async def _wait_until(target: datetime) -> None:
    delta = (target - datetime.now()).total_seconds()
    if delta > 0:
        log.info("waiting until %s (%.0fs)", target, delta)
        await asyncio.sleep(delta)


@dataclass
class BookSnapshot:
    recv_ts: str
    quote_time: str
    ask_prices: dict[int, float]
    bid_prices: dict[int, float]
    ask_sizes: dict[int, int]
    bid_sizes: dict[int, int]

    @property
    def ask1(self) -> float | None:
        return self.ask_prices.get(1)

    @property
    def bid1(self) -> float | None:
        return self.bid_prices.get(1)

    @property
    def ask_size1(self) -> int | None:
        return self.ask_sizes.get(1)

    @property
    def bid_size1(self) -> int | None:
        return self.bid_sizes.get(1)


@dataclass
class AlphaConfig:
    code: str
    levels: int
    cost_bps: float
    max_spread_bps: float
    # iNAV direction model
    vel_entry_bps: float       # min iNAV velocity (bps/sec) to call it "rising"
    vel_exit_bps: float        # |velocity| below this (bps/sec) => momentum exhausted
    vel_reversal_bps: float    # iNAV velocity (bps/sec) at/below -this => stop out
    persistence_k: int         # consecutive rising iNAV updates required to enter
    nav_age_max_sec: float     # max iNAV age (sec) trusted for a fresh entry
    proj_age_cap_sec: float    # cap (sec) when projecting stale iNAV forward
    # entry edge / execution
    max_overpay_bps: float     # max bps we will pay ABOVE projected fair (anti-chase)
    min_ask_qty: float         # min ask1 size to treat the touch as executable
    score_threshold: float     # informational score must also clear this to enter
    # exit / risk
    max_hold_sec: float        # time stop: lag-momentum is short-lived
    hard_stop_bps: float       # hard stop vs entry price (bps)
    reentry_cooldown_sec: float  # block re-entry for this long after an exit
    # misc
    nav_fid: str | None
    depth_ref_qty: float


class AlphaEngine:
    def __init__(self, cfg: AlphaConfig, seed_nav: float | None = None):
        self.cfg = cfg
        self.latest_book: BookSnapshot | None = None
        self.latest_nav: float | None = seed_nav
        self.nav_fid_used: str = ""
        self.nav_history: deque[tuple[datetime, float]] = deque(maxlen=8)
        self.up_streak: int = 0
        if seed_nav:
            self.nav_history.append((datetime.now(), seed_nav))
        # position state machine
        self.position: str = "FLAT"
        self.entry_ts: datetime | None = None
        self.entry_price: float | None = None
        self.entry_fair: float | None = None
        self.entry_inav: float | None = None
        self.cooldown_until: datetime | None = None

    def update_nav(self, values: dict[str, Any], now: datetime | None = None) -> tuple[float | None, str]:
        fid_order = [self.cfg.nav_fid] if self.cfg.nav_fid else []
        fid_order += [fid for fid in NAV_CANDIDATE_FIDS if fid not in fid_order]

        for fid in fid_order:
            if not fid:
                continue
            nav = _num(values.get(fid), abs_value=True)
            # ETF NAV should be a price-like number. This avoids accidentally
            # treating tiny rates as NAV when auto-detecting.
            if nav and nav >= 100:
                now = now or datetime.now()
                prev = self.nav_history[-1] if self.nav_history else None
                self.latest_nav = nav
                self.nav_fid_used = fid
                self.nav_history.append((now, nav))
                # Maintain a "rising" streak from time-normalized velocity so a
                # single stale->fresh jump does not masquerade as a trend.
                if prev:
                    dt = (now - prev[0]).total_seconds()
                    if dt > 0 and prev[1] > 0:
                        v = (nav / prev[1] - 1.0) * 1e4 / dt
                        self.up_streak = self.up_streak + 1 if v >= self.cfg.vel_entry_bps else 0
                return nav, fid
        return None, ""

    def parse_book(self, values: dict[str, Any]) -> BookSnapshot:
        now = datetime.now().isoformat(timespec="milliseconds")
        ask_prices: dict[int, float] = {}
        bid_prices: dict[int, float] = {}
        ask_sizes: dict[int, int] = {}
        bid_sizes: dict[int, int] = {}

        for level in range(1, 11):
            ask_price = _num(values.get(ASK_PRICE_FIDS[level]), abs_value=True)
            bid_price = _num(values.get(BID_PRICE_FIDS[level]), abs_value=True)
            ask_size = _int(values.get(ASK_SIZE_FIDS[level]), abs_value=True)
            bid_size = _int(values.get(BID_SIZE_FIDS[level]), abs_value=True)
            if ask_price:
                ask_prices[level] = ask_price
            if bid_price:
                bid_prices[level] = bid_price
            if ask_size is not None:
                ask_sizes[level] = ask_size
            if bid_size is not None:
                bid_sizes[level] = bid_size

        return BookSnapshot(
            recv_ts=now,
            quote_time=str(values.get(QUOTE_TIME_FID, "")),
            ask_prices=ask_prices,
            bid_prices=bid_prices,
            ask_sizes=ask_sizes,
            bid_sizes=bid_sizes,
        )

    def _velocity_accel_per_sec(self) -> tuple[float, float]:
        """iNAV velocity and acceleration in bps/sec (time-normalized).

        0G updates arrive irregularly, so a per-step delta is meaningless; every
        delta is divided by its own elapsed seconds.
        """
        h = self.nav_history
        if len(h) < 2:
            return 0.0, 0.0
        t1, n1 = h[-1]
        t0, n0 = h[-2]
        dt1 = (t1 - t0).total_seconds()
        if dt1 <= 0 or n0 <= 0:
            return 0.0, 0.0
        v1 = (n1 / n0 - 1.0) * 1e4 / dt1
        if len(h) < 3:
            return v1, 0.0
        t_, n_ = h[-3]
        dt0 = (t0 - t_).total_seconds()
        if dt0 <= 0 or n_ <= 0:
            return v1, 0.0
        v0 = (n0 / n_ - 1.0) * 1e4 / dt0
        span = (t1 - t_).total_seconds() / 2.0
        accel = (v1 - v0) / span if span > 0 else 0.0
        return v1, accel

    def _nav_age_sec(self, now: datetime) -> float | None:
        if not self.nav_history:
            return None
        return (now - self.nav_history[-1][0]).total_seconds()

    def _fair_now(self, now: datetime, vel_bps_per_sec: float) -> float | None:
        """Project the delayed iNAV forward to 'now' with its own velocity.

            fair_now = nav_last * (1 + v_per_sec * min(age, proj_age_cap))

        The cap stops us from extrapolating a stale velocity too far. Every edge
        in the strategy is measured against this projection, not the raw value,
        so we act on genuine lag rather than publication delay.
        """
        if not self.latest_nav or not self.nav_history:
            return None
        age = (now - self.nav_history[-1][0]).total_seconds()
        proj = max(0.0, min(age, self.cfg.proj_age_cap_sec))
        return self.latest_nav * (1.0 + (vel_bps_per_sec / 1e4) * proj)

    def _ofi1(self, book: BookSnapshot) -> float:
        prev = self.latest_book
        if not prev:
            return 0.0
        bid = book.bid1
        ask = book.ask1
        prev_bid = prev.bid1
        prev_ask = prev.ask1
        bid_qty = float(book.bid_size1 or 0)
        ask_qty = float(book.ask_size1 or 0)
        prev_bid_qty = float(prev.bid_size1 or 0)
        prev_ask_qty = float(prev.ask_size1 or 0)
        if not bid or not ask or not prev_bid or not prev_ask:
            return 0.0

        bid_flow = 0.0
        if bid > prev_bid:
            bid_flow = bid_qty
        elif bid == prev_bid:
            bid_flow = bid_qty - prev_bid_qty
        else:
            bid_flow = -prev_bid_qty

        ask_flow = 0.0
        if ask < prev_ask:
            ask_flow = -ask_qty
        elif ask == prev_ask:
            ask_flow = -(ask_qty - prev_ask_qty)
        else:
            ask_flow = prev_ask_qty
        return bid_flow + ask_flow

    def _ask_retreat_score(self, book: BookSnapshot) -> float:
        prev = self.latest_book
        if not prev or not book.ask1 or not prev.ask1:
            return 0.0
        if book.ask1 > prev.ask1:
            return 1.0
        if book.ask1 == prev.ask1 and prev.ask_size1:
            drop = (prev.ask_size1 - (book.ask_size1 or 0)) / prev.ask_size1
            return _clip(drop, 0.0, 1.0)
        return 0.0

    def compute(self, book: BookSnapshot, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now()
        nav = self.latest_nav
        ask1 = book.ask1
        bid1 = book.bid1
        ask_qty1 = book.ask_size1 or 0
        bid_qty1 = book.bid_size1 or 0

        v_bps, accel_bps = self._velocity_accel_per_sec()
        age_sec = self._nav_age_sec(now)
        fair_now = self._fair_now(now, v_bps)

        base: dict[str, Any] = {
            "inav": nav,
            "nav_fid_used": self.nav_fid_used,
            "fair_now": round(fair_now, 2) if fair_now else "",
            "nav_age_sec": round(age_sec, 3) if age_sec is not None else "",
            "inav_velocity_bps": round(v_bps, 4),
            "inav_accel_bps": round(accel_bps, 4),
            "up_streak": self.up_streak,
            "position": self.position,
            "event": "",
            "exit_reason": "",
            "veto_reason": "",
        }

        if not nav or not ask1 or not bid1 or ask1 <= 0 or bid1 <= 0:
            self.latest_book = book
            return {**base, "signal": "NO_NAV_OR_BOOK", "veto_reason": "missing_nav_or_book"}

        mid = (ask1 + bid1) / 2.0
        denom = bid_qty1 + ask_qty1
        micro = (ask1 * bid_qty1 + bid1 * ask_qty1) / denom if denom > 0 else mid

        spread = ask1 - bid1
        spread_bps = _bps(spread, mid)

        # All edges are measured against the *projected* iNAV (fair_now), not the
        # raw stale value, so we act on real lag rather than publication delay.
        ref = fair_now if fair_now else nav
        edge_vs_fair_bps = _bps(ref - ask1, ref)
        executable_edge_bps = edge_vs_fair_bps - self.cfg.cost_bps
        discount_bps = _bps(nav - micro, nav)
        micro_minus_mid_bps = _bps(micro - mid, mid)

        levels = max(1, min(self.cfg.levels, 10))
        bid_depth = sum(book.bid_sizes.get(i, 0) for i in range(1, levels + 1))
        ask_depth = sum(book.ask_sizes.get(i, 0) for i in range(1, levels + 1))
        depth_denom = bid_depth + ask_depth
        depth_imbalance = (bid_depth - ask_depth) / depth_denom if depth_denom else 0.0
        depth_to_inav = sum(
            book.ask_sizes.get(i, 0)
            for i in range(1, 11)
            if book.ask_prices.get(i) and book.ask_prices[i] <= ref
        )
        ofi1 = self._ofi1(book)
        ofi_norm = ofi1 / max(float(bid_depth + ask_depth), 1.0)
        ask_retreat = self._ask_retreat_score(book)

        # Informational confidence score: momentum-weighted, edge counted ONCE.
        score = 0.0
        score += 1.0 * _clip(executable_edge_bps / max(self.cfg.cost_bps, 1.0), -2.0, 2.0)
        score += 1.0 * _clip(v_bps / max(self.cfg.vel_entry_bps, 0.1), -2.0, 2.0)
        score += 0.5 * _clip(accel_bps / max(self.cfg.vel_entry_bps, 0.1), -1.0, 1.0)
        score += 0.8 * _clip(ofi_norm * 10.0, -2.0, 2.0)
        score += 0.6 * ask_retreat
        score += 0.5 * _clip(depth_imbalance, -1.0, 1.0)
        score += 0.5 * _clip(micro_minus_mid_bps / max(spread_bps, 1.0), -1.0, 1.0)
        score -= 0.6 * _clip(spread_bps / max(self.cfg.max_spread_bps, 1.0), 0.0, 2.0)
        score -= 0.4 * _clip(depth_to_inav / max(self.cfg.depth_ref_qty, 1.0), 0.0, 2.0)

        prev_entry_ts = self.entry_ts
        prev_entry_price = self.entry_price
        prev_entry_fair = self.entry_fair

        if self.position == "FLAT":
            signal, event = self._check_entry(
                now=now, book=book, fair_now=fair_now, v_bps=v_bps,
                age_sec=age_sec, edge_vs_fair_bps=edge_vs_fair_bps,
                ask_qty1=ask_qty1, spread_bps=spread_bps, score=score,
            )
            exit_reason = ""
        else:
            signal, exit_reason, event = self._check_exit(now=now, book=book, v_bps=v_bps)

        # Reflect entry context on the row even on the tick we just exited.
        eff_ts = self.entry_ts or prev_entry_ts
        eff_price = self.entry_price or prev_entry_price
        eff_fair = self.entry_fair or prev_entry_fair

        self.latest_book = book
        return {
            **base,
            "position": self.position,   # post-decision state (ENTER row => LONG)
            "signal": signal,
            "event": event,
            "exit_reason": exit_reason,
            "hold_sec": round((now - eff_ts).total_seconds(), 2) if eff_ts else "",
            "entry_price": eff_price if eff_price else "",
            "entry_fair": round(eff_fair, 2) if eff_fair else "",
            "ask1": ask1,
            "bid1": bid1,
            "ask_qty1": ask_qty1,
            "bid_qty1": bid_qty1,
            "mid": mid,
            "micro": micro,
            "spread": spread,
            "spread_bps": spread_bps,
            "discount_bps": discount_bps,
            "edge_vs_fair_bps": edge_vs_fair_bps,
            "executable_edge_bps": executable_edge_bps,
            "micro_minus_mid_bps": micro_minus_mid_bps,
            "ofi1": ofi1,
            "ofi_norm": ofi_norm,
            "depth_imbalance": depth_imbalance,
            "depth_to_inav": depth_to_inav,
            "ask_retreat": ask_retreat,
            "alpha_score": score,
        }

    def _check_entry(
        self,
        *,
        now: datetime,
        book: BookSnapshot,
        fair_now: float | None,
        v_bps: float,
        age_sec: float | None,
        edge_vs_fair_bps: float,
        ask_qty1: int,
        spread_bps: float,
        score: float,
    ) -> tuple[str, str]:
        """FLAT -> LONG. Eligibility gates + ONE alpha score (no double-counting).

        The old design ANDed direction/room/flow/spread gates that were *also*
        baked into alpha_score, then gated the score too -> the same cuts applied
        twice. Here the hard gates are strictly safety/execution + the directional
        thesis; the score alone carries flow / edge / depth / acceleration.
        """
        c = self.cfg
        if fair_now is None or age_sec is None:
            return "WATCH", ""
        if self.cooldown_until and now < self.cooldown_until:
            return "COOLDOWN", ""

        # --- Eligibility gates (safety / execution; NOT alpha) ----------------
        fresh_ok = age_sec <= c.nav_age_max_sec           # iNAV usable, not stale
        exec_ok = ask_qty1 >= c.min_ask_qty and spread_bps <= c.max_spread_bps
        # Anti-chase: refuse to lift an offer already richer than projected fair
        # by more than max_overpay. Replaces the old discount gate, which fought
        # the directional thesis by only entering when ETF was *below* fair.
        not_overpay = (-edge_vs_fair_bps) <= c.max_overpay_bps
        # Direction IS the thesis -> stays a gate, not just a score term.
        direction_ok = self.up_streak >= c.persistence_k and v_bps >= c.vel_entry_bps

        # --- Alpha decision (single threshold) --------------------------------
        score_ok = score >= c.score_threshold

        if fresh_ok and exec_ok and not_overpay and direction_ok and score_ok:
            self.position = "LONG"
            self.entry_ts = now
            self.entry_price = book.ask1     # we lift the offer
            self.entry_fair = fair_now       # predicted convergence target
            self.entry_inav = self.latest_nav
            return "ENTER_LONG", "ENTER_LONG"
        return "WATCH", ""

    def _check_exit(
        self, *, now: datetime, book: BookSnapshot, v_bps: float
    ) -> tuple[str, str, str]:
        """LONG -> FLAT. Any rule fires an exit; the reason is recorded."""
        c = self.cfg
        bid1 = book.bid1
        hold = (now - self.entry_ts).total_seconds() if self.entry_ts else 0.0

        reason = ""
        if bid1 and self.entry_fair and bid1 >= self.entry_fair:
            reason = "converge_tp"           # LP chased up to my predicted level
        elif v_bps <= -c.vel_reversal_bps:
            reason = "reversal_stop"          # iNAV direction flipped => thesis wrong
        elif abs(v_bps) <= c.vel_exit_bps and bid1 and self.entry_price and bid1 >= self.entry_price:
            reason = "exhaust_tp"             # momentum gone, lock the gain
        elif bid1 and self.entry_price and bid1 <= self.entry_price * (1.0 - c.hard_stop_bps / 1e4):
            reason = "hard_stop"
        elif hold >= c.max_hold_sec:
            reason = "time_stop"              # lag-momentum is short-lived

        if reason:
            self.position = "FLAT"
            self.entry_ts = None
            self.entry_price = None
            self.entry_fair = None
            self.entry_inav = None
            self.cooldown_until = now + timedelta(seconds=c.reentry_cooldown_sec)
            return "EXIT_LONG", reason, "EXIT_LONG"
        return "IN_POSITION", "", ""


class CsvLogger:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fp = path.open("w", encoding="utf-8-sig", newline="")
        self._writer = csv.DictWriter(self._fp, fieldnames=self._fields())
        self._writer.writeheader()

    @staticmethod
    def _fields() -> list[str]:
        base = [
            "recv_ts",
            "event_type",
            "code",
            "quote_time",
            "signal",
            "event",
            "exit_reason",
            "alert",
            "alert_ts",
            "position",
            "hold_sec",
            "veto_reason",
            "inav",
            "fair_now",
            "nav_age_sec",
            "inav_velocity_bps",
            "inav_accel_bps",
            "up_streak",
            "nav_fid_used",
            "ask1",
            "bid1",
            "ask_qty1",
            "bid_qty1",
            "trade_price",
            "trade_qty",
        ]
        for level in range(2, 11):
            base += [f"ask{level}", f"bid{level}", f"ask_qty{level}", f"bid_qty{level}"]
        base += [
            "mid",
            "micro",
            "spread",
            "spread_bps",
            "discount_bps",
            "edge_vs_fair_bps",
            "executable_edge_bps",
            "micro_minus_mid_bps",
            "ofi1",
            "ofi_norm",
            "depth_imbalance",
            "depth_to_inav",
            "ask_retreat",
            "entry_price",
            "entry_fair",
            "alpha_score",
            "raw_values_json",
        ]
        return base

    def write(self, row: dict[str, Any]) -> None:
        self._writer.writerow({key: row.get(key, "") for key in self._writer.fieldnames or []})
        self._fp.flush()

    def close(self) -> None:
        self._fp.close()


def _rest_post(cfg: KiwoomConfig, token: str, api_id: str, body: dict[str, Any]) -> dict[str, Any]:
    route = "/api/dostk/etf" if api_id.startswith("ka4") else "/api/dostk/mrkcond"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "authorization": f"Bearer {token}",
        "api-id": api_id,
        "cont-yn": "N",
        "next-key": "",
    }
    response = requests.post(cfg.rest_base + route, headers=headers, json=body, timeout=10)
    response.raise_for_status()
    return response.json()


def fetch_seed_nav(cfg: KiwoomConfig, token: str, code: str) -> tuple[float | None, str]:
    try:
        data = _rest_post(cfg, token, "ka40003", {"stk_cd": code})
    except requests.RequestException as exc:
        log.warning("seed NAV request failed: %s", exc)
        return None, ""
    rows = data.get("etfdaly_trnsn") or []
    if not rows:
        return None, ""
    nav = _num(rows[0].get("nav"), abs_value=True)
    return nav, str(rows[0].get("cntr_dt", ""))


def fetch_etf_name(cfg: KiwoomConfig, token: str, code: str) -> str:
    try:
        data = _rest_post(cfg, token, "ka40002", {"stk_cd": code})
    except requests.RequestException as exc:
        log.warning("ETF info request failed: %s", exc)
        return ""
    return str(data.get("stk_nm", ""))


def _emit_alert(
    *,
    event: str,
    alert_ts: str,
    code: str,
    name: str,
    features: dict[str, Any],
    beep: bool,
) -> None:
    if event == "EXIT_LONG":
        message = (
            f"EXIT  {alert_ts} "
            f"code={code}"
            f"{' name=' + name if name else ''} "
            f"reason={features.get('exit_reason', '')} "
            f"hold={features.get('hold_sec', '')}s "
            f"entry={features.get('entry_price', '')} "
            f"bid={features.get('bid1', '')} "
            f"inav={features.get('inav', '')}"
        )
    else:
        message = (
            f"ENTER {alert_ts} "
            f"code={code}"
            f"{' name=' + name if name else ''} "
            f"score={features.get('alpha_score', 0.0):.2f} "
            f"edge={features.get('edge_vs_fair_bps', 0.0):.2f}bps "
            f"v={features.get('inav_velocity_bps', 0.0):.2f}bps/s "
            f"streak={features.get('up_streak', '')} "
            f"spread={features.get('spread_bps', 0.0):.2f}bps "
            f"fair={features.get('fair_now', '')} "
            f"ask={features.get('ask1', '')}"
        )
    log.warning(message)
    print(message, flush=True)
    if not beep:
        return
    try:
        import winsound

        winsound.Beep(1500 if event == "ENTER_LONG" else 800, 250)
    except Exception:
        pass


def _build_schedule(args: argparse.Namespace) -> tuple[datetime, datetime]:
    today = datetime.now().date()
    if args.start:
        start_at = datetime.combine(today, _parse_hhmm(args.start))
        if start_at < datetime.now() - timedelta(minutes=1):
            log.warning("start time %s is already past; starting now", args.start)
            start_at = datetime.now()
    else:
        start_at = datetime.now()

    if args.end:
        end_at = datetime.combine(today, _parse_hhmm(args.end))
    else:
        end_at = start_at + timedelta(minutes=args.duration_min)
    return start_at, end_at


async def run(args: argparse.Namespace) -> None:
    cfg = load_config()
    token_mgr = TokenManager(cfg)
    token = token_mgr.get_token()
    codes = _parse_codes(args)

    names: dict[str, str] = {}
    engines: dict[str, AlphaEngine] = {}
    for idx, code in enumerate(codes):
        etf_name = fetch_etf_name(cfg, token, code)
        seed_nav, seed_nav_dt = fetch_seed_nav(cfg, token, code)
        names[code] = etf_name
        alpha_cfg = AlphaConfig(
            code=code,
            levels=args.levels,
            cost_bps=args.cost_bps,
            max_spread_bps=args.max_spread_bps,
            vel_entry_bps=args.vel_entry_bps,
            vel_exit_bps=args.vel_exit_bps,
            vel_reversal_bps=args.vel_reversal_bps,
            persistence_k=args.persistence_k,
            nav_age_max_sec=args.nav_age_max_sec,
            proj_age_cap_sec=args.proj_age_cap_sec,
            max_overpay_bps=args.max_overpay_bps,
            min_ask_qty=args.min_ask_qty,
            score_threshold=args.score_threshold,
            max_hold_sec=args.max_hold_sec,
            hard_stop_bps=args.hard_stop_bps,
            reentry_cooldown_sec=args.reentry_cooldown_sec,
            nav_fid=args.nav_fid,
            depth_ref_qty=args.depth_ref_qty,
        )
        engines[code] = AlphaEngine(alpha_cfg, seed_nav=seed_nav)
        log.info("target=%s %s seed_nav=%s seed_nav_dt=%s", code, etf_name, seed_nav, seed_nav_dt)
        if idx < len(codes) - 1:
            time.sleep(0.25)

    start_at, end_at = _build_schedule(args)
    await _wait_until(start_at)
    if end_at <= datetime.now():
        log.warning("end time %s is already past; exiting without live collection", end_at)
        return

    out = args.out
    if out is None:
        stamp = datetime.now().strftime("%Y%m%d")
        code_part = "_".join(codes)
        out = Path("out") / f"etf_inav_lob_alpha_{code_part}_{stamp}.csv"
    logger = CsvLogger(out)

    last_alert_at: dict[str, datetime] = {}

    async with websockets.connect(cfg.ws_url, ping_interval=None) as ws:
        await ws.send(json.dumps({"trnm": "LOGIN", "token": token_mgr.get_token()}))
        login_raw = await asyncio.wait_for(ws.recv(), timeout=10)
        login_msg = json.loads(login_raw)
        if login_msg.get("return_code") != 0:
            raise RuntimeError(f"ws login failed: {login_msg}")

        sub_types = [ORDERBOOK_TYPE, ETF_NAV_TYPE]
        if not args.no_trades:
            sub_types.append(TRADE_TYPE)   # 0B trade prints -> realistic fill modeling
        reg_msg = {
            "trnm": "REG",
            "grp_no": args.group_no,
            "refresh": "1",
            "data": [{"item": codes, "type": sub_types}],
        }
        await ws.send(json.dumps(reg_msg))
        log.info("subscribed codes=%s types=%s until=%s out=%s", ",".join(codes), ",".join(sub_types), end_at, out)

        try:
            while datetime.now() < end_at:
                timeout = max(0.1, min(5.0, (end_at - datetime.now()).total_seconds()))
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                except asyncio.TimeoutError:
                    continue

                msg = json.loads(raw)
                if msg.get("trnm") == "PING":
                    await ws.send(raw)
                    continue
                if msg.get("trnm") == "REG":
                    log.info("REG response: %s", msg)
                    continue
                if msg.get("trnm") != "REAL":
                    log.debug("ignored ws msg: %s", msg)
                    continue

                for item in msg.get("data", []):
                    event_type = item.get("type", "")
                    code = _clean_code(item.get("item", ""))
                    values = item.get("values", {}) or {}
                    recv_ts = datetime.now().isoformat(timespec="milliseconds")
                    raw_json = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
                    engine = engines.get(code)

                    if event_type == ETF_NAV_TYPE:
                        nav, fid = engine.update_nav(values) if engine else (None, "")
                        latest_nav = engine.latest_nav if engine else ""
                        latest_nav_fid = engine.nav_fid_used if engine else ""
                        logger.write(
                            {
                                "recv_ts": recv_ts,
                                "event_type": event_type,
                                "code": code,
                                "inav": nav if nav is not None else latest_nav,
                                "nav_fid_used": fid or latest_nav_fid,
                                "raw_values_json": raw_json,
                            }
                        )
                        if nav:
                            log.debug("NAV update %s via fid=%s", nav, fid)
                        continue

                    if event_type == TRADE_TYPE:
                        tr = parse_trade(values)
                        logger.write(
                            {
                                "recv_ts": recv_ts,
                                "event_type": event_type,
                                "code": code,
                                "quote_time": tr["trade_time"],
                                "trade_price": tr["trade_price"] if tr["trade_price"] is not None else "",
                                "trade_qty": tr["trade_qty"] if tr["trade_qty"] is not None else "",
                                "raw_values_json": raw_json,
                            }
                        )
                        continue

                    if event_type != ORDERBOOK_TYPE:
                        logger.write(
                            {
                                "recv_ts": recv_ts,
                                "event_type": event_type,
                                "code": code,
                                "raw_values_json": raw_json,
                            }
                        )
                        continue

                    if engine is None:
                        logger.write(
                            {
                                "recv_ts": recv_ts,
                                "event_type": event_type,
                                "code": code,
                                "raw_values_json": raw_json,
                            }
                        )
                        continue

                    book = engine.parse_book(values)
                    features = engine.compute(book)
                    row: dict[str, Any] = {
                        "recv_ts": recv_ts,
                        "event_type": event_type,
                        "code": code,
                        "quote_time": book.quote_time,
                        "raw_values_json": raw_json,
                    }
                    for level in range(1, 11):
                        row[f"ask{level}"] = book.ask_prices.get(level, "")
                        row[f"bid{level}"] = book.bid_prices.get(level, "")
                        row[f"ask_qty{level}"] = book.ask_sizes.get(level, "")
                        row[f"bid_qty{level}"] = book.bid_sizes.get(level, "")
                    row.update(features)

                    alert_ts = ""
                    event = features.get("event", "")
                    if event in ("ENTER_LONG", "EXIT_LONG"):
                        now = datetime.now()
                        last = last_alert_at.get(code)
                        # Exits must always fire; entries respect the cooldown.
                        if (
                            event == "EXIT_LONG"
                            or last is None
                            or (now - last).total_seconds() >= args.alert_cooldown_sec
                        ):
                            last_alert_at[code] = now
                            alert_ts = now.isoformat(timespec="seconds")
                            _emit_alert(
                                event=event,
                                alert_ts=alert_ts,
                                code=code,
                                name=names.get(code, ""),
                                features=features,
                                beep=args.beep,
                            )
                    row["alert"] = 1 if alert_ts else 0
                    row["alert_ts"] = alert_ts
                    logger.write(row)
        finally:
            try:
                await ws.send(json.dumps({"trnm": "REMOVE", "grp_no": args.group_no}))
            finally:
                logger.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="etf_inav_lob_alpha_collect",
        description="Collect ETF 0D/0G real-time data and log iNAV-lag directional ENTER/EXIT signals.",
    )
    parser.add_argument(
        "--code",
        action="append",
        nargs="+",
        help="ETF code. Repeat or pass multiple values, e.g. --code 457990 --code 069500.",
    )
    parser.add_argument("--codes", help="Comma-separated ETF codes, e.g. 457990,069500.")
    parser.add_argument("--start", help="Start time HH:MM. If omitted, start now.")
    parser.add_argument("--end", help="End time HH:MM.")
    parser.add_argument("--duration-min", type=float, default=60.0, help="Run minutes when --end is omitted.")
    parser.add_argument("--out", type=Path, help="Output CSV path.")
    parser.add_argument("--group-no", default="9101", help="Kiwoom real-time group number.")
    parser.add_argument("--no-trades", action="store_true", help="Do not subscribe to 0B trade prints (disables fill modeling data).")
    parser.add_argument("--levels", type=int, default=3, choices=range(1, 11), help="LOB levels for score features.")
    parser.add_argument("--nav-fid", help="Override ETF NAV real-time FID if auto-detection is wrong.")
    parser.add_argument("--cost-bps", type=float, default=3.0, help="Round-trip cost/slippage buffer in bps.")
    parser.add_argument("--max-spread-bps", type=float, default=25.0, help="Hard max spread bps.")
    # iNAV direction model (velocities are bps/sec, time-normalized)
    parser.add_argument("--vel-entry-bps", type=float, default=0.5, help="Min iNAV velocity (bps/sec) to call it rising.")
    parser.add_argument("--vel-exit-bps", type=float, default=0.2, help="|velocity| below this (bps/sec) => momentum exhausted.")
    parser.add_argument("--vel-reversal-bps", type=float, default=1.0, help="iNAV velocity (bps/sec) at/below -this => stop out.")
    parser.add_argument("--persistence-k", type=int, default=2, help="Consecutive rising iNAV updates required to enter.")
    parser.add_argument("--nav-age-max-sec", type=float, default=3.0, help="Max iNAV age (sec) trusted for a fresh entry.")
    parser.add_argument("--proj-age-cap-sec", type=float, default=2.0, help="Cap (sec) when projecting stale iNAV forward.")
    # entry edge / execution
    parser.add_argument("--max-overpay-bps", type=float, default=8.0, help="Max bps to pay above projected fair (anti-chase).")
    parser.add_argument("--min-ask-qty", type=float, default=100.0, help="Min ask1 size to treat the touch as executable.")
    parser.add_argument("--score-threshold", type=float, default=1.5, help="Min informational score to enter.")
    # exit / risk
    parser.add_argument("--max-hold-sec", type=float, default=60.0, help="Time stop: lag-momentum is short-lived.")
    parser.add_argument("--hard-stop-bps", type=float, default=15.0, help="Hard stop vs entry price (bps).")
    parser.add_argument("--reentry-cooldown-sec", type=float, default=10.0, help="Block re-entry for this long after an exit.")
    parser.add_argument("--depth-ref-qty", type=float, default=5000.0, help="Depth-to-iNAV normalization quantity.")
    parser.add_argument("--alert-cooldown-sec", type=float, default=30.0, help="Minimum seconds between BUY alerts per ETF.")
    parser.add_argument("--beep", action="store_true", help="Play a short Windows beep on BUY alerts.")
    return parser


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args()
    try:
        asyncio.run(run(args))
    except ConfigError as exc:
        print(f"[config error] {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
