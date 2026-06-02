# -*- coding: utf-8 -*-
"""
테마별 시장조정 수익률 산출
"""

import sys
import os
import pandas as pd
import numpy as np
from scipy import stats
import warnings
warnings.filterwarnings('ignore')

sys.path.append(os.path.join(r'C:\Users\intern9', 'truston_quant_dev'))
from util.database2 import MSSQL, DBConfig

# settings.py에서 설정 import
from Theme_model.settings import (
    KOSPI_EXCEL_PATH,
    START_DATE,
    END_DATE,
    ROLLING_WINDOW,
)


# 1. 코스피 수익률
def load_kospi_returns(filepath: str, start_date: str, end_date: str) -> pd.DataFrame:
    # 코스피 종가를 읽어 일간 수익률 계산
    df = pd.read_excel(filepath, header=None, skiprows=5)
    df.columns = ['Date', 'KOSPI_Close']

    # 날짜/종가 변환
    df['Date'] = pd.to_datetime(df['Date'], errors='coerce')
    df['KOSPI_Close'] = pd.to_numeric(df['KOSPI_Close'], errors='coerce')
    df = df.dropna(subset=['Date', 'KOSPI_Close'])
    df = df.sort_values('Date').reset_index(drop=True)

    # 일간 수익률
    df['KOSPI_Rtn'] = df['KOSPI_Close'].pct_change()

    # YYYYMMDD 문자열 키 생성 (THEME_RTN과 조인용)
    df['TRD_DT'] = df['Date'].dt.strftime('%Y%m%d')

    # 기간 필터 (수익률 계산을 위해 start_date 이전 데이터도 유지)
    start_dt = pd.to_datetime(start_date, format='%Y%m%d')
    end_dt = pd.to_datetime(end_date, format='%Y%m%d')
    df = df[(df['Date'] <= end_dt)]

    # 첫 행 NaN 제거
    df = df.dropna(subset=['KOSPI_Rtn'])

    print(f"  코스피 수익률: {len(df)} 거래일")
    print(f"  기간: {df['Date'].min().strftime('%Y-%m-%d')} ~ {df['Date'].max().strftime('%Y-%m-%d')}")
    print(f"  종가 범위: {df['KOSPI_Close'].min():.2f} ~ {df['KOSPI_Close'].max():.2f}")

    return df

# 2. THEME_RTN 조회 (DB)
def get_theme_returns(db: MSSQL, start_date: str, end_date: str) -> pd.DataFrame:
    """
    THEME_RTN: THEME_ID(int), PfmDate(datetime), ThemeRtn(float)
    롤링 베타 계산을 위해 start_date보다 앞선 데이터도 가져옴
    """
    # 롤링 윈도우만큼 앞선 데이터 필요 (약 4개월 버퍼)
    buffer_start = pd.to_datetime(start_date, format='%Y%m%d') - pd.Timedelta(days=130)
    buffer_start_str = buffer_start.strftime('%Y%m%d')

    query = f"""
        SELECT THEME_ID, PfmDate, ThemeRtn
        FROM THEME_RTN
        WHERE PfmDate BETWEEN '{buffer_start_str}' AND '{end_date}'
        ORDER BY THEME_ID, PfmDate
    """
    df = db.SELECT(query)
    if df is None or len(df) == 0:
        raise ValueError("THEME_RTN 데이터 없음")

    df['ThemeRtn'] = pd.to_numeric(df['ThemeRtn'], errors='coerce')
    df['THEME_ID'] = df['THEME_ID'].astype(str).str.strip()
    df['PfmDate'] = pd.to_datetime(df['PfmDate']).dt.strftime('%Y%m%d')

    n_themes = df['THEME_ID'].nunique()
    print(f"  THEME_RTN: {len(df)} rows, {n_themes}개 테마")
    print(f"  기간: {df['PfmDate'].min()} ~ {df['PfmDate'].max()}")

    return df


