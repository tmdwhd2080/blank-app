# -*- coding: utf-8 -*-

import sys
import os
import pandas as pd
import numpy as np
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
from Theme_model.settings import (
    KOSPI_EXCEL_PATH,
    START_DATE,
    END_DATE,
    ROLLING_WINDOW,
    J, K, TOP_N, REBAL_COST, GAP,
    USE_FILTER, MIN_CONSTITUENTS,
    ZERO_RETURN_THRESHOLD, ZERO_RETURN_LOOKBACK,
    PRINT_LAST_N_REBAL,
    PRE_FILTER_ENABLED, PRE_FILTER_N, PRE_FILTER_K,
    get_filter_config,
)
from util.database2 import MSSQL, DBConfig


# ============================================================
# 백테스트
# ============================================================
def backtest_momentum(adj_pivot: pd.DataFrame,
                      theme_pivot: pd.DataFrame,
                      kospi_pivot: pd.DataFrame,
                      J: int = 2, K: int = 1,
                      top_n: int = 30,
                      rebal_cost: float = 0.002,
                      gap: int = 0,
                      use_filter: bool = False,
                      theme_info_df: pd.DataFrame = None,
                      theme_stocks_df: pd.DataFrame = None,
                      filter_config: dict = None,
                      pre_filter_enabled: bool = False,
                      pre_filter_n: int = 5,
                      pre_filter_k: float = 10.0) -> tuple:
    """
    백테스트 메인 루프

    use_filter=True  → theme_filter 모듈로 유동성 필터 적용
    use_filter=False → 기존 방식 (notna 체크만)

    Returns:
        bt_df: 백테스트 결과 DataFrame
        filter_log: 리밸런싱별 필터 통계 리스트 (필터 미사용 시 빈 리스트)
    """
    dates = adj_pivot.index.tolist()
    n_dates = len(dates)
    fc = filter_config or {}

    # 총 리밸런싱 횟수 사전 계산
    total_periods = 0
    temp_t = J
    while temp_t + gap + K <= n_dates:
        temp_t += K
        total_periods += 1

    # 구성종목 맵 캐시
    cached_const_map = {}
    cached_month = None

    records = []
    filter_log = []
    prev_winners = set()
    period_count = 0

    t = J
    while t + gap + K <= n_dates:
        as_of_date = dates[t - 1]

        # ── 테마 선정: 필터 적용 여부에 따라 분기 ──
        if use_filter:
            # 구성종목 맵 (월 단위 캐싱)
            as_of_month = as_of_date[:6]
            if as_of_month != cached_month:
                cached_const_map = build_constituent_map(
                    theme_info_df, theme_stocks_df, as_of_date
                )
                cached_month = as_of_month

            # 유동성 필터
            valid_themes, fstats = apply_liquidity_filter(
                theme_pivot=theme_pivot,
                current_idx=t - 1,
                constituent_counts=cached_const_map,
                min_constituents=fc.get('min_constituents', 3),
                zero_return_threshold=fc.get('zero_return_threshold', 0.2),
                zero_lookback=fc.get('zero_lookback', 5),
            )
            filter_log.append(fstats)

            # 필터 통과 테마 중 AdjRtn 데이터 있는 것만
            past_window = adj_pivot.iloc[t - J:t]
            valid_in_adj = [th for th in valid_themes if th in past_window.columns]
            valid_mask = past_window[valid_in_adj].notna().sum() >= J
            rankable_themes = valid_mask[valid_mask].index.tolist()

        else:
            # 필터 없이 기존 방식
            past_window = adj_pivot.iloc[t - J:t]
            valid_mask = past_window.notna().sum() >= J
            rankable_themes = valid_mask[valid_mask].index.tolist()
            fstats = None
            cached_const_map = {}
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
        # 랭킹 가능 테마 부족 시 스킵
        if len(rankable_themes) < top_n * (1 if use_filter else 2):
            t += K
            period_count += 1
            continue

        past_cum = (1 + past_window[rankable_themes].fillna(0)).prod() - 1
        ranked = past_cum.sort_values(ascending=False)
        winners = set(ranked.head(top_n).index.tolist())

        period_count += 1

        # 마지막 3번 리밸런싱 출력
        if period_count > total_periods - 3:
            print(f"\n  [리밸런싱 {period_count}/{total_periods}] "
                  f"선정기준: {dates[t-J]}~{dates[t-1]} → 매수: {dates[t+gap]}~")
            if use_filter and fstats:
                print(f"    필터 통과: {fstats['passed']}/{fstats['total']}개 "
                      f"(F1:{fstats['f1_constituents']}, F2:{fstats['f2_zero_return']}개 탈락)")
            for rank, (theme, ret) in enumerate(ranked.head(top_n).items(), 1):
                if use_filter:
                    n_stk = cached_const_map.get(theme, 0)
                    print(f"    {rank:>2}. THEME_ID={theme} ({n_stk}종목), "
                          f"누적AdjRtn={ret*100:.3f}%")
                else:
                    print(f"    {rank:>2}. THEME_ID={theme}, "
                          f"누적AdjRtn={ret*100:.3f}%")

        # ── 거래비용 ──
        if prev_winners:
            n_sell = len(prev_winners - winners)
            n_buy = len(winners - prev_winners)
            cost = (n_sell + n_buy) / top_n * rebal_cost
        else:
            cost = rebal_cost
            n_sell = 0
            n_buy = top_n

        turnover = (n_sell + n_buy) / (2 * top_n)

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

        record = {
            'rebal_date': dates[t],
            'holding_start': dates[future_start],
            'theme_cum': theme_cum,
            'kospi_cum': kospi_cum,
            'gross_return': gross_cum,
            'cost': cost,
            'net_return': net_cum,
            'turnover': turnover,
        }
        if use_filter and fstats:
            record['n_valid'] = fstats['passed']

        records.append(record)
        prev_winners = winners
        t += K

    return pd.DataFrame(records), filter_log


