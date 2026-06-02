"""
MSCI 편입/편출 이벤트 일별 평균 및 중앙값 누적 초과수익률 그래프
- 발표일 기준 Add / Delete
- 리밸런싱일 기준 Add / Delete
- 평균(Mean) 및 중앙값(Median) 비교

실행 방법:
    pip install pandas matplotlib
    python msci_excess_return_plot_median.py

입력 파일: msci_daily_excess_return.csv (MSCI_excess_return.py 실행 결과물)
"""

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

# 한글 폰트 설정 (Windows)
plt.rcParams['font.family'] = 'Malgun Gothic'
plt.rcParams['axes.unicode_minus'] = False


def load_and_prepare_data(input_file: str) -> pd.DataFrame:
    """데이터 로드 및 전처리"""
    df = pd.read_csv(input_file, encoding='utf-8-sig')
    return df


def calculate_statistics(df: pd.DataFrame, window: int = 30) -> dict:
    """
    조건별 일별 평균 및 중앙값 누적 초과수익률 계산
    
    Returns:
        dict: {
            'add_ann_mean': Series, 'add_ann_median': Series,
            'add_eff_mean': Series, 'add_eff_median': Series,
            'del_ann_mean': Series, 'del_ann_median': Series,
            'del_eff_mean': Series, 'del_eff_median': Series,
            'counts': dict
        }
    """
    # D-30 ~ D+30 컬럼명 생성
    day_cols = [f"D{d:+d}" if d != 0 else "D0" for d in range(-window, window + 1)]
    
    # 조건별 필터링
    add_ann_df = df[(df['Type'] == 'Add') & (df['Base'] == 'Announcement')][day_cols]
    add_eff_df = df[(df['Type'] == 'Add') & (df['Base'] == 'Effective')][day_cols]
    del_ann_df = df[(df['Type'] == 'Delete') & (df['Base'] == 'Announcement')][day_cols]
    del_eff_df = df[(df['Type'] == 'Delete') & (df['Base'] == 'Effective')][day_cols]
    
    # 평균 및 중앙값 계산
    result = {
        'add_ann_mean': add_ann_df.mean(),
        'add_ann_median': add_ann_df.median(),
        'add_eff_mean': add_eff_df.mean(),
        'add_eff_median': add_eff_df.median(),
        'del_ann_mean': del_ann_df.mean(),
        'del_ann_median': del_ann_df.median(),
        'del_eff_mean': del_eff_df.mean(),
        'del_eff_median': del_eff_df.median(),
        'counts': {
            'add_ann': len(add_ann_df),
            'add_eff': len(add_eff_df),
            'del_ann': len(del_ann_df),
            'del_eff': len(del_eff_df)
        }
    }
    
    return result


def plot_mean_vs_median_combined(stats: dict, window: int = 30, output_file: str = None):
    """
    평균 vs 중앙값 비교 - 4개 조건을 2x2 서브플롯으로
    """
    fig, axes = plt.subplots(2, 2, figsize=(16, 12))
    
    x = list(range(-window, window + 1))
    counts = stats['counts']
    
    configs = [
        ('add_ann', 'Add - 발표일(Announcement) 기준', '#e74c3c', axes[0, 0]),
        ('add_eff', 'Add - 리밸런싱일(Effective) 기준', '#c0392b', axes[0, 1]),
        ('del_ann', 'Delete - 발표일(Announcement) 기준', '#3498db', axes[1, 0]),
        ('del_eff', 'Delete - 리밸런싱일(Effective) 기준', '#2980b9', axes[1, 1])
    ]
    
    for key, title, color, ax in configs:
        mean_data = stats[f'{key}_mean'].values * 100
        median_data = stats[f'{key}_median'].values * 100
        n = counts[key]
        
        # 평균 (실선)
        ax.plot(x, mean_data, color=color, linewidth=2, linestyle='-', 
                label='평균(Mean)', marker='o', markersize=2)
        
        # 중앙값 (점선)
        ax.plot(x, median_data, color=color, linewidth=2, linestyle='--', 
                label='중앙값(Median)', marker='s', markersize=2, alpha=0.7)
        
        ax.axhline(y=0, color='gray', linestyle='-', linewidth=0.8, alpha=0.7)
        ax.axvline(x=0, color='gray', linestyle='--', linewidth=1, alpha=0.7)
        
        ax.set_xlabel('Event Day (D)', fontsize=10)
        ax.set_ylabel('Cum. Excess Return (%)', fontsize=10)
        ax.set_title(f'{title} (n={n})', fontsize=12, fontweight='bold')
        ax.legend(loc='upper left', fontsize=10)
        ax.grid(True, alpha=0.3)
        ax.set_xticks(range(-30, 31, 10))
        ax.set_xlim(-32, 32)
        
        # D+30 값 표시
        mean_d30 = mean_data[-1] if not np.isnan(mean_data[-1]) else 0
        median_d30 = median_data[-1] if not np.isnan(median_data[-1]) else 0
        
        ax.annotate(f'Mean D+30: {mean_d30:+.2f}%', 
                    xy=(30, mean_d30), xytext=(20, mean_d30 + 3),
                    fontsize=9, color=color)
        ax.annotate(f'Median D+30: {median_d30:+.2f}%', 
                    xy=(30, median_d30), xytext=(20, median_d30 - 3),
                    fontsize=9, color=color, alpha=0.7)
    
    plt.suptitle('MSCI 편입/편출 이벤트 평균 vs 중앙값 누적 초과수익률 (vs KOSPI200)', 
                 fontsize=14, fontweight='bold', y=1.02)
    
    plt.tight_layout()
    
    if output_file:
        plt.savefig(output_file, dpi=150, bbox_inches='tight')
        print(f"✅ 그래프 저장: {output_file}")
    
    plt.show()


