# -*- coding: utf-8 -*-
"""
유전 알고리즘 비중 최적화 (Theme_real용)
- Theme.py 와 동일 GA 로직
- 사전 필터(Lookback 이전 N일 수익률 K%) 미사용
- CSV 기반 데이터로 동작
"""

import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings('ignore')

from Theme_real.theme_filter2_real import (
    remove_overlapping_themes,
)
from Theme_real.tmomentum_real import calc_metrics
from Theme_real.settings_real import (
    J, K, TOP_N, REBAL_COST_GA, GAP,
    USE_FILTER, MIN_CONSTITUENTS,
    OVERLAP_THRESHOLD,
    GA_LOOKBACK, GA_POP_SIZE, GA_GENERATIONS,
    GA_MUTATION_RATE, GA_ELITE_RATIO, GA_TOURNAMENT_K,
    MIN_WEIGHT_THRESHOLD,
    PRINT_LAST_N_REBAL,
    get_filter_config, get_ga_config,
)


# ============================================================
# GA: 적합도 함수 (분자=J일 누적 초과수익률, 분모=GA_LOOKBACK일 변동성)
# ============================================================
def fitness_sharpe(weights: np.ndarray,
                   theme_rtn_full: np.ndarray,
                   kospi_rtn_full: np.ndarray,
                   theme_rtn_recent: np.ndarray,
                   kospi_rtn_recent: np.ndarray) -> float:
    """
    적합도 = (최근 J일 누적 초과수익률) / (최근 GA_LOOKBACK일 일별 초과수익률 std)
    """
    # ── 분자: 최근 J일 누적 초과수익률 ──
    port_rtn_recent = theme_rtn_recent @ weights
    excess_recent = port_rtn_recent - kospi_rtn_recent
    cum_excess = (1 + pd.Series(excess_recent)).prod() - 1

    # ── 분모: GA_LOOKBACK일 일별 초과수익률 표준편차 ──
    port_rtn_full = theme_rtn_full @ weights
    excess_full = port_rtn_full - kospi_rtn_full
    std_ex = np.std(excess_full, ddof=1)

    if std_ex < 1e-10:
        return 0.0

    return cum_excess / std_ex


# ============================================================
# GA: 비중 유틸리티
# ============================================================
def normalize_weights(w: np.ndarray, min_weight: float = 0.0) -> np.ndarray:
    """비중 합 = 1, 음수 → 0, 최소 비중 미만 → 0"""
    w = np.maximum(w, 0.0)
    s = w.sum()
    if s < 1e-10:
        return np.ones(len(w)) / len(w)

    w = w / s

    if min_weight > 0:
        w[w < min_weight] = 0.0
        s = w.sum()
        if s < 1e-10:
            return np.ones(len(w)) / len(w)
        w = w / s

    return w


