# -*- coding: utf-8 -*-
"""REST polling collector for ETF orderbook + NAV (alternative to the WS feed).

Data sources (both REST):
  * orderbook  -> Kiwoom ka10004 (주식호가)
  * realtime NAV -> KIS (한국투자) ETF/ETN 현재가 FHPST02400000 -> output.nav
    (Kiwoom REST has NO realtime iNAV, so NAV is fetched from KIS instead.)

Honest limitation: REST returns SNAPSHOTS, so it cannot capture every 0B trade
print. The maker FILL simulation (backtest section D) needs trades and will show
~0 fills on REST data. REST is good for the gap SIGNAL (sections A/C) and for
verifying connectivity/field mapping; the WS collector (with 0B) remains the tool
for fill realism.

It writes the SAME CSV schema as the WS collector (orderbook re-encoded into the
0D FID layout, NAV into a 0G-style {'36': nav}), so the existing backtest reads it
unchanged.

    # confirm field mapping / connectivity (Kiwoom 호가 + KIS NAV):
    python trading/etf_inav_lob_rest_collect.py probe 457990

    # poll for 15 minutes, ~4x/sec:
    python trading/etf_inav_lob_rest_collect.py collect --code 457990 --duration-min 15 --interval-ms 250
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from trading.config import ConfigError, load_config
from trading.kiwoom.auth import TokenManager
from trading.etf_inav_lob_alpha_collect import (
    CsvLogger,
    _num,
    _rest_post,
    fetch_etf_name,
)
from trading.kis import KisClient, KisError
from trading.kis import stocks as kis_stocks

ORDERBOOK_API = "ka10004"   # 주식호가 (Kiwoom)
# NAV source: Kiwoom REST has no realtime iNAV, so NAV comes from KIS
# (한국투자) ETF/ETN 현재가 (FHPST02400000) -> output.nav.


def parse_orderbook_rest(body: dict[str, Any]) -> tuple[dict, dict, dict, dict]:
    """Defensively parse a ka10004 body into ask/bid price & size by level.

    Verified ka10004 schema:
      매도호가1(최우선): sel_fpr_bid / sel_fpr_req
      매도호가 2~10:     sel_{n}th_pre_bid / sel_{n}th_pre_req
      매수호가1(최우선): buy_fpr_bid / buy_fpr_req
      매수호가 2~10:     buy_{n}th_pre_bid / buy_{n}th_pre_req
      ('bid'=호가 price, 'req'=잔량 qty; '..._req_pre'/'..._pre' = 전일대비, ignored)
    """
    ask_p: dict[int, float] = {}
    ask_q: dict[int, float] = {}
    bid_p: dict[int, float] = {}
    bid_q: dict[int, float] = {}
    for key, raw in body.items():
        kl = str(key).lower()
        if kl.endswith("_pre"):            # 전일대비 fields (sel_..._req_pre etc.)
            continue
        is_ask = kl.startswith("sel")
        is_bid = kl.startswith("buy")
        if not (is_ask or is_bid):
            continue
        if "fpr" in kl:                    # 최우선호가 = level 1
            lvl = 1
        else:
            m = re.search(r"(\d+)", kl)
            if not m:
                continue
            lvl = int(m.group(1))
        if not 1 <= lvl <= 10:
            continue
        val = _num(raw, abs_value=True)
        if val is None:
            continue
        if "req" in kl:                    # 잔량 (quantity)
            (ask_q if is_ask else bid_q)[lvl] = val
        elif "bid" in kl:                  # 호가 (price)
            (ask_p if is_ask else bid_p)[lvl] = val
    return ask_p, ask_q, bid_p, bid_q


def _ob_to_fid_values(ask_p, ask_q, bid_p, bid_q, quote_time: str = "") -> dict[str, Any]:
    """Re-encode parsed orderbook into the WS 0D FID layout the backtest expects."""
    vals: dict[str, Any] = {"21": quote_time}
    for lvl in range(1, 11):
        if lvl in ask_p:
            vals[str(40 + lvl)] = ask_p[lvl]
        if lvl in bid_p:
            vals[str(50 + lvl)] = bid_p[lvl]
        if lvl in ask_q:
            vals[str(60 + lvl)] = ask_q[lvl]
        if lvl in bid_q:
            vals[str(70 + lvl)] = bid_q[lvl]
    return vals


def cmd_probe(args) -> int:
    cfg = load_config()
    token = TokenManager(cfg).get_token()
    code = args.code
    print(f"--- Kiwoom {ORDERBOOK_API} (호가) {code} ---")
    ob = _rest_post(cfg, token, ORDERBOOK_API, {"stk_cd": code})
    print(json.dumps(ob, ensure_ascii=False, indent=2)[:3500])
    ap, aq, bp, bq = parse_orderbook_rest(ob)
    print(f"\n[parsed] ask1={ap.get(1)} bid1={bp.get(1)} ask_qty1={aq.get(1)} bid_qty1={bq.get(1)}")
    print(f"\n--- KIS ETF/ETN 현재가 (FHPST02400000) {code} ---")
    kis = KisClient()
    out = kis_stocks.etf_inquire_price(kis, code).get("output") or {}
    print(json.dumps({k: out.get(k) for k in ("nav", "stck_prpr", "nav_prdy_ctrt", "prdy_last_nav")},
                     ensure_ascii=False, indent=2))
    print(f"\n[parsed] nav={kis_stocks.etf_nav(kis, code)}")
    return 0


def cmd_collect(args) -> int:
    cfg = load_config()
    token_mgr = TokenManager(cfg)
    token = token_mgr.get_token()
    kis = KisClient()                       # NAV source (한국투자)
    codes = [c.strip().upper() for grp in (args.code or []) for c in grp] or ["457990"]

    out = args.out
    if out is None:
        stamp = datetime.now().strftime("%Y%m%d")
        out = Path("out") / f"etf_inav_lob_rest_{'_'.join(codes)}_{stamp}.csv"
    logger = CsvLogger(Path(out))

    for code in codes:
        nm = fetch_etf_name(cfg, token, code)
        try:
            seed_nav = kis_stocks.etf_nav(kis, code)
        except KisError as exc:
            seed_nav = None
            print(f"[KIS nav warn] {code}: {exc}", file=sys.stderr)
        print(f"target={code} {nm} kis_nav={seed_nav}", file=sys.stderr)

    end_at = datetime.now() + timedelta(minutes=args.duration_min)
    interval = max(0.05, args.interval_ms / 1000.0)
    nav_every = max(1, int(round(args.nav_interval_ms / args.interval_ms)))
    print(f"polling codes={codes} interval={interval*1000:.0f}ms nav_every={nav_every} until={end_at} out={out}",
          file=sys.stderr)

    tick = 0
    try:
        while datetime.now() < end_at:
            for code in codes:
                recv_ts = datetime.now().isoformat(timespec="milliseconds")
                try:
                    ob = _rest_post(cfg, token, ORDERBOOK_API, {"stk_cd": code})
                except Exception as exc:  # noqa: BLE001
                    print(f"[skip ob] {code}: {exc}", file=sys.stderr)
                    continue
                ap, aq, bp, bq = parse_orderbook_rest(ob)
                vals = _ob_to_fid_values(ap, aq, bp, bq, quote_time=recv_ts)
                logger.write({
                    "recv_ts": recv_ts, "event_type": "0D", "code": code,
                    "ask1": ap.get(1, ""), "bid1": bp.get(1, ""),
                    "ask_qty1": aq.get(1, ""), "bid_qty1": bq.get(1, ""),
                    "raw_values_json": json.dumps(vals, ensure_ascii=False, separators=(",", ":")),
                })
                if tick % nav_every == 0:
                    try:
                        nav = kis_stocks.etf_nav(kis, code)   # KIS realtime NAV
                    except Exception:  # noqa: BLE001
                        nav = None
                    if nav:
                        logger.write({
                            "recv_ts": datetime.now().isoformat(timespec="milliseconds"),
                            "event_type": "0G", "code": code, "inav": nav,
                            "raw_values_json": json.dumps({"36": nav}, separators=(",", ":")),
                        })
            tick += 1
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        logger.close()
    print(f"done -> {out}", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="etf_inav_lob_rest_collect", description="REST polling ETF orderbook+NAV collector.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("probe", help="Dump raw ka10004 + ka40002 to confirm field mapping.")
    sp.add_argument("code")
    sp.set_defaults(handler=cmd_probe)

    sp = sub.add_parser("collect", help="Poll orderbook (+NAV) and log CSV.")
    sp.add_argument("--code", action="append", nargs="+", help="ETF code(s).")
    sp.add_argument("--duration-min", type=float, default=15.0)
    sp.add_argument("--interval-ms", type=float, default=250.0, help="Orderbook poll interval (rate-limit aware).")
    sp.add_argument("--nav-interval-ms", type=float, default=1000.0, help="NAV poll interval.")
    sp.add_argument("--out", type=Path)
    sp.set_defaults(handler=cmd_collect)
    return p


def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.handler(args))
    except ConfigError as exc:
        print(f"[config error] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
