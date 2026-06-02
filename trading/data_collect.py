# -*- coding: utf-8 -*-
"""
데이터 수집
=============
키움 REST 로 일봉/분봉/호가를 받아 DataFrame 또는 CSV 로 저장.

기본:
    # 단일 종목 일봉
    python -m trading.data_collect daily 005930
    python -m trading.data_collect daily 005930 --start 20240101 --end 20260428

    # 여러 종목 종가만 (피벗 매트릭스)
    python -m trading.data_collect close 005930 000660 035720 --start 20260101

    # 분봉 (1/3/5/10/15/30/45/60 분)
    python -m trading.data_collect minute 005930 --tic 5

    # CSV 로 저장
    python -m trading.data_collect daily 005930 --save out/005930.csv

옵션:
    --no-adjusted    수정주가 미적용 (기본은 적용)
    --pages N        연속조회 최대 페이지 수 (기본 10)
"""

from __future__ import annotations

import sys
from pathlib import Path

# 스크립트 / 모듈 모드 모두 동작하도록 sys.path 보정
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import argparse
import logging

import pandas as pd

from trading.config import ConfigError
from trading.kiwoom.exceptions import KiwoomError
from trading.service.data_loader import DataLoader


# ============================================================
#  명령별 핸들러
# ============================================================


def cmd_daily(args: argparse.Namespace, loader: DataLoader) -> pd.DataFrame:
    if len(args.codes) != 1:
        raise SystemExit("daily 명령은 종목코드 1개만 받습니다 (여러 개는 close 사용)")
    df = loader.daily_ohlcv(
        args.codes[0],
        start=args.start,
        end=args.end,
        adjusted=not args.no_adjusted,
    )
    return df


def cmd_close(args: argparse.Namespace, loader: DataLoader) -> pd.DataFrame:
    return loader.daily_close(
        args.codes,
        start=args.start,
        end=args.end,
        adjusted=not args.no_adjusted,
    )


def cmd_minute(args: argparse.Namespace, loader: DataLoader) -> pd.DataFrame:
    if len(args.codes) != 1:
        raise SystemExit("minute 명령은 종목코드 1개만 받습니다")
    from trading.kiwoom.tr import market_data as md

    rows = md.minute_chart(
        loader._client,  # noqa: SLF001 — 단일 종목 raw 접근 의도적
        args.codes[0],
        tic_scope=str(args.tic),
        adjusted=not args.no_adjusted,
        max_pages=args.pages,
    )
    return pd.DataFrame(rows)


# ============================================================
#  CLI
# ============================================================


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="data_collect",
        description="키움 REST 데이터 수집",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # 공통 옵션 추가용 헬퍼
    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("codes", nargs="+", help="종목코드 6자리 (예: 005930)")
        sp.add_argument("--start", help="시작일 YYYYMMDD")
        sp.add_argument("--end", help="종료일 YYYYMMDD (기본: 오늘)")
        sp.add_argument("--no-adjusted", action="store_true", help="수정주가 미적용")
        sp.add_argument("--pages", type=int, default=10, help="연속조회 최대 페이지")
        sp.add_argument("--save", type=Path, help="CSV 저장 경로")

    sp_d = sub.add_parser("daily", help="단일 종목 일봉 OHLCV")
    add_common(sp_d)

    sp_c = sub.add_parser("close", help="여러 종목 종가 매트릭스")
    add_common(sp_c)

    sp_m = sub.add_parser("minute", help="단일 종목 분봉")
    add_common(sp_m)
    sp_m.add_argument("--tic", type=int, default=1,
                      choices=[1, 3, 5, 10, 15, 30, 45, 60],
                      help="분봉 단위 (기본 1분)")

    return p


HANDLERS = {
    "daily": cmd_daily,
    "close": cmd_close,
    "minute": cmd_minute,
}


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args()

    try:
        loader = DataLoader()
    except ConfigError as e:
        print(f"[설정 오류] {e}", file=sys.stderr)
        return 2

    try:
        df: pd.DataFrame = HANDLERS[args.cmd](args, loader)
    except KiwoomError as e:
        print(f"[수집 실패] {e}", file=sys.stderr)
        return 3

    if df.empty:
        print("(데이터 없음)")
        return 0

    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(args.save, encoding="utf-8-sig")
        print(f"저장: {args.save} ({len(df)} rows)")
    else:
        print(df)
        print(f"\n총 {len(df)} rows")

    return 0


if __name__ == "__main__":
    sys.exit(main())
