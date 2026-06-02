# -*- coding: utf-8 -*-
"""
stock_return_range_analysis.py

테마 구성종목의 당일 수익률 범위별 익일 성과 분석

흐름
----
1. Theme.py의 backtest_ga()를 실행하여 portfolio_log를 얻는다
2. 각 리밸런싱 시점마다:
   a. 선택된 테마들의 구성종목 목록을 DB에서 가져옴
   b. pykrx로 각 종목의 당일(리밸런싱일) 종가와 전일 종가, 익일 종가를 크롤링
   c. 당일 수익률 = (당일종가 - 전일종가) / 전일종가
   d. 익일 수익률 = (익일종가 - 당일종가) / 당일종가
3. 당일 수익률을 범위별로 분류하여 익일 수익률 통계 산출
"""

import sys
import os
import time
import warnings
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

warnings.filterwarnings('ignore')

sys.path.append(os.path.join(r'C:\Users\intern9', 'truston_quant_dev'))

from pykrx import stock as krx
from util.database2 import MSSQL, DBConfig

from Theme_model.BETA import (
    load_kospi_returns,
    get_theme_returns,
    compute_market_adjusted_returns,
)
from Theme_model.theme_filter import (
    get_theme_info,
    get_theme_stocks,
)
from Theme_model.theme_filter2 import (
    build_constituent_sets,
    remove_overlapping_themes,
)
from Theme_model.settings import (
    KOSPI_EXCEL_PATH,
    START_DATE,
    END_DATE,
    ROLLING_WINDOW,
    J, K, TOP_N, REBAL_COST_GA, GAP,
    USE_FILTER, OVERLAP_THRESHOLD,
    GA_LOOKBACK,
    PRE_FILTER_ENABLED, PRE_FILTER_N, PRE_FILTER_K,
    CRAWL_DELAY,
    get_filter_config,
)
from Theme_model.Theme import backtest_ga
from Theme_model.theme_stock_return_analysis import (
    map_id_to_name_df,
    filter_stocks_df,
)


# ═══════════════════════════════════════════════════════════════
# 가격 크롤링 (캐시 포함)
# ═══════════════════════════════════════════════════════════════

# 전역 캐시: {STK_CODE: pd.Series(index=YYYYMMDD, values=종가)}
_price_cache: dict = {}


def crawl_prices_cached(stk_codes: list,
                        start_date: str,
                        end_date: str,
                        delay: float = 0.05) -> None:
    """
    종목 코드 리스트에 대해 전체 기간의 종가를 크롤링하여 _price_cache에 저장.
    이미 캐시된 종목은 스킵한다.
    """
    global _price_cache
    new_codes = [c for c in stk_codes if c not in _price_cache]
    if not new_codes:
        return

    total = len(new_codes)
    print(f"    신규 크롤링: {total}개 종목 ({start_date} ~ {end_date})")

    for i, code in enumerate(new_codes, 1):
        if i % 50 == 0 or i == total:
            print(f"      진행: {i}/{total}")
        try:
            df = krx.get_market_ohlcv_by_date(start_date, end_date, code)
            if df is not None and not df.empty and '종가' in df.columns:
                df.index = pd.to_datetime(df.index).strftime('%Y%m%d')
                _price_cache[code] = df['종가'].astype(float)
        except Exception:
            pass  # 실패 시 skip
        time.sleep(delay)


def get_cached_close(code: str, date: str) -> float:
    """캐시에서 특정 종목/날짜의 종가를 반환. 없으면 NaN."""
    if code not in _price_cache:
        return np.nan
    series = _price_cache[code]
    if date in series.index:
        return series[date]
    return np.nan


def find_prev_trading_date(code: str, date: str) -> str:
    """캐시에서 date 직전 거래일을 찾는다."""
    if code not in _price_cache:
        return None
    dates = sorted(_price_cache[code].index.tolist())
    cands = [d for d in dates if d < date]
    return cands[-1] if cands else None


