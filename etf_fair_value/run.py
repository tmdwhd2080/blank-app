from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from etf_fair_value.fair_value import FairValueConfig, build_signal
from etf_fair_value.estimator import estimate_from_intraday_samples
from etf_fair_value.krx_pdf import (
    attach_unit_cash,
    ensure_krx_login,
    fetch_pdf_static,
    fetch_top_etfs_by_value,
)
from etf_fair_value.kis_data import KisMarketData
from etf_fair_value.models import EtfStatic, bps, clean_code
from etf_fair_value.orders import KiwoomOrderRouter, OrderRequest
from etf_fair_value.secrets import write_krx_env

DEFAULT_ENTRY_EDGE_BPS = 40.0
DEFAULT_SCREEN_SCAN_LIMIT = 35
DEFAULT_SCREEN_SELECT_LIMIT = 50
DEFAULT_SCREEN_MIN_RANK = 5
DEFAULT_SCREEN_MAX_RANK = 35
DEFAULT_SCREEN_MIN_HOLDINGS = 1
DEFAULT_SCREEN_MAX_HOLDINGS = 10
DEFAULT_EXCLUDE_NAME_KEYWORDS = (
    "\ub808\ubc84\ub9ac\uc9c0",
    "2X",
    "\ucc44\uad8c\ud63c\ud569",
    "\ucc44\uad8c \ud63c\ud569",
)


def _today() -> str:
    return datetime.now().strftime("%Y%m%d")


def _json_ready(value):
    if isinstance(value, dict):
        return {str(k): _json_ready(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_ready(v) for v in value]
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    if hasattr(value, "to_dict"):
        try:
            return _json_ready(value.to_dict())
        except Exception:
            pass
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _fair_value_config(args: argparse.Namespace) -> FairValueConfig:
    return FairValueConfig(
        min_edge_bps=args.min_edge_bps,
        max_spread_bps=args.max_spread_bps,
        use_spread_filter=not getattr(args, "ignore_entry_spread", False),
        obi_weight_bps=getattr(args, "obi_weight_bps", FairValueConfig.obi_weight_bps),
        spread_penalty_weight=getattr(args, "spread_penalty_weight", FairValueConfig.spread_penalty_weight),
        adj_cap_bps=getattr(args, "adj_cap_bps", FairValueConfig.adj_cap_bps),
    )


def _infer_book_tick(book) -> float:
    diffs: list[float] = []
    for i in range(1, 10):
        ask_a = book.ask_prices.get(i)
        ask_b = book.ask_prices.get(i + 1)
        if ask_a and ask_b and ask_b > ask_a:
            diffs.append(ask_b - ask_a)
        bid_a = book.bid_prices.get(i)
        bid_b = book.bid_prices.get(i + 1)
        if bid_a and bid_b and bid_a > bid_b:
            diffs.append(bid_a - bid_b)
    if book.ask1 and book.bid1 and book.ask1 > book.bid1:
        diffs.append(book.ask1 - book.bid1)
    return min(diffs) if diffs else 1.0


def _buy_limit_price(book, policy: str) -> int:
    if policy == "take_ask":
        if not book.ask1:
            raise RuntimeError("ask1 is missing")
        return int(book.ask1)
    if not book.bid1:
        if book.ask1:
            return int(book.ask1)
        raise RuntimeError("bid1 and ask1 are missing")
    if not book.ask1:
        return int(book.bid1)
    tick = _infer_book_tick(book)
    improved = book.bid1 + tick
    if improved < book.ask1:
        return int(improved)
    return int(book.bid1)


def _buy_order_price(book, policy: str, order_type: str) -> int:
    if order_type in {"3", "13", "23"}:
        return 0
    return _buy_limit_price(book, policy)


@dataclass(frozen=True)
class EtfLongPosition:
    entry_ts: float
    entry_price: float
    entry_fair_value: float
    entry_nav: float
    entry_buy_gap_bps: float


@dataclass
class PaperExecutionPosition:
    code: str
    name: str
    qty: int
    entry_ts: float
    entry_price: float
    entry_fair_value: float
    entry_nav: float
    entry_buy_gap_bps: float
    buy_order_no: str


def _return_bps(exit_price: float | None, entry_price: float | None) -> float | None:
    if not exit_price or not entry_price:
        return None
    return bps(exit_price - entry_price, entry_price)


def _sell_order_price(book, order_type: str) -> int:
    if order_type in {"3", "13", "23"}:
        return 0
    if not book.bid1:
        raise RuntimeError("bid1 is missing")
    return int(book.bid1)


def _holding_quantity(client, code: str, *, available: bool = False) -> int:
    from trading.kiwoom.tr import account

    target = clean_code(code)
    for holding in account.holdings(client):
        if clean_code(holding.stk_cd) == target:
            return holding.available_quantity if available else holding.quantity
    return 0


def _order_is_open(client, order_no: str, code: str) -> bool:
    from trading.kiwoom.tr import account

    rows = account.open_orders(client, all_stk_tp="1", stk_cd=clean_code(code))
    needle = str(order_no).strip()
    return any(needle and needle in str(row) for row in rows)


def _cancel_if_open(client, order_no: str, code: str) -> bool:
    if not order_no or not _order_is_open(client, order_no, code):
        return False
    from trading.kiwoom.tr import order

    order.cancel(client, order_no, clean_code(code))
    return True


def _wait_stock_order(
    client,
    *,
    side: str,
    code: str,
    qty: int,
    order_no: str,
    before_qty: int,
    timeout_sec: float,
    poll_sec: float,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_sec
    last_qty = before_qty
    open_order = True
    while time.monotonic() < deadline:
        time.sleep(max(poll_sec, 0.1))
        last_qty = _holding_quantity(client, code)
        open_order = _order_is_open(client, order_no, code)
        if side == "BUY" and last_qty >= before_qty + qty:
            return {"filled": True, "status": "FILLED_BY_HOLDING", "after_qty": last_qty}
        if side == "SELL" and last_qty <= before_qty - qty:
            return {"filled": True, "status": "FILLED_BY_HOLDING", "after_qty": last_qty}
        if not open_order:
            return {"filled": True, "status": "FILLED_ASSUMED_NO_OPEN_ORDER", "after_qty": last_qty}

    cancelled = _cancel_if_open(client, order_no, code)
    return {
        "filled": False,
        "status": "OPEN_CANCELLED" if cancelled else "NOT_FILLED_NO_OPEN_ORDER",
        "after_qty": last_qty,
    }


def _fmt(value: object, digits: int = 2) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _print_signal_row(row: dict) -> None:
    print(
        " ".join(
            [
                str(row.get("recv_ts", "")),
                str(row.get("code", "")),
                str(row.get("name", "")),
                str(row.get("position_action", "")),
                f"pnl_bps={_fmt(row.get('pnl_bps'))}",
                f"buy_gap={_fmt(row.get('buy_gap_bps'))}",
                f"fv={_fmt(row.get('fair_value'), 0)}",
                f"nav={_fmt(row.get('nav'), 0)}",
                f"bid={_fmt(row.get('bid1'), 0)}",
                f"ask={_fmt(row.get('ask1'), 0)}",
                f"obi={_fmt(row.get('etf_obi'), 3)}",
                f"spread={_fmt(row.get('spread_bps'))}",
                str(row.get("reason", "")),
            ]
        ),
        flush=True,
    )


def _component_price_map(
    args: argparse.Namespace,
    kis: KisMarketData,
    static: EtfStatic,
    *,
    price_type: str,
) -> dict[str, float]:
    codes = [row.code for row in static.holdings]
    sleep_sec = float(getattr(args, "component_sleep_sec", getattr(args, "sleep_sec", 0.25)) or 0.0)
    micro_levels = int(getattr(args, "micro_levels", 1) or 1)
    if price_type == "micro":
        return kis.micro_price_map(
            codes,
            sleep_sec=sleep_sec,
            levels=micro_levels,
        )
    return kis.last_price_map(codes, sleep_sec=sleep_sec)


def _calibrate_unit_cash(
    args: argparse.Namespace,
    kis: KisMarketData,
    static: EtfStatic,
    official_nav: float,
) -> tuple[EtfStatic, dict]:
    duration = float(getattr(args, "calibration_sec", 0.0) or 0.0)
    interval = float(getattr(args, "calibration_interval_sec", 30.0) or 30.0)
    price_type = str(getattr(args, "calibration_price_type", "last") or "last")
    if duration <= 0:
        static, estimate = attach_unit_cash(
            static,
            official_nav=official_nav,
            creation_unit=args.creation_unit,
        )
        return static, asdict(estimate)

    deadline = time.monotonic() + duration
    samples: list[tuple[float, float]] = []
    while time.monotonic() < deadline:
        nav_sample = kis.etf_nav(static.etf_code)
        if nav_sample:
            prices = _component_price_map(args, kis, static, price_type=price_type)
            if len(prices) == len(static.holdings):
                samples.append((nav_sample, static.basket_value(prices)))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))

    if len(samples) >= 2:
        try:
            estimate = estimate_from_intraday_samples(samples, creation_unit=args.creation_unit)
            calibrated = replace(
                static,
                creation_unit=estimate.creation_unit,
                cash_minus_fee=estimate.cash_minus_fee,
                confidence=f"unit_cash_intraday_regression_{len(samples)}samples",
            )
            return calibrated, asdict(estimate)
        except Exception as exc:
            static, estimate = attach_unit_cash(
                static,
                official_nav=official_nav,
                creation_unit=args.creation_unit,
            )
            payload = asdict(estimate)
            payload["method"] = f"{payload['method']}_fallback_after_calibration_error: {str(exc)[:120]}"
            return static, payload

    static, estimate = attach_unit_cash(
        static,
        official_nav=official_nav,
        creation_unit=args.creation_unit,
    )
    payload = asdict(estimate)
    payload["method"] = f"{payload['method']}_fallback_after_insufficient_calibration_samples_{len(samples)}"
    return static, payload


