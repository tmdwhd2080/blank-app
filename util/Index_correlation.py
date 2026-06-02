# -*- coding: utf-8 -*-
"""
THEME_RTN vs 주요 지수(KOSPI, KOSPI200, KOSDAQ150) 수익률 상관관계 분석
분석기간: 2024.11.10 ~ 2025.11.10

테이블 구조:
- TS_IDX_DAILY: TRD_DT, SEC_CD, CLOSE_PRC (+ 기타)
- THEME_RTN:    THEME_ID(int), PfmDate(datetime), ThemeRtn(float)
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


# ============================================================
# 1. 지수 일간 수익률 계산
# ============================================================
def get_index_returns(db: MSSQL, start_date: str, end_date: str) -> pd.DataFrame:
    """
    TS_IDX_DAILY에서 IKS001, IKS200, IKQ150의 CLOSE_PRC 기반 일간 수익률 계산
    수익률 계산을 위해 start_date보다 약간 이전 데이터도 조회
    """
    # 수익률 계산 시 첫 날이 NaN이 되므로, 시작일보다 약간 앞선 데이터 포함
    buffer_start = pd.to_datetime(start_date, format='%Y%m%d') - pd.Timedelta(days=10)
    buffer_start_str = buffer_start.strftime('%Y%m%d')

    query = f"""
        SELECT TRD_DT, SEC_CD, CLOSE_PRC
        FROM TS_IDX_DAILY
        WHERE SEC_CD IN ('IKS001', 'IKS200', 'IKQ150')
          AND TRD_DT BETWEEN '{buffer_start_str}' AND '{end_date}'
        ORDER BY TRD_DT, SEC_CD
    """
    df = db.SELECT(query)
    if df is None or len(df) == 0:
        raise ValueError("TS_IDX_DAILY 데이터 없음")

    df['CLOSE_PRC'] = pd.to_numeric(df['CLOSE_PRC'], errors='coerce')
    df['TRD_DT'] = df['TRD_DT'].astype(str).str.strip()

    # 피벗: 날짜 x 지수별 종가
    pivot = df.pivot_table(index='TRD_DT', columns='SEC_CD', values='CLOSE_PRC', aggfunc='first')
    pivot = pivot.sort_index()

    # 일간 수익률 계산 (pct_change)
    returns = pivot.pct_change().dropna()

    # 실제 분석 기간만 필터
    returns = returns[returns.index >= start_date]

    name_map = {'IKS001': 'KOSPI', 'IKS200': 'KOSPI200', 'IKQ150': 'KOSDAQ150'}
    returns = returns.rename(columns=name_map)

    print(f"  지수 수익률: {len(returns)} 거래일")
    print(f"  기간: {returns.index.min()} ~ {returns.index.max()}")

    return returns


# ============================================================
# 2. THEME_RTN 데이터 조회
# ============================================================
def get_theme_returns(db: MSSQL, start_date: str, end_date: str) -> pd.DataFrame:
    """
    THEME_RTN 테이블: THEME_ID(int), PfmDate(datetime), ThemeRtn(float)
    """
    query = f"""
        SELECT THEME_ID, PfmDate, ThemeRtn
        FROM THEME_RTN
        WHERE PfmDate BETWEEN '{start_date}' AND '{end_date}'
        ORDER BY THEME_ID, PfmDate
    """
    df = db.SELECT(query)
    if df is None or len(df) == 0:
        raise ValueError("THEME_RTN 데이터 없음")

    df['ThemeRtn'] = pd.to_numeric(df['ThemeRtn'], errors='coerce')
    df['THEME_ID'] = df['THEME_ID'].astype(str).str.strip()

    # PfmDate → YYYYMMDD 문자열로 통일 (TRD_DT와 조인용)
    df['PfmDate'] = pd.to_datetime(df['PfmDate']).dt.strftime('%Y%m%d')

    n_themes = df['THEME_ID'].nunique()
    print(f"  THEME_RTN: {len(df)} rows, {n_themes}개 테마")
    print(f"  기간: {df['PfmDate'].min()} ~ {df['PfmDate'].max()}")

    return df


# ============================================================
# 3. 개별 테마별 상관관계 분석
# ============================================================
def analyze_correlation_by_theme(theme_df: pd.DataFrame,
                                  index_returns: pd.DataFrame,
                                  min_obs: int = 30) -> pd.DataFrame:
    """
    각 THEME_ID별 ThemeRtn과 3개 지수 수익률의 Pearson/Spearman 상관계수 + p-value
    """
    themes = sorted(theme_df['THEME_ID'].unique())
    print(f"\n  총 {len(themes)}개 테마 분석 중...")

    results = []

    for theme_id in themes:
        sub = theme_df[theme_df['THEME_ID'] == theme_id][['PfmDate', 'ThemeRtn']].copy()
        sub = sub.set_index('PfmDate')

        # 지수 수익률과 날짜 기준 inner join
        merged = sub.join(index_returns, how='inner').dropna()

        if len(merged) < min_obs:
            continue

        row = {'THEME_ID': theme_id, 'N_obs': len(merged)}

        best_corr = 0
        best_idx = ''

        for idx_name in ['KOSPI', 'KOSPI200', 'KOSDAQ150']:
            if idx_name not in merged.columns:
                continue

            corr_p, pval_p = stats.pearsonr(merged['ThemeRtn'], merged[idx_name])
            corr_s, pval_s = stats.spearmanr(merged['ThemeRtn'], merged[idx_name])

            row[f'{idx_name}_Pearson'] = round(corr_p, 4)
            row[f'{idx_name}_Spearman'] = round(corr_s, 4)
            row[f'{idx_name}_pvalue'] = round(pval_p, 6)

            # 유의수준 표시
            if pval_p < 0.01:
                row[f'{idx_name}_sig'] = '***'
            elif pval_p < 0.05:
                row[f'{idx_name}_sig'] = '**'
            elif pval_p < 0.1:
                row[f'{idx_name}_sig'] = '*'
            else:
                row[f'{idx_name}_sig'] = ''

            if abs(corr_p) > abs(best_corr):
                best_corr = corr_p
                best_idx = idx_name

        row['Best_Match'] = best_idx
        row['Best_Pearson'] = round(best_corr, 4)
        results.append(row)

    result_df = pd.DataFrame(results)
    if len(result_df) > 0:
        result_df = result_df.sort_values('Best_Pearson', ascending=False, key=abs)

    return result_df


# ============================================================
# 4. 전체 테마 평균 수익률 vs 지수 (집계 분석)
# ============================================================
def analyze_aggregate_correlation(theme_df: pd.DataFrame,
                                   index_returns: pd.DataFrame) -> pd.DataFrame:
    """
    전체 테마의 일평균 수익률(equal-weight)과 지수 수익률 간 상관관계
    """
    avg_theme = theme_df.groupby('PfmDate')['ThemeRtn'].mean().rename('AvgThemeRtn')
    merged = avg_theme.to_frame().join(index_returns, how='inner').dropna()

    print(f"  집계 분석: {len(merged)} 거래일 (전체 테마 일평균)")

    results = []
    for idx_name in ['KOSPI', 'KOSPI200', 'KOSDAQ150']:
        corr_p, pval_p = stats.pearsonr(merged['AvgThemeRtn'], merged[idx_name])
        corr_s, pval_s = stats.spearmanr(merged['AvgThemeRtn'], merged[idx_name])

        results.append({
            'Index': idx_name,
            'Pearson': round(corr_p, 4),
            'Spearman': round(corr_s, 4),
            'P_value': round(pval_p, 6),
            'R_squared': round(corr_p ** 2, 4),
            'N_obs': len(merged)
        })

    return pd.DataFrame(results).sort_values('Pearson', ascending=False, key=abs)


# ============================================================
# 5. 메인 실행
# ============================================================
def main():
    START_DATE = '20241110'
    END_DATE = '20251110'

    print("=" * 70)
    print("  THEME_RTN vs 지수 수익률 상관관계 분석")
    print(f"  분석기간: {START_DATE} ~ {END_DATE}")
    print("=" * 70)

    db = MSSQL(database=DBConfig.TRSTDEV_DB)
    print("✅ DB 연결 성공\n")

    try:
        # ── Step 1: 지수 수익률 ──
        print("[Step 1] 지수 종가 수익률 계산 (TS_IDX_DAILY)")
        index_returns = get_index_returns(db, START_DATE, END_DATE)

        print(f"\n  지수 수익률 기초통계:")
        print(index_returns.describe().round(6).to_string())

        # ── Step 2: THEME_RTN ──
        print(f"\n[Step 2] THEME_RTN 데이터 조회")
        theme_df = get_theme_returns(db, START_DATE, END_DATE)

        # ── Step 3: 전체 집계 상관관계 ──
        print(f"\n[Step 3] 전체 테마 평균 vs 지수 상관관계")
        agg_result = analyze_aggregate_correlation(theme_df, index_returns)

        # ── Step 4: 개별 테마별 상관관계 ──
        print(f"\n[Step 4] 개별 테마별 상관관계 분석")
        result = analyze_correlation_by_theme(theme_df, index_returns, min_obs=30)

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # 결과 출력
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        pd.set_option('display.max_columns', None)
        pd.set_option('display.width', 150)
        pd.set_option('display.max_rows', 100)

        print("\n" + "=" * 70)
        print("  📊 [결과 1] 전체 테마 평균 수익률 vs 지수 상관관계")
        print("=" * 70)
        print(agg_result.to_string(index=False))

        best = agg_result.iloc[0]
        print(f"\n  ★ 전체 테마 평균과 가장 높은 상관관계: {best['Index']} "
              f"(Pearson={best['Pearson']:.4f}, R²={best['R_squared']:.4f})")

        if len(result) == 0:
            print("\n  ⚠️ 개별 테마 분석 불가 (데이터 30일 미만)")
            return

        print("\n" + "=" * 70)
        print("  📊 [결과 2] 개별 테마별 상관관계 (상위 20개 - 절대값 기준)")
        print("=" * 70)

        display_cols = ['THEME_ID', 'N_obs',
                        'KOSPI_Pearson', 'KOSPI_sig',
                        'KOSPI200_Pearson', 'KOSPI200_sig',
                        'KOSDAQ150_Pearson', 'KOSDAQ150_sig',
                        'Best_Match', 'Best_Pearson']
        existing = [c for c in display_cols if c in result.columns]
        print(result[existing].head(20).to_string(index=False))

        # Best Match 분포
        print("\n" + "-" * 70)
        print("  📊 [결과 3] Best Match 분포")
        print("-" * 70)
        total = len(result)
        best_dist = result['Best_Match'].value_counts()
        for idx_name, count in best_dist.items():
            pct = count / total * 100
            bar = '█' * int(pct / 2)
            print(f"  {idx_name:12s}: {count:4d}개 ({pct:5.1f}%) {bar}")

        # 지수별 평균 상관계수
        print("\n" + "-" * 70)
        print("  📊 [결과 4] 지수별 Pearson 상관계수 통계")
        print("-" * 70)
        for idx_name in ['KOSPI', 'KOSPI200', 'KOSDAQ150']:
            col = f'{idx_name}_Pearson'
            if col in result.columns:
                avg = result[col].mean()
                med = result[col].median()
                std = result[col].std()
                mn = result[col].min()
                mx = result[col].max()
                print(f"  {idx_name:12s}: 평균={avg:.4f}, 중앙값={med:.4f}, "
                      f"표준편차={std:.4f}, 범위=[{mn:.4f}, {mx:.4f}]")

        # 통계적 유의성
        print("\n" + "-" * 70)
        print("  📊 [결과 5] 통계적 유의성 요약")
        print("-" * 70)
        for idx_name in ['KOSPI', 'KOSPI200', 'KOSDAQ150']:
            sig_col = f'{idx_name}_sig'
            if sig_col in result.columns:
                n_sig_01 = result[result[sig_col] == '***'].shape[0]
                n_sig_05 = result[result[sig_col].isin(['**', '***'])].shape[0]
                print(f"  {idx_name:12s}: p<0.01 → {n_sig_01:4d}/{total}개 ({n_sig_01/total*100:.1f}%), "
                      f"p<0.05 → {n_sig_05:4d}/{total}개 ({n_sig_05/total*100:.1f}%)")

        # ── 결과 엑셀 저장 ──
        output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   'theme_index_correlation_result.xlsx')
        try:
            with pd.ExcelWriter(output_path, engine='openpyxl') as writer:
                agg_result.to_excel(writer, sheet_name='집계_상관관계', index=False)
                result.to_excel(writer, sheet_name='개별테마_상관관계', index=False)
            print(f"\n  📁 결과 저장 완료: {output_path}")
        except Exception as e:
            print(f"\n  ⚠️ 엑셀 저장 실패: {e}")
            # CSV 대안
            csv_path = output_path.replace('.xlsx', '.csv')
            result.to_csv(csv_path, index=False, encoding='utf-8-sig')
            print(f"  📁 CSV로 대체 저장: {csv_path}")

    except Exception as e:
        import traceback
        print(f"\n❌ 오류 발생: {e}")
        traceback.print_exc()

    finally:
        db.close()
        print("\n✅ DB 연결 종료")


if __name__ == '__main__':
    main()
#python Theme_model/Index_correlation.py