# -*- coding: utf-8 -*-
"""
롤링 백테스트에서 NaN 처리 분석
"""
import sys
sys.path.append(r'C:\Users\intern9\truston_quant_dev')
import pandas as pd
import numpy as np
from util.database2 import MSSQL, DBConfig
from Theme_model.settings import START_DATE, END_DATE, ROLLING_WINDOW, J
from Theme_model.BETA import load_kospi_returns, get_theme_returns, compute_market_adjusted_returns, KOSPI_EXCEL_PATH

print("=" * 70)
print("  롤링 백테스트 NaN 처리 분석")
print(f"  Lookback J={J}일")
print("=" * 70)

# 데이터 로드
print("\n[Step 1] 데이터 로드")
kospi_df = load_kospi_returns(KOSPI_EXCEL_PATH, START_DATE, END_DATE)

db = MSSQL(database=DBConfig.TRSTDEV_DB)
theme_df = get_theme_returns(db, START_DATE, END_DATE)
db.close()

print("\n[Step 2] 시장조정 수익률 계산")
result_df = compute_market_adjusted_returns(theme_df, kospi_df, START_DATE, ROLLING_WINDOW)

# 피벗 테이블 생성
adj_pivot = result_df.pivot_table(
    index='PfmDate', columns='THEME_ID', values='AdjRtn', aggfunc='first'
).sort_index()

theme_pivot = result_df.pivot_table(
    index='PfmDate', columns='THEME_ID', values='ThemeRtn', aggfunc='first'
).sort_index()

n_dates = len(adj_pivot)
n_themes = len(adj_pivot.columns)

print(f"\n[기본 정보]")
print(f"  피벗 테이블: {n_dates}일 x {n_themes}개 테마")
print(f"  분석기간: {adj_pivot.index[0]} ~ {adj_pivot.index[-1]}")

# NaN 분석
nan_count = adj_pivot.isna().sum().sum()
total_cells = n_dates * n_themes
nan_ratio = nan_count / total_cells * 100
print(f"\n[NaN 분포]")
print(f"  전체 셀: {total_cells:,}개")
print(f"  NaN 셀: {nan_count:,}개 ({nan_ratio:.1f}%)")

# 테마별 NaN 비율
theme_nan = adj_pivot.isna().sum()
sparse_themes = theme_nan[theme_nan > n_dates * 0.1]  # 10% 이상 NaN
print(f"\n[테마별 NaN 비율]")
print(f"  10% 이상 NaN인 테마: {len(sparse_themes)}개")
print(f"  상위 10개:")
for theme_id, nan_cnt in sorted(sparse_themes.items(), key=lambda x: -x[1])[:10]:
    print(f"    {theme_id}: {nan_cnt}개 ({nan_cnt/n_dates*100:.1f}%)")

# 날짜별 NaN 분석
date_nan = adj_pivot.isna().sum(axis=1)
print(f"\n[날짜별 NaN]")
print(f"  날짜별 평균 NaN 테마 수: {date_nan.mean():.1f}개 ({date_nan.mean()/n_themes*100:.1f}%)")
print(f"  최소 NaN: {date_nan.min()}개 ({date_nan.min()/n_themes*100:.1f}%)")
print(f"  최대 NaN: {date_nan.max()}개 ({date_nan.max()/n_themes*100:.1f}%)")

# 롤링 윈도우 시뮬레이션 (J=2일 기준)
print(f"\n{'='*70}")
print(f"  롤링 백테스트 시뮬레이션 (J={J}일)")
print(f"{'='*70}")

dates = adj_pivot.index.tolist()

# 샘플 테마 선택 (NaN이 있는 테마)
sample_themes = sparse_themes.head(3).index.tolist() if len(sparse_themes) >= 3 else adj_pivot.columns[:3].tolist()

print(f"\n[샘플 테마 분석: {sample_themes}]")

for theme_id in sample_themes:
    series = adj_pivot[theme_id]
    nan_idx = series[series.isna()].index.tolist()
    valid_idx = series[series.notna()].index.tolist()
    
    print(f"\n  테마 {theme_id}:")
    print(f"    유효 데이터: {len(valid_idx)}일")
    print(f"    NaN: {len(nan_idx)}일")
    if valid_idx:
        print(f"    데이터 범위: {valid_idx[0]} ~ {valid_idx[-1]}")

# fillna(0) 영향 분석
print(f"\n{'='*70}")
print(f"  fillna(0) 영향 분석")
print(f"{'='*70}")