# 3. 3개월 롤링 베타 계산 + 시장조정 수익률
def compute_rolling_beta(theme_rtn: pd.Series,
                         market_rtn: pd.Series,
                         window: int = 63) -> pd.Series:
    """
    OLS 기반 롤링 베타: Cov(theme, market) / Var(market)
    """
    min_p = min(max(3, window // 2), window)  # window 이하로 제한
    cov = theme_rtn.rolling(window=window, min_periods=min_p).cov(market_rtn)
    var = market_rtn.rolling(window=window, min_periods=min_p).var()
    beta = cov / var
    return beta


def compute_market_adjusted_returns(theme_df: pd.DataFrame,
                                     kospi_df: pd.DataFrame,
                                     start_date: str,
                                     window: int = ROLLING_WINDOW) -> pd.DataFrame:
    """
    각 테마별:
      1) 3개월 롤링 베타 산출 (ThemeRtn vs KOSPI_Rtn)
      2) 시장조정 수익률 = ThemeRtn - Beta * KOSPI_Rtn
    """
    # 코스피 수익률을 TRD_DT 인덱스로
    kospi_series = kospi_df.set_index('TRD_DT')['KOSPI_Rtn']

    themes = sorted(theme_df['THEME_ID'].unique())
    print(f"\n  {len(themes)}개 테마에 대해 롤링 베타 계산 중...")

    all_results = []
    skipped = 0

    for i, theme_id in enumerate(themes):
        if (i + 1) % 50 == 0:
            print(f"    진행: {i+1}/{len(themes)}")

        sub = theme_df[theme_df['THEME_ID'] == theme_id][['PfmDate', 'ThemeRtn']].copy()
        sub = sub.set_index('PfmDate')

        # 코스피 수익률과 날짜 매칭
        merged = sub.join(kospi_series, how='inner').dropna()

        if len(merged) < window:
            skipped += 1
            continue

        # 롤링 베타 계산
        merged['Beta'] = compute_rolling_beta(
            merged['ThemeRtn'], merged['KOSPI_Rtn'], window=window
        )

        # 시장조정 수익률 = ThemeRtn - Beta * KOSPI_Rtn
        merged['AdjRtn'] = merged['ThemeRtn'] - merged['Beta'] * merged['KOSPI_Rtn']

        # 분석 기간 필터 (start_date 이후만)
        merged = merged[merged.index >= start_date]

        if len(merged) == 0:
            skipped += 1
            continue

        merged['THEME_ID'] = theme_id
        merged = merged.reset_index().rename(columns={'index': 'PfmDate'})

        all_results.append(merged[['THEME_ID', 'PfmDate', 'ThemeRtn', 'KOSPI_Rtn',
                                    'Beta', 'AdjRtn']])

    print(f"  완료: {len(all_results)}개 테마 처리, {skipped}개 스킵 (데이터 부족)")

    if not all_results:
        return pd.DataFrame()

    return pd.concat(all_results, ignore_index=True)


# ============================================================
# 4. 결과 요약 통계
# ============================================================
def summarize_results(result_df: pd.DataFrame) -> pd.DataFrame:
    """테마별 시장조정 수익률 요약 통계"""
    summary = result_df.groupby('THEME_ID').agg(
        N_obs=('AdjRtn', 'count'),
        Beta_mean=('Beta', 'mean'),
        Beta_std=('Beta', 'std'),
        Beta_last=('Beta', 'last'),
        ThemeRtn_cum=('ThemeRtn', lambda x: (1 + x).prod() - 1),
        KOSPI_cum=('KOSPI_Rtn', lambda x: (1 + x).prod() - 1),
        AdjRtn_cum=('AdjRtn', lambda x: (1 + x).prod() - 1),
        AdjRtn_mean=('AdjRtn', 'mean'),
        AdjRtn_std=('AdjRtn', 'std'),
        AdjRtn_sharpe=('AdjRtn', lambda x: x.mean() / x.std() * np.sqrt(252) if x.std() > 0 else 0),
    ).reset_index()

    summary = summary.sort_values('AdjRtn_cum', ascending=False)
    return summary


# ============================================================
# 5. 메인
# ============================================================
def main():
    print("=" * 70)
    print("  테마별 시장조정 수익률 산출 (3개월 롤링 베타)")
    print(f"  분석기간: {START_DATE} ~ {END_DATE}")
    print(f"  롤링 윈도우: {ROLLING_WINDOW}거래일 (약 3개월)")
    print("=" * 70)

    # ── Step 1: 코스피 수익률 (엑셀) ──
    print("\n[Step 1] 코스피 종가 → 수익률 (엑셀)")
    kospi_df = load_kospi_returns(KOSPI_EXCEL_PATH, START_DATE, END_DATE)

    # ── Step 2: THEME_RTN (DB) ──
    print(f"\n[Step 2] THEME_RTN 조회 (DB)")
    db = MSSQL(database=DBConfig.TRSTDEV_DB)
    print("  ✅ DB 연결 성공")

    try:
        theme_df = get_theme_returns(db, START_DATE, END_DATE)
    finally:
        db.close()
        print("  ✅ DB 연결 종료")

    # ── Step 3: 롤링 베타 + 시장조정 수익률 ──
    print(f"\n[Step 3] 3개월 롤링 베타 산출 + 시장조정 수익률 계산")
    result_df = compute_market_adjusted_returns(theme_df, kospi_df, START_DATE, ROLLING_WINDOW)

    if result_df.empty:
        print("  ⚠️ 결과 데이터 없음")
        return

    # ── Step 4: 요약 통계 ──
    print(f"\n[Step 4] 결과 요약")
    summary = summarize_results(result_df)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 결과 출력
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    pd.set_option('display.max_columns', None)
    pd.set_option('display.width', 160)
    pd.set_option('display.max_rows', 50)
    pd.set_option('display.float_format', '{:.4f}'.format)

    print("\n" + "=" * 70)
    print("  📊 전체 결과 요약")
    print("=" * 70)
    print(f"  총 테마: {summary['THEME_ID'].nunique()}개")
    print(f"  전체 데이터: {len(result_df)} rows")
    print(f"  분석기간: {result_df['PfmDate'].min()} ~ {result_df['PfmDate'].max()}")

    print("\n" + "-" * 70)
    print("  📊 베타 분포")
    print("-" * 70)
    print(f"  평균 베타: {summary['Beta_mean'].mean():.4f}")
    print(f"  중앙값 베타: {summary['Beta_mean'].median():.4f}")
    print(f"  베타 범위: [{summary['Beta_mean'].min():.4f}, {summary['Beta_mean'].max():.4f}]")

    print("\n" + "-" * 70)
    print("  📊 시장조정 수익률 상위 20개 테마")
    print("-" * 70)
    display_cols = ['THEME_ID', 'N_obs', 'Beta_mean', 'Beta_last',
                    'ThemeRtn_cum', 'KOSPI_cum', 'AdjRtn_cum', 'AdjRtn_sharpe']
    print(summary[display_cols].head(20).to_string(index=False))

    print("\n" + "-" * 70)
    print("  📊 시장조정 수익률 하위 10개 테마")
    print("-" * 70)
    print(summary[display_cols].tail(10).to_string(index=False))

    # 전체 평균
    print("\n" + "-" * 70)
    print("  📊 전체 평균 통계")
    print("-" * 70)
    print(f"  평균 테마 누적수익률:     {summary['ThemeRtn_cum'].mean():.4f} "
          f"({summary['ThemeRtn_cum'].mean()*100:.2f}%)")
    print(f"  평균 코스피 누적수익률:   {summary['KOSPI_cum'].mean():.4f} "
          f"({summary['KOSPI_cum'].mean()*100:.2f}%)")
    print(f"  평균 시장조정 누적수익률: {summary['AdjRtn_cum'].mean():.4f} "
          f"({summary['AdjRtn_cum'].mean()*100:.2f}%)")
    print(f"  시장조정 양(+) 테마 비율: "
          f"{(summary['AdjRtn_cum'] > 0).sum()}/{len(summary)} "
          f"({(summary['AdjRtn_cum'] > 0).mean()*100:.1f}%)")

    # ── 엑셀 저장 ──
    output_dir = os.path.dirname(os.path.abspath(__file__))
    output_path = os.path.join(output_dir, 'theme_market_adjusted_returns.xlsx')

    try:
        with pd.ExcelWriter(output_path, engine='openpyxl') as writer:
            summary.to_excel(writer, sheet_name='테마별_요약', index=False)
            result_df.to_excel(writer, sheet_name='일별_상세', index=False)

            # 베타 시계열 피벗
            beta_pivot = result_df.pivot_table(
                index='PfmDate', columns='THEME_ID', values='Beta', aggfunc='first'
            )
            beta_pivot.to_excel(writer, sheet_name='베타_시계열')

            # 시장조정 수익률 피벗
            adj_pivot = result_df.pivot_table(
                index='PfmDate', columns='THEME_ID', values='AdjRtn', aggfunc='first'
            )
            adj_pivot.to_excel(writer, sheet_name='시장조정수익률_시계열')

        print(f"\n  📁 결과 저장: {output_path}")
    except Exception as e:
        print(f"\n  ⚠️ 엑셀 저장 실패: {e}")
        csv_path = output_path.replace('.xlsx', '_summary.csv')
        summary.to_csv(csv_path, index=False, encoding='utf-8-sig')
        print(f"  📁 CSV 대체 저장: {csv_path}")


if __name__ == '__main__':
    main()
#python Theme_model/BETA.py