# -*- coding: utf-8 -*-
"""
Theme_model 통합 실행 파일
실행 방법:
  python Theme_model/run.py             # 대화형 메뉴
  python Theme_model/run.py --all       # 전체 실행
  python Theme_model/run.py --beta      # BETA.py만 실행
  python Theme_model/run.py --momentum  # tmomnetum.py만 실행
  python Theme_model/run.py --overlap   # theme_filter2.py만 실행
  python Theme_model/run.py --ga        # Theme.py만 실행
"""

import sys
import os
import argparse

sys.path.append(os.path.join(r'C:\Users\intern9', 'truston_quant_dev'))

import matplotlib.pyplot as plt
import matplotlib
matplotlib.rcParams['font.family'] = 'Malgun Gothic'
matplotlib.rcParams['axes.unicode_minus'] = False

from Theme_model.settings import print_settings


def run_beta():
    #BETA.py 실행"
    print("\n" + "=" * 70)
    print("  [1] BETA.py - 롤링 베타 + 시장조정 수익률 산출")
    print("=" * 70)
    from Theme_model.BETA import main as beta_main
    beta_main()


def run_momentum():
    """tmomnetum.py 실행"""
    print("\n" + "=" * 70)
    print("  [2] tmomnetum.py - 기본 테마 모멘텀 (유동성 필터)")
    print("=" * 70)
    from Theme_model.tmomnetum import main as momentum_main
    momentum_main()


def run_overlap():
    """theme_filter2.py 실행"""
    print("\n" + "=" * 70)
    print("  [3] theme_filter2.py - 테마 모멘텀 + 종목 중복 필터")
    print("=" * 70)
    from Theme_model.theme_filter2 import main as overlap_main
    overlap_main()


def run_ga():
    """Theme.py 실행 """
    print("\n" + "=" * 70)
    print("  [4] Theme.py - 테마 모멘텀 + 중복 필터 + GA 비중 최적화")
    print("=" * 70)
    from Theme_model.Theme import main as ga_main
    ga_main()


def run_jk_analysis():
    """K=1,2,3 각각에 대해 J=1~7 변화시키며 월 평균 수익률 그래프 출력"""
    import pandas as pd
    from Theme_model.BETA import (
        load_kospi_returns,
        get_theme_returns,
        compute_market_adjusted_returns,
    )
    from Theme_model.theme_filter import (
        get_theme_info,
        get_theme_stocks,
    )
    from Theme_model.tmomnetum import backtest_momentum, monthly_summary
    from Theme_model.settings import (
        KOSPI_EXCEL_PATH, START_DATE, END_DATE, ROLLING_WINDOW,
        TOP_N, REBAL_COST, GAP,
        USE_FILTER, MIN_CONSTITUENTS,
        ZERO_RETURN_THRESHOLD, ZERO_RETURN_LOOKBACK,
        PRE_FILTER_ENABLED, PRE_FILTER_N, PRE_FILTER_K,
        get_filter_config,
    )
    from util.database2 import MSSQL, DBConfig

    print("\n" + "=" * 70)
    print("  [JK 분석] K=1,2,3 / J=1~7  월 평균 수익률 그래프")
    print("=" * 70)

    # ── 데이터 로드 (1회) ──
    print("\n[Step 1] 데이터 로드")
    kospi_df = load_kospi_returns(KOSPI_EXCEL_PATH, START_DATE, END_DATE)

    db = MSSQL(database=DBConfig.TRSTDEV_DB)
    theme_info_df = pd.DataFrame()
    theme_stocks_df = pd.DataFrame()
    try:
        theme_df = get_theme_returns(db, START_DATE, END_DATE)
        if USE_FILTER:
            theme_info_df = get_theme_info(db)
            theme_stocks_df = get_theme_stocks(db)
    finally:
        db.close()

    print("[Step 2] 시장조정 수익률 계산")
    result_df = compute_market_adjusted_returns(theme_df, kospi_df, START_DATE, ROLLING_WINDOW)

    adj_pivot = result_df.pivot_table(
        index='PfmDate', columns='THEME_ID', values='AdjRtn', aggfunc='first'
    ).sort_index()
    theme_pivot = result_df.pivot_table(
        index='PfmDate', columns='THEME_ID', values='ThemeRtn', aggfunc='first'
    ).sort_index()
    kospi_pivot = result_df.drop_duplicates('PfmDate').set_index('PfmDate')['KOSPI_Rtn'].sort_index()

    filter_config = get_filter_config()

    J_range = range(1, 8)  # J = 1 ~ 7
    K_values = [1, 2, 3]

    # ── 결과 수집 ──
    results = {}  # {K: [avg_monthly_return for each J]}
    for k_val in K_values:
        avg_returns = []
        for j_val in J_range:
            print(f"\n  ▶ K={k_val}, J={j_val} 백테스트 실행 중...")
            bt_df, _ = backtest_momentum(
                adj_pivot, theme_pivot, kospi_pivot,
                J=j_val, K=k_val, top_n=TOP_N,
                rebal_cost=REBAL_COST, gap=GAP,
                use_filter=USE_FILTER,
                theme_info_df=theme_info_df,
                theme_stocks_df=theme_stocks_df,
                filter_config=filter_config,
                pre_filter_enabled=PRE_FILTER_ENABLED,
                pre_filter_n=PRE_FILTER_N,
                pre_filter_k=PRE_FILTER_K,
            )
            if bt_df.empty:
                avg_returns.append(0.0)
                print(f"    → 데이터 부족 (0.00%)")
            else:
                monthly = monthly_summary(bt_df)
                avg_monthly = monthly['monthly_cum(%)'].mean()
                avg_returns.append(avg_monthly)
                print(f"    → 월 평균 수익률: {avg_monthly:+.2f}%")
        results[k_val] = avg_returns

    # ── 그래프 출력 (3개) ──
    print("\n" + "=" * 70)
    print("  그래프 출력 중...")
    print("=" * 70)

    fig, axes = plt.subplots(1, 3, figsize=(18, 5), sharey=True)

    for idx, k_val in enumerate(K_values):
        ax = axes[idx]
        avg_returns = results[k_val]
        colors = ['green' if v >= 0 else 'red' for v in avg_returns]

        bars = ax.bar(list(J_range), avg_returns, color=colors, edgecolor='black', alpha=0.8)
        ax.set_title(f'K = {k_val}', fontsize=14, fontweight='bold')
        ax.set_xlabel('J (Lookback 일수)', fontsize=11)
        if idx == 0:
            ax.set_ylabel('월 평균 수익률 (%)', fontsize=11)
        ax.set_xticks(list(J_range))
        ax.axhline(y=0, color='black', linewidth=0.8, linestyle='--')
        ax.grid(axis='y', alpha=0.3)

        # 바 위에 수치 표시
        for bar, val in zip(bars, avg_returns):
            y_pos = bar.get_height()
            va = 'bottom' if val >= 0 else 'top'
            ax.text(bar.get_x() + bar.get_width() / 2, y_pos,
                    f'{val:.2f}%', ha='center', va=va, fontsize=9, fontweight='bold')

    fig.suptitle(f'J-K 분석: 월 평균 수익률 (Top {TOP_N}, 비용 {REBAL_COST*100:.1f}%)',
                 fontsize=15, fontweight='bold', y=1.02)
    plt.tight_layout()
    plt.show()

    print("\n  JK 분석 완료.")