def plot_median_only(stats: dict, window: int = 30, output_file: str = None):
    """
    중앙값만 표시 - 4개 시리즈를 하나의 그래프에
    """
    fig, ax = plt.subplots(figsize=(14, 8))
    
    x = list(range(-window, window + 1))
    counts = stats['counts']
    
    # 중앙값만 플롯
    ax.plot(x, stats['add_ann_median'].values * 100, 
            label=f"Add - 발표일 기준 (n={counts['add_ann']})", 
            color='#e74c3c', linewidth=2, linestyle='-', marker='o', markersize=3)
    
    ax.plot(x, stats['add_eff_median'].values * 100, 
            label=f"Add - 리밸런싱일 기준 (n={counts['add_eff']})", 
            color='#e74c3c', linewidth=2, linestyle='--', marker='s', markersize=3)
    
    ax.plot(x, stats['del_ann_median'].values * 100, 
            label=f"Delete - 발표일 기준 (n={counts['del_ann']})", 
            color='#3498db', linewidth=2, linestyle='-', marker='o', markersize=3)
    
    ax.plot(x, stats['del_eff_median'].values * 100, 
            label=f"Delete - 리밸런싱일 기준 (n={counts['del_eff']})", 
            color='#3498db', linewidth=2, linestyle='--', marker='s', markersize=3)
    
    ax.axhline(y=0, color='gray', linestyle='-', linewidth=0.8, alpha=0.7)
    ax.axvline(x=0, color='gray', linestyle='--', linewidth=1, alpha=0.7)
    
    ax.set_xlabel('Event Day (D)', fontsize=12)
    ax.set_ylabel('Cumulative Excess Return - Median (%)', fontsize=12)
    ax.set_title('MSCI 편입/편출 이벤트 일별 중앙값(Median) 누적 초과수익률\n(vs KOSPI200)', 
                 fontsize=14, fontweight='bold')
    
    ax.legend(loc='upper left', fontsize=10)
    ax.grid(True, alpha=0.3)
    ax.set_xticks(range(-30, 31, 5))
    ax.set_xlim(-32, 32)
    
    plt.tight_layout()
    
    if output_file:
        plt.savefig(output_file, dpi=150, bbox_inches='tight')
        print(f"✅ 그래프 저장: {output_file}")
    
    plt.show()


def plot_median_comparison(stats: dict, window: int = 30, output_file: str = None):
    """
    중앙값 Add vs Delete 비교 (1x2 서브플롯)
    """
    fig, axes = plt.subplots(1, 2, figsize=(16, 6))
    
    x = list(range(-window, window + 1))
    counts = stats['counts']
    
    # 발표일 기준 비교
    ax1 = axes[0]
    ax1.plot(x, stats['add_ann_median'].values * 100, 
             label=f"Add (n={counts['add_ann']})", color='#e74c3c', linewidth=2)
    ax1.plot(x, stats['del_ann_median'].values * 100, 
             label=f"Delete (n={counts['del_ann']})", color='#3498db', linewidth=2)
    ax1.axhline(y=0, color='gray', linestyle='-', linewidth=0.8)
    ax1.axvline(x=0, color='gray', linestyle='--', linewidth=1, label='발표일(D0)')
    ax1.set_xlabel('Event Day (D)', fontsize=11)
    ax1.set_ylabel('Cumulative Excess Return - Median (%)', fontsize=11)
    ax1.set_title('발표일(Announcement) 기준 - 중앙값', fontsize=13, fontweight='bold')
    ax1.legend(loc='upper left', fontsize=10)
    ax1.grid(True, alpha=0.3)
    ax1.set_xticks(range(-30, 31, 10))
    
    # 리밸런싱일 기준 비교
    ax2 = axes[1]
    ax2.plot(x, stats['add_eff_median'].values * 100, 
             label=f"Add (n={counts['add_eff']})", color='#e74c3c', linewidth=2)
    ax2.plot(x, stats['del_eff_median'].values * 100, 
             label=f"Delete (n={counts['del_eff']})", color='#3498db', linewidth=2)
    ax2.axhline(y=0, color='gray', linestyle='-', linewidth=0.8)
    ax2.axvline(x=0, color='gray', linestyle='--', linewidth=1, label='리밸런싱일(D0)')
    ax2.set_xlabel('Event Day (D)', fontsize=11)
    ax2.set_ylabel('Cumulative Excess Return - Median (%)', fontsize=11)
    ax2.set_title('리밸런싱일(Effective) 기준 - 중앙값', fontsize=13, fontweight='bold')
    ax2.legend(loc='upper left', fontsize=10)
    ax2.grid(True, alpha=0.3)
    ax2.set_xticks(range(-30, 31, 10))
    
    plt.suptitle('MSCI 편입/편출 이벤트 중앙값(Median) 누적 초과수익률 비교 (vs KOSPI200)', 
                 fontsize=14, fontweight='bold')
    
    plt.tight_layout()
    
    if output_file:
        plt.savefig(output_file, dpi=150, bbox_inches='tight')
        print(f"✅ 그래프 저장: {output_file}")
    
    plt.show()


