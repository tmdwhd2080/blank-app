# -*- coding: utf-8 -*-
"""
테마 모멘텀 지속성 분석
========================
모멘텀 시작 조건:
  1) 베타조정 수익률(AdjRtn) >= 1%
  2) 전일 AdjRtn < 0

모멘텀 종료 조건:
  - AdjRtn < 0 (음수 전환)

분석 내용:
  - 모멘텀 지속 일수별 빈도 분포
  - 지속 일수별 평균 누적 수익률
  - 전체 통계 요약
"""

import sys
import os
import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings('ignore')

sys.path.append(os.path.join(r'C:\Users\intern9', 'truston_quant_dev'))

from Theme_model.BETA import (
    load_kospi_returns,
    get_theme_returns,
    compute_market_adjusted_returns,
)
from Theme_model.settings import (
    KOSPI_EXCEL_PATH,
    START_DATE,
    END_DATE,
    ROLLING_WINDOW,
)
from util.database2 import MSSQL, DBConfig


# ============================================================
# 모멘텀 시작 조건 정의
# ============================================================
MOMENTUM_START_THRESHOLD = 0.01   # AdjRtn >= 1%
# 전일 AdjRtn < 0 (코드에서 직접 체크)


# ============================================================
# 개별 테마의 모멘텀 에피소드 탐색
# ============================================================
def find_momentum_episodes(adj_series: pd.Series,
                           theme_rtn_series: pd.Series = None,
                           start_threshold: float = MOMENTUM_START_THRESHOLD) -> list:
    """
    단일 테마의 AdjRtn 시계열에서 모멘텀 에피소드를 탐색

    Parameters:
        adj_series: 해당 테마의 AdjRtn 시계열 (index=PfmDate, value=AdjRtn)
        theme_rtn_series: 해당 테마의 ThemeRtn 시계열 (선택)
        start_threshold: 모멘텀 시작 임계값

    Returns:
        episodes: list of dict
            - start_date: 모멘텀 시작일
            - end_date: 모멘텀 종료일
            - duration: 지속 일수 (시작일 포함)
            - adj_returns: 에피소드 기간의 AdjRtn 리스트
            - cum_adj_rtn: 에피소드 기간 누적 AdjRtn
            - cum_theme_rtn: 에피소드 기간 누적 ThemeRtn (있는 경우)
    """
    values = adj_series.values
    dates = adj_series.index.tolist()
    n = len(values)

    if n < 2:
        return []

    episodes = []
    i = 1  # 전일 비교를 위해 1부터 시작

    while i < n:
        # ── 모멘텀 시작 조건 체크 ──
        prev_adj = values[i - 1]
        curr_adj = values[i]

        # 조건 1: 당일 AdjRtn >= 1%
        # 조건 2: 전일 AdjRtn < 0
        if curr_adj >= start_threshold and prev_adj < 0:
            # 모멘텀 시작!
            start_idx = i
            start_date = dates[i]

            # ── 모멘텀 지속 구간 탐색 ──
            j = i + 1
            while j < n and values[j] >= 0:  # AdjRtn >= 0이면 지속
                j += 1

            # 에피소드 범위: start_idx ~ j-1 (j에서 음수 전환 or 데이터 끝)
            end_idx = j - 1
            end_date = dates[end_idx]
            duration = end_idx - start_idx + 1

            # 수익률 수집
            ep_adj = values[start_idx:end_idx + 1].tolist()
            cum_adj = float((1 + pd.Series(ep_adj)).prod() - 1)

            ep_info = {
                'start_date': start_date,
                'end_date': end_date,
                'duration': duration,
                'adj_returns': ep_adj,
                'cum_adj_rtn': cum_adj,
            }

            # ThemeRtn 누적 (있는 경우)
            if theme_rtn_series is not None:
                ep_theme = theme_rtn_series.iloc[start_idx:end_idx + 1].values.tolist()
                ep_info['cum_theme_rtn'] = float((1 + pd.Series(ep_theme)).prod() - 1)

            episodes.append(ep_info)

            # 다음 탐색은 에피소드 종료 후부터
            i = j + 1
        else:
            i += 1

    return episodes