def apply_min_weight_constraint(weights: np.ndarray,
                                 survivors: list,
                                 min_weight: float = MIN_WEIGHT_THRESHOLD) -> tuple:
    """최소 비중 제한 적용"""
    mask = weights >= min_weight
    new_survivors = [s for s, m in zip(survivors, mask) if m]
    new_weights = weights[mask]
    n_excluded = len(survivors) - len(new_survivors)

    if len(new_weights) == 0:
        return survivors, np.ones(len(survivors)) / len(survivors), 0

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
    """GA로 최적 비중 벡터 반환"""
    n_themes = theme_rtn_full.shape[1]

    if n_themes <= 1:
        return np.ones(max(n_themes, 1))

    if theme_rtn_full.shape[0] < 3 or theme_rtn_recent.shape[0] < 1:
        return np.ones(n_themes) / n_themes

    pop = init_population(n_themes, pop_size)
    n_elite = max(1, int(pop_size * elite_ratio))

    best_ever_w = np.ones(n_themes) / n_themes
    best_ever_fit = -np.inf

    for gen in range(generations):
        fitnesses = np.array([
            fitness_sharpe(pop[i],
                           theme_rtn_full, kospi_rtn_full,
                           theme_rtn_recent, kospi_rtn_recent)
            for i in range(pop_size)
        ])

        elite_idx = np.argsort(fitnesses)[-n_elite:]
        elites = pop[elite_idx].copy()

        gen_best_idx = np.argmax(fitnesses)
        if fitnesses[gen_best_idx] > best_ever_fit:
            best_ever_fit = fitnesses[gen_best_idx]
            best_ever_w = pop[gen_best_idx].copy()

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
# 실무용: 단일 시점 최적 포트폴리오 비중 산출
# ============================================================
def compute_optimal_portfolio(
    adj_pivot: pd.DataFrame,
    theme_pivot: pd.DataFrame,
    kospi_pivot: pd.Series,
    constituent_counts: dict,
    constituent_sets: dict,
    J: int = J,
    top_n: int = TOP_N,
    use_filter: bool = USE_FILTER,
    min_constituents: int = MIN_CONSTITUENTS,
    overlap_threshold: float = OVERLAP_THRESHOLD,
    ga_lookback: int = GA_LOOKBACK,
) -> tuple:
    """
    축적된 데이터의 최신 시점 기준으로 최적 포트폴리오 비중을 산출한다.
    백테스팅 없이 현재 시점에서의 비중만 계산.

    Parameters
    ----------
    adj_pivot : (date x theme_no) 베타조정수익률
    theme_pivot : (date x theme_no) 테마 등락률
    kospi_pivot : (date) 코스피 등락률
    constituent_counts : {theme_no → 종목수}
    constituent_sets : {theme_no → set(종목코드)}

    Returns
    -------
    weight_dict : {theme_no: weight}  최적 비중
    filter_stats : dict  필터 통계
    n_removed : int  중복필터 제거 수
    n_weight_excluded : int  비중제한 제거 수
    ranked_info : list[dict]  랭킹 상세 정보 (theme_no, adj_score)
    """
    dates = adj_pivot.index.tolist()
    n_dates = len(dates)

    if n_dates < 1:
        return {}, {}, 0, 0, []

    # ── 최근 J일 또는 가용한 만큼의 데이터로 모멘텀 계산 ──
    actual_J = min(J, n_dates)
    past_window = adj_pivot.iloc[-actual_J:]

    # ── 구성종목 수 필터 ──
    if use_filter and constituent_counts:
        all_themes = past_window.columns.tolist()
        valid_themes = []
        n_filtered = 0
        for th in all_themes:
            n_const = constituent_counts.get(str(th), 0)
            if n_const < min_constituents:
                n_filtered += 1
                continue
            valid_themes.append(th)

        filter_stats = {
            'total': len(all_themes),
            'f1_constituents': n_filtered,
            'passed': len(valid_themes),
        }

        valid_mask = past_window[valid_themes].notna().sum() >= actual_J
        rankable_themes = valid_mask[valid_mask].index.tolist()
    else:
        all_themes = past_window.columns.tolist()
        valid_mask = past_window.notna().sum() >= actual_J
        rankable_themes = valid_mask[valid_mask].index.tolist()
        filter_stats = {
            'total': len(all_themes),
            'f1_constituents': 0,
            'passed': len(rankable_themes),
        }

    if len(rankable_themes) < 2:
        return {}, filter_stats, 0, 0, []

    actual_top = min(top_n, len(rankable_themes))

    # ── J일 누적수익률 기준 Top N 선정 ──
    past_cum = (1 + past_window[rankable_themes].fillna(0)).prod() - 1
    ranked = past_cum.sort_values(ascending=False)
    top_themes = ranked.head(actual_top)
    winners_ranked = top_themes.index.tolist()
    adj_scores = top_themes.to_dict()

    # 랭킹 상세
    ranked_info = [{'theme_no': th, 'adj_score': adj_scores[th]}
                   for th in winners_ranked]

    # ── 종목 중복 필터 ──
    survivors, n_removed = remove_overlapping_themes(
        winners_ranked, adj_scores, constituent_sets, overlap_threshold
    )

    if len(survivors) == 0:
        return {}, filter_stats, n_removed, 0, ranked_info

    # ── GA 비중 최적화 ──
    # 분모: GA_LOOKBACK일 변동성 (가용 범위)
    ga_full_start = max(0, n_dates - ga_lookback)
    ga_theme_full = theme_pivot.iloc[ga_full_start:][survivors].fillna(0).values
    ga_kospi_full = kospi_pivot.iloc[ga_full_start:].fillna(0).values

    # 분자: 최근 J일 누적 초과수익률
    ga_recent_start = max(0, n_dates - actual_J)
    ga_theme_recent = theme_pivot.iloc[ga_recent_start:][survivors].fillna(0).values
    ga_kospi_recent = kospi_pivot.iloc[ga_recent_start:].fillna(0).values

    if ga_theme_full.shape[0] < 3 or ga_theme_recent.shape[0] < 1:
        weights = np.ones(len(survivors)) / len(survivors)
    else:
        weights = ga_optimize_weights(
            ga_theme_full, ga_kospi_full,
            ga_theme_recent, ga_kospi_recent,
        )

    # ── 최소 비중 제한 ──
    survivors, weights, n_weight_excluded = apply_min_weight_constraint(
        weights, survivors, min_weight=MIN_WEIGHT_THRESHOLD
    )

    if len(survivors) == 0:
        return {}, filter_stats, n_removed, n_weight_excluded, ranked_info

    weight_dict = {th: round(w, 6) for th, w in zip(survivors, weights)}
    return weight_dict, filter_stats, n_removed, n_weight_excluded, ranked_info


