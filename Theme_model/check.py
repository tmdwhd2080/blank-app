# -*- coding: utf-8 -*-
"""
테마 순환매 Gap 분석: 상승 강도 × 연속일수 매트릭스
- streak 조건(일별 최소 수익률, 최소 연속일수)을 격자로 조합
- 각 조건별 평균/중앙값 gap, streak 수, 미복귀 비율 등을 매트릭스로 출력
- 특정 조건에서의 상세 gap 분포도 확인 가능
"""

import sys
import os
import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings('ignore')

sys.path.append(os.path.join(r'C:\Users\intern9', 'truston_quant_dev'))
from util.database2 import MSSQL, DBConfig

from Theme_model.settings import (
    KOSPI_EXCEL_PATH,
    START_DATE,
    END_DATE,
    ROLLING_WINDOW,
)
from Theme_model.BETA import (
    load_kospi_returns,
    get_theme_returns,
    compute_market_adjusted_returns,
)


# ============================================================
# 사용자 설정 영역
# ============================================================

# 분석할 수익률 컬럼 ('AdjRtn' = 시장조정, 'ThemeRtn' = 절대수익률)
RTN_COL = 'AdjRtn'

# 격자 분석용 파라미터 범위
# 일별 최소 수익률 (예: 0.3%, 0.5%, 0.7%, 1.0%, 1.5%, 2.0%)
DAILY_RTN_GRID = [0.003, 0.005, 0.007, 0.01, 0.015, 0.02]

# 연속일수 (정확히 N일 — "N일 이상"이 아님)
STREAK_DAYS_GRID = [1, 2, 3, 4, 5]

# 상세 분석용 (특정 조건 하나를 골라서 gap 분포 상세 출력)
DETAIL_MIN_DAILY_RTN = 0.005   # 0.5%
DETAIL_MIN_STREAK_DAYS = 2     # 2일

# ============================================================
# 복귀(다음 streak) 판단 고정 조건
# gap 종료를 판단하는 기준 — 앞 streak 조건과 무관하게 고정
# ============================================================
NEXT_THEME_RTN_THRESHOLD = 0.02   # ThemeRtn ≥ 2%
NEXT_ADJ_RTN_THRESHOLD = 0.005    # AdjRtn ≥ 1%

    
# ============================================================
# 핵심 함수: streak 추출
# ============================================================
def extract_streaks(series: pd.Series, min_days: int = 2,
                    min_daily_rtn: float = 0.0) -> list:
    """연속 양(+) 수익률 구간 추출 (일별 최소 수익률 조건 포함)"""
    qualified = (series >= min_daily_rtn).values
    streaks = []
    current_start = None
    current_len = 0

    for i in range(len(qualified)):
        if qualified[i]:
            if current_start is None:
                current_start = i
            current_len += 1
        else:
            if current_len >= min_days:
                # streak 구간의 실제 수익률 계산
                vals = series.values[current_start: current_start + current_len]
                streaks.append({
                    'start_idx': current_start,
                    'end_idx': current_start + current_len - 1,
                    'days': current_len,
                    'cum_rtn': float((1 + pd.Series(vals)).prod() - 1),
                    'avg_daily_rtn': float(np.mean(vals)),
                })
            current_start = None
            current_len = 0

    if current_len >= min_days:
        vals = series.values[current_start: current_start + current_len]
        streaks.append({
            'start_idx': current_start,
            'end_idx': current_start + current_len - 1,
            'days': current_len,
            'cum_rtn': float((1 + pd.Series(vals)).prod() - 1),
            'avg_daily_rtn': float(np.mean(vals)),
        })

    return streaks