# ============================================================
# 전체 테마에 대해 에피소드 탐색
# ============================================================
def analyze_all_themes(result_df: pd.DataFrame) -> pd.DataFrame:
    """
    모든 테마에 대해 모멘텀 에피소드를 탐색하고 결과를 DataFrame으로 반환

    Returns:
        episodes_df: 전체 에피소드 DataFrame
            columns: THEME_ID, start_date, end_date, duration, cum_adj_rtn, cum_theme_rtn
    """
    # 피벗 생성
    adj_pivot = result_df.pivot_table(
        index='PfmDate', columns='THEME_ID', values='AdjRtn', aggfunc='first'
    ).sort_index()

    theme_pivot = result_df.pivot_table(
        index='PfmDate', columns='THEME_ID', values='ThemeRtn', aggfunc='first'
    ).sort_index()

    themes = adj_pivot.columns.tolist()
    all_episodes = []

    print(f"\n  {len(themes)}개 테마에 대해 모멘텀 에피소드 탐색 중...")

    for i, theme_id in enumerate(themes):
        if (i + 1) % 50 == 0:
            print(f"    진행: {i+1}/{len(themes)}")

        adj_s = adj_pivot[theme_id].dropna()
        theme_s = theme_pivot[theme_id].reindex(adj_s.index)

        episodes = find_momentum_episodes(adj_s, theme_s)

        for ep in episodes:
            all_episodes.append({
                'THEME_ID': theme_id,
                'start_date': ep['start_date'],
                'end_date': ep['end_date'],
                'duration': ep['duration'],
                'cum_adj_rtn': ep['cum_adj_rtn'],
                'cum_theme_rtn': ep.get('cum_theme_rtn', np.nan),
            })

    if not all_episodes:
        print("  ⚠️ 모멘텀 에피소드 없음")
        return pd.DataFrame()

    episodes_df = pd.DataFrame(all_episodes)
    print(f"  ✅ 총 {len(episodes_df)}개 에피소드 발견")

    return episodes_df


# ============================================================
# 지속 일수별 분포 출력
# ============================================================
def print_duration_distribution(episodes_df: pd.DataFrame):
    """지속 일수별 빈도 및 비율 출력"""
    total = len(episodes_df)
    dur_counts = episodes_df['duration'].value_counts().sort_index()

    max_dur = dur_counts.index.max()

    print(f"\n{'='*65}")
    print(f"  📊 모멘텀 지속 일수별 분포 (총 {total}개 에피소드)")
    print(f"{'='*65}")
    print(f"  {'지속일수':>8}  {'에피소드수':>10}  {'비율':>8}  {'누적비율':>8}  {'평균AdjRtn':>12}  {'중앙AdjRtn':>12}")
    print(f"  {'-'*8}  {'-'*10}  {'-'*8}  {'-'*8}  {'-'*12}  {'-'*12}")

    cum_pct = 0.0
    for dur in range(1, max_dur + 1):
        count = dur_counts.get(dur, 0)
        pct = count / total * 100
        cum_pct += pct

        # 해당 지속일수의 평균/중앙 누적 수익률
        subset = episodes_df[episodes_df['duration'] == dur]
        if len(subset) > 0:
            avg_rtn = subset['cum_adj_rtn'].mean() * 100
            med_rtn = subset['cum_adj_rtn'].median() * 100
        else:
            avg_rtn = 0.0
            med_rtn = 0.0

        if count > 0:
            print(f"  {dur:>6}일  {count:>10}개  {pct:>7.1f}%  {cum_pct:>7.1f}%  "
                  f"{'+' if avg_rtn >= 0 else ''}{avg_rtn:>10.2f}%  "
                  f"{'+' if med_rtn >= 0 else ''}{med_rtn:>10.2f}%")

    # 구간별 요약
    print(f"\n  {'─'*60}")
    print(f"  📊 구간별 요약")
    print(f"  {'─'*60}")

    bins = [(1, 1), (2, 2), (3, 3), (4, 5), (6, 10), (11, 20), (21, None)]
    bin_labels = ['1일', '2일', '3일', '4~5일', '6~10일', '11~20일', '21일+']

    for (lo, hi), label in zip(bins, bin_labels):
        if hi is None:
            subset = episodes_df[episodes_df['duration'] >= lo]
        else:
            subset = episodes_df[(episodes_df['duration'] >= lo) &
                                  (episodes_df['duration'] <= hi)]

        n = len(subset)
        if n == 0:
            continue

        pct = n / total * 100
        avg_rtn = subset['cum_adj_rtn'].mean() * 100
        avg_theme = subset['cum_theme_rtn'].mean() * 100

        print(f"  {label:>8}  {n:>6}개 ({pct:>5.1f}%)  "
              f"평균AdjRtn: {'+' if avg_rtn >= 0 else ''}{avg_rtn:.2f}%  "
              f"평균ThemeRtn: {'+' if avg_theme >= 0 else ''}{avg_theme:.2f}%")


