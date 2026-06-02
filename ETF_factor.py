# -*- coding: utf-8 -*-
"""
ETF_factor — KODEX 표준 8 팩터 Rank Momentum 백테스트
========================================================
설계
----
- 대상: 표준 8 팩터 KODEX ETF
    LargeCap / MidSmall / EqualWt / Value / Quality / Momentum / LowVol / HighDiv
- 기간: 최근 3년 (옵션)
- 시그널: 일별 close-to-close 수익률 랭크 (1=top)
         T-1 일을 포함해 직전 10 영업일의 평균 랭크가 가장 작은 팩터 = 매일 1위
- 실행: T일 시가 매수 → T일 종가 매도 (intraday)
        실현 수익률 = close_T / open_T − 1
- 의존: trading 모듈의 SymbolSearch + DataLoader 만 import

타임라인
--------
    랭킹 윈도우      [ T-10 , T-9 , ... , T-1 ]      (10영업일, T-1 포함)
                                              │
    의사결정         T-1 종가 시점에 1위 팩터 결정
                                              │
    매수             T 시가 (open_T)
                                              │
    매도             T 종가 (close_T)
                                              │
    실현 수익률      close_T / open_T − 1

실행
----
    python ETF_factor.py
    python ETF_factor.py --years 5             # 5년치
    python ETF_factor.py --lookback 20         # 20영업일
    python ETF_factor.py --save out/etf_factor_bt.csv
"""

from __future__ import annotations

import sys
from pathlib import Path

# trading 모듈 import 가능하도록 sys.path 보정
_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import argparse
import logging
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from trading.config import ConfigError
from trading.kiwoom.exceptions import KiwoomError
from trading.service.data_loader import DataLoader
from trading.service.symbol_search import SymbolSearch


# ============================================================
#  표준 8 팩터 — KODEX 라인업 우선, 못 찾으면 fallback
# ============================================================

STANDARD_FACTORS: dict[str, list[str]] = {
    "LargeCap":  ["KODEX 200"],
    "MidSmall":  ["KODEX 코스닥150"],
    "EqualWt":   ["KODEX 200동일가중"],
    "Value":     ["KODEX 200가치저변동", "KODEX 가치"],
    "Quality":   ["KODEX MSCI 퀄리티", "KODEX 퀄리티"],
    "Momentum":  ["KODEX MSCI 모멘텀", "KODEX 모멘텀"],
    "LowVol":    ["KODEX 최소변동성", "KODEX 저변동성"],
    "HighDiv":   ["KODEX 고배당"],
}


# ============================================================
#  팩터 → 종목코드 매핑
# ============================================================


def discover_factors(searcher: SymbolSearch) -> dict[str, tuple[str, str]]:
    """패턴 후보를 순서대로 시도하여 첫 매칭 종목 사용."""
    found: dict[str, tuple[str, str]] = {}
    for factor, patterns in STANDARD_FACTORS.items():
        for p in patterns:
            results = searcher.search(p, limit=5)
            if not results:
                continue
            pat_norm = p.replace(" ", "").lower()
            best = None
            for r in results:
                if r.name.replace(" ", "").lower().startswith(pat_norm):
                    best = r
                    break
            best = best or results[0]
            found[factor] = (best.code, best.name)
            break
    return found


# ============================================================
#  OHLC 적재 — 팩터별로 daily_ohlcv 호출
# ============================================================


def load_ohlc(
    loader: DataLoader,
    codes: dict[str, tuple[str, str]],
    start,
    end,
    sleep_between: float = 0.25,
) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for factor, (code, _) in codes.items():
        try:
            df = loader.daily_ohlcv(code, start=start, end=end)
            if not df.empty:
                out[factor] = df[["open", "close"]]
        except Exception as e:
            logging.warning("[%s] %s 적재 실패: %s", factor, code, e)
        time.sleep(sleep_between)
    return out


# ============================================================
#  백테스트 코어
# ============================================================


