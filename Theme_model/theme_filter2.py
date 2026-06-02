# -*- coding: utf-8 -*-
"""
테마 모멘텀 전략 + 종목 중복 필터
==================================
기존 tmomnetum.py 결과에서:
  1) Top N 테마 선정 후, 구성종목 겹침 30% 이상인 쌍 탐지
  2) 겹치는 쌍 중 AdjRtn 낮은 테마 제거
  3) 남은 테마로 성과 재계산

Import:
  - BETA.py          → 시장조정 수익률
  - theme_filter.py  → 유동성 필터 + THEME_STOCKS 조회
  - tmomnetum.py     → 백테스트 로직, 성과 지표
"""

import sys
import os
import pandas as pd
import numpy as np
from itertools import combinations
import warnings
warnings.filterwarnings('ignore')

sys.path.append(os.path.join(r'C:\Users\intern9', 'truston_quant_dev'))

from Theme_model.BETA import (
    load_kospi_returns,
    get_theme_returns,
    compute_market_adjusted_returns,
)
from Theme_model.theme_filter import (
    get_theme_info,
    get_theme_stocks,
    build_constituent_map,
    apply_liquidity_filter,
    summarize_filter_stats,
)
from Theme_model.tmomnetum import (
    monthly_summary,
    calc_metrics,
)
from Theme_model.settings import (
    KOSPI_EXCEL_PATH,
    START_DATE,
    END_DATE,
    ROLLING_WINDOW,
    J, K, TOP_N, REBAL_COST_OVERLAP, GAP,
    USE_FILTER, MIN_CONSTITUENTS,
    ZERO_RETURN_THRESHOLD, ZERO_RETURN_LOOKBACK,
    OVERLAP_THRESHOLD,
    PRE_FILTER_ENABLED, PRE_FILTER_N, PRE_FILTER_K,
    get_filter_config,
)
from util.database2 import MSSQL, DBConfig


# ============================================================
# 종목 중복 맵 구축 (THEME_ID → 종목 set)
# ============================================================
def build_constituent_sets(theme_info_df: pd.DataFrame,
                           theme_stocks_df: pd.DataFrame,
                           as_of_date: str) -> dict:
    """
    as_of_date 기준 각 THEME_ID → set(STK_CODE) 반환
    """
    if theme_info_df.empty or theme_stocks_df.empty:
        return {}

    # THEME_INFO: as_of_date 이전 최신
    info_valid = theme_info_df[theme_info_df['UPDATED_DT'] <= as_of_date]
    if info_valid.empty:
        return {}

    info_latest = (info_valid
                   .sort_values('UPDATED_DT')
                   .groupby('THEME_NAME')
                   .last()
                   .reset_index()[['THEME_NAME', 'THEME_ID']])
    name_to_id = dict(zip(info_latest['THEME_NAME'], info_latest['THEME_ID']))

    # THEME_STOCKS: as_of_date 이전 최신
    stocks_valid = theme_stocks_df[theme_stocks_df['UPDATED_DT'] <= as_of_date]
    if stocks_valid.empty:
        return {}

    latest_dt = (stocks_valid
                 .groupby('THEME_NAME')['UPDATED_DT']
                 .max()
                 .reset_index()
                 .rename(columns={'UPDATED_DT': 'latest_dt'}))

    stocks_latest = stocks_valid.merge(
        latest_dt,
        left_on=['THEME_NAME', 'UPDATED_DT'],
        right_on=['THEME_NAME', 'latest_dt'],
        how='inner',
    )

    # THEME_NAME → set(STK_CODE)
    name_sets = stocks_latest.groupby('THEME_NAME')['STK_CODE'].apply(set).to_dict()

    # THEME_ID 기준으로 변환
    result = {}
    for theme_name, stk_set in name_sets.items():
        theme_id = name_to_id.get(theme_name)
        if theme_id:
            result[theme_id] = stk_set

    return result