# ============================================================
# 전체 통계 요약
# ============================================================
def print_summary_stats(episodes_df: pd.DataFrame):
    """전체 통계 요약"""
    total = len(episodes_df)
    n_themes = episodes_df['THEME_ID'].nunique()

    print(f"\n{'='*65}")
    print(f"  📊 전체 통계 요약")
    print(f"{'='*65}")
    print(f"  총 에피소드 수:          {total}개")
    print(f"  관련 테마 수:            {n_themes}개")
    print(f"  테마당 평균 에피소드:    {total / n_themes:.1f}개")
    print(f"")
    print(f"  평균 지속 일수:          {episodes_df['duration'].mean():.2f}일")
    print(f"  중앙 지속 일수:          {episodes_df['duration'].median():.1f}일")
    print(f"  최대 지속 일수:          {episodes_df['duration'].max()}일")
    print(f"  지속 일수 std:           {episodes_df['duration'].std():.2f}일")
    print(f"")
    print(f"  평균 누적 AdjRtn:        {episodes_df['cum_adj_rtn'].mean()*100:.3f}%")
    print(f"  중앙 누적 AdjRtn:        {episodes_df['cum_adj_rtn'].median()*100:.3f}%")
    print(f"  평균 누적 ThemeRtn:      {episodes_df['cum_theme_rtn'].mean()*100:.3f}%")

    # 지속일수별 확률 (조건부)
    print(f"\n  {'─'*60}")
    print(f"  📊 조건부 지속 확률")
    print(f"  {'─'*60}")
    print(f"  (= N일 이상 지속된 에피소드 중 N+1일 이상 지속될 확률)")
    print(f"")

    max_dur = min(episodes_df['duration'].max(), 20)
    for d in range(1, max_dur + 1):
        n_at_least_d = (episodes_df['duration'] >= d).sum()
        n_at_least_d1 = (episodes_df['duration'] >= d + 1).sum()

        if n_at_least_d == 0:
            break

        prob = n_at_least_d1 / n_at_least_d * 100
        print(f"  {d}일 지속 → {d+1}일 이상 지속 확률: "
              f"{prob:>5.1f}%  ({n_at_least_d1}/{n_at_least_d})")


# ============================================================
# 메인
# ============================================================
def main():
    print("=" * 65)
    print("  테마 모멘텀 지속성 분석")
    print(f"  시작 조건: AdjRtn >= {MOMENTUM_START_THRESHOLD*100:.0f}% AND 전일 AdjRtn < 0")
    print(f"  종료 조건: AdjRtn < 0 (음수 전환)")
    print(f"  분석기간: {START_DATE} ~ {END_DATE}")
    print(f"  롤링 윈도우: {ROLLING_WINDOW}거래일")
    print("=" * 65)

    # ── Step 1: 데이터 로드 ──
    print("\n[Step 1] 코스피 수익률 로드")
    kospi_df = load_kospi_returns(KOSPI_EXCEL_PATH, START_DATE, END_DATE)

    print(f"\n[Step 2] THEME_RTN 조회 (DB)")
    db = MSSQL(database=DBConfig.TRSTDEV_DB)
    try:
        theme_df = get_theme_returns(db, START_DATE, END_DATE)
    finally:
        db.close()

    # ── Step 2: 시장조정 수익률 계산 ──
    print(f"\n[Step 3] 시장조정 수익률 계산")
    result_df = compute_market_adjusted_returns(theme_df, kospi_df, START_DATE, ROLLING_WINDOW)

    if result_df.empty:
        print("  ⚠️ 결과 데이터 없음")
        return

    # ── Step 3: 모멘텀 에피소드 탐색 ──
    print(f"\n[Step 4] 모멘텀 에피소드 탐색")
    episodes_df = analyze_all_themes(result_df)

    if episodes_df.empty:
        return

    # ── Step 4: 결과 출력 ──
    print_duration_distribution(episodes_df)
    print_summary_stats(episodes_df)

    # ── 엑셀 저장 ──
    output_dir = os.path.dirname(os.path.abspath(__file__))
    output_path = os.path.join(output_dir, 'momentum_duration_analysis.xlsx')

    try:
        with pd.ExcelWriter(output_path, engine='openpyxl') as writer:
            episodes_df.to_excel(writer, sheet_name='에피소드_상세', index=False)

            # 지속일수별 요약
            dur_summary = episodes_df.groupby('duration').agg(
                count=('duration', 'size'),
                avg_cum_adj=('cum_adj_rtn', 'mean'),
                med_cum_adj=('cum_adj_rtn', 'median'),
                std_cum_adj=('cum_adj_rtn', 'std'),
                avg_cum_theme=('cum_theme_rtn', 'mean'),
            ).reset_index()
            dur_summary['pct'] = dur_summary['count'] / dur_summary['count'].sum() * 100
            dur_summary.to_excel(writer, sheet_name='지속일수별_요약', index=False)

            # 테마별 요약
            theme_summary = episodes_df.groupby('THEME_ID').agg(
                n_episodes=('duration', 'size'),
                avg_duration=('duration', 'mean'),
                max_duration=('duration', 'max'),
                avg_cum_adj=('cum_adj_rtn', 'mean'),
            ).sort_values('n_episodes', ascending=False).reset_index()
            theme_summary.to_excel(writer, sheet_name='테마별_요약', index=False)

        print(f"\n  📁 결과 저장: {output_path}")
    except Exception as e:
        print(f"\n  ⚠️ 엑셀 저장 실패: {e}")


if __name__ == '__main__':
    main()

# python Theme_model/mom_check.py