# ============================================================
# 월별 집계
# ============================================================
def monthly_summary(bt_df: pd.DataFrame) -> pd.DataFrame:
    bt_df = bt_df.copy()
    bt_df['YM'] = bt_df['holding_start'].str[:6]

    monthly = bt_df.groupby('YM').agg(
        N_rebal=('net_return', 'count'),
        gross_mean=('gross_return', 'mean'),
        net_mean=('net_return', 'mean'),
        cost_mean=('cost', 'mean'),
    ).reset_index()

    monthly_cum = []
    for ym in monthly['YM']:
        sub = bt_df[bt_df['YM'] == ym]
        cum = (1 + sub['net_return']).prod() - 1
        monthly_cum.append(cum)
    monthly['monthly_cum(%)'] = [x * 100 for x in monthly_cum]

    return monthly


# ============================================================
# 성과 지표
# ============================================================
def calc_metrics(bt_df: pd.DataFrame, K: int = 1) -> dict:
    net = bt_df['net_return'].values
    periods_per_year = 252 / K

    cum_series = (1 + pd.Series(net)).cumprod()
    total_cum = cum_series.iloc[-1] - 1

    rolling_max = cum_series.cummax()
    drawdown = (cum_series - rolling_max) / rolling_max
    mdd = drawdown.min()

    avg = np.mean(net)
    std = np.std(net, ddof=1)
    sharpe = (avg / std) * np.sqrt(periods_per_year) if std > 0 else 0

    n_years = len(net) / periods_per_year
    annual_return = (1 + total_cum) ** (1 / n_years) - 1 if n_years > 0 else 0

    metrics = {
        '총 누적수익률(%)': round(total_cum * 100, 2),
        '연환산 수익률(%)': round(annual_return * 100, 2),
        'Sharpe': round(sharpe, 3),
        'MDD(%)': round(mdd * 100, 2),
        '승률(%)': round((net > 0).mean() * 100, 1),
        '리밸런싱 횟수': len(net),
        '평균 거래비용(%)': round(bt_df['cost'].mean() * 100, 3),
    }

    if 'n_valid' in bt_df.columns:
        metrics['평균 유효테마수'] = round(bt_df['n_valid'].mean(), 1)

    return metrics


