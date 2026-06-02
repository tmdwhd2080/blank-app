# -*- coding: utf-8 -*-
"""
종목 검색
==========
키워드 또는 종목코드로 종목/ETF 를 찾는다.
처음 실행 시 ~/.kiwoom/symbols.csv 캐시 생성 (7일 자동 갱신).

기본:
    python trading/symbol_search.py AI
    python trading/symbol_search.py 반도체 --market etf
    python trading/symbol_search.py 005930        # 종목코드 직접 입력 OK

옵션:
    --all                  매칭 전부 출력 (limit 무시)
    --limit N              표시 개수 (기본 50)
    --market kospi|kosdaq|etf
    --refresh              캐시 강제 재다운로드
    --stats                캐시 통계 (시장별 종목 수)
    --force-add CODE       누락된 종목코드를 ka10001 로 받아 캐시에 추가
    --code-only            첫 매칭 코드만 출력 (스크립트 파이핑용)
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import argparse
import logging

from trading.config import ConfigError
from trading.kiwoom.exceptions import KiwoomError
from trading.service.symbol_search import SymbolSearch


def _print_stats(searcher: SymbolSearch) -> None:
    s = searcher.stats()
    print("\n[캐시 통계]")
    for k in sorted(s.keys()):
        if k == "TOTAL":
            continue
        print(f"  {k:<8} {s[k]:>6,} 종목")
    print(f"  {'─' * 8} {'─' * 6}")
    print(f"  {'TOTAL':<8} {s.get('TOTAL', 0):>6,} 종목\n")


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    p = argparse.ArgumentParser(prog="symbol_search",
                                description="종목/ETF 키워드·코드 검색")
    p.add_argument("keyword", nargs="?", default="",
                   help="검색어 (한글/영문/공백 무시) 또는 종목코드 6자리")
    p.add_argument("--market", choices=["kospi", "kosdaq", "etf"],
                   help="시장 필터")
    p.add_argument("--limit", type=int, default=50,
                   help="표시 개수 (기본 50)")
    p.add_argument("--all", action="store_true",
                   help="전체 매칭 표시 (limit 무시)")
    p.add_argument("--refresh", action="store_true",
                   help="캐시 강제 재다운로드")
    p.add_argument("--stats", action="store_true",
                   help="캐시 통계 출력")
    p.add_argument("--force-add", metavar="CODE",
                   help="누락된 종목코드를 캐시에 강제 추가")
    p.add_argument("--code-only", action="store_true",
                   help="첫 매칭의 종목코드만 출력")
    args = p.parse_args()

    try:
        searcher = SymbolSearch()

        if args.refresh:
            n = searcher.refresh()
            print(f"✓ 캐시 갱신 완료 ({n} 종목)")
            _print_stats(searcher)

        if args.force_add:
            sym = searcher.force_add(args.force_add)
            if sym:
                print(f"✓ 추가됨: {sym.code}  {sym.name}  ({sym.market})")
            else:
                print(f"✗ 추가 실패: {args.force_add} (응답에 종목명 없음)",
                      file=sys.stderr)
                return 4

        if args.stats and not args.refresh:
            _print_stats(searcher)

        if not args.keyword:
            if not (args.refresh or args.stats or args.force_add):
                p.print_help()
                return 1
            return 0

        limit = None if args.all else args.limit
        results = searcher.search(args.keyword, market=args.market, limit=limit)
        total = searcher.count(args.keyword, market=args.market)

    except ConfigError as e:
        print(f"[설정 오류] {e}", file=sys.stderr)
        return 2
    except KiwoomError as e:
        print(f"[API 오류] {e}", file=sys.stderr)
        return 3

    if not results:
        print(f"(매칭 없음: '{args.keyword}')")
        print("  → 캐시가 오래되었으면 --refresh, 특정 코드를 못 찾으면 --force-add CODE")
        return 0

    if args.code_only:
        print(results[0].code)
        return 0

    print(f"\n  {'코드':<8}  {'종목명':<40}  시장")
    print(f"  {'─' * 8}  {'─' * 40}  ─────")
    for s in results:
        nm = s.name if len(s.name) <= 40 else s.name[:39] + "…"
        print(f"  {s.code:<8}  {nm:<40}  {s.market}")

    shown = len(results)
    if shown < total:
        print(f"\n  총 {total} 건 매칭, {shown} 건 표시  "
              f"(전체 보려면 --all 또는 --limit {total})")
    else:
        print(f"\n  {shown} 건")
    return 0


if __name__ == "__main__":
    sys.exit(main())
