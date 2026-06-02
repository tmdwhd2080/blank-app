# -*- coding: utf-8 -*-
"""
Theme_real 설정 파일
- Theme_model/settings.py 와 동일 구조
- Theme_real 파이프라인에서 사용하는 파라미터 집중 관리
"""

import os

# ─── 경로 설정 (repo root 기준 동적 계산) ───
# settings_real.py 가 Theme_real/ 안에 있으므로 BASE_DIR = repo root
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 환경변수 THEME_DATA_DIR 로 외부 경로 override 가능
THEME_DATA_DIR = os.environ.get(
    'THEME_DATA_DIR', os.path.join(BASE_DIR, 'theme_data')
)
RESULT_DIR = THEME_DATA_DIR  # 결과 저장 위치는 동일

# ─── 전략 파라미터 ───
J = 1               # Lookback 일수 (과거 수익률 계산 기간)
K = 2               # Holding 일수 (방안 A: 2일 보유)
TOP_N = 10          # 상위 테마 수 (방안 A: 30 → 10 축소)
GAP = 0             # 갭 일수 (0=당일, 1=익일 매수)

# ─── 거래비용 설정 ─── (방안 A: 편도 1% → 0.3% 현실화)
REBAL_COST = 0.003          # 편도 거래비용 (모멘텀용)
REBAL_COST_OVERLAP = 0.003  # 편도 거래비용 (중복 필터용)
REBAL_COST_GA = 0.003       # 편도 거래비용 (GA용)

# ─── 유동성 필터 설정 ───
# 구성종목 수 필터만 사용 (제로 리턴 미사용)
USE_FILTER = True              # 필터 사용 여부
MIN_CONSTITUENTS = 3           # 구성종목 최소 개수

# ─── 종목 중복 필터 설정 ───
OVERLAP_THRESHOLD = 0.3        # 구성종목 겹침 비율 임계값 (30%)

# ─── GA (유전 알고리즘) 설정 ───
GA_LOOKBACK = 20               # GA 적합도 계산용 lookback (거래일)
GA_POP_SIZE = 80               # 인구 수
GA_GENERATIONS = 50            # 세대 수
GA_MUTATION_RATE = 0.15        # 돌연변이 확률
GA_ELITE_RATIO = 0.1           # 엘리트 비율
GA_TOURNAMENT_K = 3            # 토너먼트 선택 크기

# ─── 비중 제한 설정 ───
MIN_WEIGHT_THRESHOLD = 0.0001   # 최소 비중 임계값

# ─── 출력 설정 ───
PRINT_LAST_N_REBAL = 1         # 마지막 N개 리밸런싱 출력

# ─── 데이터 축적 기간 (일) ───
MIN_HISTORY_DAYS = 1           # 최소 데이터 축적 일수 (1일이면 비중 산출 가능)


# ============================================================
# 설정 딕셔너리 (함수에 전달용)
# ============================================================
def get_filter_config():
    """유동성 필터 설정 딕셔너리 반환"""
    return {
        'min_constituents': MIN_CONSTITUENTS,
    }


def get_strategy_config():
    """백테스트 전략 설정 딕셔너리 반환"""
    return {
        'J': J,
        'K': K,
        'top_n': TOP_N,
        'rebal_cost': REBAL_COST,
        'rebal_cost_overlap': REBAL_COST_OVERLAP,
        'rebal_cost_ga': REBAL_COST_GA,
        'gap': GAP,
        'use_filter': USE_FILTER,
        'overlap_threshold': OVERLAP_THRESHOLD,
    }


def get_ga_config():
    """GA 설정 딕셔너리 반환"""
    return {
        'lookback': GA_LOOKBACK,
        'pop_size': GA_POP_SIZE,
        'generations': GA_GENERATIONS,
        'mutation_rate': GA_MUTATION_RATE,
        'elite_ratio': GA_ELITE_RATIO,
        'tournament_k': GA_TOURNAMENT_K,
        'min_weight': MIN_WEIGHT_THRESHOLD,
    }


def print_settings(mode='all'):
    """현재 설정값 출력"""
    print("=" * 65)
    print("  Theme_real 설정")
    print("=" * 65)

    if mode in ['all', 'strategy']:
        print(f"\n  [전략 파라미터]")
        print(f"    Lookback (J): {J}일")
        print(f"    Holding (K): {K}일")
        print(f"    Top N: {TOP_N}개")
        print(f"    거래비용(GA): {REBAL_COST_GA*100:.2f}%")
        print(f"    Gap: {GAP}일")

    if mode in ['all', 'filter']:
        print(f"\n  [유동성 필터]")
        print(f"    사용: {USE_FILTER}")
        print(f"    최소 구성종목: {MIN_CONSTITUENTS}개")
        print(f"    (제로리턴 필터: 미사용)")

    if mode in ['all', 'overlap']:
        print(f"\n  [종목 중복 필터]")
        print(f"    중복 임계값: {OVERLAP_THRESHOLD*100:.0f}%")

    if mode in ['all', 'ga']:
        print(f"\n  [GA 설정]")
        print(f"    Lookback: {GA_LOOKBACK}일")
        print(f"    인구 수: {GA_POP_SIZE}")
        print(f"    세대 수: {GA_GENERATIONS}")
        print(f"    돌연변이율: {GA_MUTATION_RATE*100:.0f}%")
        print(f"    엘리트 비율: {GA_ELITE_RATIO*100:.0f}%")
        print(f"    토너먼트 K: {GA_TOURNAMENT_K}")
        print(f"    최소 비중 임계값: {MIN_WEIGHT_THRESHOLD*100:.2f}%")

    if mode in ['all', 'data']:
        print(f"\n  [데이터 경로]")
        print(f"    데이터: {THEME_DATA_DIR}")
        print(f"    결과: {RESULT_DIR}")
        print(f"    최소 축적 일수: {MIN_HISTORY_DAYS}일")

    print("=" * 65)


if __name__ == '__main__':
    print_settings()
