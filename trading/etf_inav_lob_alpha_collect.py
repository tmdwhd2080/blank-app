# -*- coding: utf-8 -*-
"""
Collect ETF iNAV + LOB data and compute a relaxed quote-lag alpha.

Default target:
    457990  PLUS 태양광&ESS

This script is intentionally a collector/signal logger, not an order executor.
It subscribes to Kiwoom WebSocket real-time feeds:
    0D: stock orderbook depth
    0G: ETF NAV

The first alpha version uses only top-of-book plus top 3 depth:
    - ask1/bid1 price and size are the minimum viable data
    - levels 1~3 support ask retreat and depth-to-iNAV filters
    - raw WebSocket values are saved so field mapping can be audited later

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
    max_nav_down_bps: float
    veto_nav_down_bps: float
    score_threshold: float
    nav_fid: str | None
    depth_ref_qty: float


class AlphaEngine:
    def __init__(self, cfg: AlphaConfig, seed_nav: float | None = None):
        self.cfg = cfg
        self.latest_book: BookSnapshot | None = None
        self.latest_nav: float | None = seed_nav
        self.nav_fid_used: str = ""
        self.nav_history: deque[tuple[datetime, float]] = deque(maxlen=3)
        if seed_nav:
            self.nav_history.append((datetime.now(), seed_nav))

    def update_nav(self, values: dict[str, Any]) -> tuple[float | None, str]:
        fid_order = [self.cfg.nav_fid] if self.cfg.nav_fid else []
        fid_order += [fid for fid in NAV_CANDIDATE_FIDS if fid not in fid_order]

        for fid in fid_order:
            if not fid:
                continue
            nav = _num(values.get(fid), abs_value=True)
            # ETF NAV should be a price-like number. This avoids accidentally
            # treating tiny rates as NAV when auto-detecting.
            if nav and nav >= 100:
                self.latest_nav = nav
                self.nav_fid_used = fid
                self.nav_history.append((datetime.now(), nav))
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

    def _nav_velocity_accel_bps(self) -> tuple[float, float]:
        if len(self.nav_history) < 2:
            return 0.0, 0.0
        nav_now = self.nav_history[-1][1]
        nav_prev = self.nav_history[-2][1]
        velocity = _bps(nav_now - nav_prev, nav_prev)
        if len(self.nav_history) < 3:
            return velocity, 0.0
        nav_prev2 = self.nav_history[-3][1]
        prev_velocity = _bps(nav_prev - nav_prev2, nav_prev2)
        return velocity, velocity - prev_velocity

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

    def compute(self, book: BookSnapshot) -> dict[str, Any]:
        nav = self.latest_nav
        ask1 = book.ask1
        bid1 = book.bid1
        ask_qty1 = book.ask_size1 or 0
        bid_qty1 = book.bid_size1 or 0
        if not nav or not ask1 or not bid1 or ask1 <= 0 or bid1 <= 0:
            self.latest_book = book
            return {
                "signal": "NO_NAV_OR_BOOK",
                "hard_ok": 0,
                "veto_reason": "missing_nav_or_book",
            }

        mid = (ask1 + bid1) / 2.0
        denom = bid_qty1 + ask_qty1
        if denom > 0:
            micro = (ask1 * bid_qty1 + bid1 * ask_qty1) / denom
        else:
            micro = mid

        spread = ask1 - bid1
        spread_bps = _bps(spread, mid)
        discount_bps = _bps(nav - micro, nav)
        executable_edge_bps = _bps(nav - ask1, nav) - self.cfg.cost_bps
        micro_minus_mid_bps = _bps(micro - mid, mid)

        levels = max(1, min(self.cfg.levels, 10))
        bid_depth = sum(book.bid_sizes.get(i, 0) for i in range(1, levels + 1))
        ask_depth = sum(book.ask_sizes.get(i, 0) for i in range(1, levels + 1))
        depth_denom = bid_depth + ask_depth
        depth_imbalance = (bid_depth - ask_depth) / depth_denom if depth_denom else 0.0
        depth_to_inav = sum(
            book.ask_sizes.get(i, 0)
            for i in range(1, 11)
            if book.ask_prices.get(i) and book.ask_prices[i] <= nav
        )

        nav_velocity_bps, nav_accel_bps = self._nav_velocity_accel_bps()
        ofi1 = self._ofi1(book)
        ofi_norm = ofi1 / max(float(bid_depth + ask_depth), 1.0)
        ask_retreat = self._ask_retreat_score(book)
        quote_lag_bps = max(0.0, _bps(nav - ask1, nav))

        hard_ok = (
            ask1 < nav
            and nav_velocity_bps > -self.cfg.max_nav_down_bps
            and spread_bps <= self.cfg.max_spread_bps
        )

        veto_reason = ""
        prev = self.latest_book
        bid_collapse = (
            bool(prev and prev.bid_size1 and book.bid_size1 is not None)
            and book.bid1 == prev.bid1
            and book.bid_size1 < prev.bid_size1 * 0.25
        )
        if nav_velocity_bps <= -self.cfg.veto_nav_down_bps:
            veto_reason = "nav_down_veto"
        elif spread_bps > self.cfg.max_spread_bps:
            veto_reason = "spread_veto"
        elif bid_collapse:
            veto_reason = "bid_collapse_veto"

        score = 0.0
        score += 1.4 * _clip(executable_edge_bps / max(self.cfg.cost_bps, 1.0), -2.0, 2.0)
        score += 0.7 * _clip(nav_velocity_bps / max(self.cfg.max_nav_down_bps, 1.0), -1.0, 1.0)
        score += 0.4 * _clip(nav_accel_bps / max(self.cfg.max_nav_down_bps, 1.0), -1.0, 1.0)
        score += 0.8 * _clip(ofi_norm * 10.0, -2.0, 2.0)
        score += 0.7 * _clip(micro_minus_mid_bps / max(spread_bps, 1.0), -1.0, 1.0)
        score += 0.6 * ask_retreat
        score += 0.7 * _clip(quote_lag_bps / max(self.cfg.cost_bps, 1.0), 0.0, 2.0)
        score += 0.5 * _clip(depth_imbalance, -1.0, 1.0)
        score -= 0.6 * _clip(spread_bps / max(self.cfg.max_spread_bps, 1.0), 0.0, 2.0)
        score -= 0.4 * _clip(depth_to_inav / max(self.cfg.depth_ref_qty, 1.0), 0.0, 2.0)

        if not hard_ok:
            signal = "HOLD"
        elif veto_reason:
            signal = "VETO"
        elif score >= self.cfg.score_threshold:
            signal = "BUY_CANDIDATE"
        else:
            signal = "WATCH"

        self.latest_book = book
        return {
            "signal": signal,
            "hard_ok": int(hard_ok),
            "veto_reason": veto_reason,
            "inav": nav,
            "nav_fid_used": self.nav_fid_used,
            "ask1": ask1,
            "bid1": bid1,
            "ask_qty1": ask_qty1,
            "bid_qty1": bid_qty1,
            "mid": mid,
            "micro": micro,
            "spread": spread,
            "spread_bps": spread_bps,
            "discount_bps": discount_bps,
            "executable_edge_bps": executable_edge_bps,
            "inav_velocity_bps": nav_velocity_bps,
            "inav_accel_bps": nav_accel_bps,
            "ofi1": ofi1,
            "ofi_norm": ofi_norm,
            "depth_imbalance": depth_imbalance,
            "depth_to_inav": depth_to_inav,
            "ask_retreat": ask_retreat,
            "quote_lag_bps": quote_lag_bps,
            "alpha_score": score,
        }


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
            "alert",
            "alert_ts",
            "hard_ok",
            "veto_reason",
            "inav",
            "nav_fid_used",
            "ask1",
            "bid1",
            "ask_qty1",
            "bid_qty1",
        ]
        for level in range(2, 11):
            base += [f"ask{level}", f"bid{level}", f"ask_qty{level}", f"bid_qty{level}"]
        base += [
            "mid",
            "micro",
            "spread",
            "spread_bps",
            "discount_bps",
            "executable_edge_bps",
            "inav_velocity_bps",
            "inav_accel_bps",
            "ofi1",
            "ofi_norm",
            "depth_imbalance",
            "depth_to_inav",
            "ask_retreat",
            "quote_lag_bps",
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


def _emit_buy_alert(
    *,
    alert_ts: str,
    code: str,
    name: str,
    features: dict[str, Any],
    beep: bool,
) -> None:
    message = (
        f"BUY ALERT {alert_ts} "
        f"code={code}"
        f"{' name=' + name if name else ''} "
        f"score={features.get('alpha_score', 0.0):.2f} "
        f"edge={features.get('executable_edge_bps', 0.0):.2f}bps "
        f"spread={features.get('spread_bps', 0.0):.2f}bps "
        f"inav={features.get('inav', '')} "
        f"ask={features.get('ask1', '')} "
        f"bid={features.get('bid1', '')}"
    )
    log.warning(message)
    print(message, flush=True)
    if not beep:
        return
    try:
        import winsound

        winsound.Beep(1200, 250)
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
            max_nav_down_bps=args.max_nav_down_bps,
            veto_nav_down_bps=args.veto_nav_down_bps,
            score_threshold=args.score_threshold,
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

        reg_msg = {
            "trnm": "REG",
            "grp_no": args.group_no,
            "refresh": "1",
            "data": [{"item": codes, "type": [ORDERBOOK_TYPE, ETF_NAV_TYPE]}],
        }
        await ws.send(json.dumps(reg_msg))
        log.info("subscribed codes=%s types=%s,%s until=%s out=%s", ",".join(codes), ORDERBOOK_TYPE, ETF_NAV_TYPE, end_at, out)

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
                    if features.get("signal") == "BUY_CANDIDATE":
                        now = datetime.now()
                        last = last_alert_at.get(code)
                        if last is None or (now - last).total_seconds() >= args.alert_cooldown_sec:
                            last_alert_at[code] = now
                            alert_ts = now.isoformat(timespec="seconds")
                            _emit_buy_alert(
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
        description="Collect ETF 0D/0G real-time data and log relaxed iNAV quote-lag alpha.",
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
    parser.add_argument("--levels", type=int, default=3, choices=range(1, 11), help="LOB levels for score features.")
    parser.add_argument("--nav-fid", help="Override ETF NAV real-time FID if auto-detection is wrong.")
    parser.add_argument("--cost-bps", type=float, default=3.0, help="Round-trip cost/slippage buffer in bps.")
    parser.add_argument("--max-spread-bps", type=float, default=25.0, help="Hard max spread bps.")
    parser.add_argument("--max-nav-down-bps", type=float, default=3.0, help="Allowed mild iNAV down move in bps.")
    parser.add_argument("--veto-nav-down-bps", type=float, default=7.0, help="Veto if iNAV velocity is below this.")
    parser.add_argument("--score-threshold", type=float, default=2.5, help="BUY_CANDIDATE threshold.")
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
