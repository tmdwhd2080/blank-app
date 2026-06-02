# -*- coding: utf-8 -*-
"""
테마 순환매 간격(gap) 분석
- 각 테마별 "2일+ 연속 상승(일평균 0.5%+)" 종료 후 → 다음 streak 시작까지 대기일수
- streak 1회만 발생 후 미복귀 테마 비율 포함
- gap 1~12일 후 복귀한 streak의 평균 수익률 그래프
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
# 설정
# ============================================================
MIN_STREAK_DAYS = 2       # 최소 연속일수
MIN_DAILY_RTN = 0.01     # 최소 일별 수익률 (0.5%)


# ============================================================
# 연속 상승 구간 추출 (일별 최소 수익률 조건 추가)
# ============================================================
def extract_streaks(series: pd.Series, min_days: int = 2,
                    min_daily_rtn: float = 0.0) -> list:
    """
    일별 수익률 시리즈에서 연속 양(+) 수익률 구간 추출

    Parameters:
        series: 일별 수익률
        min_days: 최소 연속일수
        min_daily_rtn: 각 날의 최소 수익률 (0.005 = 0.5%)

    Returns:
        list of dict: [{'start_idx': int, 'end_idx': int, 'days': int}, ...]
    """
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
                streaks.append({
                    'start_idx': current_start,
                    'end_idx': current_start + current_len - 1,
                    'days': current_len,
                })
            current_start = None
            current_len = 0

    if current_len >= min_days:
        streaks.append({
            'start_idx': current_start,
            'end_idx': current_start + current_len - 1,
            'days': current_len,
        })

    return streaks


# ============================================================
# 순환매 간 간격(gap) + 복귀 streak 수익률 수집
# ============================================================
def compute_gaps_with_streak_returns(series: pd.Series,
                                     min_streak_days: int = 2,
                                     min_daily_rtn: float = 0.0) -> tuple:
    streaks = extract_streaks(series, min_days=min_streak_days,
                              min_daily_rtn=min_daily_rtn)
    values = series.values

    if len(streaks) < 2:
        return [], []

    gaps = []
    gap_details = []

    for i in range(1, len(streaks)):
        prev_end = streaks[i - 1]['end_idx']
        next_start = streaks[i]['start_idx']
        next_end = streaks[i]['end_idx']
        gap = next_start - prev_end - 1
        gaps.append(gap)

        streak_rtns = values[next_start: next_end + 1]
        cum_rtn = (1 + pd.Series(streak_rtns)).prod() - 1
        daily_rtn = np.mean(streak_rtns)

        gap_details.append({
            'gap_days': gap,
            'next_streak_days': streaks[i]['days'],
            'next_streak_cum_rtn': cum_rtn,
            'next_streak_daily_rtn': daily_rtn,
        })

    return gaps, gap_details


# ============================================================
# 전체 테마 분석
# ============================================================
def analyze_rotation_gaps(result_df: pd.DataFrame, rtn_col: str = 'AdjRtn',
                          min_streak_days: int = 2,
                          min_daily_rtn: float = 0.0) -> tuple:
    themes = sorted(result_df['THEME_ID'].unique())
    all_gaps = []
    all_gap_details = []
    theme_stats = []

    n_no_streak = 0
    n_single_streak = 0
    n_multi_streak = 0
    single_streak_themes = []

    for theme_id in themes:
        sub = result_df[result_df['THEME_ID'] == theme_id].copy()
        sub = sub.sort_values('PfmDate')
        series = sub.set_index('PfmDate')[rtn_col]

        streaks = extract_streaks(series, min_days=min_streak_days,
                                  min_daily_rtn=min_daily_rtn)
        n_streaks = len(streaks)

        if n_streaks == 0:
            n_no_streak += 1
            continue

        if n_streaks == 1:
            n_single_streak += 1
            days_since = len(series) - 1 - streaks[0]['end_idx']
            single_streak_themes.append({
                'THEME_ID': theme_id,
                'streak_days': streaks[0]['days'],
                'days_since_last': days_since,
            })
            continue

        n_multi_streak += 1
        gaps, gap_details = compute_gaps_with_streak_returns(
            series, min_streak_days=min_streak_days, min_daily_rtn=min_daily_rtn
        )
        all_gaps.extend(gaps)
        all_gap_details.extend(gap_details)

        theme_stats.append({
            'THEME_ID': theme_id,
            'n_streaks': n_streaks,
            'n_gaps': len(gaps),
            'avg_gap': np.mean(gaps),
            'med_gap': np.median(gaps),
            'min_gap': min(gaps),
            'max_gap': max(gaps),
        })

    theme_stats_df = pd.DataFrame(theme_stats)
    single_streak_df = pd.DataFrame(single_streak_themes)

    no_return_stats = {
        'total_themes': len(themes),
        'n_no_streak': n_no_streak,
        'n_single_streak': n_single_streak,
        'n_multi_streak': n_multi_streak,
        'single_streak_df': single_streak_df,
    }

    return all_gaps, theme_stats_df, no_return_stats, all_gap_details


# ============================================================
# 미복귀 통계 출력
# ============================================================
def print_no_return_stats(no_return_stats: dict, label: str):
    total = no_return_stats['total_themes']
    n_no = no_return_stats['n_no_streak']
    n_single = no_return_stats['n_single_streak']
    n_multi = no_return_stats['n_multi_streak']
    single_df = no_return_stats['single_streak_df']

    print(f"\n{'='*70}")
    print(f"  [{label}] 테마별 순환매 발생 현황")
    print(f"{'='*70}")
    print(f"  전체 테마:           {total}개")
    print(f"  ─────────────────────────────────────────")
    print(f"  순환매 0회 (streak 없음):   {n_no:>5}개  ({n_no/total*100:>5.1f}%)")
    print(f"  순환매 1회 (미복귀):        {n_single:>5}개  ({n_single/total*100:>5.1f}%)")
    print(f"  순환매 2회+ (복귀 경험):    {n_multi:>5}개  ({n_multi/total*100:>5.1f}%)")
    print(f"  ─────────────────────────────────────────")

    has_streak = n_single + n_multi
    if has_streak > 0:
        print(f"\n  streak 경험 테마 중 미복귀 비율:")
        print(f"    {n_single}/{has_streak} = {n_single/has_streak*100:.1f}%")
        print(f"    (1번 순환매 후 다시는 2일+ 연속 0.5%+ 상승이 없었던 테마)")

    if not single_df.empty:
        print(f"\n  [미복귀 테마 상세 - 마지막 streak 이후 경과일수]")
        avg_since = single_df['days_since_last'].mean()
        med_since = single_df['days_since_last'].median()
        print(f"    평균 경과일: {avg_since:.0f}일")
        print(f"    중앙값:      {med_since:.0f}일")
        print(f"    최소/최대:   {single_df['days_since_last'].min()} / "
              f"{single_df['days_since_last'].max()}일")

        bins = [0, 20, 60, 120, 250, 9999]
        labels_b = ['~20일', '21~60일', '61~120일', '121~250일', '250일+']
        single_df['band'] = pd.cut(single_df['days_since_last'], bins=bins, labels=labels_b)
        band_counts = single_df['band'].value_counts().sort_index()
        print(f"\n    경과일수 분포:")
        for band, cnt in band_counts.items():
            print(f"      {band:>12}: {cnt:>4}개 ({cnt/len(single_df)*100:.1f}%)")


# ============================================================
# 히스토그램 출력
# ============================================================
def print_gap_histogram(all_gaps: list, title: str, max_bar: int = 50,
                        max_display: int = 40):
    if not all_gaps:
        print("  gap 데이터 없음")
        return

    gaps = np.array(all_gaps)
    total = len(gaps)

    print(f"\n{'='*80}")
    print(f"  {title}")
    print(f"{'='*80}")
    print(f"  총 gap 수: {total}  |  평균: {gaps.mean():.1f}일  |  "
          f"중앙값: {np.median(gaps):.0f}일  |  최대: {gaps.max()}일")
    print(f"{'='*80}")

    max_gap = min(int(gaps.max()), max_display)
    bins = list(range(0, max_gap + 2))
    counts, edges = np.histogram(gaps, bins=bins)
    max_count = counts.max() if len(counts) > 0 else 1

    cumulative = 0

    print(f"\n  {'Gap(일)':>10}  {'횟수':>6}  {'비율':>6}  {'누적':>6}  분포")
    print(f"  {'-'*10}  {'-'*6}  {'-'*6}  {'-'*6}  {'-'*max_bar}")

    for i in range(len(counts)):
        if counts[i] == 0 and edges[i] > np.percentile(gaps, 95):
            continue

        label = f"{int(edges[i]):>4}"
        count = counts[i]
        pct = count / total * 100
        cumulative += pct
        bar_len = int(count / max_count * max_bar) if max_count > 0 else 0
        bar = '█' * bar_len

        print(f"  {label:>10}  {count:>6}  {pct:>5.1f}%  {cumulative:>5.1f}%  {bar}")

    over = (gaps > max_display).sum()
    if over > 0:
        cumulative += over / total * 100
        print(f"  {f'{max_display+1}+':>10}  {over:>6}  {over/total*100:>5.1f}%  {cumulative:>5.1f}%")

    print(f"\n  {'─'*60}")
    for threshold in [5, 10, 15, 20, 30]:
        pct = (gaps <= threshold).mean() * 100
        print(f"  {threshold}일 이내 복귀: {pct:.1f}% ({(gaps <= threshold).sum()}/{total})")


# ============================================================
# 구간별 상세 통계
# ============================================================
def print_gap_band_stats(all_gaps: list):
    if not all_gaps:
        return

    gaps = np.array(all_gaps)
    total = len(gaps)

    bands = [
        (0, 0, "0일 (연속 순환매)"),
        (1, 2, "1~2일"),
        (3, 5, "3~5일"),
        (6, 10, "6~10일 (1~2주)"),
        (11, 20, "11~20일 (2~4주)"),
        (21, 40, "21~40일 (1~2개월)"),
        (41, 60, "41~60일 (2~3개월)"),
        (61, None, "61일+ (3개월+)"),
    ]

    print(f"\n{'='*65}")
    print(f"  순환매 간격 구간별 분포")
    print(f"{'='*65}")
    print(f"  {'구간':>22}  {'횟수':>6}  {'비율':>6}  {'누적':>6}")
    print(f"  {'-'*22}  {'-'*6}  {'-'*6}  {'-'*6}")

    cumulative = 0
    for lo, hi, label in bands:
        if hi is None:
            mask = gaps >= lo
        else:
            mask = (gaps >= lo) & (gaps <= hi)
        count = mask.sum()
        pct = count / total * 100
        cumulative += pct
        print(f"  {label:>22}  {count:>6}  {pct:>5.1f}%  {cumulative:>5.1f}%")


# ============================================================
# 퍼센타일 통계
# ============================================================
def print_percentile_stats(all_gaps: list):
    if not all_gaps:
        return

    gaps = np.array(all_gaps)

    print(f"\n{'='*50}")
    print(f"  순환매 간격 퍼센타일")
    print(f"{'='*50}")

    for p in [10, 25, 50, 75, 90, 95, 99]:
        val = np.percentile(gaps, p)
        print(f"  {p:>3}%ile: {val:>5.0f}일")

    print(f"\n  평균: {gaps.mean():.1f}일")
    print(f"  표준편차: {gaps.std():.1f}일")
    print(f"  최소: {gaps.min()}일  /  최대: {gaps.max()}일")


# ============================================================
# gap N일 후 복귀한 streak의 평균 수익률 그래프
# ============================================================
def print_streak_return_by_gap(all_gap_details: list, label: str,
                                min_gap: int = 1, max_gap: int = 12,
                                bar_width: int = 40):
    if not all_gap_details:
        print("  데이터 없음")
        return

    df = pd.DataFrame(all_gap_details)

    print(f"\n{'='*85}")
    print(f"  [{label}] Gap N일 후 복귀한 streak의 평균 수익률")
    print(f"  = N일 쉬고 다시 시작된 '2일+ 연속 0.5%+ 상승'의 수익률")
    print(f"{'='*85}")

    stats = []
    for g in range(min_gap, max_gap + 1):
        sub = df[df['gap_days'] == g]
        if len(sub) == 0:
            stats.append({
                'gap': g, 'n': 0,
                'avg_cum': 0.0, 'med_cum': 0.0,
                'avg_days': 0.0, 'avg_daily': 0.0,
                'win_rate': 0.0,
            })
        else:
            stats.append({
                'gap': g,
                'n': len(sub),
                'avg_cum': sub['next_streak_cum_rtn'].mean(),
                'med_cum': sub['next_streak_cum_rtn'].median(),
                'avg_days': sub['next_streak_days'].mean(),
                'avg_daily': sub['next_streak_daily_rtn'].mean(),
                'win_rate': (sub['next_streak_cum_rtn'] > 0).mean() * 100,
            })

    stats_df = pd.DataFrame(stats)
    valid = stats_df[stats_df['n'] > 0]

    if valid.empty:
        print("  유효 데이터 없음")
        return

    max_abs = max(abs(valid['avg_cum'].min()), abs(valid['avg_cum'].max()), 1e-10)

    # 수치 테이블
    print(f"\n  {'Gap':>5}  {'N':>6}  {'평균일수':>8}  {'일평균':>9}  "
          f"{'누적평균':>10}  {'누적중앙':>10}  {'양(+)':>7}")
    print(f"  {'-'*5}  {'-'*6}  {'-'*8}  {'-'*9}  "
          f"{'-'*10}  {'-'*10}  {'-'*7}")

    for _, r in stats_df.iterrows():
        g = int(r['gap'])
        n = int(r['n'])
        if n == 0:
            print(f"  {g:>3}일  {n:>6}  {'─':>8}  {'─':>9}  "
                  f"{'─':>10}  {'─':>10}  {'─':>7}")
        else:
            print(f"  {g:>3}일  {n:>6}  {r['avg_days']:>7.1f}일  "
                  f"{r['avg_daily']*100:>+8.3f}%  "
                  f"{r['avg_cum']*100:>+9.3f}%  "
                  f"{r['med_cum']*100:>+9.3f}%  "
                  f"{r['win_rate']:>6.1f}%")

    # 바 차트
    half_bar = bar_width // 2

    print(f"\n  Gap 후 복귀 streak 누적수익률 차트")
    print(f"  {'─'*75}")

    left_pct = f"{-max_abs*100:+.2f}%"
    right_pct = f"{+max_abs*100:+.2f}%"
    print(f"  {'':>5}  {left_pct:<{half_bar}}{'0':^3}{right_pct:>{half_bar}}")

    for _, r in stats_df.iterrows():
        g = int(r['gap'])
        n = int(r['n'])
        avg_cum = r['avg_cum']

        if n == 0:
            line = ' ' * half_bar + '│'
            print(f"  {g:>3}일  {line}  (N=0)")
            continue

        bar_len = int(abs(avg_cum) / max_abs * half_bar) if max_abs > 0 else 0
        bar_len = max(bar_len, 1)

        if avg_cum >= 0:
            left_part = ' ' * half_bar
            right_part = '█' * bar_len + ' ' * (half_bar - bar_len)
            line = left_part + '│' + right_part
        else:
            pad = half_bar - bar_len
            left_part = ' ' * pad + '█' * bar_len
            right_part = ' ' * half_bar
            line = left_part + '│' + right_part

        print(f"  {g:>3}일  {line}  {avg_cum*100:>+.3f}% (N={n})")

    print(f"  {'─'*75}")

    # 요약
    worst = valid.loc[valid['avg_cum'].idxmin()]
    best = valid.loc[valid['avg_cum'].idxmax()]
    print(f"\n  [요약]")
    print(f"    가장 약한 복귀: gap {int(worst['gap'])}일 → streak 평균 "
          f"{worst['avg_cum']*100:>+.3f}% (N={int(worst['n'])})")
    print(f"    가장 강한 복귀: gap {int(best['gap'])}일 → streak 평균 "
          f"{best['avg_cum']*100:>+.3f}% (N={int(best['n'])})")
    overall = valid['avg_cum'].mean()
    print(f"    1~12일 gap 전체 평균: {overall*100:>+.3f}%")

    short_gap = valid[valid['gap'] <= 3]
    long_gap = valid[(valid['gap'] >= 7) & (valid['gap'] <= 12)]
    if not short_gap.empty and not long_gap.empty:
        short_avg = short_gap['avg_cum'].mean()
        long_avg = long_gap['avg_cum'].mean()
        print(f"\n    gap 1~3일 평균: {short_avg*100:>+.3f}%")
        print(f"    gap 7~12일 평균: {long_avg*100:>+.3f}%")
        if short_avg > long_avg:
            print(f"    → 빠른 복귀일수록 streak이 강함 "
                  f"(차이: {(short_avg-long_avg)*100:+.3f}%p)")
        else:
            print(f"    → 늦은 복귀일수록 streak이 강함 "
                  f"(차이: {(long_avg-short_avg)*100:+.3f}%p)")


# ============================================================
# 메인
# ============================================================
def main():
    print("=" * 75)
    print("  테마 순환매 간격(gap) 분석")
    print(f"  streak 조건: {MIN_STREAK_DAYS}일+ 연속 / 일별 {MIN_DAILY_RTN*100:.1f}%+ 상승")
    print(f"  기간: {START_DATE} ~ {END_DATE}")
    print(f"  롤링 베타: {ROLLING_WINDOW}일")
    print("=" * 75)

    # 데이터 로드
    print("\n[Step 1] 데이터 로드")
    kospi_df = load_kospi_returns(KOSPI_EXCEL_PATH, START_DATE, END_DATE)

    db = MSSQL(database=DBConfig.TRSTDEV_DB)
    try:
        theme_df = get_theme_returns(db, START_DATE, END_DATE)
    finally:
        db.close()

    print("\n[Step 2] 시장조정 수익률 계산")
    result_df = compute_market_adjusted_returns(theme_df, kospi_df, START_DATE, ROLLING_WINDOW)

    if result_df.empty:
        print("  데이터 없음")
        return

    n_themes = result_df['THEME_ID'].nunique()
    n_dates = result_df['PfmDate'].nunique()
    print(f"\n  데이터: {n_themes}개 테마 × {n_dates}거래일")

    # ════════════════════════════════════════════════
    # AdjRtn 기준
    # ════════════════════════════════════════════════
    print("\n" + "#" * 75)
    print(f"#  시장조정 수익률(AdjRtn) 기준 / 일별 {MIN_DAILY_RTN*100:.1f}%+")
    print("#" * 75)

    gaps_adj, stats_adj, no_return_adj, details_adj = analyze_rotation_gaps(
        result_df, rtn_col='AdjRtn',
        min_streak_days=MIN_STREAK_DAYS, min_daily_rtn=MIN_DAILY_RTN,
    )

    print_no_return_stats(no_return_adj, "AdjRtn")
    print_gap_histogram(gaps_adj, "AdjRtn 기준: 순환매 간격 분포 (X=대기일수, Y=횟수)")
    print_gap_band_stats(gaps_adj)
    print_percentile_stats(gaps_adj)
    print_streak_return_by_gap(details_adj, "AdjRtn", min_gap=1, max_gap=12)

    if not stats_adj.empty:
        print(f"\n{'='*65}")
        print(f"  테마별 평균 순환매 간격 (상위 10 = 간격 짧은 테마)")
        print(f"{'='*65}")
        top10 = stats_adj.sort_values('avg_gap').head(10)
        print(f"  {'THEME_ID':>10}  {'순환매수':>8}  {'평균gap':>8}  {'중앙gap':>8}  {'최소':>5}  {'최대':>5}")
        print(f"  {'-'*10}  {'-'*8}  {'-'*8}  {'-'*8}  {'-'*5}  {'-'*5}")
        for _, r in top10.iterrows():
            print(f"  {r['THEME_ID']:>10}  {int(r['n_streaks']):>8}  "
                  f"{r['avg_gap']:>7.1f}일  {r['med_gap']:>7.0f}일  "
                  f"{int(r['min_gap']):>5}  {int(r['max_gap']):>5}")

    # ════════════════════════════════════════════════
    # ThemeRtn 기준
    # ════════════════════════════════════════════════
    print("\n" + "#" * 75)
    print(f"#  절대 수익률(ThemeRtn) 기준 / 일별 {MIN_DAILY_RTN*100:.1f}%+")
    print("#" * 75)

    gaps_theme, stats_theme, no_return_theme, details_theme = analyze_rotation_gaps(
        result_df, rtn_col='ThemeRtn',
        min_streak_days=MIN_STREAK_DAYS, min_daily_rtn=MIN_DAILY_RTN,
    )

    print_no_return_stats(no_return_theme, "ThemeRtn")
    print_gap_histogram(gaps_theme, "ThemeRtn 기준: 순환매 간격 분포 (X=대기일수, Y=횟수)")
    print_gap_band_stats(gaps_theme)
    print_percentile_stats(gaps_theme)
    print_streak_return_by_gap(details_theme, "ThemeRtn", min_gap=1, max_gap=12)

    # ════════════════════════════════════════════════
    # 비교 요약
    # ════════════════════════════════════════════════
    print(f"\n{'='*75}")
    print(f"  비교 요약 (streak 조건: {MIN_STREAK_DAYS}일+ / 일별 {MIN_DAILY_RTN*100:.1f}%+)")
    print(f"{'='*75}")

    for label, gaps, nr in [
        ("시장조정(AdjRtn)", gaps_adj, no_return_adj),
        ("절대수익(ThemeRtn)", gaps_theme, no_return_theme),
    ]:
        total = nr['total_themes']
        has_streak = nr['n_single_streak'] + nr['n_multi_streak']
        print(f"\n  [{label}]")
        print(f"    전체 테마: {total}개")
        print(f"    streak 없음: {nr['n_no_streak']}개 ({nr['n_no_streak']/total*100:.1f}%)")
        print(f"    1회만(미복귀): {nr['n_single_streak']}개 ({nr['n_single_streak']/total*100:.1f}%)")
        print(f"    2회+(복귀): {nr['n_multi_streak']}개 ({nr['n_multi_streak']/total*100:.1f}%)")
        if has_streak > 0:
            print(f"    미복귀 비율(streak 경험 중): "
                  f"{nr['n_single_streak']}/{has_streak} = "
                  f"{nr['n_single_streak']/has_streak*100:.1f}%")
        if gaps:
            g = np.array(gaps)
            print(f"    평균 간격: {g.mean():.1f}일  |  중앙값: {np.median(g):.0f}일")
            print(f"    5일 이내 복귀: {(g <= 5).mean()*100:.1f}%")
            print(f"    10일 이내 복귀: {(g <= 10).mean()*100:.1f}%")
            print(f"    20일 이내 복귀: {(g <= 20).mean()*100:.1f}%")


if __name__ == '__main__':
    main()


# python Theme_model/seasonality.py