# ============================================================
# 백테스트 (중복필터 + GA 비중) — 테스트용 보존
# ============================================================
def backtest_ga(
    adj_pivot: pd.DataFrame,
    theme_pivot: pd.DataFrame,
    kospi_pivot: pd.Series,
    constituent_counts: dict,
    constituent_sets: dict,
    J: int = J, K: int = K,
    top_n: int = TOP_N,
    rebal_cost: float = REBAL_COST_GA,
    gap: int = GAP,
    use_filter: bool = USE_FILTER,
    min_constituents: int = MIN_CONSTITUENTS,
    overlap_threshold: float = OVERLAP_THRESHOLD,
    ga_lookback: int = GA_LOOKBACK,
) -> tuple:
    """
    Returns
    -------
    bt_df : 백테스트 결과
    filter_log : 유동성 필터 로그
    portfolio_log : 포트폴리오 비중 로그
    """
    dates = adj_pivot.index.tolist()
    n_dates = len(dates)

    records = []
    filter_log = []
    portfolio_log = []
    prev_winners = set()
    prev_weights = {}

    t = J
    while t + gap + K <= n_dates:
        past_window = adj_pivot.iloc[t - J:t]

        # ── 구성종목 수 필터 ──
        if use_filter and constituent_counts:
            all_themes_at_t = past_window.columns.tolist()
            valid_themes = []
            n_filtered = 0
            for th in all_themes_at_t:
                n_const = constituent_counts.get(str(th), 0)
                if n_const < min_constituents:
                    n_filtered += 1
                    continue
                valid_themes.append(th)

            fstats = {
                'total': len(all_themes_at_t),
                'f1_constituents': n_filtered,
                'passed': len(valid_themes),
            }
            filter_log.append(fstats)

            valid_mask = past_window[valid_themes].notna().sum() >= J
            rankable_themes = valid_mask[valid_mask].index.tolist()
        else:
            valid_mask = past_window.notna().sum() >= J
            rankable_themes = valid_mask[valid_mask].index.tolist()

        if len(rankable_themes) < min(top_n, 2):
            t += K
            continue

        actual_top = min(top_n, len(rankable_themes))

        # ── AdjRtn 기준 Top N 선정 ──
        past_cum = (1 + past_window[rankable_themes].fillna(0)).prod() - 1
        ranked = past_cum.sort_values(ascending=False)
        top_themes = ranked.head(actual_top)
        winners_ranked = top_themes.index.tolist()
        adj_scores = top_themes.to_dict()

        # ── 종목 중복 필터 ──
        survivors, n_removed = remove_overlapping_themes(
            winners_ranked, adj_scores, constituent_sets, overlap_threshold
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

        # ── 최소 비중 제한 ──
        survivors, weights, n_weight_excluded = apply_min_weight_constraint(
            weights, survivors, min_weight=MIN_WEIGHT_THRESHOLD
        )

        if len(survivors) == 0:
            t += K
            continue

        weight_dict = {th: round(w, 4) for th, w in zip(survivors, weights)}
        portfolio_log.append({
            'date': dates[t - 1],
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
            ) / 2
            cost = turnover * rebal_cost * 2   # 왕복 (매도 + 매수)
        else:
            cost = rebal_cost                  # 첫 회차: 매수만 편도 1회
            turnover = 1.0

        # ── 성과 측정 (가중 수익률) ──
        future_start = t + gap
        future_end = t + gap + K
        if future_end > n_dates:
            break

        future_dates = adj_pivot.index[future_start:future_end]

        future_theme_rtn = theme_pivot.loc[future_dates, survivors].fillna(0)
        weighted_rtn = (future_theme_rtn.values @ weights)
        kospi_rtn = kospi_pivot.loc[future_dates].fillna(0).values

        excess_rtn = weighted_rtn - kospi_rtn

        gross_cum = (1 + pd.Series(excess_rtn)).prod() - 1
        net_cum = gross_cum - cost
        theme_cum = (1 + pd.Series(weighted_rtn)).prod() - 1
        kospi_cum = (1 + pd.Series(kospi_rtn)).prod() - 1

        records.append({
            'rebal_date': dates[t],
            'holding_start': dates[future_start] if future_start < n_dates else dates[-1],
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
        prev_weights = weight_dict
        t += K

    return pd.DataFrame(records), filter_log, portfolio_log