def backtest(
    ohlc_data: dict[str, pd.DataFrame],
    lookback: int = 5,
) -> tuple[pd.Series, pd.Series, pd.DataFrame]:
    """Rank Momentum + Intraday 실행.

    Returns:
        strat_ret: 일별 전략 수익률 (open→close)
        pick:      일별 선택 팩터
        intraday:  팩터별 일별 intraday return (참고/벤치마크용)
    """
    factors = list(ohlc_data.keys())

    close = pd.DataFrame({f: ohlc_data[f]["close"] for f in factors})
    open_ = pd.DataFrame({f: ohlc_data[f]["open"] for f in factors})
    close.index = pd.to_datetime(close.index)
    open_.index = pd.to_datetime(open_.index)

    # 공통 거래일만 사용
    common = close.dropna(how="any").index.intersection(
        open_.dropna(how="any").index
    )
    close = close.loc[common].sort_index()
    open_ = open_.loc[common].sort_index()

    # 랭킹용: 일별 close-to-close 수익률
    cc_ret = close.pct_change()

    # 실행용: 일별 intraday 수익률 (T 시가 매수 → T 종가 매도)
    intraday = (close - open_) / open_

    # 일별 랭크 (1 = 그 날 최상위)
    daily_rank = cc_ret.rank(axis=1, ascending=False, method="min")

    # rolling(lookback) at index t = mean of t-(lookback-1) to t.
    # shift(1) → at index t = mean of t-lookback to t-1.
    # 즉 T일 결정 시 T-10 ~ T-1 구간 평균 랭크 사용 (T-1 포함, 10영업일).
    avg_rank = daily_rank.rolling(lookback).mean()
    signal = avg_rank.shift(1).dropna(how="all")

    # 평균 랭크가 가장 작은 (= 최상위) 팩터 선택
    pick = signal.idxmin(axis=1).reindex(intraday.index)

    # 선택 팩터의 intraday 수익률 실현 — vectorized
    strat_ret = pd.Series(index=intraday.index, dtype=float)
    for f in pick.dropna().unique():
        if f in intraday.columns:
            mask = pick == f
            strat_ret.loc[mask] = intraday.loc[mask, f]

    return strat_ret.dropna(), pick.dropna(), intraday


# ============================================================
#  성과 통계
# ============================================================


def summarize(strat: pd.Series, benchmark: pd.Series | None = None) -> dict:
    s = strat.dropna()
    if s.empty:
        return {"days": 0}

    cum = float((1 + s).prod() - 1)
    n_years = len(s) / 252
    cagr = (1 + cum) ** (1 / n_years) - 1 if n_years > 0 else 0.0
    win = float((s > 0).mean())
    sharpe = float(s.mean() / s.std() * np.sqrt(252)) if s.std() > 0 else 0.0
    cum_curve = (1 + s).cumprod()
    peak = cum_curve.cummax()
    mdd = float(((cum_curve - peak) / peak).min())

    out = {
        "days": int(len(s)),
        "cum_return": cum,
        "cagr": cagr,
        "win_rate": win,
        "sharpe": sharpe,
        "mdd": mdd,
        "avg_daily": float(s.mean()),
    }
    if benchmark is not None:
        bm = benchmark.reindex(s.index).dropna()
        common = s.index.intersection(bm.index)
        if len(common) > 0:
            out["beat_bm"] = float((s.loc[common] > bm.loc[common]).mean())
    return out


def print_summary(label: str, stats: dict) -> None:
    if stats.get("days", 0) == 0:
        print(f"\n[{label}] (데이터 없음)")
        return
    print(f"\n[{label}]")
    print(f"  거래일       : {stats['days']:>10,} 일")
    print(f"  누적수익률   : {stats['cum_return']*100:>+10.2f} %")
    print(f"  CAGR         : {stats['cagr']*100:>+10.2f} %")
    print(f"  평균 일수익  : {stats['avg_daily']*100:>+10.4f} %")
    print(f"  일별 승률    : {stats['win_rate']*100:>+10.2f} %")
    if "beat_bm" in stats:
        print(f"  벤치마크대비 : {stats['beat_bm']*100:>+10.2f} %  (outperform 일 비율)")
    print(f"  샤프         : {stats['sharpe']:>+11.2f}")
    print(f"  MDD          : {stats['mdd']*100:>+10.2f} %")


