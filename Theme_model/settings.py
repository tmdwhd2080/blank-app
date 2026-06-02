# -*- coding: utf-8 -*-
"""
Theme_model 설정 파일
"""

import os

# 경로 설정
BASE_DIR = r'C:\Users\intern9\truston_quant_dev'
KOSPI_EXCEL_PATH = os.path.join(BASE_DIR, 'util', '코스피 all.xlsx')

# 분석 기간 설정
START_DATE = '20231108'
END_DATE = '20260204'


# 롤링 베타 설정 (BETA.py)
ROLLING_WINDOW = 20  # 거래일 기준

# 백테스트 전략 파라미터 (공통)
J = 1               # Lookback 일수 (과거 수익률 계산 기간)
K = 1               # Holding 일수 (보유 기간)
TOP_N = 30          # 상위 테마 수
GAP = 0             # 갭 일수 (0=당일, 1=익일 매수)

# ── 사전 필터 설정 (Lookback 이전 기간 수익률 필터) ──
PRE_FILTER_ENABLED = False       # True=필터 사용, False=미사용
PRE_FILTER_N = 3                 # Lookback 이전 N일 동안의 수익률 확인 기간
PRE_FILTER_K = 5.0             # 누적 조정 수익률 K% 이하인 테마만 유니버스에 포함

# ── 거래비용 설정 ──
# 첫 리밸런싱 거래 비용
REBAL_COST = 0.01           # 편도 거래비용 (0.003 = 0.3%)
# 종목 중복 필터 
REBAL_COST_OVERLAP = 0.01   # 편도 거래비용
# GA 최적화
REBAL_COST_GA = 0.01        # 편도 거래비용


# 유동성 필터 설정
USE_FILTER = True              # 필터 사용 여부
MIN_CONSTITUENTS = 3           # 구성종목 최소 개수
ZERO_RETURN_THRESHOLD = 0.2    # 제로리턴 비율 임계값 (0.2 = 20%)
ZERO_RETURN_LOOKBACK = 5       # 제로리턴 판단 기간 (일)


# 종목 중복 필터 설정
OVERLAP_THRESHOLD = 0.3        # 구성종목 겹침 비율 임계값 (30%)


# GA (유전 알고리즘) 설정 (Theme.py)
GA_LOOKBACK = 20               # GA 적합도 계산용 lookback (거래일)
GA_POP_SIZE = 80               # 인구 수
GA_GENERATIONS = 50            # 세대 수
GA_MUTATION_RATE = 0.15        # 돌연변이 확률
GA_ELITE_RATIO = 0.1           # 엘리트 비율
GA_TOURNAMENT_K = 3            # 토너먼트 선택 크기

# ── 비중 제한 설정 ──
MIN_WEIGHT_THRESHOLD = 0.000000000001 # 최소 비중 임계값 (0.0005 = 0.05%)


# 출력 설정
PRINT_LAST_N_REBAL = 5         # 마지막 N개 리밸런싱 출력

# ── 구성종목 수익률 분석 설정 ──
STOCK_ANALYSIS_ENABLED = True  # True=리밸런싱마다 구성종목 분석 실행
CRAWL_DELAY = 0.05             # pykrx 요청 간 딜레이 (초)


# 설정 딕셔너리 (함수에 전달용)
def get_filter_config():
    """유동성 필터 설정 딕셔너리 반환"""
    return {
        'min_constituents': MIN_CONSTITUENTS,
        'zero_return_threshold': ZERO_RETURN_THRESHOLD,
        'zero_lookback': ZERO_RETURN_LOOKBACK,
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
        'pre_filter_enabled': PRE_FILTER_ENABLED,
        'pre_filter_n': PRE_FILTER_N,
        'pre_filter_k': PRE_FILTER_K,
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
    """현재 설정값 출력
    
    Args:
        mode: 'all', 'beta', 'momentum', 'filter', 'overlap', 'ga'
    """
    print("=" * 65)
    print("  Theme_model 설정")
    print("=" * 65)
    
    if mode in ['all', 'beta']:
        print(f"\n  [경로/기간]")
        print(f"    KOSPI 엑셀: {KOSPI_EXCEL_PATH}")
        print(f"    분석기간: {START_DATE} ~ {END_DATE}")
        print(f"    롤링 윈도우: {ROLLING_WINDOW}일")
    
    if mode in ['all', 'momentum']:
        print(f"\n  [전략 파라미터]")
        print(f"    Lookback (J): {J}일")
        print(f"    Holding (K): {K}일")
        print(f"    Top N: {TOP_N}개")
        print(f"    거래비용: {REBAL_COST*100:.2f}%")
        print(f"    Gap: {GAP}일")
        print(f"\n  [사전 필터 (Lookback 이전 수익률)]")
        print(f"    사용: {PRE_FILTER_ENABLED}")
        print(f"    확인 기간 (N): {PRE_FILTER_N}일")
        print(f"    수익률 임계값 (K): {PRE_FILTER_K}%")
    
    if mode in ['all', 'filter']:
        print(f"\n  [유동성 필터]")
        print(f"    사용: {USE_FILTER}")
        print(f"    최소 구성종목: {MIN_CONSTITUENTS}개")
        print(f"    제로리턴 임계값: {ZERO_RETURN_THRESHOLD*100:.0f}%")
        print(f"    제로리턴 Lookback: {ZERO_RETURN_LOOKBACK}일")
    
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
    
    print("=" * 65)


if __name__ == '__main__':
    print_settings()