# ============================================================
# 단일 조건에서 전체 테마 gap 통계 계산 (비대칭)
# - 앞 streak: 격자 조건 (rtn_col 기준, 정확히 N일)
# - 복귀 판단: ThemeRtn ≥ NEXT_THEME_RTN_THRESHOLD AND
#              AdjRtn ≥ NEXT_ADJ_RTN_THRESHOLD (하루라도 충족 시 복귀)
# ============================================================
def compute_gap_stats_for_condition(result_df: pd.DataFrame,
                                    rtn_col: str,
                                    exact_streak_days: int,
                                    min_daily_rtn: float) -> dict:
    """
    비대칭 gap 분석:
    - 앞 streak: rtn_col 기준, 일별 >= min_daily_rtn, 정확히 exact_streak_days일
    - 복귀 판단: ThemeRtn >= NEXT_THEME_RTN_THRESHOLD AND
                 AdjRtn >= NEXT_ADJ_RTN_THRESHOLD 인 날이 나오면 복귀
    - gap = 앞 streak 종료 다음날 ~ 복귀일 전날
    """
    themes = sorted(result_df['THEME_ID'].unique())
    all_gaps = []
    all_streak_info = []

    n_no_streak = 0      # 앞 streak 자체가 없는 테마
    n_single = 0         # 앞 streak 1회, 이후 복귀 없음
    n_multi = 0          # 앞 streak 후 복귀 경험 있는 테마

    for theme_id in themes:
        sub = result_df[result_df['THEME_ID'] == theme_id].copy()
        sub = sub.sort_values('PfmDate').reset_index(drop=True)

        series_rtn = sub[rtn_col].values
        series_theme = sub['ThemeRtn'].values
        series_adj = sub['AdjRtn'].values
        n_days = len(sub)

        # (1) 앞 streak 추출: rtn_col 기준으로 모든 streak 뽑고 정확히 N일 필터
        rtn_series = sub.set_index('PfmDate')[rtn_col]
        all_streaks = extract_streaks(rtn_series, min_days=1,
                                      min_daily_rtn=min_daily_rtn)
        front_streaks = [s for s in all_streaks if s['days'] == exact_streak_days]

        if len(front_streaks) == 0:
            n_no_streak += 1
            continue

        # (2) 복귀 조건 마스크: ThemeRtn >= threshold AND AdjRtn >= threshold
        next_mask = ((series_theme >= NEXT_THEME_RTN_THRESHOLD) &
                     (series_adj >= NEXT_ADJ_RTN_THRESHOLD))

        # (3) 각 앞 streak 종료 후, 가장 빠른 복귀일 찾기
        has_return = False
        for fs in front_streaks:
            search_start = fs['end_idx'] + 1  # streak 종료 다음날부터 탐색

            # search_start 이후에서 next_mask == True인 첫 인덱스 찾기
            return_idx = None
            for j in range(search_start, n_days):
                if next_mask[j]:
                    return_idx = j
                    break

            if return_idx is not None:
                has_return = True
                gap = return_idx - fs['end_idx'] - 1  # 순수 대기일수

                all_gaps.append(gap)
                all_streak_info.append({
                    'gap_days': gap,
                    'prev_streak_days': fs['days'],
                    'prev_streak_cum_rtn': fs['cum_rtn'],
                    'prev_streak_avg_daily': fs['avg_daily_rtn'],
                    'next_day_theme_rtn': float(series_theme[return_idx]),
                    'next_day_adj_rtn': float(series_adj[return_idx]),
                })

        if has_return:
            n_multi += 1
        else:
            n_single += 1  # streak은 있지만 이후 복귀 없음

    total = len(themes)
    has_streak = n_single + n_multi

    result = {
        'total_themes': total,
        'n_no_streak': n_no_streak,
        'n_single': n_single,
        'n_multi': n_multi,
        'total_gaps': len(all_gaps),
        'no_return_rate': n_single / has_streak * 100 if has_streak > 0 else 0.0,
    }

    if all_gaps:
        g = np.array(all_gaps)
        result.update({
            'avg_gap': g.mean(),
            'med_gap': np.median(g),
            'p25_gap': np.percentile(g, 25),
            'p75_gap': np.percentile(g, 75),
            'pct_within_5d': (g <= 5).mean() * 100,
            'pct_within_10d': (g <= 10).mean() * 100,
            'pct_within_20d': (g <= 20).mean() * 100,
        })
    else:
        result.update({
            'avg_gap': np.nan, 'med_gap': np.nan,
            'p25_gap': np.nan, 'p75_gap': np.nan,
            'pct_within_5d': np.nan, 'pct_within_10d': np.nan,
            'pct_within_20d': np.nan,
        })

    return result, all_gaps, all_streak_info


