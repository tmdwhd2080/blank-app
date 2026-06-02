# -*- coding: utf-8 -*-
"""
테마 유동성 필터 모듈 (theme_filter.py)
- 2단계 유동성 필터 적용
  F1: 구성 종목 수 미달
  F2: 제로 리턴 비율 과다
"""

import pandas as pd
import numpy as np


# THEME_INFO 조회
def get_theme_info(db) -> pd.DataFrame:
    """
    THEME_INFO: THEME_ID(int), THEME_NAME(str), UPDATED_DT
    """
    query = """
        SELECT THEME_ID, THEME_NAME, UPDATED_DT
        FROM THEME_INFO
        ORDER BY THEME_NAME, UPDATED_DT
    """
    df = db.SELECT(query)
    if df is None or len(df) == 0:
        print("  ⚠️ THEME_INFO 데이터 없음")
        return pd.DataFrame()

    df['THEME_ID'] = df['THEME_ID'].astype(str).str.strip()
    df['THEME_NAME'] = df['THEME_NAME'].astype(str).str.strip()
    df['UPDATED_DT'] = pd.to_datetime(df['UPDATED_DT']).dt.strftime('%Y%m%d')

    print(f"  THEME_INFO: {len(df)} rows, "
          f"{df['THEME_ID'].nunique()}개 테마ID, "
          f"{df['THEME_NAME'].nunique()}개 테마명")
    return df


# THEME_STOCKS 조회
def get_theme_stocks(db) -> pd.DataFrame:
    """
    THEME_STOCKS: THEME_NAME(str), STK_NAME(str), STK_CODE, UPDATED_DT
    """
    query = """
        SELECT THEME_NAME, STK_NAME, STK_CODE, UPDATED_DT
        FROM THEME_STOCKS
        ORDER BY THEME_NAME, UPDATED_DT
    """
    df = db.SELECT(query)
    if df is None or len(df) == 0:
        print("  ⚠️ THEME_STOCKS 데이터 없음")
        return pd.DataFrame()

    df['THEME_NAME'] = df['THEME_NAME'].astype(str).str.strip()
    df['STK_NAME']   = df['STK_NAME'].astype(str).str.strip() if 'STK_NAME' in df.columns else ''
    df['STK_CODE']   = df['STK_CODE'].astype(str).str.strip().str.zfill(6)
    df['UPDATED_DT'] = pd.to_datetime(df['UPDATED_DT']).dt.strftime('%Y%m%d')

    print(f"  THEME_STOCKS: {len(df)} rows, "
          f"{df['THEME_NAME'].nunique()}개 테마, "
          f"UPDATED_DT: {df['UPDATED_DT'].min()} ~ {df['UPDATED_DT'].max()}")
    return df


def build_constituent_map(theme_info_df: pd.DataFrame,
                          theme_stocks_df: pd.DataFrame,
                          as_of_date: str) -> dict:
    """
    as_of_date 이전 가장 최근 UPDATED_DT 데이터만 가져오기
    THEME_ID → 구성종목 수 딕셔너리 반환
    """
    if theme_info_df.empty or theme_stocks_df.empty:
        return {}

    # THEME_INFO: as_of_date 이전 가장 최근
    info_valid = theme_info_df[theme_info_df['UPDATED_DT'] <= as_of_date]
    if info_valid.empty:
        return {}

    info_latest = (info_valid
                   .sort_values('UPDATED_DT')
                   .groupby('THEME_NAME')
                   .last()
                   .reset_index()[['THEME_NAME', 'THEME_ID']])
    name_to_id = dict(zip(info_latest['THEME_NAME'], info_latest['THEME_ID']))

    # THEME_STOCKS: as_of_date 이전 가장 최근
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
        how='inner'
    )

    name_counts = stocks_latest.groupby('THEME_NAME')['STK_CODE'].nunique().to_dict()

    result = {}
    for theme_name, count in name_counts.items():
        theme_id = name_to_id.get(theme_name)
        if theme_id:
            result[theme_id] = count

    return result


# 유동성 필터 적용
def apply_liquidity_filter(theme_pivot: pd.DataFrame,
                           current_idx: int,
                           constituent_counts: dict,
                           min_constituents: int = 3,
                           zero_return_threshold: float = 0.2,
                           zero_lookback: int = 5) -> tuple:
    """
    theme_pivot의 current_idx 시점 기준으로 유동성 필터 적용

    Returns:
        valid_themes: 필터 통과 테마 리스트
        filter_stats: 각 필터별 탈락 수 dict
    """
    start_zero = max(0, current_idx - zero_lookback + 1)
    data_zero = theme_pivot.iloc[start_zero:current_idx + 1]

    all_themes = theme_pivot.columns.tolist()
    valid_themes = []
    stats = {'total': len(all_themes), 'f1_constituents': 0,
             'f2_zero_return': 0}

    for theme_id in all_themes:
        # ── Filter 1: 구성 종목 수 ──
        if constituent_counts:
            n_const = constituent_counts.get(theme_id, 0)
            if n_const < min_constituents:
                stats['f1_constituents'] += 1
                continue

        # ── Filter 2: 제로 리턴 비율 ──
        last_zero = data_zero[theme_id].dropna()
        if len(last_zero) == 0:
            stats['f2_zero_return'] += 1
            continue

        zero_count = (last_zero.abs() < 1e-6).sum()
        zero_ratio = zero_count / len(last_zero)
        if zero_ratio > zero_return_threshold:
            stats['f2_zero_return'] += 1
            continue

        valid_themes.append(theme_id)

    stats['passed'] = len(valid_themes)
    return valid_themes, stats


# ============================================================
# 필터 통계 요약 출력
# ============================================================
def summarize_filter_stats(filter_log: list, config: dict = None) -> None:
    """
    filter_log: apply_liquidity_filter에서 반환된 stats dict 리스트
    config: 임계값 표시용 (min_constituents, zero_return_threshold 등)
    """
    if not filter_log:
        return

    df = pd.DataFrame(filter_log)

    print(f"\n  📊 유동성 필터 통계 (총 {len(df)}회 리밸런싱)")
    print(f"  {'─'*55}")
    print(f"  전체 테마 수 (평균):      {df['total'].mean():.1f}개")

    if config:
        print(f"  F1 구성종목 미달 (평균):  {df['f1_constituents'].mean():.1f}개 "
              f"(임계값: {config.get('min_constituents', '?')}개 미만)")
        print(f"  F2 제로리턴 과다 (평균):  {df['f2_zero_return'].mean():.1f}개 "
              f"(임계값: {config.get('zero_return_threshold', 0)*100:.0f}%, "
              f"{config.get('zero_lookback', '?')}일)")
    else:
        print(f"  F1 구성종목 미달 (평균):  {df['f1_constituents'].mean():.1f}개")
        print(f"  F2 제로리턴 과다 (평균):  {df['f2_zero_return'].mean():.1f}개")

    print(f"  필터 통과 (평균):         {df['passed'].mean():.1f}개 "
          f"({df['passed'].mean()/df['total'].mean()*100:.1f}%)")
    print(f"  {'─'*55}")
    print(f"  필터 통과 (최소/중앙/최대): "
          f"{df['passed'].min()} / {df['passed'].median():.0f} / {df['passed'].max()}개")

# python Theme_model/theme_filter.py