# ============================================================
# 종목 중복 필터: 겹침 30% 이상이면 AdjRtn 낮은 쪽 제거
# ============================================================
def remove_overlapping_themes(winners_ranked: list,
                              adj_scores: dict,
                              constituent_sets: dict,
                              overlap_threshold: float = 0.3) -> tuple:
    """
    Parameters:
        winners_ranked: AdjRtn 내림차순 정렬된 THEME_ID 리스트
        adj_scores:     {THEME_ID: 누적 AdjRtn} 딕셔너리
        constituent_sets: {THEME_ID: set(STK_CODE)}
        overlap_threshold: 겹침 비율 임계값

    Returns:
        survivors: 필터 통과 테마 리스트
        n_removed: 제거된 테마 수
    """
    removed = set()

    # AdjRtn 높은 순서대로 처리 → 높은 쪽이 살아남음
    for i, theme_a in enumerate(winners_ranked):
        if theme_a in removed:
            continue
        set_a = constituent_sets.get(theme_a)
        if not set_a:
            continue

        for theme_b in winners_ranked[i + 1:]:
            if theme_b in removed:
                continue
            set_b = constituent_sets.get(theme_b)
            if not set_b:
                continue

            # 겹침 비율 = 교집합 / min(두 집합 크기)
            intersection = len(set_a & set_b)
            min_size = min(len(set_a), len(set_b))
            if min_size == 0:
                continue

            overlap_ratio = intersection / min_size
            if overlap_ratio >= overlap_threshold:
                # AdjRtn 낮은 쪽(theme_b) 제거 (이미 내림차순이므로 b가 낮음)
                removed.add(theme_b)

    survivors = [th for th in winners_ranked if th not in removed]
    return survivors, len(removed)


# ============================================================
# 백테스트 (종목 중복 필터 추가)
# ============================================================
def backtest_momentum_with_overlap(
    adj_pivot: pd.DataFrame,
    theme_pivot: pd.DataFrame,
    kospi_pivot: pd.DataFrame,
    J: int = 3, K: int = 1,
    top_n: int = 30,
    rebal_cost: float = 0.01,
    gap: int = 0,
    use_filter: bool = True,
    theme_info_df: pd.DataFrame = None,
    theme_stocks_df: pd.DataFrame = None,
    filter_config: dict = None,
    overlap_threshold: float = 0.3,
    pre_filter_enabled: bool = False,
    pre_filter_n: int = 5,
    pre_filter_k: float = 10.0,
) -> tuple:
    """
    Returns:
        bt_df: 백테스트 결과
        filter_log: 유동성 필터 로그
        overlap_log: 종목 중복 필터 로그 (리밸런싱별)
    """
    dates = adj_pivot.index.tolist()
    n_dates = len(dates)
    fc = filter_config or {}

    # 구성종목 캐시 (월 단위)
    cached_const_map = {}
    cached_const_sets = {}
    cached_month = None

    records = []
    filter_log = []
    overlap_log = []
    prev_winners = set()

    t = J
    while t + gap + K <= n_dates:
        as_of_date = dates[t - 1]

        # ── 월 단위 캐싱 ──
        as_of_month = as_of_date[:6]
        if as_of_month != cached_month:
            if use_filter:
                cached_const_map = build_constituent_map(
                    theme_info_df, theme_stocks_df, as_of_date
                )
            cached_const_sets = build_constituent_sets(
                theme_info_df, theme_stocks_df, as_of_date
            )
            cached_month = as_of_month

        # ── 유동성 필터 ──
        if use_filter:
            valid_themes, fstats = apply_liquidity_filter(
                theme_pivot=theme_pivot,
                current_idx=t - 1,
                constituent_counts=cached_const_map,
                min_constituents=fc.get('min_constituents', 3),
                zero_return_threshold=fc.get('zero_return_threshold', 0.2),
                zero_lookback=fc.get('zero_lookback', 5),
            )
            filter_log.append(fstats)

            past_window = adj_pivot.iloc[t - J:t]
            valid_in_adj = [th for th in valid_themes if th in past_window.columns]
            valid_mask = past_window[valid_in_adj].notna().sum() >= J
            rankable_themes = valid_mask[valid_mask].index.tolist()
        else:
            past_window = adj_pivot.iloc[t - J:t]
            valid_mask = past_window.notna().sum() >= J
            rankable_themes = valid_mask[valid_mask].index.tolist()
            fstats = None

        # ── 사전 필터: Lookback 이전 N일 수익률 K% 이하만 유니버스 ──
        if pre_filter_enabled:
            pre_start = max(0, t - J - pre_filter_n)
            pre_end = t - J
            if pre_end > pre_start and pre_end > 0:
                pre_window = adj_pivot.iloc[pre_start:pre_end]
                pre_available = [th for th in rankable_themes if th in pre_window.columns]
                if pre_available:
                    pre_cum = (1 + pre_window[pre_available].fillna(0)).prod() - 1
                    rankable_themes = pre_cum[pre_cum <= pre_filter_k / 100.0].index.tolist()

        if len(rankable_themes) < top_n:
            t += K
            continue

        # ── AdjRtn 기준 Top N 선정 ──
        past_cum = (1 + past_window[rankable_themes].fillna(0)).prod() - 1
        ranked = past_cum.sort_values(ascending=False)
        top_themes = ranked.head(top_n)

        winners_ranked = top_themes.index.tolist()
        adj_scores = top_themes.to_dict()

        # ── 종목 중복 필터 ──
        survivors, n_removed = remove_overlapping_themes(
            winners_ranked, adj_scores, cached_const_sets, overlap_threshold
        )
        overlap_log.append({
            'date': as_of_date,
            'before': len(winners_ranked),
            'removed': n_removed,
            'after': len(survivors),
        })

        winners = set(survivors)

        # ── 거래비용 ──
        n_active = len(winners)
        if n_active == 0:
            t += K
            continue

        if prev_winners:
            n_sell = len(prev_winners - winners)
            n_buy = len(winners - prev_winners)
            cost = (n_sell + n_buy) / n_active * rebal_cost
        else:
            cost = rebal_cost
            n_sell = 0
            n_buy = n_active

        turnover = (n_sell + n_buy) / (2 * max(n_active, 1))

        # ── 성과 측정 ──
        future_start = t + gap
        future_end = t + gap + K
        future_dates = adj_pivot.index[future_start:future_end]

        theme_returns = theme_pivot.loc[future_dates, list(winners)].mean(axis=1)
        kospi_returns = kospi_pivot.loc[future_dates]
        excess_returns = theme_returns - kospi_returns

        gross_cum = (1 + excess_returns).prod() - 1
        net_cum = gross_cum - cost
        theme_cum = (1 + theme_returns).prod() - 1
        kospi_cum = (1 + kospi_returns).prod() - 1

        records.append({
            'rebal_date': dates[t],
            'holding_start': dates[future_start],
            'theme_cum': theme_cum,
            'kospi_cum': kospi_cum,
            'gross_return': gross_cum,
            'cost': cost,
            'net_return': net_cum,
            'turnover': turnover,
            'n_themes': n_active,
            'n_removed': n_removed,
        })

        prev_winners = winners
        t += K

    return pd.DataFrame(records), filter_log, overlap_log