# ============================================================
# 격자 분석: 강도 × 연속일수 매트릭스
# ============================================================
def run_grid_analysis(result_df: pd.DataFrame, rtn_col: str,
                      daily_rtn_grid: list, streak_days_grid: list) -> pd.DataFrame:
    """
    일별 최소 수익률 × 최소 연속일수 격자 조합별 gap 통계 계산
    """
    records = []
    total_combos = len(daily_rtn_grid) * len(streak_days_grid)
    done = 0

    for min_rtn in daily_rtn_grid:
        for exact_days in streak_days_grid:
            done += 1
            print(f"\r  진행: {done}/{total_combos} "
                  f"(일별≥{min_rtn*100:.1f}%, 연속={exact_days}일)", end='')

            stats, _, _ = compute_gap_stats_for_condition(
                result_df, rtn_col, exact_days, min_rtn
            )
            stats['min_daily_rtn'] = min_rtn
            stats['exact_streak_days'] = exact_days
            records.append(stats)

    print()
    return pd.DataFrame(records)


# ============================================================
# 매트릭스 출력 함수들
# ============================================================
def print_matrix(grid_df: pd.DataFrame, value_col: str, title: str,
                 fmt: str = '.1f', daily_rtn_grid: list = None,
                 streak_days_grid: list = None):
    """pivot 매트릭스를 콘솔에 출력"""
    pivot = grid_df.pivot_table(
        index='min_daily_rtn', columns='exact_streak_days',
        values=value_col, aggfunc='first'
    )

    print(f"\n{'='*75}")
    print(f"  {title}")
    print(f"  (행: 일별 최소 수익률 / 열: 정확히 N일 연속)")
    print(f"{'='*75}")

    # 헤더
    col_labels = [f"{d}일" for d in pivot.columns]
    header = f"  {'일별수익률':>10}  " + "  ".join(f"{c:>10}" for c in col_labels)
    print(header)
    print(f"  {'-'*10}  " + "  ".join(['-' * 10] * len(col_labels)))

    for rtn_val in pivot.index:
        row_label = f"{rtn_val*100:.1f}%+"
        cells = []
        for d in pivot.columns:
            val = pivot.loc[rtn_val, d]
            if pd.isna(val):
                cells.append(f"{'─':>10}")
            else:
                cells.append(f"{val:{fmt}}" if fmt else f"{val}")
                cells[-1] = f"{cells[-1]:>10}"
        print(f"  {row_label:>10}  " + "  ".join(cells))


def print_heatmap_ascii(grid_df: pd.DataFrame, value_col: str, title: str,
                        fmt: str = '.1f', reverse: bool = False):
    """ASCII 히트맵 (값 크기에 따라 음영)"""
    pivot = grid_df.pivot_table(
        index='min_daily_rtn', columns='exact_streak_days',
        values=value_col, aggfunc='first'
    )

    vals = pivot.values.flatten()
    vals = vals[~np.isnan(vals)]
    if len(vals) == 0:
        return

    vmin, vmax = vals.min(), vals.max()
    shades = ['░░', '▒▒', '▓▓', '██']

    print(f"\n  {title} (히트맵)")
    print(f"  {'':>10}  ", end='')
    for d in pivot.columns:
        print(f" {d}일  ", end='')
    print()

    for rtn_val in pivot.index:
        print(f"  {rtn_val*100:.1f}%+  ", end='')
        for d in pivot.columns:
            val = pivot.loc[rtn_val, d]
            if pd.isna(val):
                print(f"  ── ", end='')
            else:
                if vmax == vmin:
                    idx = 0
                else:
                    norm = (val - vmin) / (vmax - vmin)
                    if reverse:
                        norm = 1 - norm
                    idx = min(int(norm * len(shades)), len(shades) - 1)
                print(f"  {shades[idx]} ", end='')
        print()

    if reverse:
        print(f"  ░░=높음  ██=낮음")
    else:
        print(f"  ░░=낮음  ██=높음")


