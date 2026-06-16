# -*- coding: utf-8 -*-
"""Replay collected ETF 0D/0G CSVs through AlphaEngine and score the strategy.

It answers two SEPARATE questions, because they fail for different reasons:

  (A) THESIS check (execution-free).  Does an iNAV up-move predict the *next*
      few seconds of iNAV return?  If short-horizon iNAV returns have no positive
      autocorrelation, there is NO directional edge to capture no matter how good
      the execution is.  This isolates "is the signal real" from "can we trade it".

  (B) STRATEGY check (with costs).  Replay the engine's ENTER/EXIT events using
      the recorded timestamps and order book, then compute realized round-trip
      P&L = exit_bid - entry_ask - commissions.  Korean ETFs have no securities
      transaction tax on the sell, so cost is essentially the crossed spread plus
      commission.  This is the number that decides whether it makes money.

Usage:
    python trading/etf_inav_lob_backtest.py
    python trading/etf_inav_lob_backtest.py --glob "out/etf_inav_lob_alpha_457990_*.csv"
    python trading/etf_inav_lob_backtest.py --horizons 2,5,10 --commission-bps 0.3
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from trading.etf_inav_lob_alpha_collect import (  # noqa: E402
    AlphaConfig,
    AlphaEngine,
    ORDERBOOK_TYPE,
    ETF_NAV_TYPE,
    TRADE_TYPE,
)


def _f(value) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _default_cfg() -> AlphaConfig:
    return AlphaConfig(
        code="", levels=3, cost_bps=3.0, max_spread_bps=25.0,
        vel_entry_bps=0.5, vel_exit_bps=0.2, vel_reversal_bps=1.0, persistence_k=2,
        nav_age_max_sec=3.0, proj_age_cap_sec=2.0, max_overpay_bps=8.0, min_ask_qty=100.0,
        score_threshold=1.5, max_hold_sec=60.0, hard_stop_bps=15.0, reentry_cooldown_sec=10.0,
        nav_fid=None, depth_ref_qty=5000.0,
    )


def _load_rows(paths: list[Path]) -> dict[str, list[dict]]:
    by_code: dict[str, list[dict]] = defaultdict(list)
    for p in paths:
        with p.open(encoding="utf-8-sig") as fh:
            for d in csv.DictReader(fh):
                code = d.get("code") or "?"
                ts = d.get("recv_ts")
                raw = d.get("raw_values_json")
                if not ts or not raw:
                    continue
                by_code[code].append(d)
    # sort each code by recv_ts
    for code in by_code:
        by_code[code].sort(key=lambda r: r["recv_ts"])
    return by_code


# ============================================================
# (A) Thesis: does iNAV velocity predict forward iNAV return?
# ============================================================
def thesis_check(nav_series: list[tuple[datetime, float]], horizons: list[float]) -> dict:
    """nav_series = chronological (timestamp, iNAV). For each point compute the
    per-second velocity to that point and the forward return over each horizon,
    then report whether 'rising now' implies 'rising next'."""
    out: dict[str, dict] = {}
    n = len(nav_series)
    for h in horizons:
        rows: list[tuple[float, float]] = []  # (velocity_bps_per_sec, fwd_bps)
        for i in range(1, n):
            t_i, nav_i = nav_series[i]
            t_p, nav_p = nav_series[i - 1]
            dt = (t_i - t_p).total_seconds()
            if dt <= 0 or nav_p <= 0 or nav_i <= 0:
                continue
            v = (nav_i / nav_p - 1.0) * 1e4 / dt
            # first point at or after t_i + h
            j = i + 1
            while j < n and (nav_series[j][0] - t_i).total_seconds() < h:
                j += 1
            if j >= n:
                break
            nav_f = nav_series[j][1]
            if nav_f <= 0:
                continue
            fwd = (nav_f / nav_i - 1.0) * 1e4
            rows.append((v, fwd))
        if len(rows) < 8:
            out[f"{h:g}s"] = {"n": len(rows)}
            continue
        up = [fwd for v, fwd in rows if v > 0]
        dn = [fwd for v, fwd in rows if v < 0]
        # Pearson corr(v, fwd)
        vs = [v for v, _ in rows]
        fs = [fwd for _, fwd in rows]
        try:
            corr = st.correlation(vs, fs)
        except (st.StatisticsError, ValueError):
            corr = float("nan")
        out[f"{h:g}s"] = {
            "n": len(rows),
            "corr": corr,
            "fwd_after_up_mean": st.mean(up) if up else float("nan"),
            "fwd_after_up_hit": (sum(1 for x in up if x > 0) / len(up)) if up else float("nan"),
            "n_up": len(up),
            "fwd_after_down_mean": st.mean(dn) if dn else float("nan"),
            "n_down": len(dn),
        }
    return out


# ============================================================
# (B) Strategy: replay engine, realize round-trip P&L
# ============================================================
def strategy_check(rows: list[dict], cfg: AlphaConfig, commission_bps: float) -> dict:
    engine = AlphaEngine(cfg)
    nav_series: list[tuple[datetime, float]] = []
    trades: list[dict] = []
    open_entry: dict | None = None
    n_enter = n_exit = 0
    sig_count: dict[str, int] = defaultdict(int)
    n_book_ok = 0

    for d in rows:
        try:
            now = datetime.fromisoformat(d["recv_ts"])
        except ValueError:
            continue
        try:
            raw = json.loads(d["raw_values_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        et = d.get("event_type")

        if et == ETF_NAV_TYPE:
            engine.update_nav(raw, now=now)
            if engine.latest_nav:
                nav_series.append((now, engine.latest_nav))
            continue
        if et != ORDERBOOK_TYPE:
            continue

        book = engine.parse_book(raw)
        if book.ask1 and book.bid1:
            n_book_ok += 1
        feat = engine.compute(book, now=now)
        sig_count[feat.get("signal", "")] += 1
        ev = feat.get("event")
        if ev == "ENTER_LONG":
            n_enter += 1
            entry_ask = _f(feat.get("entry_price")) or _f(feat.get("ask1"))
            if entry_ask:
                open_entry = {"ts": now, "ask": entry_ask, "fair": _f(feat.get("entry_fair"))}
        elif ev == "EXIT_LONG" and open_entry:
            n_exit += 1
            exit_bid = _f(feat.get("bid1"))
            if exit_bid and open_entry["ask"]:
                gross = (exit_bid / open_entry["ask"] - 1.0) * 1e4
                net = gross - commission_bps
                trades.append({
                    "hold": (now - open_entry["ts"]).total_seconds(),
                    "reason": feat.get("exit_reason", ""),
                    "gross_bps": gross,
                    "net_bps": net,
                })
            open_entry = None

    pnl = [t["net_bps"] for t in trades]
    by_reason: dict[str, int] = defaultdict(int)
    for t in trades:
        by_reason[t["reason"]] += 1
    return {
        "n_enter": n_enter,
        "n_exit_closed": len(trades),
        "n_book_ok": n_book_ok,
        "sig_count": dict(sig_count),
        "nav_points": len(nav_series),
        "nav_series": nav_series,
        "trades": trades,
        "by_reason": dict(by_reason),
        "net_sum_bps": sum(pnl) if pnl else 0.0,
        "net_mean_bps": st.mean(pnl) if pnl else float("nan"),
        "win_rate": (sum(1 for x in pnl if x > 0) / len(pnl)) if pnl else float("nan"),
        "avg_hold": st.mean([t["hold"] for t in trades]) if trades else float("nan"),
    }


# ============================================================
# (C) Gap-convergence: does "ETF cheap vs fair" (fair_now - ask1) close
#     UPWARD (LP lifts ask -> buyer wins) or DOWNWARD (fair was wrong)?
# ============================================================
_GAP_BUCKETS = [(-1e9, 0.0), (0.0, 3.0), (3.0, 6.0), (6.0, 12.0), (12.0, 1e9)]


def gap_convergence_check(rows: list[dict], cfg: AlphaConfig, horizons: list[float],
                          commission_bps: float) -> dict:
    """For each 0D tick record (t, fair_now, ask1, bid1). Then, bucketed by the
    gap (fair_now-ask1 in bps), measure over each forward horizon:
      - taker_pnl: buy ask now, sell bid at t+H  (what a slow taker actually gets)
      - ask_fwd:   how much ask1 itself moved     (did the LP lift the offer?)
    If big positive gaps do NOT produce positive taker_pnl, the gap is fair_now
    error (mean-reverting noise), not a catchable LP-repricing edge."""
    engine = AlphaEngine(cfg)
    pts: list[tuple[datetime, float, float, float]] = []
    for d in rows:
        try:
            now = datetime.fromisoformat(d["recv_ts"])
            raw = json.loads(d["raw_values_json"])
        except (ValueError, json.JSONDecodeError, TypeError):
            continue
        et = d.get("event_type")
        if et == ETF_NAV_TYPE:
            engine.update_nav(raw, now=now)
            continue
        if et != ORDERBOOK_TYPE:
            continue
        book = engine.parse_book(raw)
        if not book.ask1 or not book.bid1:
            continue
        v, _ = engine._velocity_accel_per_sec()
        fair = engine._fair_now(now, v)
        if fair:
            pts.append((now, fair, book.ask1, book.bid1))

    n = len(pts)
    out: dict[str, list] = {}
    for h in horizons:
        buckets: dict[tuple, list[tuple[float, float]]] = {b: [] for b in _GAP_BUCKETS}
        for i in range(n):
            t_i, fair_i, ask_i, bid_i = pts[i]
            gap = (fair_i - ask_i) / fair_i * 1e4
            j = i + 1
            while j < n and (pts[j][0] - t_i).total_seconds() < h:
                j += 1
            if j >= n:
                break
            _, _, ask_j, bid_j = pts[j]
            taker = (bid_j / ask_i - 1.0) * 1e4 - commission_bps   # buy ask, sell bid (cross both)
            maker = (bid_j / bid_i - 1.0) * 1e4 - commission_bps   # buy bid (limit fill), sell bid
            ask_fwd = (ask_j / ask_i - 1.0) * 1e4
            for b in _GAP_BUCKETS:
                if b[0] <= gap < b[1]:
                    buckets[b].append((taker, ask_fwd, maker))
                    break
        out[f"{h:g}s"] = [
            (b, len(v),
             st.mean([x[0] for x in v]) if v else float("nan"),   # taker
             st.mean([x[1] for x in v]) if v else float("nan"),   # ask_fwd
             st.mean([x[2] for x in v]) if v else float("nan"),   # maker
             (sum(1 for x in v if x[2] > 0) / len(v)) if v else float("nan"))  # maker win%
            for b, v in buckets.items()
        ]
    return out


# ============================================================
# (D) Maker fill simulation — the decisive test.
#     Post a BUY LIMIT at the bid when the gap is wide, then model REAL fills:
#       * we fill only when SELL-initiated trade prints reach our limit price
#         AND enough volume trades through to clear the queue ahead of us;
#       * if the bid runs UP above our limit first, we MISS (price ran away) ->
#         this is the adverse selection that naive maker backtests ignore.
#     Needs 0B trade rows; on legacy data (0D/0G only) it still reports post/miss
#     stats from the book, but fills require trades.
# ============================================================
def maker_gap_sim(rows: list[dict], cfg: AlphaConfig, *, gap_entry_bps: float,
                  queue_factor: float, max_wait_fill_sec: float, max_hold_sec: float,
                  commission_bps: float, gap_ref: str = "ask",
                  post_offset_ticks: int = 0, tick_size: float = 5.0,
                  queue_jump_gap_bps: float = 1e9) -> dict:
    """gap_ref: which ETF price to compare iNAV against -> 'ask' | 'micro' | 'mid'.
    post_offset_ticks: post the buy limit this many ticks ABOVE the bid (queue jump).
    queue_jump_gap_bps: only apply the offset when the gap is at least this wide
    (so we only pay a tick to skip the queue when the edge justifies it).
    """
    engine = AlphaEngine(cfg)
    last_fair: float | None = None
    state = "FLAT"
    limit_price = queue_ahead = filled_vol = 0.0
    t_post = entry_fair = entry_price = t_fill = cooldown_until = None
    n_trade_rows = 0
    posts = fills = misses = timeouts = signal_gone = jumps = 0
    trades: list[dict] = []

    for d in rows:
        try:
            now = datetime.fromisoformat(d["recv_ts"])
            raw = json.loads(d["raw_values_json"])
        except (ValueError, json.JSONDecodeError, TypeError):
            continue
        et = d.get("event_type")

        if et == ETF_NAV_TYPE:
            engine.update_nav(raw, now=now)
            v, _ = engine._velocity_accel_per_sec()
            last_fair = engine._fair_now(now, v)
            continue

        if et == TRADE_TYPE:
            n_trade_rows += 1
            if state == "PENDING":
                tp = _f(d.get("trade_price"))
                if tp is None:
                    tp = _f(raw.get("10"))
                tq = _f(d.get("trade_qty"))
                if tq is None:
                    tq = _f(raw.get("15"))
                # sell-initiated print at/below our limit -> consumes the queue
                if tp is not None and tp <= limit_price + 1e-9:
                    filled_vol += abs(tq or 0.0)
                    if filled_vol >= queue_ahead * queue_factor:
                        state = "LONG"
                        fills += 1
                        entry_price = limit_price
                        t_fill = now
            continue

        if et != ORDERBOOK_TYPE:
            continue
        book = engine.parse_book(raw)
        if not book.ask1 or not book.bid1:
            continue
        ask1, bid1, bid_sz1 = book.ask1, book.bid1, (book.bid_size1 or 0)
        v, _ = engine._velocity_accel_per_sec()
        last_fair = engine._fair_now(now, v)

        if state == "FLAT":
            if cooldown_until and now < cooldown_until:
                continue
            if last_fair and last_fair > 0:
                ask_sz1 = book.ask_size1 or 0
                if gap_ref == "micro":
                    denom = bid_sz1 + ask_sz1
                    ref = (ask1 * bid_sz1 + bid1 * ask_sz1) / denom if denom else (ask1 + bid1) / 2
                elif gap_ref == "mid":
                    ref = (ask1 + bid1) / 2
                else:
                    ref = ask1
                gap = (last_fair - ref) / last_fair * 1e4
                if gap >= gap_entry_bps and bid_sz1 > 0:
                    # Queue jump: post above the bid only when the gap is wide
                    # enough to pay a tick for first-in-line priority.
                    offset = post_offset_ticks if gap >= queue_jump_gap_bps else 0
                    if offset > 0:
                        jumps += 1
                    state = "PENDING"
                    posts += 1
                    limit_price = bid1 + offset * tick_size
                    queue_ahead = 0.0 if offset > 0 else bid_sz1   # new best bid => first in line
                    filled_vol = 0.0
                    t_post = now
                    entry_fair = last_fair       # predicted convergence target
        elif state == "PENDING":
            if bid1 > limit_price:                                  # ran away -> missed
                misses += 1
                state = "FLAT"
                cooldown_until = now + timedelta(seconds=cfg.reentry_cooldown_sec)
            elif last_fair is not None and last_fair <= limit_price:  # signal evaporated
                signal_gone += 1
                state = "FLAT"
                cooldown_until = now + timedelta(seconds=cfg.reentry_cooldown_sec)
            elif (now - t_post).total_seconds() > max_wait_fill_sec:
                timeouts += 1
                state = "FLAT"
                cooldown_until = now + timedelta(seconds=cfg.reentry_cooldown_sec)
        elif state == "LONG":
            hold = (now - t_fill).total_seconds()
            reason = ""
            if entry_fair and bid1 >= entry_fair:
                reason = "converge_tp"
            elif bid1 <= entry_price * (1.0 - cfg.hard_stop_bps / 1e4):
                reason = "hard_stop"
            elif last_fair is not None and last_fair < entry_price:
                reason = "fair_reversal"
            elif hold >= max_hold_sec:
                reason = "time_stop"
            if reason:
                pnl = (bid1 / entry_price - 1.0) * 1e4 - commission_bps   # maker in, taker out
                trades.append({"reason": reason, "pnl": pnl, "hold": hold})
                state = "FLAT"
                cooldown_until = now + timedelta(seconds=cfg.reentry_cooldown_sec)

    pnl = [t["pnl"] for t in trades]
    by_reason: dict[str, int] = defaultdict(int)
    for t in trades:
        by_reason[t["reason"]] += 1
    return {
        "n_trade_rows": n_trade_rows,
        "posts": posts, "fills": fills, "misses": misses, "jumps": jumps,
        "timeouts": timeouts, "signal_gone": signal_gone,
        "fill_rate": fills / posts if posts else float("nan"),
        "n_closed": len(trades),
        "by_reason": dict(by_reason),
        "mean_pnl_filled": st.mean(pnl) if pnl else float("nan"),
        "win_rate": (sum(1 for x in pnl if x > 0) / len(pnl)) if pnl else float("nan"),
        "exp_per_post": (sum(pnl) / posts) if posts else float("nan"),
        "avg_hold": st.mean([t["hold"] for t in trades]) if trades else float("nan"),
    }


def _fmt(x) -> str:
    if isinstance(x, float):
        return "nan" if x != x else f"{x:+.2f}"
    return str(x)


def main() -> int:
    ap = argparse.ArgumentParser(description="Backtest the ETF iNAV-lag directional strategy on collected CSVs.")
    ap.add_argument("--glob", default="out/etf_inav_lob_alpha_*.csv", help="CSV glob (repo-relative).")
    ap.add_argument("--code", help="Only this ETF code.")
    ap.add_argument("--horizons", default="2,5,10", help="Forward horizons (sec) for the thesis check.")
    ap.add_argument("--commission-bps", type=float, default=0.3, help="Round-trip commission (bps). ETF: no sell tax.")
    # strategy knobs (override engine defaults)
    ap.add_argument("--vel-entry-bps", type=float)
    ap.add_argument("--persistence-k", type=int)
    ap.add_argument("--max-overpay-bps", type=float)
    ap.add_argument("--score-threshold", type=float)
    ap.add_argument("--max-spread-bps", type=float)
    ap.add_argument("--max-hold-sec", type=float)
    ap.add_argument("--min-ask-qty", type=float)
    ap.add_argument("--nav-age-max-sec", type=float)
    # maker fill sim (D)
    ap.add_argument("--gap-entry-bps", type=float, default=3.0, help="Post a buy limit when (fair-ref) >= this.")
    ap.add_argument("--gap-ref", choices=["ask", "micro", "mid"], default="micro",
                    help="ETF price compared to iNAV: ask (taker edge), micro (vol-weighted), mid.")
    ap.add_argument("--queue-factor", type=float, default=1.0, help="Queue ahead = bid_size * this (1=behind whole bid).")
    ap.add_argument("--max-wait-fill-sec", type=float, default=10.0, help="Cancel an unfilled limit after this.")
    ap.add_argument("--post-offset-ticks", type=int, default=1, help="Queue jump: post N ticks above bid when gap is wide.")
    ap.add_argument("--tick-size", type=float, default=5.0, help="ETF tick size (KRW).")
    ap.add_argument("--queue-jump-gap-bps", type=float, default=6.0, help="Only apply the tick offset when gap >= this.")
    args = ap.parse_args()

    paths = sorted((_REPO_ROOT).glob(args.glob))
    if not paths:
        print(f"no files match {args.glob}", file=sys.stderr)
        return 2

    cfg = _default_cfg()
    for name in ("vel_entry_bps", "persistence_k", "max_overpay_bps",
                 "score_threshold", "max_spread_bps", "max_hold_sec",
                 "min_ask_qty", "nav_age_max_sec"):
        v = getattr(args, name)
        if v is not None:
            setattr(cfg, name, v)

    horizons = [float(x) for x in args.horizons.split(",") if x.strip()]
    by_code = _load_rows(paths)
    print(f"files={len(paths)}  codes={list(by_code)}  commission_bps={args.commission_bps}")
    print(f"cfg: vel_entry={cfg.vel_entry_bps} k={cfg.persistence_k} overpay={cfg.max_overpay_bps} "
          f"score_thr={cfg.score_threshold} max_spread={cfg.max_spread_bps} max_hold={cfg.max_hold_sec}\n")

    for code, rows in by_code.items():
        if args.code and code != args.code:
            continue
        res = strategy_check(rows, cfg, args.commission_bps)
        thesis = thesis_check(res["nav_series"], horizons)

        print(f"================ {code}  ({len(rows)} rows, {res['nav_points']} iNAV pts) ================")
        print("  (A) THESIS  iNAV velocity -> forward iNAV return:")
        for h, s in thesis.items():
            if s.get("n", 0) < 8:
                print(f"      {h:>4}: n={s.get('n', 0)} (too few)")
                continue
            print(f"      {h:>4}: corr={_fmt(s['corr'])}  "
                  f"fwd|up={_fmt(s['fwd_after_up_mean'])}bps (hit {s['fwd_after_up_hit']*100:.0f}%, n={s['n_up']})  "
                  f"fwd|down={_fmt(s['fwd_after_down_mean'])}bps (n={s['n_down']})")
        print("  (B) STRATEGY  replayed ENTER/EXIT with costs:")
        print(f"      0D books_ok={res['n_book_ok']}  signals={res['sig_count']}")
        print(f"      entries={res['n_enter']}  closed_trades={res['n_exit_closed']}  by_reason={res['by_reason']}")
        if res["n_exit_closed"]:
            print(f"      net/trade={_fmt(res['net_mean_bps'])}bps  net_sum={_fmt(res['net_sum_bps'])}bps  "
                  f"win_rate={res['win_rate']*100:.0f}%  avg_hold={res['avg_hold']:.1f}s")

        gap = gap_convergence_check(rows, cfg, horizons, args.commission_bps)
        print("  (C) GAP  buy when (fair_now - ask1) wide -> taker(cross spread) vs maker(limit at bid):")
        for h, buckets in gap.items():
            print(f"      horizon {h}:  [gap bucket] n  taker  ask_moved  MAKER  maker_win%")
            for (lo, hi), nb, taker, askf, maker, mwin in buckets:
                if not nb:
                    continue
                lab = f"[{'<0' if lo < 0 else f'{lo:g}'}..{'inf' if hi > 1e8 else f'{hi:g}'})"
                print(f"        {lab:>12} n={nb:<4} taker={_fmt(taker)}bps  ask_fwd={_fmt(askf)}bps  "
                      f"maker={_fmt(maker)}bps  win={mwin*100:.0f}%")

        sim = maker_gap_sim(
            rows, cfg, gap_entry_bps=args.gap_entry_bps, queue_factor=args.queue_factor,
            max_wait_fill_sec=args.max_wait_fill_sec, max_hold_sec=cfg.max_hold_sec,
            commission_bps=args.commission_bps, gap_ref=args.gap_ref,
            post_offset_ticks=args.post_offset_ticks, tick_size=args.tick_size,
            queue_jump_gap_bps=args.queue_jump_gap_bps,
        )
        print(f"  (D) MAKER SIM  gap=iNAV-{args.gap_ref}; post buy-limit when gap>={args.gap_entry_bps}bps, "
              f"jump +{args.post_offset_ticks}tick when gap>={args.queue_jump_gap_bps}bps:")
        print(f"      0B trade rows={sim['n_trade_rows']}  posts={sim['posts']} (jumps={sim['jumps']})  "
              f"fills={sim['fills']} (rate={sim['fill_rate']*100:.0f}%)  "
              f"ran_away={sim['misses']}  signal_gone={sim['signal_gone']}  timeouts={sim['timeouts']}")
        if sim["n_trade_rows"] == 0:
            print("      -> no 0B trade data in CSV; RECOLLECT with trades to model fills. "
                  "(ran_away/posts above is still informative.)")
        if sim["n_closed"]:
            print(f"      filled trades={sim['n_closed']}  by_reason={sim['by_reason']}")
            print(f"      net/filled={_fmt(sim['mean_pnl_filled'])}bps  win={sim['win_rate']*100:.0f}%  "
                  f"EXP/POST={_fmt(sim['exp_per_post'])}bps (incl. missed=0)  avg_hold={sim['avg_hold']:.1f}s")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
