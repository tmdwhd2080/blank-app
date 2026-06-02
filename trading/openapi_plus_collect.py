# -*- coding: utf-8 -*-
"""File-based Kiwoom OpenAPI+ data collector.

Examples:
    python -m trading.openapi_plus_collect doctor
    python -m trading.openapi_plus_collect doctor --control
    python -m trading.openapi_plus_collect future-codes --out out/openapi_future_codes.csv
    python -m trading.openapi_plus_collect futures-realtime --front --seconds 60 --out out/futures.csv
    python -m trading.openapi_plus_collect futures-realtime 101T6000 --seconds 60 --out out/futures.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from trading.kiwoom.openapi_plus import (
    OpenApiPlusClient,
    OpenApiPlusRuntimeError,
    diagnose_environment,
)


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return

    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)

    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def cmd_doctor(args: argparse.Namespace) -> int:
    rows = diagnose_environment(check_control=args.control)
    width = max(len(name) for name, _, _ in rows)
    ok = True
    for name, passed, detail in rows:
        ok = ok and passed
        status = "OK" if passed else "FAIL"
        print(f"{name:<{width}}  {status:<4}  {detail}")
    return 0 if ok else 2


def cmd_future_codes(args: argparse.Namespace) -> int:
    client = OpenApiPlusClient(screen_no=args.screen)
    client.login(timeout_sec=args.timeout)
    rows = client.get_future_codes()
    if args.out:
        _write_csv(args.out, rows)
        print(f"saved {args.out} ({len(rows)} rows)")
    else:
        for row in rows:
            print(row)
    return 0


def _resolve_codes(client: OpenApiPlusClient, args: argparse.Namespace) -> list[str]:
    if args.front:
        rows = client.get_future_codes()
        if not rows:
            raise OpenApiPlusRuntimeError("GetFutureList returned no futures codes.")
        return [rows[0]["code"]]
    return list(args.codes)


def cmd_futures_realtime(args: argparse.Namespace) -> int:
    client = OpenApiPlusClient(screen_no=args.screen)
    client.login(timeout_sec=args.timeout)
    codes = _resolve_codes(client, args)
    rows = client.collect_futures_realtime(
        codes,
        seconds=args.seconds,
        fid_kind=args.fields,
    )
    if args.out:
        _write_csv(args.out, rows)
        print(f"saved {args.out} ({len(rows)} rows)")
    else:
        for row in rows:
            print(row)
        print(f"total {len(rows)} rows")
    return 0


def _parse_inputs(items: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise OpenApiPlusRuntimeError(f"--input must be NAME=VALUE, got: {item}")
        key, value = item.split("=", 1)
        parsed[key] = value
    return parsed


def cmd_tr(args: argparse.Namespace) -> int:
    client = OpenApiPlusClient(screen_no=args.screen)
    client.login(timeout_sec=args.timeout)
    rows = client.request_tr(
        rq_name=args.rq,
        tr_code=args.tr,
        inputs=_parse_inputs(args.input),
        fields=args.field,
        screen_no=args.screen,
        timeout_sec=args.tr_timeout,
    )
    if args.out:
        _write_csv(args.out, rows)
        print(f"saved {args.out} ({len(rows)} rows)")
    else:
        for row in rows:
            print(row)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="openapi_plus_collect",
        description="Kiwoom OpenAPI+ file collector",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("doctor", help="check local OpenAPI+ runtime")
    sp.add_argument("--control", action="store_true", help="also try to instantiate the OCX")
    sp.set_defaults(handler=cmd_doctor)

    sp = sub.add_parser("future-codes", help="export index futures code list")
    sp.add_argument("--out", type=Path, help="CSV output path")
    sp.add_argument("--screen", default="9100", help="OpenAPI+ screen number")
    sp.add_argument("--timeout", type=int, default=120, help="login timeout seconds")
    sp.set_defaults(handler=cmd_future_codes)

    sp = sub.add_parser("futures-realtime", help="capture realtime futures quote/orderbook rows")
    sp.add_argument("codes", nargs="*", help="OpenAPI+ futures codes")
    sp.add_argument("--front", action="store_true", help="use the first code from GetFutureList")
    sp.add_argument("--seconds", type=int, default=60, help="collection duration")
    sp.add_argument(
        "--fields",
        choices=["quote", "orderbook", "theory", "all"],
        default="all",
        help="FID set to collect",
    )
    sp.add_argument("--out", type=Path, help="CSV output path")
    sp.add_argument("--screen", default="9100", help="OpenAPI+ screen number")
    sp.add_argument("--timeout", type=int, default=120, help="login timeout seconds")
    sp.set_defaults(handler=cmd_futures_realtime)

    sp = sub.add_parser("tr", help="request a generic OpenAPI+ TR and export selected fields")
    sp.add_argument("--tr", required=True, help="TR code, e.g. opt50001")
    sp.add_argument("--rq", default="openapi_plus_tr", help="request name")
    sp.add_argument(
        "--input",
        action="append",
        default=[],
        help="TR input as NAME=VALUE. Repeat for multiple inputs.",
    )
    sp.add_argument(
        "--field",
        action="append",
        required=True,
        help="output field name as shown in KOA Studio. Repeat for multiple fields.",
    )
    sp.add_argument("--out", type=Path, help="CSV output path")
    sp.add_argument("--screen", default="9100", help="OpenAPI+ screen number")
    sp.add_argument("--timeout", type=int, default=120, help="login timeout seconds")
    sp.add_argument("--tr-timeout", type=int, default=30, help="TR response timeout seconds")
    sp.set_defaults(handler=cmd_tr)

    return p


def main() -> int:
    args = build_parser().parse_args()
    if args.cmd == "futures-realtime" and not args.front and not args.codes:
        print("futures-realtime requires codes or --front", file=sys.stderr)
        return 2
    try:
        return int(args.handler(args))
    except OpenApiPlusRuntimeError as exc:
        print(f"[openapi+ runtime] {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