# ============================================================
# 상세 분석: 이전 streak 강도별 gap 분석
# ============================================================
def print_prev_streak_intensity_vs_gap(all_streak_info: list, label: str):
    """
    이전 streak의 강도(누적수익률, 연속일수)에 따른 gap 차이 분석
    """
    if not all_streak_info:
        print("  데이터 없음")
        return

    df = pd.DataFrame(all_streak_info)

    print(f"\n{'='*80}")
    print(f"  [{label}] 이전 streak 강도 → 복귀까지 gap 분석")
    print(f"  복귀 조건: ThemeRtn≥{NEXT_THEME_RTN_THRESHOLD*100:.0f}% "
          f"AND AdjRtn≥{NEXT_ADJ_RTN_THRESHOLD*100:.0f}%")
    print(f"{'='*80}")

    # --- (A) 이전 streak 누적수익률 구간별 ---
    rtn_bins = [0, 0.01, 0.02, 0.03, 0.05, 0.10, 1.0]
    rtn_labels = ['~1%', '1~2%', '2~3%', '3~5%', '5~10%', '10%+']
    df['prev_rtn_band'] = pd.cut(df['prev_streak_cum_rtn'], bins=rtn_bins,
                                  labels=rtn_labels, right=True)

    print(f"\n  (A) 이전 streak 누적수익률별 평균 gap")
    print(f"  {'수익률 구간':>12}  {'N':>6}  {'평균gap':>8}  {'중앙gap':>8}  "
          f"{'5일내':>7}  {'10일내':>7}")
    print(f"  {'-'*12}  {'-'*6}  {'-'*8}  {'-'*8}  {'-'*7}  {'-'*7}")

    for band in rtn_labels:
        sub = df[df['prev_rtn_band'] == band]
        if len(sub) == 0:
            print(f"  {band:>12}  {0:>6}  {'─':>8}  {'─':>8}  {'─':>7}  {'─':>7}")
            continue
        g = sub['gap_days']
        print(f"  {band:>12}  {len(sub):>6}  {g.mean():>7.1f}일  "
              f"{g.median():>7.0f}일  "
              f"{(g <= 5).mean()*100:>6.1f}%  "
              f"{(g <= 10).mean()*100:>6.1f}%")

    # --- (B) 이전 streak 연속일수별 ---
    day_bins = [0, 1, 2, 3, 4, 5, 7, 100]
    day_labels = ['1일', '2일', '3일', '4일', '5일', '6~7일', '8일+']
    df['prev_days_band'] = pd.cut(df['prev_streak_days'], bins=day_bins,
                                   labels=day_labels, right=True)

    print(f"\n  (B) 이전 streak 연속일수별 평균 gap")
    print(f"  {'연속일수':>12}  {'N':>6}  {'평균gap':>8}  {'중앙gap':>8}  "
          f"{'5일내':>7}  {'10일내':>7}")
    print(f"  {'-'*12}  {'-'*6}  {'-'*8}  {'-'*8}  {'-'*7}  {'-'*7}")

    for band in day_labels:
        sub = df[df['prev_days_band'] == band]
        if len(sub) == 0:
            print(f"  {band:>12}  {0:>6}  {'─':>8}  {'─':>8}  {'─':>7}  {'─':>7}")
            continue
        g = sub['gap_days']
        print(f"  {band:>12}  {len(sub):>6}  {g.mean():>7.1f}일  "
              f"{g.median():>7.0f}일  "
              f"{(g <= 5).mean()*100:>6.1f}%  "
              f"{(g <= 10).mean()*100:>6.1f}%")

    # --- (C) 2차원: 이전 streak (수익률 × 일수) → gap ---
    print(f"\n  (C) 이전 streak [수익률 × 일수] → 평균 gap (일)")

    pivot = df.pivot_table(
        index='prev_rtn_band', columns='prev_days_band',
        values='gap_days', aggfunc='mean'
    )

    if not pivot.empty:
        print(f"\n  {'':>12}", end='')
        for col in pivot.columns:
            print(f"  {col:>8}", end='')
        print()
        print(f"  {'-'*12}", end='')
        for _ in pivot.columns:
            print(f"  {'-'*8}", end='')
        print()

        for idx in pivot.index:
            print(f"  {idx:>12}", end='')
            for col in pivot.columns:
                val = pivot.loc[idx, col]
                if pd.isna(val):
                    print(f"  {'─':>8}", end='')
                else:
                    print(f"  {val:>7.1f}일", end='')
            print()

    # N 카운트 매트릭스
    pivot_n = df.pivot_table(
        index='prev_rtn_band', columns='prev_days_band',
        values='gap_days', aggfunc='count'
    )

    if not pivot_n.empty:
        print(f"\n  [샘플 수 (N)]")
        print(f"  {'':>12}", end='')
        for col in pivot_n.columns:
            print(f"  {col:>8}", end='')
        print()

        for idx in pivot_n.index:
            print(f"  {idx:>12}", end='')
            for col in pivot_n.columns:
                val = pivot_n.loc[idx, col]
                if pd.isna(val):
                    print(f"  {'─':>8}", end='')
                else:
                    print(f"  {int(val):>8}", end='')
            print()


