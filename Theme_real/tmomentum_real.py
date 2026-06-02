# -*- coding: utf-8 -*-
"""
테마 모멘텀 백테스트 (Theme_real용)
- CSV 기반 실시간 데이터 (beta_rebound_detail, beta_all_themes)로 동작
- tmomnetum.py 와 동일 로직이되, DB 의존성 없음
- 구성종목 수 미달 필터만 적용 (제로 리턴 미사용)
- 사전 필터(Lookback 이전 N일 수익률) 미사용
"""

import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings('ignore')

from Theme_real.theme_filter_real import (
    apply_constituent_filter,
    summarize_filter_stats,
)
from Theme_real.settings_real import (
    J, K, TOP_N, REBAL_COST, GAP,
    USE_FILTER, MIN_CONSTITUENTS,
    get_filter_config,
)


# ============================================================
# 백테스트
# ============================================================
def backtest_momentum(adj_pivot: pd.DataFrame,
                      theme_pivot: pd.DataFrame,
                      kospi_pivot: pd.Series,
                      constituent_counts: dict,
                      J: int = J, K: int = K,
                      top_n: int = TOP_N,
                      rebal_cost: float = REBAL_COST,
                      gap: int = GAP,
                      use_filter: bool = USE_FILTER,
                      min_constituents: int = MIN_CONSTITUENTS) -> tuple:
    """
    모멘텀 백테스트 (CSV 데이터 기반)

    Parameters
    ----------
    adj_pivot : 피벗 - (날짜 x 테마) 베타조정수익률
    theme_pivot : 피벗 - (날짜 x 테마) 테마 등락률
    kospi_pivot : 시리즈 - (날짜) 코스피 등락률
    constituent_counts : {theme_no(str) → 구성종목 수}

    Returns
    -------
    bt_df : 백테스트 결과 DataFrame
    filter_log : 필터 통계 리스트
    """
    dates = adj_pivot.index.tolist()
    n_dates = len(dates)

    records = []
    filter_log = []
    prev_winners = set()

    t = J
    while t + gap + K <= n_dates:
        past_window = adj_pivot.iloc[t - J:t]

        # ── 유동성 필터 (구성종목 수만) ──
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

        # 랭킹 가능 테마 부족 시 스킵
        if len(rankable_themes) < min(top_n, len(rankable_themes)):
            t += K
            continue

        actual_top = min(top_n, len(rankable_themes))
        past_cum = (1 + past_window[rankable_themes].fillna(0)).prod() - 1
        ranked = past_cum.sort_values(ascending=False)
        winners = set(ranked.head(actual_top).index.tolist())

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
        })

        prev_winners = winners
        t += K

    return pd.DataFrame(records), filter_log


# ============================================================
# 월별 집계
# ============================================================
def monthly_summary(bt_df: pd.DataFrame) -> pd.DataFrame:
    if bt_df.empty:
        return pd.DataFrame()

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
    if bt_df.empty:
        return {}

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

    return metrics