def find_next_trading_date(code: str, date: str) -> str:
    """캐시에서 date 직후 거래일을 찾는다."""
    if code not in _price_cache:
        return None
    dates = sorted(_price_cache[code].index.tolist())
    cands = [d for d in dates if d > date]
    return cands[0] if cands else None


# ═══════════════════════════════════════════════════════════════
# 당일/익일 수익률 계산
# ═══════════════════════════════════════════════════════════════

def compute_daily_next_returns(stk_codes: list, rebal_date: str) -> pd.DataFrame:
    """
    각 종목에 대해:
    - 당일 수익률 = (당일종가 - 전일종가) / 전일종가
    - 익일 수익률 = (익일종가 - 당일종가) / 당일종가

    Returns: DataFrame [STK_CODE, day_return, next_day_return]
    """
    rows = []
    for code in stk_codes:
        close_today = get_cached_close(code, rebal_date)
        if np.isnan(close_today) or close_today <= 0:
            continue

        prev_date = find_prev_trading_date(code, rebal_date)
        next_date = find_next_trading_date(code, rebal_date)

        if prev_date is None or next_date is None:
            continue

        close_prev = get_cached_close(code, prev_date)
        close_next = get_cached_close(code, next_date)

        if np.isnan(close_prev) or close_prev <= 0:
            continue
        if np.isnan(close_next) or close_next <= 0:
            continue

        day_return = (close_today - close_prev) / close_prev
        next_day_return = (close_next - close_today) / close_today

        rows.append({
            'STK_CODE': code,
            'rebal_date': rebal_date,
            'day_return': day_return,
            'next_day_return': next_day_return,
        })

    return pd.DataFrame(rows)


# ═══════════════════════════════════════════════════════════════
# 범위별 분류 및 통계
# ═══════════════════════════════════════════════════════════════

RETURN_BINS = [-np.inf, -0.03, -0.01, 0.0, 0.01, 0.03, 0.05, np.inf]
RETURN_LABELS = [
    '  ~ -3%',
    '-3% ~ -1%',
    '-1% ~  0%',
    ' 0% ~  1%',
    ' 1% ~  3%',
    ' 3% ~  5%',
    ' 5% ~   ',
]


def classify_and_summarize(all_returns_df: pd.DataFrame) -> pd.DataFrame:
    """
    당일 수익률을 범위별로 분류하고 익일 수익률 통계를 산출한다.

    Returns: DataFrame with columns
        [range, count, mean_next_return, median_next_return, win_rate]
    """
    if all_returns_df.empty:
        return pd.DataFrame()

    df = all_returns_df.copy()
    df['range'] = pd.cut(
        df['day_return'],
        bins=RETURN_BINS,
        labels=RETURN_LABELS,
        right=False,
    )

    rows = []
    for label in RETURN_LABELS:
        sub = df[df['range'] == label]
        if sub.empty:
            rows.append({
                'range': label,
                'count': 0,
                'mean_next_return': np.nan,
                'median_next_return': np.nan,
                'win_rate': np.nan,
            })
            continue

        rows.append({
            'range': label,
            'count': len(sub),
            'mean_next_return': sub['next_day_return'].mean(),
            'median_next_return': sub['next_day_return'].median(),
            'win_rate': (sub['next_day_return'] > 0).mean(),
        })

    return pd.DataFrame(rows)


# ═══════════════════════════════════════════════════════════════
# 메인 흐름
# ═══════════════════════════════════════════════════════════════