# ============================================================
# 월별 집계 (재정의 - n_themes 포함)
# ============================================================
def monthly_summary_overlap(bt_df: pd.DataFrame) -> pd.DataFrame:
    bt_df = bt_df.copy()
    bt_df['YM'] = bt_df['holding_start'].str[:6]

    monthly = bt_df.groupby('YM').agg(
        N_rebal=('net_return', 'count'),
        avg_themes=('n_themes', 'mean'),
        avg_removed=('n_removed', 'mean'),
    ).reset_index()

    monthly_cum = []
    for ym in monthly['YM']:
        sub = bt_df[bt_df['YM'] == ym]
        cum = (1 + sub['net_return']).prod() - 1
        monthly_cum.append(cum)
    monthly['monthly_cum(%)'] = [x * 100 for x in monthly_cum]

    return monthly


# ============================================================
# 메인
# ============================================================
def main():
    print("=" * 65)
    print("  테마 모멘텀 전략 + 종목 중복 필터")
    print(f"  J={J}일 / K={K}일 / Gap={GAP}일 / Top {TOP_N}")
    print(f"  종목 중복 임계값: {OVERLAP_THRESHOLD*100:.0f}%")
    if USE_FILTER:
        print(f"  유동성 필터: 종목수>={MIN_CONSTITUENTS}, "
              f"제로리턴<{ZERO_RETURN_THRESHOLD*100:.0f}%({ZERO_RETURN_LOOKBACK}일)")
    if PRE_FILTER_ENABLED:
        print(f"  사전 필터: Lookback 이전 {PRE_FILTER_N}일 수익률 {PRE_FILTER_K}% 이하")
    print(f"  기간: {START_DATE} ~ {END_DATE}")
    print("=" * 65)

    # ── 데이터 준비 ──
    print("\n[Step 1] 데이터 로드")
    kospi_df = load_kospi_returns(KOSPI_EXCEL_PATH, START_DATE, END_DATE)

    db = MSSQL(database=DBConfig.TRSTDEV_DB)
    try:
        theme_df = get_theme_returns(db, START_DATE, END_DATE)
        print("\n  THEME_INFO / THEME_STOCKS 로드...")
        theme_info_df = get_theme_info(db)
        theme_stocks_df = get_theme_stocks(db)
    finally:
        db.close()

    print("\n[Step 2] 시장조정 수익률 계산")
    result_df = compute_market_adjusted_returns(theme_df, kospi_df, START_DATE, ROLLING_WINDOW)

    # 피벗
    adj_pivot = result_df.pivot_table(
        index='PfmDate', columns='THEME_ID', values='AdjRtn', aggfunc='first'
    ).sort_index()

    theme_pivot = result_df.pivot_table(
        index='PfmDate', columns='THEME_ID', values='ThemeRtn', aggfunc='first'
    ).sort_index()

    kospi_pivot = (result_df.drop_duplicates('PfmDate')
                   .set_index('PfmDate')['KOSPI_Rtn'].sort_index())

    print(f"\n  피벗: {adj_pivot.shape[0]}일 x {adj_pivot.shape[1]}개 테마\n")

    filter_config = get_filter_config()

    # ── 백테스트 ──
    print("[Step 3] 백테스트 (종목 중복 필터 포함)")
    bt_df, filter_log, overlap_log = backtest_momentum_with_overlap(
        adj_pivot, theme_pivot, kospi_pivot,
        J=J, K=K, top_n=TOP_N, rebal_cost=REBAL_COST_OVERLAP, gap=GAP,
        use_filter=USE_FILTER,
        theme_info_df=theme_info_df,
        theme_stocks_df=theme_stocks_df,
        filter_config=filter_config,
        overlap_threshold=OVERLAP_THRESHOLD,
        pre_filter_enabled=PRE_FILTER_ENABLED,
        pre_filter_n=PRE_FILTER_N,
        pre_filter_k=PRE_FILTER_K,
    )

    if bt_df.empty:
        print("  데이터 부족")
        return

    # ── 유동성 필터 통계 ──
    if USE_FILTER and filter_log:
        summarize_filter_stats(filter_log, config=filter_config)

    # ── 종목 중복 필터 통계 ──
    ol_df = pd.DataFrame(overlap_log)
    print(f"\n  📊 종목 중복 필터 통계 (총 {len(ol_df)}회 리밸런싱)")
    print(f"  {'─'*50}")
    print(f"  중복 필터 전 테마 수 (평균): {ol_df['before'].mean():.1f}개")
    print(f"  제거된 테마 수 (평균):       {ol_df['removed'].mean():.1f}개")
    print(f"  남은 테마 수 (평균):         {ol_df['after'].mean():.1f}개")
    print(f"  제거 비율 (평균):            {ol_df['removed'].mean()/ol_df['before'].mean()*100:.1f}%")
    print(f"  남은 테마 (최소/중앙/최대):  "
          f"{ol_df['after'].min()} / {ol_df['after'].median():.0f} / {ol_df['after'].max()}개")

    # ── 성과 지표 ──
    metrics = calc_metrics(bt_df, K)
    print(f"\n  {'='*50}")
    print(f"  📊 성과 지표")
    print(f"  {'='*50}")
    print(f"  총 누적수익률: {metrics['총 누적수익률(%)']:.2f}%")
    print(f"  연환산 수익률: {metrics['연환산 수익률(%)']:.2f}%")
    print(f"  Sharpe Ratio:  {metrics['Sharpe']:.3f}")
    print(f"  MDD:           {metrics['MDD(%)']:.2f}%")
    print(f"  승률:          {metrics['승률(%)']:.1f}%")
    print(f"  리밸런싱:      {metrics['리밸런싱 횟수']}회")
    print(f"  평균 거래비용: {metrics['평균 거래비용(%)']:.3f}%/회")
    print(f"  평균 보유테마: {bt_df['n_themes'].mean():.1f}개")
    print(f"  평균 제거테마: {bt_df['n_removed'].mean():.1f}개")

    # ── 월별 요약 ──
    monthly = monthly_summary_overlap(bt_df)
    print(f"\n  {'월':>8}  {'리밸':>4}  {'평균보유':>8}  {'평균제거':>8}  {'월 누적수익률':>12}")
    print(f"  {'-'*8}  {'-'*4}  {'-'*8}  {'-'*8}  {'-'*12}")
    for _, row in monthly.iterrows():
        cum = row['monthly_cum(%)']
        sign = '+' if cum > 0 else ''
        print(f"  {row['YM']:>8}  {int(row['N_rebal']):>4}  "
              f"{row['avg_themes']:>7.1f}개  {row['avg_removed']:>7.1f}개  "
              f"{sign}{cum:>10.2f}%")

    avg_monthly = monthly['monthly_cum(%)'].mean()
    print(f"  {'─'*55}")
    print(f"  {'월평균':>8}                                {'+' if avg_monthly > 0 else ''}{avg_monthly:>10.2f}%")


if __name__ == '__main__':
    main()

# python Theme_model/theme_filter2.py