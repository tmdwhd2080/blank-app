# -*- coding: utf-8 -*-
"""
유전 알고리즘 시행
- fitness 함수: 과열 테마 탐색
- 사전 필터: Lookback 이전 N일 수익률 K% 이하 테마만 유니버스
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
from Theme_model.theme_filter2 import (
    build_constituent_sets,
    remove_overlapping_themes,
)
from Theme_model.tmomnetum import calc_metrics
from Theme_model.settings import (
    KOSPI_EXCEL_PATH,
    START_DATE,
    END_DATE,
    ROLLING_WINDOW,
    J, K, TOP_N, REBAL_COST_GA, GAP,
    USE_FILTER, MIN_CONSTITUENTS,
    ZERO_RETURN_THRESHOLD, ZERO_RETURN_LOOKBACK,
    OVERLAP_THRESHOLD,
    GA_LOOKBACK, GA_POP_SIZE, GA_GENERATIONS,
    GA_MUTATION_RATE, GA_ELITE_RATIO, GA_TOURNAMENT_K,
    MIN_WEIGHT_THRESHOLD,
    PRINT_LAST_N_REBAL,
    PRE_FILTER_ENABLED, PRE_FILTER_N, PRE_FILTER_K,
    STOCK_ANALYSIS_ENABLED, CRAWL_DELAY,
    get_filter_config, get_ga_config,
)
from util.database2 import MSSQL, DBConfig
from Theme_model.theme_stock_return_analysis import (
    run_stock_return_analysis,
    run_all_rolling_analysis,
    aggregate_and_print_rolling_results,
)


# ============================================================
# GA: 적합도 함수 (분자=J일 누적 초과수익률, 분모=20일 변동성)
# ============================================================
def fitness_sharpe(weights: np.ndarray,
                   theme_rtn_full: np.ndarray,
                   kospi_rtn_full: np.ndarray,
                   theme_rtn_recent: np.ndarray,
                   kospi_rtn_recent: np.ndarray) -> float:
    """
    적합도 = (최근 J일 누적 초과수익률) / (최근 GA_LOOKBACK일 일별 초과수익률 std)

    Parameters:
        weights:          (n_themes,) 비중 벡터
        theme_rtn_full:   (ga_lookback, n_themes) GA_LOOKBACK일간 테마 수익률 (변동성용)
        kospi_rtn_full:   (ga_lookback,) GA_LOOKBACK일간 코스피 수익률 (변동성용)
        theme_rtn_recent: (J, n_themes) 최근 J일간 테마 수익률 (수익률용)
        kospi_rtn_recent: (J,) 최근 J일간 코스피 수익률 (수익률용)
    """
    # ── 분자: 최근 J일 누적 초과수익률 ──
    port_rtn_recent = theme_rtn_recent @ weights        # (J,)
    excess_recent = port_rtn_recent - kospi_rtn_recent  # (J,)
    cum_excess = (1 + pd.Series(excess_recent)).prod() - 1  # 누적

    # ── 분모: GA_LOOKBACK일 일별 초과수익률 표준편차 ──
    port_rtn_full = theme_rtn_full @ weights            # (ga_lookback,)
    excess_full = port_rtn_full - kospi_rtn_full        # (ga_lookback,)
    std_ex = np.std(excess_full, ddof=1)

    if std_ex < 1e-10:
        return 0.0

    return cum_excess / std_ex


# ============================================================
# GA: 비중 유틸리티
# ============================================================
def normalize_weights(w: np.ndarray, min_weight: float = 0.0) -> np.ndarray:
    """비중 합 = 1, 음수 → 0 처리, 최소 비중 미만 → 0 처리 후 재정규화
    
    Args:
        w: 비중 배열
        min_weight: 최소 비중 임계값 (0.0005 = 0.05%)
    """
    w = np.maximum(w, 0.0)
    s = w.sum()
    if s < 1e-10:
        return np.ones(len(w)) / len(w)
    
    # 1차 정규화
    w = w / s
    
    # 최소 비중 적용: min_weight 미만인 테마는 0으로 설정
    if min_weight > 0:
        w[w < min_weight] = 0.0
        s = w.sum()
        if s < 1e-10:
            return np.ones(len(w)) / len(w)
        w = w / s  # 재정규화
    
    return w


def apply_min_weight_constraint(weights: np.ndarray, 
                                 survivors: list, 
                                 min_weight: float = MIN_WEIGHT_THRESHOLD) -> tuple:
    """최소 비중 제한 적용 후 살아남은 테마와 비중 반환
    
    Args:
        weights: GA 최적화된 비중 배열
        survivors: 테마 ID 리스트
        min_weight: 최소 비중 임계값
    
    Returns:
        new_survivors: 최소 비중 이상인 테마 리스트
        new_weights: 재정규화된 비중 배열
        n_excluded: 제외된 테마 수
    """
    # 최소 비중 이상인 테마만 선택
    mask = weights >= min_weight
    new_survivors = [s for s, m in zip(survivors, mask) if m]
    new_weights = weights[mask]
    n_excluded = len(survivors) - len(new_survivors)
    
    if len(new_weights) == 0:
        # 모든 테마가 제외된 경우, 동일 비중으로 원복
        return survivors, np.ones(len(survivors)) / len(survivors), 0
    
    # 재정규화
    new_weights = new_weights / new_weights.sum()
    
    return new_survivors, new_weights, n_excluded


def init_population(n_themes: int, pop_size: int) -> np.ndarray:
    """초기 인구 생성: Dirichlet 분포 + 동일비중"""
    pop = np.random.dirichlet(np.ones(n_themes), size=pop_size - 1)
    equal_w = np.ones((1, n_themes)) / n_themes
    return np.vstack([equal_w, pop])


def tournament_select(fitnesses: np.ndarray, k: int = 3) -> int:
    """토너먼트 선택"""
    candidates = np.random.choice(len(fitnesses), size=k, replace=False)
    return candidates[np.argmax(fitnesses[candidates])]


def crossover(p1: np.ndarray, p2: np.ndarray) -> np.ndarray:
    """BLX-α 크로스오버"""
    alpha = 0.5
    low = np.minimum(p1, p2) - alpha * np.abs(p1 - p2)
    high = np.maximum(p1, p2) + alpha * np.abs(p1 - p2)
    child = np.random.uniform(low, high)
    return normalize_weights(child)


def mutate(w: np.ndarray, rate: float = 0.15) -> np.ndarray:
    """가우시안 돌연변이"""
    if np.random.rand() < rate:
        noise = np.random.normal(0, 0.05, size=len(w))
        w = w + noise
    return normalize_weights(w)


def ga_optimize_weights(theme_rtn_full: np.ndarray,
                        kospi_rtn_full: np.ndarray,
                        theme_rtn_recent: np.ndarray,
                        kospi_rtn_recent: np.ndarray,
                        pop_size: int = GA_POP_SIZE,
                        generations: int = GA_GENERATIONS,
                        mutation_rate: float = GA_MUTATION_RATE,
                        elite_ratio: float = GA_ELITE_RATIO,
                        tournament_k: int = GA_TOURNAMENT_K) -> np.ndarray:
    """
    GA로 최적 비중 벡터 반환

    Parameters:
        theme_rtn_full:   (ga_lookback, n_themes) 변동성 계산용
        kospi_rtn_full:   (ga_lookback,)          변동성 계산용
        theme_rtn_recent: (J, n_themes)           수익률 계산용
        kospi_rtn_recent: (J,)                    수익률 계산용

    Returns:
        best_weights: (n_themes,)
    """
    n_themes = theme_rtn_full.shape[1]

    if n_themes <= 1:
        return np.ones(max(n_themes, 1))

    # 데이터 부족 시 동일비중
    if theme_rtn_full.shape[0] < 3 or theme_rtn_recent.shape[0] < 1:
        return np.ones(n_themes) / n_themes

    pop = init_population(n_themes, pop_size)
    n_elite = max(1, int(pop_size * elite_ratio))

    best_ever_w = np.ones(n_themes) / n_themes
    best_ever_fit = -np.inf

    for gen in range(generations):
        # 적합도 계산
        fitnesses = np.array([
            fitness_sharpe(pop[i],
                           theme_rtn_full, kospi_rtn_full,
                           theme_rtn_recent, kospi_rtn_recent)
            for i in range(pop_size)
        ])

        # 엘리트 보존
        elite_idx = np.argsort(fitnesses)[-n_elite:]
        elites = pop[elite_idx].copy()

        # 최고 기록 갱신
        gen_best_idx = np.argmax(fitnesses)
        if fitnesses[gen_best_idx] > best_ever_fit:
            best_ever_fit = fitnesses[gen_best_idx]
            best_ever_w = pop[gen_best_idx].copy()

        # 다음 세대
        new_pop = [elites[i] for i in range(n_elite)]
        while len(new_pop) < pop_size:
            i1 = tournament_select(fitnesses, tournament_k)
            i2 = tournament_select(fitnesses, tournament_k)
            child = crossover(pop[i1], pop[i2])
            child = mutate(child, mutation_rate)
            new_pop.append(child)

        pop = np.array(new_pop[:pop_size])

    return normalize_weights(best_ever_w)


# ============================================================
# 백테스트 (중복필터 + GA 비중)
# ============================================================
def backtest_ga(
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
    ga_lookback: int = 20,
    pre_filter_enabled: bool = False,
    pre_filter_n: int = 5,
    pre_filter_k: float = 10.0,
) -> tuple:
    """
    Returns:
        bt_df, filter_log, portfolio_log
    """
    dates = adj_pivot.index.tolist()
    n_dates = len(dates)
    fc = filter_config or {}

    cached_const_map = {}
    cached_const_sets = {}
    cached_month = None

    records = []
    filter_log = []
    portfolio_log = []
    prev_winners = set()
    prev_weights = {}  # 이전 비중 벡터 (테마 ID -> 비중)

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

        # ── AdjRtn 기준 Top N 선정 (과열 테마 = 상위) ──
        past_cum = (1 + past_window[rankable_themes].fillna(0)).prod() - 1
        ranked = past_cum.sort_values(ascending=False)
        top_themes = ranked.head(top_n)
        winners_ranked = top_themes.index.tolist()
        adj_scores = top_themes.to_dict()

        # ── 종목 중복 필터 ──
        survivors, n_removed = remove_overlapping_themes(
            winners_ranked, adj_scores, cached_const_sets, overlap_threshold
        )

        if len(survivors) == 0:
            t += K
            continue

        # ── GA 비중 최적화 ──
        # 분모용: GA_LOOKBACK일간 데이터 (변동성)
        ga_full_start = max(0, t - ga_lookback)
        ga_theme_full = theme_pivot.iloc[ga_full_start:t][survivors].fillna(0).values
        ga_kospi_full = kospi_pivot.iloc[ga_full_start:t].fillna(0).values

        # 분자용: 최근 J일간 데이터 (누적 초과수익률)
        ga_recent_start = max(0, t - J)
        ga_theme_recent = theme_pivot.iloc[ga_recent_start:t][survivors].fillna(0).values
        ga_kospi_recent = kospi_pivot.iloc[ga_recent_start:t].fillna(0).values

        if ga_theme_full.shape[0] < 3 or ga_theme_recent.shape[0] < 1:
            weights = np.ones(len(survivors)) / len(survivors)
        else:
            weights = ga_optimize_weights(
                ga_theme_full, ga_kospi_full,
                ga_theme_recent, ga_kospi_recent,
            )

        # ── 최소 비중 제한 적용 ──
        survivors, weights, n_weight_excluded = apply_min_weight_constraint(
            weights, survivors, min_weight=MIN_WEIGHT_THRESHOLD
        )

        if len(survivors) == 0:
            t += K
            continue

        # 비중 딕셔너리 저장
        weight_dict = {th: round(w, 4) for th, w in zip(survivors, weights)}
        # holding_end: 보유종료 지수 (future_end-1)
        holding_end_idx = min(t + gap + K - 1, n_dates - 1)
        portfolio_log.append({
            'date': as_of_date,
            'holding_end': dates[holding_end_idx],
            'n_themes': len(survivors),
            'n_removed': n_removed,
            'n_weight_excluded': n_weight_excluded,
            'weights': weight_dict,
        })

        winners = set(survivors)

        # ── 거래비용 (비중 벡터 기반 turnover) ──
        if prev_weights:
            all_themes = set(weight_dict.keys()) | set(prev_weights.keys())
            turnover = sum(
                abs(weight_dict.get(th, 0) - prev_weights.get(th, 0))
                for th in all_themes
            ) / 2  # 편도 기준
            cost = turnover * rebal_cost * 2  # 왕복
        else:
            # 첫 리밸런싱: 전체 매수
            cost = rebal_cost * 2  # 왕복
            turnover = 1.0

        # ── 성과 측정 (가중 수익률) ──
        future_start = t + gap
        future_end = t + gap + K
        future_dates = adj_pivot.index[future_start:future_end]

        future_theme_rtn = theme_pivot.loc[future_dates, survivors].fillna(0)
        weighted_rtn = (future_theme_rtn.values @ weights)
        kospi_rtn = kospi_pivot.loc[future_dates].fillna(0).values

        # ── 초과수익률 계산 ──
        excess_rtn = weighted_rtn - kospi_rtn

        gross_cum = (1 + pd.Series(excess_rtn)).prod() - 1
        net_cum = gross_cum - cost
        theme_cum = (1 + pd.Series(weighted_rtn)).prod() - 1
        kospi_cum = (1 + pd.Series(kospi_rtn)).prod() - 1

        records.append({
            'rebal_date': dates[t],
            'holding_start': dates[future_start],
            'theme_cum': theme_cum,
            'kospi_cum': kospi_cum,
            'gross_return': gross_cum,
            'cost': cost,
            'turnover': turnover,
            'net_return': net_cum,
            'n_themes': len(survivors),
            'n_removed': n_removed,
            'n_weight_excluded': n_weight_excluded,
        })

        prev_winners = winners
        prev_weights = weight_dict  # 다음 리밸런싱을 위해 현재 비중 저장
        t += K

    return pd.DataFrame(records), filter_log, portfolio_log


# ============================================================
# 메인
# ============================================================
def main():

    print("=" * 65)
    print(f"  테마 모멘텀 + 중복필터 + GA 비중 최적화")
    print(f"  J={J} / K={K} / Gap={GAP} / Top {TOP_N}")
    print(f"  중복 임계값: {OVERLAP_THRESHOLD*100:.0f}%")
    print(f"  최소 비중: {MIN_WEIGHT_THRESHOLD*100:.2f}%")
    print(f"  GA: lookback(변동성)={GA_LOOKBACK}일, "
          f"수익률=J({J}일)")
    print(f"  GA: pop={GA_POP_SIZE}, gen={GA_GENERATIONS}, "
          f"mut={GA_MUTATION_RATE}")
    if PRE_FILTER_ENABLED:
        print(f"  사전 필터: Lookback 이전 {PRE_FILTER_N}일 수익률 {PRE_FILTER_K}% 이하")
    print(f"  기간: {START_DATE} ~ {END_DATE}")
    print("=" * 65)

    # ── 데이터 로드 ──
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

    adj_pivot = result_df.pivot_table(
        index='PfmDate', columns='THEME_ID', values='AdjRtn', aggfunc='first'
    ).sort_index()

    theme_pivot = result_df.pivot_table(
        index='PfmDate', columns='THEME_ID', values='ThemeRtn', aggfunc='first'
    ).sort_index()

    kospi_pivot = (result_df.drop_duplicates('PfmDate')
                   .set_index('PfmDate')['KOSPI_Rtn'].sort_index())

    print(f"\n  피벗: {adj_pivot.shape[0]}일 x {adj_pivot.shape[1]}개 테마")

    filter_config = get_filter_config()

    # ── 백테스트 ──
    print(f"\n[Step 3] 백테스트 (중복필터 + GA 비중)")
    print(f"  fitness = (J={J}일 누적초과수익률) / "
          f"({GA_LOOKBACK}일 일별초과수익률 std)")
    print(f"  성과 = (테마가중 - KOSPI) - 거래비용")

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

    if bt_df.empty:
        print("  데이터 부족")
        return

    # ── 성과 지표 ──
    metrics = calc_metrics(bt_df, K)

    print(f"\n{'='*65}")
    print(f"  📊 성과 요약")
    print(f"{'='*65}")
    print(f"  Sharpe Ratio:  {metrics['Sharpe']:.3f}")
    print(f"  MDD:           {metrics['MDD(%)']:.2f}%")
    print(f"  총 누적수익률: {metrics['총 누적수익률(%)']:.2f}%")
    print(f"  연환산 수익률: {metrics['연환산 수익률(%)']:.2f}%")
    print(f"  승률:          {metrics['승률(%)']:.1f}%")
    print(f"  리밸런싱:      {metrics['리밸런싱 횟수']}회")
    print(f"  평균 Turnover:  {bt_df['turnover'].mean()*100:.2f}%")
    print(f"  평균 거래비용: {bt_df['cost'].mean()*100:.3f}%")
    if 'n_weight_excluded' in bt_df.columns:
        print(f"  평균 비중제한 제외: {bt_df['n_weight_excluded'].mean():.1f}개")

    # ── 월별 수익률 ──
    bt_df_c = bt_df.copy()
    bt_df_c['YM'] = bt_df_c['holding_start'].str[:6]

    monthly_cum = []
    for ym in sorted(bt_df_c['YM'].unique()):
        sub = bt_df_c[bt_df_c['YM'] == ym]
        cum = (1 + sub['net_return']).prod() - 1
        monthly_cum.append({'YM': ym, 'N_rebal': len(sub),
                            'monthly_cum(%)': cum * 100})

    monthly = pd.DataFrame(monthly_cum)

    print(f"\n{'='*65}")
    print(f"  📊 월별 수익률")
    print(f"{'='*65}")
    print(f"  {'월':>8}  {'리밸':>4}  {'월 누적수익률':>14}")
    print(f"  {'-'*8}  {'-'*4}  {'-'*14}")
    for _, row in monthly.iterrows():
        cum = row['monthly_cum(%)']
        sign = '+' if cum > 0 else ''
        print(f"  {row['YM']:>8}  {int(row['N_rebal']):>4}  {sign}{cum:>12.2f}%")

    avg_m = monthly['monthly_cum(%)'].mean()
    print(f"  {'─'*32}")
    print(f"  {'월평균':>8}        {'+' if avg_m > 0 else ''}{avg_m:>12.2f}%")

    # ── 마지막 N번 포트폴리오 비중 ──
    print(f"\n{'='*65}")
    print(f"  📊 마지막 {PRINT_LAST_N_REBAL}회 리밸런싱 포트폴리오 비중")
    print(f"{'='*65}")

    last_n = portfolio_log[-PRINT_LAST_N_REBAL:] if len(portfolio_log) >= PRINT_LAST_N_REBAL else portfolio_log

    for entry in last_n:
        date = entry['date']
        n_th = entry['n_themes']
        n_rm = entry['n_removed']
        n_wex = entry.get('n_weight_excluded', 0)
        wdict = entry['weights']

        print(f"\n  [{date}] 보유 {n_th}개 테마 (중복제거 {n_rm}개, 비중제한 {n_wex}개)")
        print(f"  {'THEME_ID':>10}  {'비중':>8}")
        print(f"  {'-'*10}  {'-'*8}")

        sorted_w = sorted(wdict.items(), key=lambda x: -x[1])
        for theme_id, w in sorted_w:
            print(f"  {theme_id:>10}  {w*100:>7.2f}%")

        top3_w = sum(w for _, w in sorted_w[:3])
        print(f"  {'─'*20}")
        print(f"  Top3 비중합: {top3_w*100:.1f}%  |  합계: {sum(wdict.values())*100:.1f}%")

    # ── Step 5: 구성종목 수익률 분석 (전체 롤링 1회 크롤링 → 최종 집계 출력) ──
    if STOCK_ANALYSIS_ENABLED and portfolio_log:
        print(f"\n{'='*65}")
        print(f"  [Step 5] 구성종목 수익률 분석 (J={J}일 LB → K={K}일 HOLDING)")
        print(f"{'='*65}")
        all_stock_results = run_all_rolling_analysis(
            portfolio_log   = portfolio_log,
            theme_info_df   = theme_info_df,
            theme_stocks_df = theme_stocks_df,
            j               = J,
            crawl_delay     = CRAWL_DELAY,
        )
        aggregate_and_print_rolling_results(all_stock_results)


if __name__ == '__main__':
    main()

# python Theme_model/Theme.py