# ============================================================
#  메인
# ============================================================


def main() -> int:
    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")

    p = argparse.ArgumentParser(
        prog="ETF_factor",
        description="KODEX 표준 8 팩터 Rank Momentum 백테스트 (Intraday 실행)",
    )
    p.add_argument("--lookback", type=int, default=10,
                   help="시그널 영업일 (기본 10, T-1 포함)")
    p.add_argument("--years", type=int, default=3,
                   help="가격 적재 연수 (기본 3)")
    p.add_argument("--save", type=Path, help="일별 결과 CSV 저장")
    args = p.parse_args()

    # ---- 1. 팩터 매핑 ----
    try:
        searcher = SymbolSearch()
        mapping = discover_factors(searcher)
    except ConfigError as e:
        print(f"[설정 오류] {e}", file=sys.stderr)
        return 2

    print("\n[팩터 매핑 — 표준 8개]")
    for factor in STANDARD_FACTORS:
        if factor in mapping:
            code, name = mapping[factor]
            print(f"  {factor:<10} {code}  {name}")
        else:
            print(f"  {factor:<10} (MISSING — KODEX 라인업에 없거나 패턴 불일치)")

    if len(mapping) < 4:
        print("\n팩터 매핑 부족 (4개 미만). 패턴 보강 후 재시도.", file=sys.stderr)
        return 4

    # ---- 2. OHLC 적재 ----
    end_d = datetime.now().date()
    start_d = end_d - timedelta(days=args.years * 365 + args.lookback * 2 + 30)

    print(f"\nOHLC 적재 중 ({start_d} ~ {end_d}) ...")
    try:
        loader = DataLoader()
        ohlc_data = load_ohlc(loader, mapping, start_d, end_d)
    except KiwoomError as e:
        print(f"[가격 적재 실패] {e}", file=sys.stderr)
        return 3

    if not ohlc_data:
        print("(가격 데이터 없음)", file=sys.stderr)
        return 5
    if len(ohlc_data) < len(mapping):
        missing = set(mapping) - set(ohlc_data)
        print(f"⚠ OHLC 적재 실패 팩터: {missing}")
    print(f"  → 적재 완료: {len(ohlc_data)} 팩터")

    # ---- 3. 백테스트 ----
    strat_ret, pick, intraday = backtest(ohlc_data, lookback=args.lookback)

    if strat_ret.empty:
        print("\n전략 수익률이 비어있음. lookback 또는 years 조정 필요.", file=sys.stderr)
        return 6

    benchmark = intraday.mean(axis=1).reindex(strat_ret.index)

    # ---- 4. 결과 출력 ----
    print(f"\n{'=' * 64}")
    print(f"  Rank Momentum (Intraday 실행) — Lookback {args.lookback} 영업일")
    print(f"  실행: T일 시가 매수 → T일 종가 매도")
    print(f"  기간: {strat_ret.index[0].date()} ~ {strat_ret.index[-1].date()}")
    print(f"{'=' * 64}")

    print_summary("Benchmark — 8팩터 등가중 (intraday)", summarize(benchmark))

    if "LargeCap" in intraday.columns:
        lc = intraday["LargeCap"].reindex(strat_ret.index).dropna()
        print_summary("Benchmark — LargeCap 단독 (intraday)",
                      summarize(lc, benchmark))

    print_summary("Rank Momentum (선정 1위)", summarize(strat_ret, benchmark))

    # 픽 분포
    print("\n[Rank Momentum — 픽 분포]")
    dist = pick.value_counts(normalize=True).sort_values(ascending=False)
    for f, pct in dist.items():
        bar = "█" * int(pct * 50)
        print(f"  {f:<10} {pct*100:>5.1f} %  {bar}")

    # ---- 5. CSV 저장 ----
    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        result = pd.DataFrame({
            "pick": pick.reindex(strat_ret.index),
            "strat_ret": strat_ret,
            "benchmark_ew": benchmark,
        })
        result.to_csv(args.save, encoding="utf-8-sig")
        print(f"\n저장: {args.save}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
# python ETF_factor.py
