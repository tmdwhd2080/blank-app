# -*- coding: utf-8 -*-
"""
종목 중복 필터 (Theme_real용)
- theme_filter2.py 와 동일 로직
- CSV 기반 데이터에서 구성종목 set를 직접 구축
- 겹침 비율 >= OVERLAP_THRESHOLD 인 쌍의 낮은 쪽 제거
"""

import pandas as pd
import numpy as np
from itertools import combinations
import warnings
warnings.filterwarnings('ignore')

from Theme_real.tmomentum_real import calc_metrics
from Theme_real.settings_real import (
    J, K, TOP_N, REBAL_COST_OVERLAP, GAP,
    USE_FILTER, MIN_CONSTITUENTS,
    OVERLAP_THRESHOLD,
    get_filter_config,
)


# ============================================================
# 종목 중복 맵 구축 (CSV 기반)
# ============================================================
def build_constituent_sets_from_csv(detail_df: pd.DataFrame) -> dict:
    """
    beta_rebound_detail CSV에서 테마별 구성종목 set을 구축한다.

    Parameters
    ----------
    detail_df : CSV 데이터프레임 (theme_no, 종목코드 포함)

    Returns
    -------
    dict : {theme_no(str) → set(종목코드)}
    """
    if detail_df.empty:
        return {}

    code_col = '종목코드' if '종목코드' in detail_df.columns else '종목명'

    result = {}
    for theme_no, group in detail_df.groupby('theme_no'):
        stk_set = set(group[code_col].dropna().astype(str).unique())
        result[str(theme_no)] = stk_set

    return result


# ============================================================
# 종목 중복 필터: 겹침 비율 >= threshold 이면 AdjRtn 낮은 쪽 제거
# ============================================================
def remove_overlapping_themes(winners_ranked: list,
                              adj_scores: dict,
                              constituent_sets: dict,
                              overlap_threshold: float = OVERLAP_THRESHOLD) -> tuple:
    """
    Parameters
    ----------
    winners_ranked : AdjRtn 내림차순 정렬된 theme_no 리스트
    adj_scores : {theme_no: 누적 AdjRtn}
    constituent_sets : {theme_no: set(종목코드)}
    overlap_threshold : 겹침 비율 임계값

    Returns
    -------
    survivors : 필터 통과 테마 리스트
    n_removed : 제거된 테마 수
    """
    removed = set()

    for i, theme_a in enumerate(winners_ranked):
        if theme_a in removed:
            continue
        set_a = constituent_sets.get(str(theme_a))
        if not set_a:
            continue

        for theme_b in winners_ranked[i + 1:]:
            if theme_b in removed:
                continue
            set_b = constituent_sets.get(str(theme_b))
            if not set_b:
                continue

            intersection = len(set_a & set_b)
            min_size = min(len(set_a), len(set_b))
            if min_size == 0:
                continue

            overlap_ratio = intersection / min_size
            if overlap_ratio >= overlap_threshold:
                removed.add(theme_b)

    survivors = [th for th in winners_ranked if th not in removed]
    return survivors, len(removed)


# ============================================================
# 백테스트 (종목 중복 필터 추가)
# ============================================================
def backtest_momentum_with_overlap(
    adj_pivot: pd.DataFrame,
    theme_pivot: pd.DataFrame,
    kospi_pivot: pd.Series,
    constituent_counts: dict,
    constituent_sets: dict,
    J: int = J, K: int = K,
    top_n: int = TOP_N,
    rebal_cost: float = REBAL_COST_OVERLAP,
    gap: int = GAP,
    use_filter: bool = USE_FILTER,
    min_constituents: int = MIN_CONSTITUENTS,
    overlap_threshold: float = OVERLAP_THRESHOLD,
) -> tuple:
    """
    Returns
    -------
    bt_df : 백테스트 결과
    filter_log : 유동성 필터 로그
    overlap_log : 종목 중복 필터 로그
    """
    dates = adj_pivot.index.tolist()
    n_dates = len(dates)

    records = []
    filter_log = []
    overlap_log = []
    prev_winners = set()

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
        overlap_log.append({
            'date': dates[t - 1],
            'before': len(winners_ranked),
            'removed': n_removed,
            'after': len(survivors),
        })

        winners = set(survivors)
        n_active = len(winners)
        if n_active == 0:
            t += K
            continue

        # ── 거래비용 ──
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
        if future_end > n_dates:
            break

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
            'holding_start': dates[future_start] if future_start < n_dates else dates[-1],
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
# 월별 집계 (n_themes 포함)
# ============================================================
def monthly_summary_overlap(bt_df: pd.DataFrame) -> pd.DataFrame:
    if bt_df.empty:
        return pd.DataFrame()

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
