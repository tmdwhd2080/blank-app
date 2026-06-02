# -*- coding: utf-8 -*-
"""
combined_strategy_backtest.py

통합 전략 백테스팅:
  1. Theme.py backtest_ga() -> portfolio_log에서 선택된 테마 획득
  2. 각 테마의 일치율(구성종목 중 당일 수익률 양(+) 비율) 계산
  3. 일치율 70~100% 또는 0~30% 테마 중 상위 2개 선택
  4. 선택 테마 구성종목 중 당일 수익률 -3% 이하 종목 선별 (없으면 -1% 이하, 그래도 없으면 전체)
  5. 선별 종목 동일비중, 익일 수익률로 성과 측정
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
from Theme_model.settings import (
    KOSPI_EXCEL_PATH,
    START_DATE, END_DATE,
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
    _trading_date_buffer_start,
)
from Theme_model.tmomnetum import calc_metrics


# ============================================================
# 전역 가격 캐시
# ============================================================
_price_cache: dict = {}  # {STK_CODE: pd.Series(index=YYYYMMDD, values=close)}


def crawl_and_cache(stk_codes: list, start_date: str, end_date: str,
                    delay: float = 0.05) -> None:
    """종목 종가를 크롤링하여 전역 캐시에 저장. 이미 캐시된 종목은 스킵."""
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
            pass
        time.sleep(delay)


def get_close(code: str, date: str) -> float:
    """캐시에서 종가 반환."""
    if code not in _price_cache:
        return np.nan
    s = _price_cache[code]
    return s[date] if date in s.index else np.nan


def find_prev_date(code: str, date: str) -> str:
    """캐시에서 date 직전 거래일."""
    if code not in _price_cache:
        return None
    dates = sorted(_price_cache[code].index.tolist())
    cands = [d for d in dates if d < date]
    return cands[-1] if cands else None


def find_next_date(code: str, date: str) -> str:
    """캐시에서 date 직후 거래일."""
    if code not in _price_cache:
        return None
    dates = sorted(_price_cache[code].index.tolist())
    cands = [d for d in dates if d > date]
    return cands[0] if cands else None


# ============================================================
# 종목 당일/익일 수익률 계산
# ============================================================
def compute_day_and_next_returns(stk_codes: list, rebal_date: str) -> pd.DataFrame:
    """
    각 종목의 당일 수익률, 익일 수익률 계산.
    Returns: DataFrame [STK_CODE, day_return, next_day_return]
    """
    rows = []
    for code in stk_codes:
        close_today = get_close(code, rebal_date)
        if np.isnan(close_today) or close_today <= 0:
            continue

        prev_d = find_prev_date(code, rebal_date)
        next_d = find_next_date(code, rebal_date)
        if prev_d is None or next_d is None:
            continue

        close_prev = get_close(code, prev_d)
        close_next = get_close(code, next_d)
        if np.isnan(close_prev) or close_prev <= 0:
            continue
        if np.isnan(close_next) or close_next <= 0:
            continue

        day_return = (close_today - close_prev) / close_prev
        next_day_return = (close_next - close_today) / close_today

        rows.append({
            'STK_CODE': code,
            'day_return': day_return,
            'next_day_return': next_day_return,
        })

    return pd.DataFrame(rows)


# ============================================================
# 일치율 계산 (테마별)
# ============================================================
def compute_agreement_rates(stk_codes_by_theme: dict,
                            returns_df: pd.DataFrame) -> dict:
    """
    테마별 일치율 = (당일 수익률 > 0인 종목 수) / (전체 종목 수)

    Args:
        stk_codes_by_theme: {THEME_ID: [STK_CODE, ...]}
        returns_df: DataFrame [STK_CODE, day_return, next_day_return]

    Returns:
        {THEME_ID: agreement_rate}
    """
    result = {}
    for theme_id, codes in stk_codes_by_theme.items():
        sub = returns_df[returns_df['STK_CODE'].isin(codes)]
        if sub.empty:
            result[theme_id] = np.nan
            continue
        n_up = (sub['day_return'] > 0).sum()
        result[theme_id] = n_up / len(sub)
    return result


# ============================================================
# 테마 선택 (일치율 기준 상위 2개)
# ============================================================
def select_themes_by_agreement(agreement_rates: dict, n_select: int = 2) -> list:
    """
    일치율 70~100% 또는 0~30% 테마 중 상위 2개 선택.
    우선순위: 70~100% > 0~30%
    부족 시 전체에서 일치율 상/하위로 채움.
    """
    valid = {k: v for k, v in agreement_rates.items() if not np.isnan(v)}
    if not valid:
        return []

    # 70~100% 구간
    high_group = {k: v for k, v in valid.items() if v >= 0.70}
    # 0~30% 구간
    low_group = {k: v for k, v in valid.items() if v < 0.30}

    selected = []

    # 1) 70~100% 구간에서 일치율 높은 순 선택
    if high_group:
        sorted_high = sorted(high_group.items(), key=lambda x: -x[1])
        for tid, _ in sorted_high:
            if len(selected) >= n_select:
                break
            selected.append(tid)

    # 2) 부족하면 0~30% 구간에서 일치율 낮은 순 선택
    if len(selected) < n_select and low_group:
        sorted_low = sorted(low_group.items(), key=lambda x: x[1])
        for tid, _ in sorted_low:
            if len(selected) >= n_select:
                break
            if tid not in selected:
                selected.append(tid)

    # 3) 그래도 부족하면 전체에서 채움 (상위/하위)
    if len(selected) < n_select:
        remaining = {k: v for k, v in valid.items() if k not in selected}
        if remaining:
            sorted_all = sorted(remaining.items(), key=lambda x: -x[1])
            # 일치율이 가장 높은 것과 가장 낮은 것 번갈아 추가
            low_idx = len(sorted_all) - 1
            high_idx = 0
            use_high = True
            while len(selected) < n_select and high_idx <= low_idx:
                if use_high:
                    selected.append(sorted_all[high_idx][0])
                    high_idx += 1
                else:
                    selected.append(sorted_all[low_idx][0])
                    low_idx -= 1
                use_high = not use_high

    return selected[:n_select]


# ============================================================
# 종목 선별 (당일 수익률 기준)
# ============================================================
def select_stocks_by_return(returns_df: pd.DataFrame,
                            stk_codes: list) -> list:
    """
    선택된 테마 구성종목 중:
    1) 당일 -3% 이하 종목
    2) 없으면 -1% 이하 종목
    3) 그래도 없으면 전체 종목
    """
    sub = returns_df[returns_df['STK_CODE'].isin(stk_codes)].copy()
    if sub.empty:
        return stk_codes  # 수익률 데이터 없으면 전체

    # -3% 이하
    severe = sub[sub['day_return'] <= -0.03]['STK_CODE'].tolist()
    if severe:
        return severe

    # -1% 이하
    mild = sub[sub['day_return'] <= -0.01]['STK_CODE'].tolist()
    if mild:
        return mild

    # 전체
    return sub['STK_CODE'].tolist()


# ============================================================
# KOSPI 익일 수익률 (캐시에서 계산)
# ============================================================
def get_kospi_next_return(kospi_pivot: pd.Series, rebal_date: str) -> float:
    """kospi_pivot에서 rebal_date 다음 거래일의 수익률 반환."""
    dates = sorted(kospi_pivot.index.tolist())
    cands = [d for d in dates if d > rebal_date]
    if not cands:
        return np.nan
    next_d = cands[0]
    return kospi_pivot.get(next_d, np.nan)


# ============================================================
# 메인 백테스팅
# ============================================================
def main():
    print("=" * 70)
    print("  통합 전략 백테스팅: 일치율 기반 테마 선택 + 종목 수익률 범위 선별")
    print(f"  J={J} / K={K} / TOP_N={TOP_N}")
    print(f"  기간: {START_DATE} ~ {END_DATE}")
    print(f"  테마 선택: 일치율 70~100% 또는 0~30% 구간 우선, 상위 2개")
    print(f"  종목 선별: 당일 -3% 이하 > -1% 이하 > 전체")
    print(f"  비중: 동일비중 / 보유: 익일 1일")
    print("=" * 70)

    # ── Step 1: 데이터 로드 ──
    print("\n[Step 1] 데이터 로드")
    kospi_df = load_kospi_returns(KOSPI_EXCEL_PATH, START_DATE, END_DATE)

    db = MSSQL(database=DBConfig.TRSTDEV_DB)
    try:
        theme_df = get_theme_returns(db, START_DATE, END_DATE)
    finally:
        db.close()

    db2 = MSSQL(database=DBConfig.TRSTDEV_DB)
    try:
        theme_info_df = get_theme_info(db2)
    finally:
        db2.close()

    db3 = MSSQL(database=DBConfig.TRSTDEV_DB)
    try:
        theme_stocks_df = get_theme_stocks(db3)
    finally:
        db3.close()

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

    filter_config = get_filter_config()

    # ── Step 3: 기존 Theme.py 백테스트 실행 ──
    print("\n[Step 3] backtest_ga() 실행")
    bt_df_orig, filter_log, portfolio_log = backtest_ga(
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
        print("  portfolio_log 비어있음. 종료.")
        return

    n_rollings = len(portfolio_log)
    print(f"  리밸런싱 횟수: {n_rollings}")

    # ── Step 4: 전체 종목 코드 수집 + 1회 크롤링 ──
    print(f"\n[Step 4] 구성종목 수집 및 pykrx 크롤링")

    # 사전에 theme_info_df에서 전체 ID->Name 매핑 구축 (월 캐싱)
    # theme_stocks_df도 전처리하여 빠른 룩업 가능하게
    print("  구성종목 매핑 구축 중...")

    # theme_info: THEME_ID -> THEME_NAME (UPDATED_DT별)
    ti = theme_info_df.copy()
    ti['THEME_ID'] = ti['THEME_ID'].astype(str).str.strip()
    ti['THEME_NAME'] = ti['THEME_NAME'].astype(str).str.strip()

    # theme_stocks: 전처리
    ts = theme_stocks_df.copy()
    ts['THEME_NAME'] = ts['THEME_NAME'].astype(str).str.strip()
    ts['STK_CODE'] = ts['STK_CODE'].astype(str).str.strip().str.zfill(6)
    ts['UPDATED_DT'] = ts['UPDATED_DT'].astype(str)

    # 월별 캐시: {month_str: {theme_name: [stk_codes]}}
    _month_cache_id2name = {}  # {month: {theme_id: theme_name}}
    _month_cache_stocks = {}   # {month: {theme_name: [stk_codes]}}

    def get_id_to_name_cached(theme_ids, as_of_date):
        month = as_of_date[:6]
        if month not in _month_cache_id2name:
            sub = ti[ti['UPDATED_DT'] <= as_of_date]
            if sub.empty:
                _month_cache_id2name[month] = {}
            else:
                latest = (sub.sort_values('UPDATED_DT', ascending=False)
                           .groupby('THEME_ID', as_index=False).first())
                _month_cache_id2name[month] = dict(zip(latest['THEME_ID'], latest['THEME_NAME']))
        cache = _month_cache_id2name[month]
        return {tid: cache[tid] for tid in theme_ids if tid in cache}

    def get_theme_stk_codes_cached(theme_names, as_of_date):
        month = as_of_date[:6]
        if month not in _month_cache_stocks:
            sub = ts[ts['UPDATED_DT'] <= as_of_date]
            if sub.empty:
                _month_cache_stocks[month] = {}
            else:
                # 각 테마의 최신 UPDATED_DT만 유지
                max_dt = sub.groupby('THEME_NAME')['UPDATED_DT'].max().reset_index()
                max_dt.columns = ['THEME_NAME', 'max_dt']
                sub2 = sub.merge(max_dt, on='THEME_NAME')
                sub2 = sub2[sub2['UPDATED_DT'] == sub2['max_dt']]
                result = {}
                for tn, grp in sub2.groupby('THEME_NAME'):
                    result[tn] = grp['STK_CODE'].unique().tolist()
                _month_cache_stocks[month] = result
        cache = _month_cache_stocks[month]
        return {tn: cache[tn] for tn in theme_names if tn in cache}

    rolling_meta = []
    all_codes = set()
    min_rebal = None
    max_holding = None

    for idx_e, entry in enumerate(portfolio_log):
        rebal_date = entry['date']
        holding_date = entry['holding_end']
        weight_dict = entry['weights']
        theme_ids = list(weight_dict.keys())

        id_to_name = get_id_to_name_cached(theme_ids, rebal_date)
        if not id_to_name:
            rolling_meta.append((rebal_date, holding_date, weight_dict, {}))
            continue

        name_to_id = {v: k for k, v in id_to_name.items()}
        theme_names = list(id_to_name.values())

        stocks_by_name = get_theme_stk_codes_cached(theme_names, rebal_date)
        if not stocks_by_name:
            rolling_meta.append((rebal_date, holding_date, weight_dict, {}))
            continue

        # 테마ID별 종목 코드
        theme_stk_map = {}
        for tn, codes in stocks_by_name.items():
            tid = name_to_id.get(tn)
            if tid:
                theme_stk_map[tid] = codes
                all_codes.update(codes)

        if min_rebal is None or rebal_date < min_rebal:
            min_rebal = rebal_date
        if max_holding is None or holding_date > max_holding:
            max_holding = holding_date

        rolling_meta.append((rebal_date, holding_date, weight_dict, theme_stk_map))

        if (idx_e + 1) % 100 == 0:
            print(f"    메타 수집 진행: {idx_e + 1}/{n_rollings}")

    print(f"  메타 수집 완료: {n_rollings}개")

    if not all_codes or min_rebal is None:
        print("  분석할 종목 없음. 종료.")
        return

    print(f"  전체 고유 종목: {len(all_codes)}개")
    print(f"  기간: {min_rebal} ~ {max_holding}")

    # 크롤링 (전일/익일 포함 버퍼)
    buffer_start = (datetime.strptime(min_rebal, '%Y%m%d') - timedelta(days=30)).strftime('%Y%m%d')
    buffer_end = (datetime.strptime(max_holding, '%Y%m%d') + timedelta(days=15)).strftime('%Y%m%d')
    crawl_and_cache(sorted(all_codes), buffer_start, buffer_end, delay=CRAWL_DELAY)

    # ── Step 5: 통합 전략 백테스팅 ──
    print(f"\n[Step 5] 통합 전략 백테스팅 ({n_rollings}개 리밸런싱)")

    combined_records = []
    selection_log = []

    for i, (rebal_date, holding_date, weight_dict, theme_stk_map) in enumerate(rolling_meta):
        if not theme_stk_map:
            continue

        # 모든 구성종목의 당일/익일 수익률 계산
        all_stk_codes = []
        for codes in theme_stk_map.values():
            all_stk_codes.extend(codes)
        all_stk_codes = list(set(all_stk_codes))

        returns_df = compute_day_and_next_returns(all_stk_codes, rebal_date)
        if returns_df.empty:
            continue

        # 일치율 계산
        agreement_rates = compute_agreement_rates(theme_stk_map, returns_df)

        # 테마 선택 (일치율 기준 상위 2개)
        selected_themes = select_themes_by_agreement(agreement_rates, n_select=2)
        if not selected_themes:
            continue

        # 선택된 테마의 구성종목 합치기
        selected_stk_codes = []
        for tid in selected_themes:
            if tid in theme_stk_map:
                selected_stk_codes.extend(theme_stk_map[tid])
        selected_stk_codes = list(set(selected_stk_codes))

        if not selected_stk_codes:
            continue

        # 종목 선별 (당일 수익률 기준)
        final_stocks = select_stocks_by_return(returns_df, selected_stk_codes)
        if not final_stocks:
            continue

        # 선별된 종목의 익일 수익률 (동일비중)
        final_returns = returns_df[returns_df['STK_CODE'].isin(final_stocks)]
        if final_returns.empty:
            continue

        port_next_return = final_returns['next_day_return'].mean()

        # KOSPI 익일 수익률
        kospi_next = get_kospi_next_return(kospi_pivot, rebal_date)
        if np.isnan(kospi_next):
            kospi_next = 0.0

        # 어떤 필터가 적용되었는지 기록
        n_severe = len(final_returns[final_returns['STK_CODE'].isin(
            returns_df[returns_df['day_return'] <= -0.03]['STK_CODE']
        )])
        n_mild = len(final_returns[final_returns['STK_CODE'].isin(
            returns_df[(returns_df['day_return'] > -0.03) & (returns_df['day_return'] <= -0.01)]['STK_CODE']
        )])

        if n_severe > 0:
            filter_type = '-3%이하'
        elif n_mild > 0:
            filter_type = '-1%이하'
        else:
            filter_type = '전체'

        combined_records.append({
            'rebal_date': rebal_date,
            'n_themes_selected': len(selected_themes),
            'n_stocks_selected': len(final_stocks),
            'filter_type': filter_type,
            'port_return': port_next_return,
            'kospi_return': kospi_next,
            'excess_return': port_next_return - kospi_next,
        })

        selection_log.append({
            'rebal_date': rebal_date,
            'selected_themes': selected_themes,
            'agreement_rates': {tid: agreement_rates.get(tid, np.nan) for tid in selected_themes},
            'n_stocks': len(final_stocks),
            'filter_type': filter_type,
        })

        if (i + 1) % 100 == 0 or (i + 1) == n_rollings:
            print(f"  진행: {i + 1}/{n_rollings}")

    if not combined_records:
        print("  통합 전략 결과 없음. 종료.")
        return

    combined_df = pd.DataFrame(combined_records)
    print(f"\n  통합 전략 리밸런싱: {len(combined_df)}회")

    # ── Step 6: 성과 지표 산출 ──
    print(f"\n[Step 6] 성과 지표 산출")

    # --- 통합 전략 ---
    net_returns = combined_df['port_return'].values
    cum_series = (1 + pd.Series(net_returns)).cumprod()
    total_cum = cum_series.iloc[-1] - 1
    rolling_max = cum_series.cummax()
    drawdown = (cum_series - rolling_max) / rolling_max
    mdd = drawdown.min()
    periods_per_year = 252 / K
    n_years = len(net_returns) / periods_per_year
    annual_return = (1 + total_cum) ** (1 / n_years) - 1 if n_years > 0 else 0
    avg_ret = np.mean(net_returns)
    std_ret = np.std(net_returns, ddof=1)
    sharpe = (avg_ret / std_ret) * np.sqrt(periods_per_year) if std_ret > 0 else 0
    win_rate = (net_returns > 0).mean()

    combined_metrics = {
        '총 누적수익률(%)': round(total_cum * 100, 2),
        '연환산 수익률(%)': round(annual_return * 100, 2),
        'Sharpe': round(sharpe, 3),
        'MDD(%)': round(mdd * 100, 2),
        '승률(%)': round(win_rate * 100, 1),
        '리밸런싱 횟수': len(net_returns),
    }

    # --- 기존 전략 ---
    orig_metrics = calc_metrics(bt_df_orig, K) if not bt_df_orig.empty else {}

    # --- KOSPI ---
    kospi_returns = combined_df['kospi_return'].values
    kospi_cum_series = (1 + pd.Series(kospi_returns)).cumprod()
    kospi_total_cum = kospi_cum_series.iloc[-1] - 1
    kospi_max = kospi_cum_series.cummax()
    kospi_dd = (kospi_cum_series - kospi_max) / kospi_max
    kospi_mdd = kospi_dd.min()
    kospi_annual = (1 + kospi_total_cum) ** (1 / n_years) - 1 if n_years > 0 else 0
    kospi_avg = np.mean(kospi_returns)
    kospi_std = np.std(kospi_returns, ddof=1)
    kospi_sharpe = (kospi_avg / kospi_std) * np.sqrt(periods_per_year) if kospi_std > 0 else 0
    kospi_wr = (kospi_returns > 0).mean()

    kospi_metrics = {
        '총 누적수익률(%)': round(kospi_total_cum * 100, 2),
        '연환산 수익률(%)': round(kospi_annual * 100, 2),
        'Sharpe': round(kospi_sharpe, 3),
        'MDD(%)': round(kospi_mdd * 100, 2),
        '승률(%)': round(kospi_wr * 100, 1),
        '리밸런싱 횟수': len(kospi_returns),
    }

    # ── 출력: 비교 테이블 ──
    print(f"\n{'=' * 75}")
    print(f"  성과 비교 테이블")
    print(f"{'=' * 75}")
    print(f"  {'지표':>18}  {'통합전략':>12}  {'기존전략(GA)':>12}  {'KOSPI':>12}")
    print(f"  {'-' * 18}  {'-' * 12}  {'-' * 12}  {'-' * 12}")

    metrics_keys = ['총 누적수익률(%)', '연환산 수익률(%)', 'Sharpe', 'MDD(%)', '승률(%)', '리밸런싱 횟수']
    for key in metrics_keys:
        c_val = combined_metrics.get(key, 'N/A')
        o_val = orig_metrics.get(key, 'N/A')
        k_val = kospi_metrics.get(key, 'N/A')

        if isinstance(c_val, (int, float)):
            c_str = f"{c_val:>12.2f}" if not isinstance(c_val, int) else f"{c_val:>12}"
        else:
            c_str = f"{c_val:>12}"

        if isinstance(o_val, (int, float)):
            o_str = f"{o_val:>12.2f}" if not isinstance(o_val, int) else f"{o_val:>12}"
        else:
            o_str = f"{o_val:>12}"

        if isinstance(k_val, (int, float)):
            k_str = f"{k_val:>12.2f}" if not isinstance(k_val, int) else f"{k_val:>12}"
        else:
            k_str = f"{k_val:>12}"

        print(f"  {key:>18}  {c_str}  {o_str}  {k_str}")

    # ── 종목 선별 필터 사용 통계 ──
    print(f"\n{'=' * 75}")
    print(f"  종목 선별 필터 사용 통계")
    print(f"{'=' * 75}")
    filter_counts = combined_df['filter_type'].value_counts()
    for ft, cnt in filter_counts.items():
        print(f"  {ft}: {cnt}회 ({cnt / len(combined_df) * 100:.1f}%)")
    print(f"  평균 선별 종목 수: {combined_df['n_stocks_selected'].mean():.1f}개")

    # ── 월별 수익률 ──
    combined_df_c = combined_df.copy()
    combined_df_c['YM'] = combined_df_c['rebal_date'].str[:6]

    monthly_cum = []
    for ym in sorted(combined_df_c['YM'].unique()):
        sub = combined_df_c[combined_df_c['YM'] == ym]
        cum = (1 + sub['port_return']).prod() - 1
        kospi_cum_m = (1 + sub['kospi_return']).prod() - 1
        monthly_cum.append({
            'YM': ym,
            'N_rebal': len(sub),
            '전략(%)': cum * 100,
            'KOSPI(%)': kospi_cum_m * 100,
            '초과(%)': (cum - kospi_cum_m) * 100,
        })

    monthly_df = pd.DataFrame(monthly_cum)

    print(f"\n{'=' * 75}")
    print(f"  월별 수익률")
    print(f"{'=' * 75}")
    print(f"  {'월':>8}  {'리밸':>4}  {'전략':>10}  {'KOSPI':>10}  {'초과':>10}")
    print(f"  {'-' * 8}  {'-' * 4}  {'-' * 10}  {'-' * 10}  {'-' * 10}")

    for _, row in monthly_df.iterrows():
        s_val = row['전략(%)']
        k_val = row['KOSPI(%)']
        e_val = row['초과(%)']
        s_sign = '+' if s_val > 0 else ''
        k_sign = '+' if k_val > 0 else ''
        e_sign = '+' if e_val > 0 else ''
        print(f"  {row['YM']:>8}  {int(row['N_rebal']):>4}  "
              f"{s_sign}{s_val:>8.2f}%  {k_sign}{k_val:>8.2f}%  {e_sign}{e_val:>8.2f}%")

    avg_s = monthly_df['전략(%)'].mean()
    avg_k = monthly_df['KOSPI(%)'].mean()
    avg_e = monthly_df['초과(%)'].mean()
    print(f"  {'─' * 52}")
    print(f"  {'월평균':>8}        "
          f"{'+' if avg_s > 0 else ''}{avg_s:>8.2f}%  "
          f"{'+' if avg_k > 0 else ''}{avg_k:>8.2f}%  "
          f"{'+' if avg_e > 0 else ''}{avg_e:>8.2f}%")

    # ── CSV 저장 ──
    output_dir = os.path.dirname(os.path.abspath(__file__))
    csv_path = os.path.join(output_dir, 'combined_strategy_result.csv')

    # 일별 결과 저장
    combined_df.to_csv(csv_path, index=False, encoding='utf-8-sig')
    print(f"\n  일별 결과 CSV 저장: {csv_path}")

    # 월별 결과 저장
    monthly_csv = os.path.join(output_dir, 'combined_strategy_monthly.csv')
    monthly_df.to_csv(monthly_csv, index=False, encoding='utf-8-sig')
    print(f"  월별 결과 CSV 저장: {monthly_csv}")

    # ── 마지막 5회 선택 상세 ──
    print(f"\n{'=' * 75}")
    print(f"  마지막 5회 리밸런싱 상세")
    print(f"{'=' * 75}")
    last_n = selection_log[-5:] if len(selection_log) >= 5 else selection_log
    for entry in last_n:
        print(f"\n  [{entry['rebal_date']}] 선택 테마: {entry['selected_themes']}")
        for tid in entry['selected_themes']:
            ar = entry['agreement_rates'].get(tid, np.nan)
            print(f"    THEME_ID {tid}: 일치율 {ar * 100:.1f}%")
        print(f"    종목 {entry['n_stocks']}개 선별 (필터: {entry['filter_type']})")

    print(f"\n{'=' * 75}")
    print(f"  백테스팅 완료")
    print(f"{'=' * 75}")


if __name__ == '__main__':
    main()

# python Theme_model/combined_strategy_backtest.py
