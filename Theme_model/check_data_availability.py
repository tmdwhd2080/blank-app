# -*- coding: utf-8 -*-
"""
테마 데이터 가용성 분석 스크립트
"""
import sys
sys.path.append(r'C:\Users\intern9\truston_quant_dev')
import pandas as pd
import numpy as np
from util.database2 import MSSQL, DBConfig
from Theme_model.settings import START_DATE, END_DATE

print("=" * 70)
print("  테마 데이터 가용성 분석")
print("=" * 70)

# DB 연결
db = MSSQL(database=DBConfig.TRSTDEV_DB)

# 테마별 데이터 분포 확인
query = f"""
SELECT THEME_ID, 
       COUNT(DISTINCT PfmDate) as cnt, 
       MIN(PfmDate) as min_dt, 
       MAX(PfmDate) as max_dt
FROM THEME_RTN
WHERE PfmDate BETWEEN '{START_DATE}' AND '{END_DATE}'
GROUP BY THEME_ID
ORDER BY cnt
"""
df = db.SELECT(query)

# 전체 거래일수 확인
query_dates = f"""
SELECT DISTINCT PfmDate FROM THEME_RTN
WHERE PfmDate BETWEEN '{START_DATE}' AND '{END_DATE}'
ORDER BY PfmDate
"""
all_dates = db.SELECT(query_dates)
total_trading_days = len(all_dates)

db.close()

total_themes = len(df)
print(f"\n[기본 통계]")
print(f"  분석기간: {START_DATE} ~ {END_DATE}")
print(f"  총 거래일수: {total_trading_days}일")
print(f"  총 테마 수: {total_themes}개")

print(f"\n[테마별 데이터 건수 분포]")
print(f"  최소: {df['cnt'].min()}일")
print(f"  최대: {df['cnt'].max()}일")
print(f"  평균: {df['cnt'].mean():.1f}일")
print(f"  중앙값: {df['cnt'].median():.0f}일")
print(f"  표준편차: {df['cnt'].std():.1f}일")

# 데이터 부족 테마 비율
for threshold in [50, 100, 200, 300]:
    sparse = df[df['cnt'] < threshold]
    print(f"  {threshold}일 미만: {len(sparse)}개 ({len(sparse)/total_themes*100:.1f}%)")

# 분석기간 전체 데이터가 있는 테마
full_data = df[df['cnt'] >= total_trading_days * 0.95]
print(f"\n[데이터 완전성]")
print(f"  전체 기간 95%+ 데이터 있는 테마: {len(full_data)}개 ({len(full_data)/total_themes*100:.1f}%)")

# 중간에 사라진 테마 (마지막 데이터가 END_DATE 이전)
end_dt = pd.to_datetime(END_DATE, format='%Y%m%d')
df['max_dt'] = pd.to_datetime(df['max_dt'])
disappeared = df[df['max_dt'] < end_dt - pd.Timedelta(days=5)]

print(f"\n[중간에 사라진 테마] (마지막 데이터가 {END_DATE}보다 5일+ 이전)")
print(f"  총 {len(disappeared)}개 ({len(disappeared)/total_themes*100:.1f}%)")

if not disappeared.empty:
    print(f"\n  상위 10개 샘플:")
    print(f"  {'THEME_ID':>10}  {'데이터일수':>10}  {'시작일':>12}  {'마지막일':>12}")
    print(f"  {'-'*10}  {'-'*10}  {'-'*12}  {'-'*12}")
    for _, row in disappeared.head(10).iterrows():
        print(f"  {row['THEME_ID']:>10}  {row['cnt']:>10}  "
              f"{row['min_dt'].strftime('%Y%m%d') if pd.notna(row['min_dt']) else 'N/A':>12}  "
              f"{row['max_dt'].strftime('%Y%m%d'):>12}")

# 늦게 시작한 테마 (시작 데이터가 START_DATE 이후)
start_dt = pd.to_datetime(START_DATE, format='%Y%m%d')
df['min_dt'] = pd.to_datetime(df['min_dt'])
late_start = df[df['min_dt'] > start_dt + pd.Timedelta(days=5)]

print(f"\n[늦게 시작한 테마] (시작일이 {START_DATE}보다 5일+ 이후)")
print(f"  총 {len(late_start)}개 ({len(late_start)/total_themes*100:.1f}%)")

if not late_start.empty:
    print(f"\n  상위 10개 샘플:")
    print(f"  {'THEME_ID':>10}  {'데이터일수':>10}  {'시작일':>12}  {'마지막일':>12}")
    print(f"  {'-'*10}  {'-'*10}  {'-'*12}  {'-'*12}")
    for _, row in late_start.head(10).iterrows():
        print(f"  {row['THEME_ID']:>10}  {row['cnt']:>10}  "
              f"{row['min_dt'].strftime('%Y%m%d'):>12}  "
              f"{row['max_dt'].strftime('%Y%m%d'):>12}")

# 중간에 데이터 누락이 있는 테마 확인
expected_days = (df['max_dt'] - df['min_dt']).dt.days + 1
# 대략적인 거래일 비율 (약 70% = 휴일 제외)
df['expected_trading'] = (expected_days * 0.7).astype(int)
df['completeness'] = df['cnt'] / df['expected_trading'] * 100

incomplete = df[(df['completeness'] < 80) & (df['cnt'] >= 50)]
print(f"\n[중간 데이터 누락 테마] (50일 이상 & 완전성 80% 미만)")
print(f"  총 {len(incomplete)}개")

if not incomplete.empty and len(incomplete) > 0:
    print(f"\n  상위 10개 샘플 (완전성 낮은 순):")
    print(f"  {'THEME_ID':>10}  {'데이터':>6}  {'예상':>6}  {'완전성':>8}")
    print(f"  {'-'*10}  {'-'*6}  {'-'*6}  {'-'*8}")
    for _, row in incomplete.sort_values('completeness').head(10).iterrows():
        print(f"  {row['THEME_ID']:>10}  {row['cnt']:>6}  "
              f"{row['expected_trading']:>6}  {row['completeness']:>7.1f}%")

print("\n" + "=" * 70)