def _load_static(args: argparse.Namespace, kis: KisMarketData) -> tuple[EtfStatic, dict]:
    official_nav = kis.etf_nav(args.code)
    if not official_nav:
        raise RuntimeError(f"KIS did not return official ETF NAV for {args.code}")
    try:
        static = fetch_pdf_static(args.code, args.date)
        static, estimate = _calibrate_unit_cash(
            args,
            kis,
            static,
            official_nav,
        )
        return static, estimate
    except Exception as exc:
        if args.no_kis_component_fallback:
            raise
        static = kis.pseudo_static_from_kis_components(
            args.code,
            official_nav=official_nav,
            trade_date=args.date,
        )
        return static, {
            "creation_unit": 1.0,
            "cash_minus_fee": 0.0,
            "raw_creation_unit": 1.0,
            "method": f"kis_component_weight_fallback_after_pdf_error: {exc}",
            "max_relative_rounding_error": 0.0,
            "sample_count": len(static.holdings),
        }


def _static_load_args(args: argparse.Namespace, code: str, *, calibration_sec: float = 0.0) -> argparse.Namespace:
    return argparse.Namespace(
        code=code,
        date=args.date,
        creation_unit=args.creation_unit,
        no_kis_component_fallback=True,
        calibration_sec=calibration_sec,
        calibration_interval_sec=getattr(args, "calibration_interval_sec", 30.0),
        calibration_price_type=getattr(args, "calibration_price_type", "last"),
        component_sleep_sec=getattr(args, "component_sleep_sec", 0.25),
        micro_levels=getattr(args, "micro_levels", 1),
    )


def _bulk_calibrate_static_cache(
    args: argparse.Namespace,
    kis: KisMarketData,
    static_cache: dict[str, tuple[EtfStatic, dict]],
) -> None:
    duration = float(getattr(args, "calibration_sec", 0.0) or 0.0)
    if duration <= 0 or not static_cache:
        return

    samples: dict[str, list[tuple[float, float]]] = {code: [] for code in static_cache}
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        for code, (static, _estimate) in list(static_cache.items()):
            nav_sample = kis.etf_nav(code)
            if not nav_sample:
                continue
            prices = _component_price_map(
                args,
                kis,
                static,
                price_type=args.calibration_price_type,
            )
            if len(prices) == len(static.holdings):
                samples[code].append((nav_sample, static.basket_value(prices)))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(args.calibration_interval_sec, remaining))

    for code, rows in samples.items():
        if len(rows) < 2:
            continue
        static, _old_estimate = static_cache[code]
        try:
            estimate = estimate_from_intraday_samples(rows, creation_unit=args.creation_unit)
        except Exception as exc:
            old_static, old_estimate = static_cache[code]
            failed = dict(old_estimate)
            failed["method"] = f"{failed.get('method', 'unknown')}_calibration_failed: {str(exc)[:120]}"
            static_cache[code] = (old_static, failed)
            continue
        static_cache[code] = (
            replace(
                static,
                creation_unit=estimate.creation_unit,
                cash_minus_fee=estimate.cash_minus_fee,
                confidence=f"unit_cash_intraday_regression_{len(rows)}samples",
            ),
            asdict(estimate),
        )


def cmd_top(args: argparse.Namespace) -> int:
    rows = fetch_top_etfs_by_value(args.date, limit=args.limit)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else [])
            if rows:
                writer.writeheader()
                writer.writerows(rows)
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


def cmd_screen(args: argparse.Namespace) -> int:
    selected, errors = _screen_candidates(args)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("w", newline="", encoding="utf-8-sig") as f:
            fields = list(selected[0].keys()) if selected else [
                "date",
                "code",
                "name",
                "rank",
                "holdings",
                "trading_value",
            ]
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(selected)
    print(json.dumps(_json_ready({"selected": selected, "errors": errors[:10]}), ensure_ascii=False, indent=2))
    return 0