# ============================================================
# 상세 gap 분포 (특정 조건)
# ============================================================
def print_detailed_gap_distribution(all_gaps: list, min_rtn: float,
                                     min_days: int, label: str):
    """특정 조건의 gap 분포 상세 출력"""
    if not all_gaps:
        print("  gap 데이터 없음")
        return

    gaps = np.array(all_gaps)
    total = len(gaps)

    print(f"\n{'='*75}")
    print(f"  [{label}] Gap 분포 상세")
    print(f"  조건: 일별 ≥ {min_rtn*100:.1f}% / 연속 ≥ {min_days}일")
    print(f"  총 gap 수: {total}  |  평균: {gaps.mean():.1f}일  |  "
          f"중앙값: {np.median(gaps):.0f}일")
    print(f"{'='*75}")

    # 히스토그램
    max_display = min(int(np.percentile(gaps, 97)), 40)
    bins = list(range(0, max_display + 2))
    counts, edges = np.histogram(gaps, bins=bins)
    max_count = counts.max() if len(counts) > 0 else 1
    bar_width = 45
    cumulative = 0

    print(f"\n  {'Gap':>6}  {'N':>5}  {'비율':>6}  {'누적':>6}  분포")
    print(f"  {'-'*6}  {'-'*5}  {'-'*6}  {'-'*6}  {'-'*bar_width}")

    for i in range(len(counts)):
        if counts[i] == 0 and edges[i] > np.percentile(gaps, 95):
            continue
        gap_val = int(edges[i])
        count = counts[i]
        pct = count / total * 100
        cumulative += pct
        bar_len = int(count / max_count * bar_width) if max_count > 0 else 0
        bar = '█' * bar_len
        print(f"  {gap_val:>4}일  {count:>5}  {pct:>5.1f}%  {cumulative:>5.1f}%  {bar}")

    over = (gaps > max_display).sum()
    if over > 0:
        cumulative += over / total * 100
        print(f"  {f'{max_display+1}+':>6}  {over:>5}  "
              f"{over/total*100:>5.1f}%  {cumulative:>5.1f}%")

    # 퍼센타일
    print(f"\n  퍼센타일:")
    for p in [10, 25, 50, 75, 90, 95]:
        print(f"    {p:>3}%ile = {np.percentile(gaps, p):.0f}일", end='  ')
        if p in [50, 95]:
            print()