# 특정 날짜에서 J일 윈도우 확인
sample_t = J + 10  # 10번째 리밸런싱 시점
if sample_t < n_dates:
    as_of_date = dates[sample_t - 1]
    past_window = adj_pivot.iloc[sample_t - J:sample_t]
    
    print(f"\n[리밸런싱 시점: t={sample_t}, 기준일={as_of_date}]")
    print(f"  과거 윈도우: {dates[sample_t-J]} ~ {dates[sample_t-1]}")
    
    # 각 테마별 NaN 개수
    theme_nan_in_window = past_window.isna().sum()
    partial_nan = theme_nan_in_window[(theme_nan_in_window > 0) & (theme_nan_in_window < J)]
    full_nan = theme_nan_in_window[theme_nan_in_window == J]
    
    print(f"\n  [윈도우 내 테마별 데이터 가용성]")
    print(f"    전체 유효 (NaN=0): {(theme_nan_in_window == 0).sum()}개")
    print(f"    부분 NaN (0 < NaN < J): {len(partial_nan)}개")
    print(f"    전체 NaN (NaN=J): {len(full_nan)}개")
    
    # 기존 코드 방식으로 valid_mask 계산
    valid_mask = past_window.notna().sum() >= J
    rankable_themes = valid_mask[valid_mask].index.tolist()
    
    print(f"\n  [current code: notna().sum() >= J]")
    print(f"    랭킹 가능 테마: {len(rankable_themes)}개")
    
    # fillna(0) 적용 후 영향
    if partial_nan.any():
        print(f"\n  [fillna(0) 영향 예시 - 부분 NaN 테마]")
        for theme_id in partial_nan.head(3).index:
            original = past_window[theme_id].values
            filled = past_window[theme_id].fillna(0).values
            cum_orig = (1 + pd.Series(original).dropna()).prod() - 1 if not pd.Series(original).isna().all() else np.nan
            cum_filled = (1 + pd.Series(filled)).prod() - 1
            print(f"    {theme_id}: 원본={original} -> 채운후={filled}")
            print(f"            누적수익률: 원본={cum_orig*100:.2f}% vs 채운후={cum_filled*100:.2f}%")

# 미래 수익률 계산 시 NaN 처리 분석
print(f"\n{'='*70}")
print(f"  미래 수익률 계산 시 NaN 처리 분석")
print(f"{'='*70}")

# 중간에 사라진 테마 ID 확인
db = MSSQL(database=DBConfig.TRSTDEV_DB)
query = f"""
SELECT THEME_ID, MAX(PfmDate) as max_dt
FROM THEME_RTN
WHERE PfmDate BETWEEN '{START_DATE}' AND '{END_DATE}'
GROUP BY THEME_ID
HAVING MAX(PfmDate) < '{END_DATE}'
ORDER BY MAX(PfmDate) DESC
"""
disappeared = db.SELECT(query)
db.close()

if not disappeared.empty:
    disappeared['THEME_ID'] = disappeared['THEME_ID'].astype(str).str.strip()
    disappeared['max_dt'] = pd.to_datetime(disappeared['max_dt']).dt.strftime('%Y%m%d')
    
    print(f"\n[중간에 사라진 테마 중 피벗에 포함된 테마]")
    included = [t for t in disappeared['THEME_ID'].values if t in adj_pivot.columns]
    print(f"  {len(included)}개 테마가 피벗에 포함됨")
    
    if included:
        # 첫번째 사라진 테마로 시뮬레이션
        sample_disappeared = included[0]
        max_dt_str = disappeared[disappeared['THEME_ID'] == sample_disappeared]['max_dt'].values[0]
        
        print(f"\n  [시뮬레이션: 테마 {sample_disappeared} (마지막 데이터: {max_dt_str})]")
        
        # 마지막 데이터 이후 날짜에서 선정되는 경우
        max_dt_idx = None
        for i, d in enumerate(dates):
            if d == max_dt_str:
                max_dt_idx = i
                break
        
        if max_dt_idx is not None and max_dt_idx + 5 < n_dates:
            future_start = max_dt_idx + 2
            future_end = max_dt_idx + 3
            future_dates_sample = adj_pivot.index[future_start:future_end]
            
            print(f"    미래 기간: {future_dates_sample.tolist()}")
            print(f"    theme_pivot 값: {theme_pivot.loc[future_dates_sample, sample_disappeared].values}")
            print(f"    fillna(0) 후: {theme_pivot.loc[future_dates_sample, sample_disappeared].fillna(0).values}")
            print(f"\n    ⚠️ 문제: 테마가 사라진 후 선정되면 수익률 0%로 계산됨!")

print("\n" + "=" * 70)
print("  분석 완료")
print("=" * 70)
