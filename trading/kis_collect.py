# -*- coding: utf-8 -*-
"""File-based KIS Open API collector."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

from trading.kis import KisClient, KisError
from trading.kis import futures


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
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


def _flatten_response(data: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    meta = {
        key: value
        for key, value in data.items()
        if key not in {"output", "output1", "output2", "output3"}
    }
    for section in ("output", "output1", "output2", "output3"):
        value = data.get(section)
        if isinstance(value, list):
            for idx, item in enumerate(value):
                row = {"section": section, "row_index": idx, **meta}
                if isinstance(item, dict):
                    row.update(item)
                else:
                    row["value"] = item
                rows.append(row)
        elif isinstance(value, dict):
            rows.append({"section": section, "row_index": 0, **meta, **value})
    if not rows:
        rows.append(meta or data)
    return rows


def cmd_token(args: argparse.Namespace) -> int:
    client = KisClient()
    token = client.issue_token(force=args.force)
    print(f"token_ok=True env={client.config.env} token_prefix={token[:8]}...")
    return 0


def cmd_master(args: argparse.Namespace) -> int:
    rows = [row.as_dict() for row in futures.download_index_futures_master()]
    if args.out:
        _write_csv(args.out, rows)
        print(f"saved {args.out} ({len(rows)} rows)")
    else:
        print(json.dumps(rows[: args.limit], ensure_ascii=False, indent=2))
        print(f"total={len(rows)}")
    return 0


def _resolve_code(args: argparse.Namespace) -> str:
    if args.code:
        return args.code
    rows = futures.download_index_futures_master()
    front = futures.pick_front_kospi200_future(rows)
    print(f"using_front_code={front.short_code} name={front.korean_name}", file=sys.stderr)
    return front.short_code


def cmd_price(args: argparse.Namespace) -> int:
    client = KisClient()
    code = _resolve_code(args)
    data = futures.inquire_price(client, code, market_code=args.market)
    rows = _flatten_response(data)
    for row in rows:
        row.setdefault("code", code)
        row.setdefault("market_code", args.market)
    if args.out:
        _write_csv(args.out, rows)
        print(f"saved {args.out} ({len(rows)} rows)")
    else:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


def cmd_asking_price(args: argparse.Namespace) -> int:
    client = KisClient()
    code = _resolve_code(args)
    data = futures.inquire_asking_price(client, code, market_code=args.market)
    rows = _flatten_response(data)
    for row in rows:
        row.setdefault("code", code)
        row.setdefault("market_code", args.market)
    if args.out:
        _write_csv(args.out, rows)
        print(f"saved {args.out} ({len(rows)} rows)")
    else:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kis_collect", description="KIS Open API collector")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("token", help="issue/check access token")
    sp.add_argument("--force", action="store_true", help="ignore cached token")
    sp.set_defaults(handler=cmd_token)

    sp = sub.add_parser("index-futures-master", help="download index futures/options master")
    sp.add_argument("--out", type=Path, help="CSV output path")
    sp.add_argument("--limit", type=int, default=10, help="print limit when --out is omitted")
    sp.set_defaults(handler=cmd_master)

    sp = sub.add_parser("futures-price", help="get futures/options price snapshot")
    sp.add_argument("code", nargs="?", help="KIS futures/options short code")
    sp.add_argument("--market", default="F", help="FID_COND_MRKT_DIV_CODE, F=index futures")
    sp.add_argument("--out", type=Path, help="CSV output path")
    sp.set_defaults(handler=cmd_price)

    sp = sub.add_parser("futures-asking-price", help="get futures/options orderbook snapshot")
    sp.add_argument("code", nargs="?", help="KIS futures/options short code")
    sp.add_argument("--market", default="F", help="FID_COND_MRKT_DIV_CODE, F=index futures")
    sp.add_argument("--out", type=Path, help="CSV output path")
    sp.set_defaults(handler=cmd_asking_price)

    return p


def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.handler(args))
    except KisError as exc:
        print(f"[kis] {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