def _screen_candidates(args: argparse.Namespace) -> tuple[list[dict], list[dict]]:
    rows = fetch_top_etfs_by_value(args.date, limit=args.scan_limit)
    selected: list[dict] = []
    errors: list[dict] = []
    exclude_keywords = [
        *DEFAULT_EXCLUDE_NAME_KEYWORDS,
        *(x.strip() for x in args.exclude_name_keyword if x.strip()),
    ]
    for rank, row in enumerate(rows, start=1):
        if rank <= args.skip_top:
            continue
        if args.min_rank and rank < args.min_rank:
            continue
        if args.max_rank and rank > args.max_rank:
            continue
        value = row.get("trading_value") or 0
        if args.min_trading_value and value < args.min_trading_value:
            continue
        name = str(row.get("name") or "")
        if exclude_keywords and any(keyword in name for keyword in exclude_keywords):
            continue
        code = row["code"]
        try:
            static = fetch_pdf_static(code, args.date)
        except Exception as exc:
            errors.append({"rank": rank, "code": code, "error": str(exc)[:200]})
            continue
        holdings = len(static.holdings)
        if holdings < args.min_holdings or holdings > args.max_holdings:
            continue
        selected.append(
            _json_ready({
                **row,
                "rank": rank,
                "holdings": holdings,
                "pdf_equity_amount": static.pdf_equity_amount,
                "cash_like_amount": static.cash_like_amount,
            })
        )
        if len(selected) >= args.select_limit:
            break
    return selected, errors


def _nav_for_signal(args: argparse.Namespace, kis: KisMarketData, static: EtfStatic) -> tuple[float | None, str, int]:
    if args.price_type == "official":
        return kis.etf_nav(static.etf_code), "KIS_OFFICIAL_NAV", 0

    codes = [row.code for row in static.holdings]
    if args.price_type == "micro":
        prices = kis.micro_price_map(codes, sleep_sec=args.component_sleep_sec, levels=args.micro_levels)
        source = f"LOCAL_COMPONENT_MICRO_L{args.micro_levels}"
    else:
        prices = kis.last_price_map(codes, sleep_sec=args.component_sleep_sec)
        source = "LOCAL_COMPONENT_LAST"

    if len(prices) != len(static.holdings):
        return None, source, len(prices)
    return static.nav_from_prices(prices), source, len(prices)