def print_summary_table(stats: dict, window: int = 30):
    """주요 시점 평균 및 중앙값 요약 테이블 출력"""
    
    print("\n" + "=" * 90)
    print("📊 주요 시점 평균(Mean) 누적 초과수익률 요약 (%)")
    print("=" * 90)
    
    key_days = ['D-30', 'D-20', 'D-10', 'D-5', 'D0', 'D+5', 'D+10', 'D+20', 'D+30']
    
    print(f"\n{'시점':<8} {'Add-발표일':>12} {'Add-리밸':>12} {'Del-발표일':>12} {'Del-리밸':>12}")
    print("-" * 60)
    
    for day in key_days:
        vals = [
            stats['add_ann_mean'].get(day, np.nan) * 100,
            stats['add_eff_mean'].get(day, np.nan) * 100,
            stats['del_ann_mean'].get(day, np.nan) * 100,
            stats['del_eff_mean'].get(day, np.nan) * 100
        ]
        print(f"{day:<8} {vals[0]:>+12.2f} {vals[1]:>+12.2f} {vals[2]:>+12.2f} {vals[3]:>+12.2f}")
    
    print("\n" + "=" * 90)
    print("📊 주요 시점 중앙값(Median) 누적 초과수익률 요약 (%)")
    print("=" * 90)
    
    print(f"\n{'시점':<8} {'Add-발표일':>12} {'Add-리밸':>12} {'Del-발표일':>12} {'Del-리밸':>12}")
    print("-" * 60)
    
    for day in key_days:
        vals = [
            stats['add_ann_median'].get(day, np.nan) * 100,
            stats['add_eff_median'].get(day, np.nan) * 100,
            stats['del_ann_median'].get(day, np.nan) * 100,
            stats['del_eff_median'].get(day, np.nan) * 100
        ]
        print(f"{day:<8} {vals[0]:>+12.2f} {vals[1]:>+12.2f} {vals[2]:>+12.2f} {vals[3]:>+12.2f}")
    
    print("-" * 60)
    counts = stats['counts']
    print(f"{'이벤트수':<8} {counts['add_ann']:>12} {counts['add_eff']:>12} {counts['del_ann']:>12} {counts['del_eff']:>12}")
    
    # 평균 vs 중앙값 차이 출력
    print("\n" + "=" * 90)
    print("📊 D+30 시점 평균 vs 중앙값 비교")
    print("=" * 90)
    
    print(f"\n{'구분':<20} {'평균(Mean)':>15} {'중앙값(Median)':>15} {'차이':>12}")
    print("-" * 65)
    
    comparisons = [
        ('Add-발표일', 'add_ann'),
        ('Add-리밸런싱일', 'add_eff'),
        ('Delete-발표일', 'del_ann'),
        ('Delete-리밸런싱일', 'del_eff')
    ]
    
    for label, key in comparisons:
        mean_val = stats[f'{key}_mean'].get('D+30', np.nan) * 100
        median_val = stats[f'{key}_median'].get('D+30', np.nan) * 100
        diff = mean_val - median_val
        print(f"{label:<20} {mean_val:>+15.2f}% {median_val:>+15.2f}% {diff:>+12.2f}%")


def main(input_file: str = 'msci_daily_excess_return.csv'):
    """
    메인 실행 함수
    """
    print("=" * 70)
    print("MSCI 편입/편출 이벤트 평균 및 중앙값 누적 초과수익률 시각화")
    print("=" * 70)
    
    # 1. 데이터 로드
    df = load_and_prepare_data(input_file)
    print(f"📊 데이터 로드 완료: {len(df)}개 행")
    
    # 2. 통계 계산 (평균 + 중앙값)
    stats = calculate_statistics(df, window=30)
    
    # 3. 요약 테이블 출력
    print_summary_table(stats)
    
    # 4. 그래프 그리기
    print("\n그래프 생성 중...")
    
    # 4-1. 평균 vs 중앙값 비교 (2x2)
    plot_mean_vs_median_combined(stats, window=30, output_file='msci_mean_vs_median.png')
    
    # 4-2. 중앙값만 통합 그래프
    plot_median_only(stats, window=30, output_file='msci_median_combined.png')
    
    # 4-3. 중앙값 Add vs Delete 비교
    plot_median_comparison(stats, window=30, output_file='msci_median_comparison.png')
    
    print("\n✅ 완료!")
    
    return stats


if __name__ == "__main__":
    stats = main(
        input_file=r'C:\Users\intern9\kospi 200\msci_daily_excess_return.csv'
    )