def main():
    print("=" * 65)
    print("  테마 구성종목 당일 수익률 범위별 익일 성과 분석")
    print(f"  기간: {START_DATE} ~ {END_DATE}")
    print(f"  J={J} / K={K} / TOP_N={TOP_N}")
    print("=" * 65)

    # ── Step 1: 데이터 로드 & 백테스트 ──
    print("\n[Step 1] 데이터 로드 및 백테스트 실행")

    kospi_df = load_kospi_returns(KOSPI_EXCEL_PATH, START_DATE, END_DATE)

    db = MSSQL(database=DBConfig.TRSTDEV_DB)
    try:
        theme_df = get_theme_returns(db, START_DATE, END_DATE)
        theme_info_df = get_theme_info(db)
        theme_stocks_df = get_theme_stocks(db)
    finally:
        db.close()

    result_df = compute_market_adjusted_returns(theme_df, kospi_df, START_DATE, ROLLING_WINDOW)

    adj_pivot = result_df.pivot_table(
        index='PfmDate', columns='THEME_ID', values='AdjRtn', aggfunc='first'
    ).sort_index()

    theme_pivot = result_df.pivot_table(
        index='PfmDate', columns='THEME_ID', values='ThemeRtn', aggfunc='first'
    ).sort_index()

    kospi_pivot = (result_df.drop_duplicates('PfmDate')
                   .set_index('PfmDate')['KOSPI_Rtn'].sort_index())

    filter_config = get_filter_config()

    print("\n  백테스트 실행 중...")
    bt_df, filter_log, portfolio_log = backtest_ga(
        adj_pivot, theme_pivot, kospi_pivot,
        J=J, K=K, top_n=TOP_N, rebal_cost=REBAL_COST_GA, gap=GAP,
        use_filter=USE_FILTER,
        theme_info_df=theme_info_df,
        theme_stocks_df=theme_stocks_df,
        filter_config=filter_config,
        overlap_threshold=OVERLAP_THRESHOLD,
        ga_lookback=GA_LOOKBACK,
        pre_filter_enabled=PRE_FILTER_ENABLED,
        pre_filter_n=PRE_FILTER_N,
        pre_filter_k=PRE_FILTER_K,
    )

    if not portfolio_log:
        print("  portfolio_log가 비어있습니다. 종료합니다.")
        return

    print(f"  백테스트 완료: {len(portfolio_log)}개 리밸런싱 시점")

    # ── Step 2: 크롤링 버퍼 날짜 계산 ──
    # START_DATE 이전 여유를 두어 전일 종가를 확보
    buffer_start = (datetime.strptime(START_DATE, '%Y%m%d') - timedelta(days=30)).strftime('%Y%m%d')
    # END_DATE 이후 여유를 두어 익일 종가를 확보
    buffer_end = (datetime.strptime(END_DATE, '%Y%m%d') + timedelta(days=15)).strftime('%Y%m%d')

    # ── Step 3: 각 리밸런싱 시점별 처리 ──
    print(f"\n[Step 2] 각 리밸런싱 시점 구성종목 수익률 계산")

    all_returns_list = []
    total_rebal = len(portfolio_log)

    for idx, entry in enumerate(portfolio_log, 1):
        rebal_date = entry['date']
        weight_dict = entry['weights']
        theme_ids = list(weight_dict.keys())

        if idx % 50 == 0 or idx == 1 or idx == total_rebal:
            print(f"\n  [{idx}/{total_rebal}] 리밸런싱일: {rebal_date}, 테마 {len(theme_ids)}개")

        # 테마 ID -> 테마명 매핑
        name_map = map_id_to_name_df(
            theme_info_df, theme_ids, rebal_date, _verbose=False
        )
        if not name_map:
            continue

        theme_names = list(name_map.values())

        # 구성종목 가져오기
        stocks_df = filter_stocks_df(
            theme_stocks_df, theme_names, rebal_date, _verbose=False
        )
        if stocks_df.empty:
            continue

        stk_codes = stocks_df['STK_CODE'].unique().tolist()

        # 크롤링 (캐시 활용)
        crawl_prices_cached(stk_codes, buffer_start, buffer_end, delay=CRAWL_DELAY)

        # 당일/익일 수익률 계산
        returns_df = compute_daily_next_returns(stk_codes, rebal_date)
        if not returns_df.empty:
            all_returns_list.append(returns_df)

    if not all_returns_list:
        print("\n  수익률 데이터가 없습니다. 종료합니다.")
        return

    all_returns_df = pd.concat(all_returns_list, ignore_index=True)
    print(f"\n  전체 수집: {len(all_returns_df)}개 종목-날짜 조합")

    # ── Step 4: 범위별 통계 산출 ──
    print(f"\n[Step 3] 당일 수익률 범위별 익일 성과 통계")
    summary = classify_and_summarize(all_returns_df)

    if summary.empty:
        print("  통계 산출 실패")
        return

    # ── 결과 출력 ──
    print(f"\n{'='*70}")
    print(f"  당일 수익률 범위별 익일 수익률 통계")
    print(f"  (전체 {len(all_returns_df)}개 관측치, {len(portfolio_log)}회 리밸런싱)")
    print(f"{'='*70}")
    print(f"  {'당일 수익률 범위':>16}  {'표본수':>6}  {'평균 익일수익률':>14}  {'중앙값':>10}  {'승률':>8}")
    print(f"  {'-'*16}  {'-'*6}  {'-'*14}  {'-'*10}  {'-'*8}")

    for _, row in summary.iterrows():
        rng = row['range']
        cnt = int(row['count'])
        if cnt == 0:
            print(f"  {rng:>16}  {cnt:>6}  {'N/A':>14}  {'N/A':>10}  {'N/A':>8}")
        else:
            mean_r = row['mean_next_return'] * 100
            med_r = row['median_next_return'] * 100
            wr = row['win_rate'] * 100
            sign_m = '+' if mean_r > 0 else ''
            sign_d = '+' if med_r > 0 else ''
            print(f"  {rng:>16}  {cnt:>6}  {sign_m}{mean_r:>12.4f}%  {sign_d}{med_r:>8.4f}%  {wr:>7.1f}%")

    print(f"  {'='*62}")

    # 전체 평균
    total_mean = all_returns_df['next_day_return'].mean() * 100
    total_med = all_returns_df['next_day_return'].median() * 100
    total_wr = (all_returns_df['next_day_return'] > 0).mean() * 100
    sign_t = '+' if total_mean > 0 else ''
    sign_td = '+' if total_med > 0 else ''
    print(f"  {'전체':>16}  {len(all_returns_df):>6}  {sign_t}{total_mean:>12.4f}%  {sign_td}{total_med:>8.4f}%  {total_wr:>7.1f}%")

    # ── CSV 저장 ──
    output_dir = os.path.dirname(os.path.abspath(__file__))
    csv_path = os.path.join(output_dir, 'stock_return_range_result.csv')

    summary_out = summary.copy()
    summary_out['mean_next_return_pct'] = summary_out['mean_next_return'] * 100
    summary_out['median_next_return_pct'] = summary_out['median_next_return'] * 100
    summary_out['win_rate_pct'] = summary_out['win_rate'] * 100
    summary_out.to_csv(csv_path, index=False, encoding='utf-8-sig')
    print(f"\n  결과 저장: {csv_path}")

    # ── 추가: 당일 수익률 분포 ──
    print(f"\n{'='*70}")
    print(f"  참고: 당일 수익률 분포")
    print(f"{'='*70}")
    day_mean = all_returns_df['day_return'].mean() * 100
    day_med = all_returns_df['day_return'].median() * 100
    day_std = all_returns_df['day_return'].std() * 100
    print(f"  평균 당일 수익률:  {'+' if day_mean > 0 else ''}{day_mean:.4f}%")
    print(f"  중앙값 당일 수익률: {'+' if day_med > 0 else ''}{day_med:.4f}%")
    print(f"  표준편차:          {day_std:.4f}%")


if __name__ == '__main__':
    main()

# python Theme_model/stock_return_range_analysis.py