def cmd_signal_scan(args: argparse.Namespace) -> int:
    candidates, errors = _screen_candidates(args)
    out = args.out or Path("out") / f"etf_signal_scan_{args.date}_{datetime.now().strftime('%H%M%S')}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)

    kis = KisMarketData()
    cfg = _fair_value_config(args)
    static_cache: dict[str, tuple[EtfStatic, dict]] = {}
    positions: dict[str, EtfLongPosition] = {}
    for item in candidates:
        code = item["code"]
        try:
            static_cache[code] = _load_static(_static_load_args(args, code, calibration_sec=0.0), kis)
        except Exception as exc:
            errors.append({"code": code, "error": f"static_load_failed: {str(exc)[:200]}"})
    _bulk_calibrate_static_cache(args, kis, static_cache)

    fields = [
        "recv_ts",
        "rank",
        "code",
        "name",
        "holdings",
        "nav_source",
        "priced_components",
        "nav",
        "fair_value",
        "ask1",
        "bid1",
        "micro",
        "spread_bps",
        "etf_obi",
        "current_basis_bps",
        "expected_basis_bps",
        "buy_gap_bps",
        "sell_gap_bps",
        "decision",
        "position_action",
        "entry_price",
        "exit_price",
        "pnl_bps",
        "hold_sec",
        "order_side",
        "order_qty",
        "order_price",
        "order_no",
        "order_message",
        "reason",
        "estimate_method",
    ]
    counts: dict[str, int] = {"BUY": 0, "HOLD": 0, "ERROR": 0}
    action_counts: dict[str, int] = {}
    router = None
    if args.paper_order:
        router = KiwoomOrderRouter(
            dry_run=False,
            env=args.kiwoom_env,
            require_paper=not args.allow_real_order,
        )
    deadline = time.monotonic() + args.duration_sec
    cycle = 0
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        while time.monotonic() < deadline:
            for item in candidates:
                code = item["code"]
                try:
                    if code not in static_cache:
                        counts["ERROR"] += 1
                        continue
                    static, estimate = static_cache[code]
                    nav, nav_source, priced = _nav_for_signal(args, kis, static)
                    if not nav:
                        counts["ERROR"] += 1
                        continue
                    book = kis.orderbook(code)
                    signal = build_signal(code=code, nav=nav, etf_book=book, cfg=cfg)
                    counts[signal.decision] = counts.get(signal.decision, 0) + 1
                    now_mono = time.monotonic()
                    position_action = "WAIT"
                    entry_price = None
                    exit_price = None
                    pnl_bps = None
                    hold_sec = None
                    order_side = ""
                    order_qty = None
                    order_price = None
                    order_no = ""
                    order_message = ""
                    reason = signal.reason
                    quote_exit_price = book.bid1 or signal.etf_micro
                    pos = positions.get(code)
                    if pos:
                        entry_price = pos.entry_price
                        hold_sec = now_mono - pos.entry_ts
                        pnl_bps = _return_bps(quote_exit_price, pos.entry_price)
                        fv_exit_gap_bps = bps((quote_exit_price or 0.0) - signal.etf_fair_value, signal.etf_fair_value)
                        if fv_exit_gap_bps >= args.exit_fv_buffer_bps:
                            position_action = "EXIT_FV"
                            exit_price = quote_exit_price
                            positions.pop(code, None)
                            reason = f"exit_price_reached_fair_value_{fv_exit_gap_bps:.2f}bps"
                        elif pnl_bps is not None and pnl_bps <= -args.loss_stop_bps:
                            position_action = "EXIT_LOSS"
                            exit_price = quote_exit_price
                            positions.pop(code, None)
                            reason = f"loss_stop_{pnl_bps:.2f}bps"
                        elif hold_sec >= args.exit_max_hold_sec:
                            position_action = "EXIT_TIME"
                            exit_price = quote_exit_price
                            positions.pop(code, None)
                            reason = f"max_hold_{hold_sec:.1f}s"
                        else:
                            position_action = "HOLD_POSITION"
                    elif signal.decision == "BUY":
                        entry_price = book.ask1 or signal.etf_micro
                        if entry_price:
                            if router is not None:
                                req = OrderRequest(
                                    "BUY",
                                    static.etf_code,
                                    args.qty,
                                    _buy_order_price(book, args.buy_limit_policy, args.order_type),
                                    args.order_type,
                                    args.exchange,
                                )
                                result = router.place(req)
                                order_side = result.request.side
                                order_qty = result.request.qty
                                order_price = result.request.price
                                order_no = result.order_no
                                order_message = result.message
                            positions[code] = EtfLongPosition(
                                entry_ts=now_mono,
                                entry_price=entry_price,
                                entry_fair_value=signal.etf_fair_value,
                                entry_nav=nav,
                                entry_buy_gap_bps=signal.executable_buy_gap_bps,
                            )
                            position_action = "ENTER_BUY"
                            reason = "paper_entry_on_buy_signal"

                    action_counts[position_action] = action_counts.get(position_action, 0) + 1
                    row = {
                        "recv_ts": datetime.now().isoformat(timespec="milliseconds"),
                        "rank": item.get("rank"),
                        "code": code,
                        "name": item.get("name"),
                        "holdings": item.get("holdings"),
                        "nav_source": nav_source,
                        "priced_components": priced,
                        "nav": nav,
                        "fair_value": signal.etf_fair_value,
                        "ask1": book.ask1,
                        "bid1": book.bid1,
                        "micro": signal.etf_micro,
                        "spread_bps": signal.spread_bps,
                        "etf_obi": signal.etf_obi,
                        "current_basis_bps": signal.current_basis_bps,
                        "expected_basis_bps": signal.expected_basis_bps,
                        "buy_gap_bps": signal.executable_buy_gap_bps,
                        "sell_gap_bps": signal.executable_sell_gap_bps,
                        "decision": signal.decision,
                        "position_action": position_action,
                        "entry_price": entry_price,
                        "exit_price": exit_price,
                        "pnl_bps": pnl_bps,
                        "hold_sec": hold_sec,
                        "order_side": order_side,
                        "order_qty": order_qty,
                        "order_price": order_price,
                        "order_no": order_no,
                        "order_message": order_message,
                        "reason": reason,
                        "estimate_method": estimate.get("method"),
                    }
                    writer.writerow(row)
                    if args.print_all or position_action != "WAIT":
                        _print_signal_row(row)
                    f.flush()
                except Exception:
                    counts["ERROR"] += 1
            cycle += 1
            if args.once:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(args.interval_sec, remaining))

    print(
        json.dumps(
            _json_ready(
                {
                    "out": str(out),
                    "candidates": len(candidates),
                    "screen_errors": errors[:10],
                    "cycles": cycle,
                    "counts": counts,
                    "action_counts": action_counts,
                    "open_positions": len(positions),
                }
            ),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_paper_exec_probe(args: argparse.Namespace) -> int:
    candidates, errors = _screen_candidates(args) if not args.codes else (
        [
            {
                "rank": i,
                "code": clean_code(code.strip()),
                "name": clean_code(code.strip()),
                "holdings": "",
            }
            for i, code in enumerate(args.codes.split(","), start=1)
            if code.strip()
        ],
        [],
    )
    if args.force_codes:
        seen = {clean_code(item["code"]) for item in candidates}
        for raw_code in args.force_codes.split(","):
            code = clean_code(raw_code.strip())
            if not code or code in seen:
                continue
            candidates.append(
                {
                    "rank": f"forced-{len(candidates) + 1}",
                    "code": code,
                    "name": code,
                    "holdings": "",
                }
            )
            seen.add(code)
    out = args.out or Path("out") / f"etf_paper_exec_probe_{args.date}_{datetime.now().strftime('%H%M%S')}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)

    kis = KisMarketData()
    cfg = _fair_value_config(args)
    static_cache: dict[str, tuple[EtfStatic, dict]] = {}
    for item in candidates:
        code = item["code"]
        try:
            static_cache[code] = _load_static(_static_load_args(args, code, calibration_sec=0.0), kis)
        except Exception as exc:
            errors.append({"code": code, "error": f"static_load_failed: {str(exc)[:200]}"})
    _bulk_calibrate_static_cache(args, kis, static_cache)

    router = KiwoomOrderRouter(
        dry_run=not args.paper_order,
        env=args.kiwoom_env,
        require_paper=not args.allow_real_order,
    )
    kiwoom_client = router.client if args.paper_order else None
    positions: dict[str, PaperExecutionPosition] = {}
    order_disabled_codes: dict[str, str] = {}
    counts: dict[str, int] = {
        "cycles": 0,
        "buy_signals": 0,
        "buy_orders": 0,
        "buy_filled": 0,
        "buy_not_filled": 0,
        "sell_signals": 0,
        "sell_orders": 0,
        "sell_filled": 0,
        "sell_not_filled": 0,
        "round_trips_filled": 0,
        "errors": 0,
    }
    fields = [
        "recv_ts",
        "cycle",
        "rank",
        "code",
        "name",
        "action",
        "decision",
        "nav",
        "fair_value",
        "bid1",
        "ask1",
        "spread_bps",
        "etf_obi",
        "buy_gap_bps",
        "sell_gap_bps",
        "pnl_bps",
        "hold_sec",
        "order_side",
        "order_qty",
        "order_price",
        "order_no",
        "filled",
        "fill_status",
        "before_qty",
        "after_qty",
        "reason",
    ]

    deadline = time.monotonic() + args.duration_sec
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        while time.monotonic() < deadline:
            counts["cycles"] += 1
            for item in candidates:
                code = item["code"]
                now_iso = datetime.now().isoformat(timespec="milliseconds")
                try:
                    if code not in static_cache:
                        counts["errors"] += 1
                        continue
                    static, _estimate = static_cache[code]
                    nav, _nav_source, _priced = _nav_for_signal(args, kis, static)
                    if not nav:
                        counts["errors"] += 1
                        continue
                    book = kis.orderbook(code)
                    signal = build_signal(code=code, nav=nav, etf_book=book, cfg=cfg)
                    pos = positions.get(code)
                    action = "WAIT"
                    reason = signal.reason
                    pnl_bps = None
                    hold_sec = None
                    order_side = ""
                    order_qty = None
                    order_price = None
                    order_no = ""
                    filled = None
                    fill_status = ""
                    before_qty = None
                    after_qty = None

                    if pos is not None:
                        hold_sec = time.monotonic() - pos.entry_ts
                        exit_price = book.bid1 or signal.etf_micro
                        pnl_bps = _return_bps(exit_price, pos.entry_price)
                        fv_exit_gap_bps = bps((exit_price or 0.0) - signal.etf_fair_value, signal.etf_fair_value)
                        exit_action = ""
                        if fv_exit_gap_bps >= args.exit_fv_buffer_bps:
                            exit_action = "EXIT_FV"
                            reason = f"exit_price_reached_fair_value_{fv_exit_gap_bps:.2f}bps"
                        elif pnl_bps is not None and pnl_bps <= -args.loss_stop_bps:
                            exit_action = "EXIT_LOSS"
                            reason = f"loss_stop_{pnl_bps:.2f}bps"
                        elif hold_sec >= args.exit_max_hold_sec:
                            exit_action = "EXIT_TIME"
                            reason = f"max_hold_{hold_sec:.1f}s"

                        if exit_action:
                            action = exit_action
                            counts["sell_signals"] += 1
                            order_side = "SELL"
                            order_qty = pos.qty
                            order_price = _sell_order_price(book, args.sell_order_type)
                            if args.paper_order:
                                before_qty = _holding_quantity(kiwoom_client, code)
                                result = router.place(
                                    OrderRequest(
                                        "SELL",
                                        code,
                                        pos.qty,
                                        order_price,
                                        args.sell_order_type,
                                        args.exchange,
                                    )
                                )
                                counts["sell_orders"] += 1
                                order_no = result.order_no
                                wait = _wait_stock_order(
                                    kiwoom_client,
                                    side="SELL",
                                    code=code,
                                    qty=pos.qty,
                                    order_no=order_no,
                                    before_qty=int(before_qty),
                                    timeout_sec=args.fill_wait_sec,
                                    poll_sec=args.fill_poll_sec,
                                )
                                filled = bool(wait["filled"])
                                fill_status = str(wait["status"])
                                after_qty = wait["after_qty"]
                                if filled:
                                    counts["sell_filled"] += 1
                                    counts["round_trips_filled"] += 1
                                    positions.pop(code, None)
                                else:
                                    counts["sell_not_filled"] += 1
                            else:
                                fill_status = "dry_run"
                        else:
                            action = "HOLD_POSITION"

                    elif signal.decision == "BUY" and len(positions) < args.max_open_positions:
                        counts["buy_signals"] += 1
                        if code in order_disabled_codes:
                            action = "BUY_SKIPPED_ORDER_DISABLED"
                            reason = order_disabled_codes[code]
                        else:
                            action = "ENTER_BUY"
                            order_side = "BUY"
                            order_qty = args.qty
                            order_price = _buy_order_price(book, "take_ask", args.buy_order_type)
                            reason = "buy_signal_hit_ask"
                        if args.paper_order and action == "ENTER_BUY":
                            before_qty = _holding_quantity(kiwoom_client, code)
                            try:
                                result = router.place(
                                    OrderRequest(
                                        "BUY",
                                        code,
                                        args.qty,
                                        order_price,
                                        args.buy_order_type,
                                        args.exchange,
                                    )
                                )
                                counts["buy_orders"] += 1
                                order_no = result.order_no
                                wait = _wait_stock_order(
                                    kiwoom_client,
                                    side="BUY",
                                    code=code,
                                    qty=args.qty,
                                    order_no=order_no,
                                    before_qty=int(before_qty),
                                    timeout_sec=args.fill_wait_sec,
                                    poll_sec=args.fill_poll_sec,
                                )
                                filled = bool(wait["filled"])
                                fill_status = str(wait["status"])
                                after_qty = wait["after_qty"]
                                if filled:
                                    counts["buy_filled"] += 1
                                    positions[code] = PaperExecutionPosition(
                                        code=code,
                                        name=str(item.get("name") or code),
                                        qty=args.qty,
                                        entry_ts=time.monotonic(),
                                        entry_price=book.ask1 or signal.etf_micro,
                                        entry_fair_value=signal.etf_fair_value,
                                        entry_nav=nav,
                                        entry_buy_gap_bps=signal.executable_buy_gap_bps,
                                        buy_order_no=order_no,
                                    )
                                else:
                                    counts["buy_not_filled"] += 1
                            except Exception as exc:  # noqa: BLE001
                                filled = False
                                fill_status = "ORDER_REJECTED"
                                reason = str(exc)[:300]
                                order_disabled_codes[code] = reason
                                counts["buy_not_filled"] += 1
                        else:
                            fill_status = "dry_run"
                    elif signal.decision == "BUY":
                        action = "BUY_SKIPPED_MAX_POSITIONS"
                        reason = "max_open_positions"

                    row = {
                        "recv_ts": now_iso,
                        "cycle": counts["cycles"],
                        "rank": item.get("rank"),
                        "code": code,
                        "name": item.get("name"),
                        "action": action,
                        "decision": signal.decision,
                        "nav": nav,
                        "fair_value": signal.etf_fair_value,
                        "bid1": book.bid1,
                        "ask1": book.ask1,
                        "spread_bps": signal.spread_bps,
                        "etf_obi": signal.etf_obi,
                        "buy_gap_bps": signal.executable_buy_gap_bps,
                        "sell_gap_bps": signal.executable_sell_gap_bps,
                        "pnl_bps": pnl_bps,
                        "hold_sec": hold_sec,
                        "order_side": order_side,
                        "order_qty": order_qty,
                        "order_price": order_price,
                        "order_no": order_no,
                        "filled": filled,
                        "fill_status": fill_status,
                        "before_qty": before_qty,
                        "after_qty": after_qty,
                        "reason": reason,
                    }
                    writer.writerow(row)
                    if action != "WAIT":
                        print(json.dumps(_json_ready(row), ensure_ascii=False), flush=True)
                    f.flush()
                    if args.per_code_sleep_sec > 0:
                        time.sleep(args.per_code_sleep_sec)
                except Exception as exc:  # noqa: BLE001
                    counts["errors"] += 1
                    writer.writerow(
                        {
                            "recv_ts": now_iso,
                            "cycle": counts["cycles"],
                            "rank": item.get("rank"),
                            "code": code,
                            "name": item.get("name"),
                            "action": "ERROR",
                            "reason": str(exc)[:300],
                        }
                    )
                    f.flush()
                    if args.per_code_sleep_sec > 0:
                        time.sleep(args.per_code_sleep_sec)

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(args.interval_sec, remaining))

    summary = {
        **counts,
        "out": str(out),
        "candidates": len(candidates),
        "open_positions": len(positions),
        "open_position_codes": sorted(positions),
        "order_disabled_codes": order_disabled_codes,
        "screen_errors": errors[:10],
        "buy_fill_rate": None if counts["buy_orders"] == 0 else counts["buy_filled"] / counts["buy_orders"],
        "sell_fill_rate": None if counts["sell_orders"] == 0 else counts["sell_filled"] / counts["sell_orders"],
        "round_trip_rate_vs_buy_filled": None
        if counts["buy_filled"] == 0
        else counts["round_trips_filled"] / counts["buy_filled"],
    }
    print(json.dumps(_json_ready(summary), ensure_ascii=False, indent=2))
    return 0


def cmd_setup_krx(args: argparse.Namespace) -> int:
    path = write_krx_env(login_id=args.krx_id)
    print(f"wrote {path}")
    return 0


def cmd_krx_check(args: argparse.Namespace) -> int:
    ok = ensure_krx_login()
    if not ok:
        print("KRX login failed or KRX_ID/KRX_PW is not configured.")
        return 1
    print("KRX login ok")
    if args.code:
        static = fetch_pdf_static(args.code, args.date)
        print(
            json.dumps(
                {
                    "code": static.etf_code,
                    "date": static.trade_date,
                    "holdings": len(static.holdings),
                    "pdf_equity_amount": static.pdf_equity_amount,
                    "cash_like_amount": static.cash_like_amount,
                    "source": static.source,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    kis = KisMarketData()
    static, estimate = _load_static(args, kis)
    book = kis.orderbook(args.code)
    nav = kis.etf_nav(args.code)
    if nav is None:
        raise RuntimeError(f"KIS did not return official ETF NAV for {args.code}")
    signal = build_signal(
        code=clean_code(args.code),
        nav=nav,
        etf_book=book,
        cfg=_fair_value_config(args),
    )
    payload = {
        "static": {
            "code": static.etf_code,
            "date": static.trade_date,
            "holdings": len(static.holdings),
            "pdf_equity_amount": static.pdf_equity_amount,
            "cash_like_amount": static.cash_like_amount,
            "creation_unit": static.creation_unit,
            "cash_minus_fee": static.cash_minus_fee,
            "confidence": static.confidence,
        },
        "unit_cash_estimate": estimate,
        "book": {
            "ask1": book.ask1,
            "bid1": book.bid1,
            "spread_bps": book.spread_bps,
            "obi_3": book.depth_imbalance(3),
            "micro": book.micro_price(),
        },
        "signal": asdict(signal),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    kis = KisMarketData()
    static, estimate = _load_static(args, kis)
    out = args.out or Path("out") / f"etf_fair_value_{clean_code(args.code)}_{_today()}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "recv_ts",
        "code",
        "nav_source",
        "nav",
        "fair_value",
        "micro",
        "mid",
        "ask1",
        "bid1",
        "spread_bps",
        "etf_obi",
        "current_basis_bps",
        "expected_basis_bps",
        "fair_gap_bps",
        "buy_gap_bps",
        "sell_gap_bps",
        "decision",
        "reason",
        "creation_unit",
        "cash_minus_fee",
        "estimate_method",
    ]
    deadline = time.monotonic() + args.duration_sec
    cfg = _fair_value_config(args)
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        while time.monotonic() < deadline:
            nav = kis.etf_nav(args.code)
            nav_source = "KIS_OFFICIAL_NAV"
            if args.component_prices:
                codes = (row.code for row in static.holdings)
                if args.component_price_type == "micro":
                    prices = kis.micro_price_map(
                        codes,
                        sleep_sec=args.component_sleep_sec,
                        levels=args.micro_levels,
                    )
                    nav_source = f"LOCAL_PDF_COMPONENT_MICRO_L{args.micro_levels}"
                else:
                    prices = kis.last_price_map(codes, sleep_sec=args.component_sleep_sec)
                    nav_source = "LOCAL_PDF_COMPONENT_LAST_PRICE"
                calc_nav = static.nav_from_prices(prices)
                if calc_nav and len(prices) == len(static.holdings):
                    nav = calc_nav
            if not nav:
                time.sleep(args.interval_sec)
                continue
            book = kis.orderbook(args.code)
            signal = build_signal(code=static.etf_code, nav=nav, etf_book=book, cfg=cfg)
            writer.writerow(
                {
                    "recv_ts": datetime.now().isoformat(timespec="milliseconds"),
                    "code": static.etf_code,
                    "nav_source": nav_source,
                    "nav": nav,
                    "fair_value": signal.etf_fair_value,
                    "micro": signal.etf_micro,
                    "mid": signal.etf_mid,
                    "ask1": book.ask1,
                    "bid1": book.bid1,
                    "spread_bps": signal.spread_bps,
                    "etf_obi": signal.etf_obi,
                    "current_basis_bps": signal.current_basis_bps,
                    "expected_basis_bps": signal.expected_basis_bps,
                    "fair_gap_bps": signal.fair_gap_bps,
                    "buy_gap_bps": signal.executable_buy_gap_bps,
                    "sell_gap_bps": signal.executable_sell_gap_bps,
                    "decision": signal.decision,
                    "reason": signal.reason,
                    "creation_unit": static.creation_unit,
                    "cash_minus_fee": static.cash_minus_fee,
                    "estimate_method": estimate["method"],
                }
            )
            f.flush()
            time.sleep(args.interval_sec)
    print(f"saved {out}")
    return 0


def cmd_nav_check(args: argparse.Namespace) -> int:
    kis = KisMarketData()
    static, estimate = _load_static(args, kis)
    official_nav = kis.etf_nav(args.code)
    if not official_nav:
        raise RuntimeError(f"KIS did not return official ETF NAV for {args.code}")

    rows = list(static.holdings)
    if args.limit_components:
        rows = rows[: args.limit_components]

    price_map: dict[str, float] = {}
    missing: list[str] = []
    errors: dict[str, str] = {}
    started = time.monotonic()
    for i, row in enumerate(rows, start=1):
        try:
            if args.price_type == "micro":
                price = kis.micro_price(row.code, levels=args.micro_levels)
                if price:
                    price_map[row.code] = price
            else:
                price_map[row.code] = kis.last_price(row.code)
        except Exception as exc:
            missing.append(row.code)
            errors[row.code] = str(exc)[:200]
        if args.sleep_sec > 0 and i < len(rows):
            time.sleep(args.sleep_sec)

    full_coverage = len(price_map) == len(static.holdings)
    local_nav = static.nav_from_prices(price_map) if full_coverage else None
    payload = {
        "code": static.etf_code,
        "date": static.trade_date,
        "source": static.source,
        "confidence": static.confidence,
        "holdings_total": len(static.holdings),
        "priced": len(price_map),
        "missing": len(missing),
        "full_coverage": full_coverage,
        "official_nav": official_nav,
        "local_nav": local_nav,
        "diff_bps": None if local_nav is None else (local_nav - official_nav) / official_nav * 10000.0,
        "price_type": args.price_type,
        "micro_levels": args.micro_levels if args.price_type == "micro" else None,
        "creation_unit": static.creation_unit,
        "cash_minus_fee": static.cash_minus_fee,
        "estimate": estimate,
        "elapsed_sec": round(time.monotonic() - started, 3),
        "missing_codes": missing[:20],
        "errors_sample": dict(list(errors.items())[:5]),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.require_full and not full_coverage:
        return 1
    return 0


def cmd_trade_once(args: argparse.Namespace) -> int:
    kis = KisMarketData()
    static, estimate = _load_static(args, kis)
    nav, nav_source, priced_components = _nav_for_signal(args, kis, static)
    if not nav:
        raise RuntimeError(f"could not calculate ETF NAV for {args.code}")
    book = kis.orderbook(args.code)
    cfg = _fair_value_config(args)
    signal = build_signal(code=static.etf_code, nav=nav, etf_book=book, cfg=cfg)
    print(
        json.dumps(
            {
                "signal": asdict(signal),
                "estimate": estimate,
                "nav_source": nav_source,
                "priced_components": priced_components,
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    if signal.decision == "BUY":
        req = OrderRequest(
            "BUY",
            static.etf_code,
            args.qty,
            _buy_order_price(book, args.buy_limit_policy, args.order_type),
            args.order_type,
            args.exchange,
        )
    else:
        print("no order: decision is HOLD")
        return 0

    send_order = args.paper_order or args.live_order
    router = KiwoomOrderRouter(
        dry_run=not send_order,
        env=args.kiwoom_env,
        require_paper=not args.allow_real_order,
    )
    result = router.place(req)
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="etf_fair_value")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("setup-krx", help="Create local ignored KRX secret file.")
    sp.add_argument("--krx-id", help="KRX login id. Password is always prompted.")
    sp.set_defaults(handler=cmd_setup_krx)

    sp = sub.add_parser("krx-check", help="Check KRX login and optionally fetch one ETF PDF.")
    sp.add_argument("--code", help="ETF code, e.g. 069500")
    sp.add_argument("--date", default=_today(), help="PDF trade date YYYYMMDD")
    sp.set_defaults(handler=cmd_krx_check)

    sp = sub.add_parser("top", help="Fetch top ETFs by trading value from KRX/pykrx.")
    sp.add_argument("--date", default=_today())
    sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("--out", type=Path)
    sp.set_defaults(handler=cmd_top)

    sp = sub.add_parser("screen", help="Screen ETFs by trading value rank and PDF holding count.")
    sp.add_argument("--date", default=_today())
    sp.add_argument("--scan-limit", type=int, default=DEFAULT_SCREEN_SCAN_LIMIT, help="Fetch this many top trading-value ETFs first.")
    sp.add_argument("--select-limit", type=int, default=DEFAULT_SCREEN_SELECT_LIMIT, help="Return at most this many screened ETFs.")
    sp.add_argument("--skip-top", type=int, default=0, help="Skip the first N most-traded ETFs.")
    sp.add_argument("--min-rank", type=int, default=DEFAULT_SCREEN_MIN_RANK)
    sp.add_argument("--max-rank", type=int, default=DEFAULT_SCREEN_MAX_RANK)
    sp.add_argument("--min-holdings", type=int, default=DEFAULT_SCREEN_MIN_HOLDINGS)
    sp.add_argument("--max-holdings", type=int, default=DEFAULT_SCREEN_MAX_HOLDINGS)
    sp.add_argument("--min-trading-value", type=float, default=0.0)
    sp.add_argument(
        "--exclude-name-keyword",
        action="append",
        default=[],
        help="Exclude ETFs whose name contains this keyword. Can be repeated.",
    )
    sp.add_argument("--out", type=Path)
    sp.set_defaults(handler=cmd_screen)

    def add_fv_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--obi-weight-bps", type=float, default=FairValueConfig.obi_weight_bps)
        sp.add_argument("--spread-penalty-weight", type=float, default=FairValueConfig.spread_penalty_weight)
        sp.add_argument("--adj-cap-bps", type=float, default=FairValueConfig.adj_cap_bps)
        sp.add_argument("--ignore-entry-spread", action="store_true", help="Do not block BUY entries when spread_bps exceeds max_spread_bps.")

    def add_calibration_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--calibration-sec", type=float, default=0.0, help="Collect intraday NAV/basket samples for this many seconds before fixing U and C-F.")
        sp.add_argument("--calibration-interval-sec", type=float, default=30.0, help="Seconds between calibration samples.")
        sp.add_argument("--calibration-price-type", choices=["last", "micro"], default="last", help="Component price type used during U/C-F calibration.")

    sp = sub.add_parser("signal-scan", help="Screen candidates and log BUY/HOLD signal snapshots.")
    sp.add_argument("--date", default=_today())
    sp.add_argument("--scan-limit", type=int, default=DEFAULT_SCREEN_SCAN_LIMIT)
    sp.add_argument("--select-limit", type=int, default=DEFAULT_SCREEN_SELECT_LIMIT)
    sp.add_argument("--skip-top", type=int, default=0)
    sp.add_argument("--min-rank", type=int, default=DEFAULT_SCREEN_MIN_RANK)
    sp.add_argument("--max-rank", type=int, default=DEFAULT_SCREEN_MAX_RANK)
    sp.add_argument("--min-holdings", type=int, default=DEFAULT_SCREEN_MIN_HOLDINGS)
    sp.add_argument("--max-holdings", type=int, default=DEFAULT_SCREEN_MAX_HOLDINGS)
    sp.add_argument("--min-trading-value", type=float, default=0.0)
    sp.add_argument("--exclude-name-keyword", action="append", default=[])
    sp.add_argument("--creation-unit", type=float)
    sp.add_argument("--min-edge-bps", type=float, default=DEFAULT_ENTRY_EDGE_BPS)
    sp.add_argument("--max-spread-bps", type=float, default=12.0)
    sp.add_argument("--price-type", choices=["official", "last", "micro"], default="micro")
    sp.add_argument("--micro-levels", type=int, default=1)
    sp.add_argument("--component-sleep-sec", type=float, default=0.25)
    sp.add_argument("--duration-sec", type=float, default=60.0)
    sp.add_argument("--interval-sec", type=float, default=10.0)
    sp.add_argument("--per-code-sleep-sec", type=float, default=0.25)
    sp.add_argument("--once", action="store_true", help="Run one scan cycle and exit.")
    sp.add_argument("--out", type=Path)
    sp.add_argument("--exit-max-hold-sec", type=float, default=600.0, help="Paper-exit after this many seconds.")
    sp.add_argument("--loss-stop-bps", type=float, default=10.0, help="Paper-exit if bid-based PnL falls below -N bps.")
    sp.add_argument("--exit-fv-buffer-bps", type=float, default=0.0, help="Paper-exit when bid is this many bps above fair value.")
    sp.add_argument("--print-all", action="store_true", help="Print WAIT rows too. By default only entries, open positions, and exits are printed.")
    sp.add_argument("--paper-order", action="store_true", help="Send Kiwoom paper BUY orders on ENTER_BUY.")
    sp.add_argument("--qty", type=int, default=1, help="Order quantity used with --paper-order.")
    sp.add_argument("--order-type", default="0", help="Kiwoom trde_tp. 0=limit, 3=market.")
    sp.add_argument("--buy-limit-policy", choices=["improve_bid", "take_ask"], default="improve_bid")
    sp.add_argument("--kiwoom-env", choices=["paper", "real"], default="paper")
    sp.add_argument("--exchange", choices=["KRX", "NXT", "SOR"], default="KRX")
    sp.add_argument("--allow-real-order", action="store_true", help="Allow env=real. By default orders are paper-only.")
    add_calibration_args(sp)
    add_fv_args(sp)
    sp.set_defaults(handler=cmd_signal_scan)

    sp = sub.add_parser("paper-exec-probe", help="Send paper buy/sell orders on ETF FV entry/exit signals and log fills.")
    sp.add_argument("--date", default=_today())
    sp.add_argument("--codes", default="", help="Comma-separated ETF codes. If empty, use the screen filters.")
    sp.add_argument("--force-codes", default="", help="Comma-separated ETF codes to append even if they fail the screen filters.")
    sp.add_argument("--scan-limit", type=int, default=DEFAULT_SCREEN_SCAN_LIMIT)
    sp.add_argument("--select-limit", type=int, default=DEFAULT_SCREEN_SELECT_LIMIT)
    sp.add_argument("--skip-top", type=int, default=0)
    sp.add_argument("--min-rank", type=int, default=DEFAULT_SCREEN_MIN_RANK)
    sp.add_argument("--max-rank", type=int, default=DEFAULT_SCREEN_MAX_RANK)
    sp.add_argument("--min-holdings", type=int, default=DEFAULT_SCREEN_MIN_HOLDINGS)
    sp.add_argument("--max-holdings", type=int, default=DEFAULT_SCREEN_MAX_HOLDINGS)
    sp.add_argument("--min-trading-value", type=float, default=0.0)
    sp.add_argument("--exclude-name-keyword", action="append", default=[])
    sp.add_argument("--creation-unit", type=float)
    sp.add_argument("--min-edge-bps", type=float, default=DEFAULT_ENTRY_EDGE_BPS)
    sp.add_argument("--max-spread-bps", type=float, default=12.0)
    sp.add_argument("--price-type", choices=["official", "last", "micro"], default="official")
    sp.add_argument("--micro-levels", type=int, default=1)
    sp.add_argument("--component-sleep-sec", type=float, default=0.25)
    sp.add_argument("--duration-sec", type=float, default=1200.0)
    sp.add_argument("--interval-sec", type=float, default=10.0)
    sp.add_argument("--per-code-sleep-sec", type=float, default=0.25)
    sp.add_argument("--out", type=Path)
    sp.add_argument("--qty", type=int, default=5)
    sp.add_argument("--max-open-positions", type=int, default=5)
    sp.add_argument("--buy-order-type", default="0", help="Kiwoom trde_tp for buys. 0=limit at ask1.")
    sp.add_argument("--sell-order-type", default="0", help="Kiwoom trde_tp for sells. 0=limit at bid1.")
    sp.add_argument("--fill-wait-sec", type=float, default=5.0)
    sp.add_argument("--fill-poll-sec", type=float, default=1.0)
    sp.add_argument("--exit-max-hold-sec", type=float, default=600.0)
    sp.add_argument("--loss-stop-bps", type=float, default=10.0)
    sp.add_argument("--exit-fv-buffer-bps", type=float, default=0.0)
    sp.add_argument("--paper-order", action="store_true", help="Actually send Kiwoom paper orders. Default is dry-run.")
    sp.add_argument("--kiwoom-env", choices=["paper", "real"], default="paper")
    sp.add_argument("--exchange", choices=["KRX", "NXT", "SOR"], default="KRX")
    sp.add_argument("--allow-real-order", action="store_true")
    add_calibration_args(sp)
    add_fv_args(sp)
    sp.set_defaults(handler=cmd_paper_exec_probe)

    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--code", required=True, help="ETF code, e.g. 069500")
        sp.add_argument("--date", default=_today(), help="PDF trade date YYYYMMDD")
        sp.add_argument("--creation-unit", type=float, help="Override inferred U.")
        sp.add_argument("--min-edge-bps", type=float, default=DEFAULT_ENTRY_EDGE_BPS)
        sp.add_argument("--max-spread-bps", type=float, default=12.0)
        add_fv_args(sp)
        add_calibration_args(sp)
        sp.add_argument(
            "--no-kis-component-fallback",
            action="store_true",
            help="Fail if pykrx/KRX PDF is unavailable instead of using KIS weights.",
        )

    sp = sub.add_parser("probe", help="One-shot PDF/KIS orderbook/fair-value snapshot.")
    add_common(sp)
    sp.set_defaults(handler=cmd_probe)

    sp = sub.add_parser("collect", help="Poll KIS data and save ETF fair-value rows.")
    add_common(sp)
    sp.add_argument("--duration-sec", type=float, default=60.0)
    sp.add_argument("--interval-sec", type=float, default=1.0)
    sp.add_argument("--component-prices", action="store_true", help="Slow REST component NAV calculation.")
    sp.add_argument("--component-price-type", choices=["last", "micro"], default="last")
    sp.add_argument("--micro-levels", type=int, default=1, help="Orderbook levels for component microprice.")
    sp.add_argument("--component-sleep-sec", type=float, default=0.25, help="Delay between KIS component price calls.")
    sp.add_argument("--out", type=Path)
    sp.set_defaults(handler=cmd_collect)

    sp = sub.add_parser("nav-check", help="Compare local PDF component NAV with KIS official NAV.")
    add_common(sp)
    sp.add_argument("--price-type", choices=["last", "micro"], default="last")
    sp.add_argument("--micro-levels", type=int, default=1, help="Orderbook levels for component microprice.")
    sp.add_argument("--sleep-sec", type=float, default=0.05, help="Delay between KIS component price calls.")
    sp.add_argument("--limit-components", type=int, help="Smoke-test only the first N PDF holdings.")
    sp.add_argument("--require-full", action="store_true", help="Exit nonzero if any component price is missing.")
    sp.set_defaults(handler=cmd_nav_check)

    sp = sub.add_parser("trade-once", help="Evaluate once and optionally send a Kiwoom order.")
    add_common(sp)
    sp.add_argument("--qty", type=int, required=True)
    sp.add_argument("--order-type", default="0", help="Kiwoom trde_tp. 0=limit.")
    sp.add_argument("--price-type", choices=["official", "last", "micro"], default="micro")
    sp.add_argument("--micro-levels", type=int, default=1, help="Orderbook levels for component microprice.")
    sp.add_argument("--component-sleep-sec", type=float, default=0.25, help="Delay between KIS component price calls.")
    sp.add_argument("--buy-limit-policy", choices=["improve_bid", "take_ask"], default="improve_bid")
    sp.add_argument("--paper-order", action="store_true", help="Actually send a Kiwoom paper order. Default is dry-run.")
    sp.add_argument("--live-order", action="store_true", help="Backward-compatible alias for sending an order.")
    sp.add_argument("--kiwoom-env", choices=["paper", "real"], default="paper")
    sp.add_argument("--exchange", choices=["KRX", "NXT", "SOR"], default="KRX")
    sp.add_argument("--allow-real-order", action="store_true", help="Allow env=real. By default orders are paper-only.")
    sp.set_defaults(handler=cmd_trade_once)

    return p


def main() -> int:
    args = build_parser().parse_args()
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