# ============================================================
# 메인
# ============================================================
def main():
    print("=" * 65)
    if USE_FILTER:
        print("  테마 모멘텀 전략 (유동성 필터 적용)")
    else:
        print("  테마 모멘텀 전략 (필터 없음)")
    print(f"  선정: AdjRtn(베타조정) 기준 Top {TOP_N}")
    print(f"  성과: ThemeRtn - KOSPI_Rtn (단순 초과수익률)")
    print(f"  J={J}일 / K={K}일 / Gap={GAP}일 / 비용 {REBAL_COST*100}%")
    if USE_FILTER:
        print(f"  필터: 종목수>={MIN_CONSTITUENTS}, "
              f"제로리턴<{ZERO_RETURN_THRESHOLD*100:.0f}%({ZERO_RETURN_LOOKBACK}일)")
    if PRE_FILTER_ENABLED:
        print(f"  사전 필터: Lookback 이전 {PRE_FILTER_N}일 수익률 {PRE_FILTER_K}% 이하")
    print(f"  기간: {START_DATE} ~ {END_DATE}")
    print("=" * 65)

    # ── 데이터 준비 ──
    print("\n[Step 1] 데이터 로드")
    kospi_df = load_kospi_returns(KOSPI_EXCEL_PATH, START_DATE, END_DATE)

    db = MSSQL(database=DBConfig.TRSTDEV_DB)
    theme_info_df = pd.DataFrame()
    theme_stocks_df = pd.DataFrame()
    try:
        theme_df = get_theme_returns(db, START_DATE, END_DATE)
        if USE_FILTER:
            print("\n  THEME_INFO / THEME_STOCKS 로드...")
            theme_info_df = get_theme_info(db)
            theme_stocks_df = get_theme_stocks(db)
    finally:
        db.close()

    print("\n[Step 2] 시장조정 수익률 계산")
    result_df = compute_market_adjusted_returns(theme_df, kospi_df, START_DATE, ROLLING_WINDOW)

    # ── 피벗 3종 생성 ──
    adj_pivot = result_df.pivot_table(
        index='PfmDate', columns='THEME_ID', values='AdjRtn', aggfunc='first'
    ).sort_index()

    theme_pivot = result_df.pivot_table(
        index='PfmDate', columns='THEME_ID', values='ThemeRtn', aggfunc='first'
    ).sort_index()

    kospi_pivot = result_df.drop_duplicates('PfmDate').set_index('PfmDate')['KOSPI_Rtn'].sort_index()

    print(f"\n  피벗: {adj_pivot.shape[0]}일 x {adj_pivot.shape[1]}개 테마\n")

    # ── 필터 설정 ──
    filter_config = get_filter_config()

    # ── 백테스트 ──
    print("=" * 65)
    print(f"  📊 J={J}일 / K={K}일 / Gap={GAP}일 / Top {TOP_N}")
    print("=" * 65)

    bt_df, filter_log = backtest_momentum(
        adj_pivot, theme_pivot, kospi_pivot,
        J=J, K=K, top_n=TOP_N, rebal_cost=REBAL_COST, gap=GAP,
        use_filter=USE_FILTER,
        theme_info_df=theme_info_df,
        theme_stocks_df=theme_stocks_df,
        filter_config=filter_config,
        pre_filter_enabled=PRE_FILTER_ENABLED,
        pre_filter_n=PRE_FILTER_N,
        pre_filter_k=PRE_FILTER_K,
    )

    if bt_df.empty:
        print("  데이터 부족")
        return

    # ── 필터 통계 ──
    if USE_FILTER and filter_log:
        summarize_filter_stats(filter_log, config=filter_config)

    # ── 성과 지표 ──
    metrics = calc_metrics(bt_df, K)
    print(f"\n  총 누적수익률: {metrics['총 누적수익률(%)']:.2f}%")
    print(f"  연환산 수익률: {metrics['연환산 수익률(%)']:.2f}%")
    print(f"  Sharpe Ratio:  {metrics['Sharpe']:.3f}")
    print(f"  MDD:           {metrics['MDD(%)']:.2f}%")
    print(f"  승률:          {metrics['승률(%)']:.1f}%")
    print(f"  리밸런싱:      {metrics['리밸런싱 횟수']}회")
    print(f"  평균 거래비용: {metrics['평균 거래비용(%)']:.3f}%/회")
    if '평균 유효테마수' in metrics:
        print(f"  평균 유효테마: {metrics['평균 유효테마수']}개")

    # ── 월별 요약 ──
    monthly = monthly_summary(bt_df)
    print(f"\n  {'월':>8}  {'리밸':>4}  {'월 누적수익률':>12}")
    print(f"  {'-'*8}  {'-'*4}  {'-'*12}")
    for _, row in monthly.iterrows():
        cum = row['monthly_cum(%)']
        sign = '+' if cum > 0 else ''
        print(f"  {row['YM']:>8}  {int(row['N_rebal']):>4}  {sign}{cum:>10.2f}%")

    avg_monthly = monthly['monthly_cum(%)'].mean()
    print(f"  {'─'*30}")
    print(f"  {'월평균':>8}        {'+' if avg_monthly > 0 else ''}{avg_monthly:>10.2f}%")


if __name__ == '__main__':
    main()

#python Theme_model/tmomnetum.py