# ============================================================
# 메인
# ============================================================
def main():
    print("=" * 75)
    print("  테마 순환매 Gap 분석: 상승 강도 × 연속일수 매트릭스")
    print(f"  기간: {START_DATE} ~ {END_DATE}")
    print(f"  앞 streak 수익률 기준: {RTN_COL}")
    print(f"  일별 수익률 격자: {[f'{r*100:.1f}%' for r in DAILY_RTN_GRID]}")
    print(f"  연속일수 격자:    {STREAK_DAYS_GRID}")
    print(f"  복귀 판단 (고정): ThemeRtn≥{NEXT_THEME_RTN_THRESHOLD*100:.0f}% "
          f"AND AdjRtn≥{NEXT_ADJ_RTN_THRESHOLD*100:.0f}%")
    print("=" * 75)

    # ─── 데이터 로드 ───
    print("\n[Step 1] 데이터 로드")
    kospi_df = load_kospi_returns(KOSPI_EXCEL_PATH, START_DATE, END_DATE)

    db = MSSQL(database=DBConfig.TRSTDEV_DB)
    try:
        theme_df = get_theme_returns(db, START_DATE, END_DATE)
    finally:
        db.close()

    print("[Step 2] 시장조정 수익률 계산")
    result_df = compute_market_adjusted_returns(theme_df, kospi_df, START_DATE, ROLLING_WINDOW)

    if result_df.empty:
        print("  데이터 없음")
        return

    n_themes = result_df['THEME_ID'].nunique()
    n_dates = result_df['PfmDate'].nunique()
    print(f"  데이터: {n_themes}개 테마 × {n_dates}거래일")

    # ═══════════════════════════════════════════════════
    # Part 1: 격자 매트릭스 분석
    # ═══════════════════════════════════════════════════
    print(f"\n{'#'*75}")
    print(f"#  Part 1: 강도 × 연속일수 격자 분석")
    print(f"{'#'*75}")
    print("\n[Step 3] 격자 분석 실행...")

    grid_df = run_grid_analysis(result_df, RTN_COL, DAILY_RTN_GRID, STREAK_DAYS_GRID)

    # 매트릭스 1: 평균 gap
    print_matrix(grid_df, 'avg_gap', '평균 Gap (일)', fmt='>7.1f')
    print_heatmap_ascii(grid_df, 'avg_gap', '평균 Gap', reverse=True)

    # 매트릭스 2: 중앙값 gap
    print_matrix(grid_df, 'med_gap', '중앙값 Gap (일)', fmt='>7.0f')

    # 매트릭스 3: 총 gap 수 (샘플 크기)
    print_matrix(grid_df, 'total_gaps', '총 Gap 수 (샘플 크기)', fmt='>7.0f')

    # 매트릭스 4: 5일 이내 복귀율
    print_matrix(grid_df, 'pct_within_5d', '5일 이내 복귀율 (%)', fmt='>7.1f')
    print_heatmap_ascii(grid_df, 'pct_within_5d', '5일 이내 복귀율')

    # 매트릭스 5: 10일 이내 복귀율
    print_matrix(grid_df, 'pct_within_10d', '10일 이내 복귀율 (%)', fmt='>7.1f')

    # 매트릭스 6: 미복귀 비율
    print_matrix(grid_df, 'no_return_rate', '미복귀 비율 (%, streak 경험 중)', fmt='>7.1f')
    print_heatmap_ascii(grid_df, 'no_return_rate', '미복귀 비율')

    # 매트릭스 7: 복귀 테마 수
    print_matrix(grid_df, 'n_multi', '복귀 경험 테마 수 (2회+ streak)', fmt='>7.0f')

    # ═══════════════════════════════════════════════════
    # Part 2: 특정 조건 상세 분석
    # ═══════════════════════════════════════════════════
    print(f"\n{'#'*75}")
    print(f"#  Part 2: 상세 분석 (일별≥{DETAIL_MIN_DAILY_RTN*100:.1f}%, "
          f"연속≥{DETAIL_MIN_STREAK_DAYS}일)")
    print(f"{'#'*75}")

    stats, detail_gaps, detail_streak_info = compute_gap_stats_for_condition(
        result_df, RTN_COL, DETAIL_MIN_STREAK_DAYS, DETAIL_MIN_DAILY_RTN
    )

    # 상세 gap 분포
    print_detailed_gap_distribution(
        detail_gaps, DETAIL_MIN_DAILY_RTN, DETAIL_MIN_STREAK_DAYS, RTN_COL
    )

    # 이전 streak 강도별 gap 분석
    print_prev_streak_intensity_vs_gap(detail_streak_info, RTN_COL)

    # ═══════════════════════════════════════════════════
    # Part 3: 핵심 인사이트 요약
    # ═══════════════════════════════════════════════════
    print(f"\n{'#'*75}")
    print(f"#  Part 3: 핵심 인사이트 요약")
    print(f"{'#'*75}")

    valid = grid_df.dropna(subset=['avg_gap'])
    if not valid.empty:
        # 가장 빠른 복귀 조건
        fastest = valid.loc[valid['avg_gap'].idxmin()]
        print(f"\n  ▶ 가장 빠른 평균 복귀:")
        print(f"    조건: 일별 ≥{fastest['min_daily_rtn']*100:.1f}%, "
              f"연속 {int(fastest['exact_streak_days'])}일")
        print(f"    평균 gap: {fastest['avg_gap']:.1f}일  "
              f"(N={int(fastest['total_gaps'])})")

        # 가장 느린 복귀 조건
        slowest = valid.loc[valid['avg_gap'].idxmax()]
        print(f"\n  ▶ 가장 느린 평균 복귀:")
        print(f"    조건: 일별 ≥{slowest['min_daily_rtn']*100:.1f}%, "
              f"연속 {int(slowest['exact_streak_days'])}일")
        print(f"    평균 gap: {slowest['avg_gap']:.1f}일  "
              f"(N={int(slowest['total_gaps'])})")

        # 강도 증가에 따른 gap 변화 (연속일수 2일 고정)
        days2 = valid[valid['exact_streak_days'] == 2].sort_values('min_daily_rtn')
        if len(days2) >= 2:
            print(f"\n  ▶ 연속 2일 고정, 일별 수익률 강도 증가 시:")
            for _, r in days2.iterrows():
                if pd.notna(r['avg_gap']):
                    print(f"    {r['min_daily_rtn']*100:.1f}%+ → "
                          f"평균 gap {r['avg_gap']:.1f}일 "
                          f"(N={int(r['total_gaps'])}, "
                          f"미복귀 {r['no_return_rate']:.1f}%)")

        # 연속일수 증가에 따른 gap 변화 (일별 0.5% 고정)
        rtn05 = valid[valid['min_daily_rtn'] == 0.005].sort_values('exact_streak_days')
        if len(rtn05) >= 2:
            print(f"\n  ▶ 일별 0.5%+ 고정, 연속일수 변화 시:")
            for _, r in rtn05.iterrows():
                if pd.notna(r['avg_gap']):
                    print(f"    {int(r['exact_streak_days'])}일 → "
                          f"평균 gap {r['avg_gap']:.1f}일 "
                          f"(N={int(r['total_gaps'])}, "
                          f"미복귀 {r['no_return_rate']:.1f}%)")

    print(f"\n{'='*75}")
    print("  분석 완료")
    print(f"{'='*75}")


if __name__ == '__main__':
    main()
#python Theme_model/check.py