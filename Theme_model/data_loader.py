# -*- coding: utf-8 -*-
"""
Theme_model 공통 유틸리티
"""

import sys
import os
import pandas as pd

sys.path.append(os.path.join(r'C:\Users\intern9', 'truston_quant_dev'))

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
    START_DATE,
    END_DATE,
    ROLLING_WINDOW,
    USE_FILTER,
)
from util.database2 import MSSQL, DBConfig


def load_all_data(verbose: bool = True):
    """
    모든 테마 모멘텀 분석에 필요한 데이터를 로드하고 피벗 테이블 생성

    Returns:
        dict: {
            'adj_pivot': AdjRtn 피벗 테이블,
            'theme_pivot': ThemeRtn 피벗 테이블,
            'kospi_pivot': KOSPI_Rtn 시리즈,
            'theme_info_df': THEME_INFO DataFrame,
            'theme_stocks_df': THEME_STOCKS DataFrame,
            'result_df': 원본 결과 DataFrame
        }
    """
    if verbose:
        print("=" * 65)
        print("  데이터 로드 시작")
        print("=" * 65)

    # ── Step 1: KOSPI 데이터 로드 ──
    if verbose:
        print("\n[Step 1] 코스피 수익률 로드")
    kospi_df = load_kospi_returns(KOSPI_EXCEL_PATH, START_DATE, END_DATE)

    # ── Step 2: DB 데이터 로드 ──
    if verbose:
        print("\n[Step 2] DB 데이터 로드")

    db = MSSQL(database=DBConfig.TRSTDEV_DB)
    theme_info_df = pd.DataFrame()
    theme_stocks_df = pd.DataFrame()

    try:
        theme_df = get_theme_returns(db, START_DATE, END_DATE)
        if USE_FILTER:
            if verbose:
                print("\n  THEME_INFO / THEME_STOCKS 로드...")
            theme_info_df = get_theme_info(db)
            theme_stocks_df = get_theme_stocks(db)
    finally:
        db.close()

    # ── Step 3: 시장조정 수익률 계산 ──
    if verbose:
        print("\n[Step 3] 시장조정 수익률 계산")
    result_df = compute_market_adjusted_returns(
        theme_df, kospi_df, START_DATE, ROLLING_WINDOW
    )

    # ── Step 4: 피벗 테이블 생성 ──
    if verbose:
        print("\n[Step 4] 피벗 테이블 생성")

    adj_pivot = result_df.pivot_table(
        index='PfmDate', columns='THEME_ID', values='AdjRtn', aggfunc='first'
    ).sort_index()

    theme_pivot = result_df.pivot_table(
        index='PfmDate', columns='THEME_ID', values='ThemeRtn', aggfunc='first'
    ).sort_index()

    kospi_pivot = (result_df.drop_duplicates('PfmDate')
                   .set_index('PfmDate')['KOSPI_Rtn'].sort_index())

    if verbose:
        print(f"\n  피벗: {adj_pivot.shape[0]}일 x {adj_pivot.shape[1]}개 테마")
        print("=" * 65)

    return {
        'adj_pivot': adj_pivot,
        'theme_pivot': theme_pivot,
        'kospi_pivot': kospi_pivot,
        'theme_info_df': theme_info_df,
        'theme_stocks_df': theme_stocks_df,
        'result_df': result_df,
    }


def print_metrics(metrics: dict, title: str = "성과 지표"):
    """성과 지표 출력 헬퍼 함수"""
    print(f"\n{'='*50}")
    print(f"  📊 {title}")
    print(f"{'='*50}")
    print(f"  총 누적수익률: {metrics['총 누적수익률(%)']:.2f}%")
    print(f"  연환산 수익률: {metrics['연환산 수익률(%)']:.2f}%")
    print(f"  Sharpe Ratio:  {metrics['Sharpe']:.3f}")
    print(f"  MDD:           {metrics['MDD(%)']:.2f}%")
    print(f"  승률:          {metrics['승률(%)']:.1f}%")
    print(f"  리밸런싱:      {metrics['리밸런싱 횟수']}회")
    print(f"  평균 거래비용: {metrics['평균 거래비용(%)']:.3f}%/회")
    if '평균 유효테마수' in metrics:
        print(f"  평균 유효테마: {metrics['평균 유효테마수']}개")


def print_monthly_summary(monthly_df: pd.DataFrame):
    """월별 수익률 출력 헬퍼 함수"""
    print(f"\n  {'월':>8}  {'리밸':>4}  {'월 누적수익률':>12}")
    print(f"  {'-'*8}  {'-'*4}  {'-'*12}")

    for _, row in monthly_df.iterrows():
        cum = row['monthly_cum(%)']
        sign = '+' if cum > 0 else ''
        print(f"  {row['YM']:>8}  {int(row['N_rebal']):>4}  {sign}{cum:>10.2f}%")

    avg_monthly = monthly_df['monthly_cum(%)'].mean()
    print(f"  {'─'*30}")
    print(f"  {'월평균':>8}        {'+' if avg_monthly > 0 else ''}{avg_monthly:>10.2f}%")


if __name__ == '__main__':
    # 테스트
    data = load_all_data(verbose=True)
    print(f"\n로드된 데이터:")
    for key, val in data.items():
        if isinstance(val, pd.DataFrame):
            print(f"  {key}: {val.shape}")
        elif isinstance(val, pd.Series):
            print(f"  {key}: {len(val)} rows")