def run_all():
    """모든 모듈 순차 실행"""
    print("\n" + "#" * 70)
    print("#" + " " * 68 + "#")
    print("#" + "  Theme_model 전체 실행".center(68) + "#")
    print("#" + " " * 68 + "#")
    print("#" * 70)

    print_settings()

    run_beta()
    run_momentum()
    run_overlap()
    run_ga()

    print("\n" + "#" * 70)
    print("#" + "  전체 실행 완료".center(68) + "#")
    print("#" * 70)


def interactive_menu():
    """대화형 메뉴"""
    while True:
        print("\n" + "=" * 50)
        print("  Theme_model 실행 메뉴")
        print("=" * 50)
        print("  [0] 현재 설정값 확인")
        print("  [1] BETA.py - 롤링 베타 + 시장조정 수익률")
        print("  [2] tmomnetum.py - 기본 테마 모멘텀")
        print("  [3] theme_filter2.py - 종목 중복 필터")
        print("  [4] Theme.py - GA 비중 최적화")
        print("  [5] 전체 실행 (1→2→3→4)")
        print("  [6] JK 분석 - K=1,2,3 / J=1~7 월 평균 수익률 그래프")
        print("  [q] 종료")
        print("=" * 50)

        choice = input("  선택: ").strip().lower()

        if choice == '0':
            print_settings()
        elif choice == '1':
            run_beta()
        elif choice == '2':
            run_momentum()
        elif choice == '3':
            run_overlap()
        elif choice == '4':
            run_ga()
        elif choice == '5':
            run_all()
        elif choice == '6':
            run_jk_analysis()
        elif choice == 'q':
            print("  종료합니다.")
            break
        else:
            print("  잘못된 선택입니다.")


def main():
    parser = argparse.ArgumentParser(
        description='Theme_model 통합 실행',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  python Theme_model/run.py             # 대화형 메뉴
  python Theme_model/run.py --all       # 전체 실행
  python Theme_model/run.py --beta      # BETA.py만 실행
  python Theme_model/run.py --momentum  # tmomnetum.py만 실행
  python Theme_model/run.py --overlap   # theme_filter2.py만 실행
  python Theme_model/run.py --ga        # Theme.py만 실행
  python Theme_model/run.py --settings  # 설정값 확인
        """
    )

    parser.add_argument('--all', action='store_true',
                        help='전체 모듈 순차 실행')
    parser.add_argument('--beta', action='store_true',
                        help='BETA.py 실행 (롤링 베타 + 시장조정 수익률)')
    parser.add_argument('--momentum', action='store_true',
                        help='tmomnetum.py 실행 (기본 테마 모멘텀)')
    parser.add_argument('--overlap', action='store_true',
                        help='theme_filter2.py 실행 (종목 중복 필터)')
    parser.add_argument('--ga', action='store_true',
                        help='Theme.py 실행 (GA 비중 최적화)')
    parser.add_argument('--settings', action='store_true',
                        help='현재 설정값 출력')
    parser.add_argument('--jk', action='store_true',
                        help='JK 분석 (K=1,2,3 / J=1~7 월평균수익률 그래프)')

    args = parser.parse_args()

    # 옵션이 지정된 경우
    if args.settings:
        print_settings()
        return

    if args.all:
        run_all()
        return

    if args.beta:
        run_beta()
        return

    if args.momentum:
        run_momentum()
        return

    if args.overlap:
        run_overlap()
        return

    if args.ga:
        run_ga()
        return

    if args.jk:
        run_jk_analysis()
        return

    # 옵션 없으면 대화형 메뉴
    interactive_menu()


if __name__ == '__main__':
    main()
# python Theme_model/run.py