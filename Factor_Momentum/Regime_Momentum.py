# -*- coding: utf-8 -*-
"""
국면별 팩터 모멘텀 분석 (개별 Lookback 설정)
"""

import os
import sys
import pandas as pd
import numpy as np
import scipy.stats as stats
from datetime import datetime
from typing import Dict, Tuple, Optional

sys.path.append(r'C:\Users\intern9\truston_quant_dev')
from util.database2 import MSSQL, FACTOR_MAPPING

# ===== 설정 =====
START_DATE = '2003-01-01'
END_DATE = '2025-12-19'
TOP_N = 2  # 횡단면 모멘텀

# 국면 코드 매핑
REGIME_CODES = {
    'RG00101': 'DRAI',
    'RG00211': 'MACRO_GROWTH',
    'RG00311': 'MACRO_INFLATION'
}

# 국면 상태 매핑
STATE_MAPPING = {
    1: 'Positive',
    0: 'Neutral',
    -1: 'Negative'
}

# 국면별 상태 설명
REGIME_STATE_DESC = {
    'DRAI': {1: 'Risk-On', 0: 'Neutral', -1: 'Risk-Off'},
    'MACRO_GROWTH': {1: 'Expansion', 0: 'Neutral', -1: 'Contraction'},
    'MACRO_INFLATION': {1: 'High', 0: 'Moderate', -1: 'Low'}
}

# ===== 국면별 개별 Lookback 및 Skip 설정 =====
# 형식: (regime, state): {'lookback': N, 'skip': M}
REGIME_PARAMS = {
    # DRAI
    ('DRAI', 1): {'lookback': 10, 'skip': 5},      # Risk-On
    ('DRAI', 0): {'lookback': 10, 'skip': 5},      # Neutral
    ('DRAI', -1): {'lookback': 10, 'skip': 5},     # Risk-Off
    
    # MACRO_GROWTH
    ('MACRO_GROWTH', 1): {'lookback': 10, 'skip': 5},   # Expansion
    ('MACRO_GROWTH', 0): {'lookback': 10, 'skip': 5},   # Neutral
    ('MACRO_GROWTH', -1): {'lookback': 10, 'skip': 5},  # Contraction
    
    # MACRO_INFLATION
    ('MACRO_INFLATION', 1): {'lookback': 10, 'skip': 5},   # High
    ('MACRO_INFLATION', 0): {'lookback': 10, 'skip': 5},   # Moderate
    ('MACRO_INFLATION', -1): {'lookback': 10, 'skip': 5},  # Low
}


# ===== 1. 데이터 로드 =====
def load_factor_returns() -> pd.DataFrame:
    """DB에서 팩터 수익률 로드"""
    
    print("=" * 70)
    print("[Step 1] 팩터 수익률 데이터 로드")
    print("=" * 70)
    
    db = MSSQL()
    
    query = f"""
    SELECT 
        BaseDate,
        FLD_NAME,
        Rtn_L_S
    FROM PFM_FCTR
    WHERE MODEL = 'COM_FCTR'
      AND FREQ = 'W'
      AND LAG = 1
      AND BaseDate BETWEEN '{START_DATE}' AND '{END_DATE}'
      AND FLD_NAME IN ('CP_V', 'CP_G', 'CP_Q', 'CP_LV', 'CP_MOM', 'CP_S')
    ORDER BY BaseDate, FLD_NAME
    """
    
    df = db.SELECT(query)
    db.close()
    
    if df is None or len(df) == 0:
        raise ValueError("팩터 수익률 데이터가 없습니다.")
    
    df['BaseDate'] = pd.to_datetime(df['BaseDate'])
    df['Rtn_L_S'] = pd.to_numeric(df['Rtn_L_S'], errors='coerce')
    
    df_pivot = df.pivot_table(
        index='BaseDate',
        columns='FLD_NAME',
        values='Rtn_L_S',
        aggfunc='first'
    )
    
    df_pivot = df_pivot.rename(columns=FACTOR_MAPPING)
    df_pivot = df_pivot.sort_index()
    df_pivot = df_pivot.dropna(how='all')
    
    print(f"  기간: {df_pivot.index.min().date()} ~ {df_pivot.index.max().date()}")
    print(f"  관측 수: {len(df_pivot)}주")
    print(f"  팩터: {list(df_pivot.columns)}")
    
    return df_pivot


def load_regime_data() -> pd.DataFrame:
    """DB에서 국면 데이터 로드 (LookBackDate 기준)"""
    
    print("\n" + "=" * 70)
    print("[Step 2] 국면 데이터 로드")
    print("=" * 70)
    
    db = MSSQL()
    
    query = f"""
    SELECT 
        LookBackDate,
        RegimeCode,
        STATES
    FROM REGIME_QMS
    WHERE RegimeCode IN ('RG00101', 'RG00211', 'RG00311')
      AND LookBackDate BETWEEN '{START_DATE}' AND '{END_DATE}'
      AND RECENT = 1
    ORDER BY LookBackDate, RegimeCode
    """
    
    df = db.SELECT(query)
    db.close()
    
    if df is None or len(df) == 0:
        raise ValueError("국면 데이터가 없습니다.")
    
    df['LookBackDate'] = pd.to_datetime(df['LookBackDate'])
    df['STATES'] = pd.to_numeric(df['STATES'], errors='coerce').astype(int)
    df['RegimeName'] = df['RegimeCode'].map(REGIME_CODES)
    
    # 피벗: LookBackDate별 각 국면의 상태
    df_pivot = df.pivot_table(
        index='LookBackDate',
        columns='RegimeName',
        values='STATES',
        aggfunc='first'
    )
    
    df_pivot = df_pivot.sort_index()
    
    print(f"  기간: {df_pivot.index.min().date()} ~ {df_pivot.index.max().date()}")
    print(f"  관측 수: {len(df_pivot)}")
    
    # 각 국면별 상태 분포 출력
    for regime in df_pivot.columns:
        states = df_pivot[regime].dropna()
        state_counts = states.value_counts().sort_index()
        print(f"\n  {regime} 상태 분포:")
        for state, count in state_counts.items():
            state_name = STATE_MAPPING.get(int(state), 'Unknown')
            desc = REGIME_STATE_DESC.get(regime, {}).get(int(state), '')
            print(f"    {int(state):2d} ({state_name:8s} / {desc:12s}): {count}일")
    
    return df_pivot


def merge_full_data(factor_returns: pd.DataFrame, regime_data: pd.DataFrame) -> pd.DataFrame:
    """팩터 수익률과 국면 데이터 전체 병합"""
    
    print("\n" + "=" * 70)
    print("[Step 3] 전체 데이터 병합")
    print("=" * 70)
    
    # 국면 데이터를 주간으로 리샘플링 (금요일 기준)
    regime_weekly = regime_data.resample('W-FRI').last()
    
    # 인덱스명 통일
    factor_returns.index.name = 'Date'
    regime_weekly.index.name = 'Date'
    
    # 병합
    merged = factor_returns.join(regime_weekly, how='inner')
    
    print(f"  병합 후 전체 관측 수: {len(merged)}주")
    print(f"  기간: {merged.index.min().date()} ~ {merged.index.max().date()}")
    
    return merged


# ===== 2. 국면 상태별 데이터 분리 =====
def split_data_by_regime_state(merged_data: pd.DataFrame) -> Dict[Tuple[str, int], pd.DataFrame]:
    """
    각 국면의 각 상태별로 데이터 분리
    
    Returns:
        Dict[(regime_name, state), DataFrame]: 국면-상태별 분리된 데이터
    """
    
    print("\n" + "=" * 70)
    print("[Step 4] 국면 상태별 데이터 분리")
    print("=" * 70)
    
    factor_cols = [c for c in merged_data.columns if c in FACTOR_MAPPING.values()]
    regime_cols = ['DRAI', 'MACRO_GROWTH', 'MACRO_INFLATION']
    
    split_data = {}
    
    for regime in regime_cols:
        if regime not in merged_data.columns:
            print(f"  {regime}: 데이터 없음")
            continue
            
        for state in [1, 0, -1]:
            state_desc = REGIME_STATE_DESC.get(regime, {}).get(state, '')
            
            # 해당 국면의 해당 상태인 모든 행 추출
            mask = merged_data[regime] == state
            state_data = merged_data.loc[mask, factor_cols].copy()
            
            # 연속성 확인을 위해 원본 인덱스 유지
            state_data = state_data.dropna(how='all')
            
            split_data[(regime, state)] = state_data
            
            print(f"  {regime} - {STATE_MAPPING[state]} ({state_desc}): {len(state_data)}주")
    
    return split_data


# ===== 3. 모멘텀 시그널 계산 (개별 Lookback) =====
def calculate_momentum_signals_individual(
    state_data: pd.DataFrame,
    lookback: int,
    skip: int
) -> pd.DataFrame:
    """
    개별 국면 상태 데이터에 대해 모멘텀 시그널 계산
    
    주의: 상태가 연속되지 않은 기간이 있으므로, 
          연속된 구간에서만 롤링 계산이 유효함
    """
    
    if len(state_data) < lookback + skip + 1:
        return pd.DataFrame()
    
    # 누적 수익률 계산 (연속된 인덱스에서만 유효)
    # 원본 데이터의 시간 간격을 확인하여 연속성 체크
    factor_mom = (1 + state_data).rolling(window=lookback, min_periods=lookback).apply(np.prod, raw=True) - 1
    
    # shift 적용
    trade_signals = factor_mom.shift(1 + skip)
    
    return trade_signals


def calculate_momentum_with_continuity_check(
    full_data: pd.DataFrame,
    regime: str,
    state: int,
    lookback: int,
    skip: int
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.Series]:
    """
    연속성을 고려한 모멘텀 계산
    
    전체 데이터에서 해당 국면 상태 기간만 추출하되,
    모멘텀 계산은 전체 시계열에서 수행 후 해당 상태 기간만 필터링
    
    Returns:
        factor_returns: 해당 상태 기간의 팩터 수익률
        trade_signals: 해당 상태 기간의 모멘텀 시그널
        valid_mask: 유효한 관측 마스크
    """
    
    factor_cols = [c for c in full_data.columns if c in FACTOR_MAPPING.values()]
    
    # 전체 시계열에서 모멘텀 계산
    factor_mom = (1 + full_data[factor_cols]).rolling(window=lookback, min_periods=lookback).apply(np.prod, raw=True) - 1
    trade_signals_full = factor_mom.shift(1 + skip)
    
    # 해당 국면 상태인 시점 마스크
    state_mask = full_data[regime] == state
    
    # 추가 조건: 모멘텀 계산 기간(lookback + skip) 동안 해당 상태가 유지된 경우만
    # 이 조건을 완화하려면 아래 주석 해제
    # state_consistency = full_data[regime].shift(1).rolling(window=lookback + skip).apply(
    #     lambda x: (x == state).all()
    # ).fillna(False).astype(bool)
    # valid_mask = state_mask & state_consistency
    
    # 현재는 단순히 현재 시점이 해당 상태인 경우만 사용
    valid_mask = state_mask
    
    # 해당 상태 기간만 추출
    factor_returns = full_data.loc[valid_mask, factor_cols]
    trade_signals = trade_signals_full.loc[valid_mask]
    
    return factor_returns, trade_signals, valid_mask


# ===== 4. 시계열 모멘텀 (TSFM) =====
def time_series_momentum(
    factor_returns: pd.DataFrame,
    trade_signals: pd.DataFrame
) -> Tuple[Optional[pd.DataFrame], Optional[Dict]]:
    """시계열 모멘텀 분석"""
    
    # 유효한 시그널이 있는 인덱스
    valid_signals = trade_signals.dropna(how='all')
    valid_idx = valid_signals.index.intersection(factor_returns.index)
    
    if len(valid_idx) < 30:
        return None, None
    
    factor_cols = factor_returns.columns.tolist()
    
    # 시그널 부호
    ts_weights = np.sign(trade_signals.loc[valid_idx])
    
    # TSFM 수익률
    ts_factor_returns = ts_weights * factor_returns.loc[valid_idx]
    
    # 전체 포트폴리오 (Equal Weight)
    ts_portfolio = ts_factor_returns.mean(axis=1)
    
    # 팩터별 통계
    ts_stats = pd.DataFrame(index=factor_cols)
    
    for col in factor_cols:
        ret = ts_factor_returns[col].dropna()
        if len(ret) < 10:
            continue
            
        ann_ret = ret.mean() * 52
        ann_vol = ret.std() * np.sqrt(52)
        sharpe = ann_ret / ann_vol if ann_vol > 0 else 0
        t_stat, p_val = stats.ttest_1samp(ret, 0)
        
        ts_stats.loc[col, 'Ann_Return'] = ann_ret
        ts_stats.loc[col, 'Sharpe'] = sharpe
        ts_stats.loc[col, 't_stat'] = t_stat
        ts_stats.loc[col, 'p_value'] = p_val
    
    # 포트폴리오 전체 통계
    port_ret = ts_portfolio.mean() * 52
    port_vol = ts_portfolio.std() * np.sqrt(52)
    port_sharpe = port_ret / port_vol if port_vol > 0 else 0
    port_t, port_p = stats.ttest_1samp(ts_portfolio, 0)
    
    portfolio_stats = {
        'n_obs': len(valid_idx),
        'Ann_Return': port_ret,
        'Ann_Vol': port_vol,
        'Sharpe': port_sharpe,
        't_stat': port_t,
        'p_value': port_p,
        'significant': port_p < 0.05 and port_sharpe > 0
    }
    
    return ts_stats, portfolio_stats


# ===== 5. 횡단면 모멘텀 (CSFM) =====
def cross_sectional_momentum(
    factor_returns: pd.DataFrame,
    trade_signals: pd.DataFrame
) -> Optional[Dict]:
    """횡단면 모멘텀 분석"""
    
    factor_cols = factor_returns.columns.tolist()
    n_factors = len(factor_cols)
    
    # 유효한 시그널이 있는 인덱스
    valid_signals = trade_signals.dropna(how='all')
    valid_idx = valid_signals.index.intersection(factor_returns.index)
    
    if len(valid_idx) < 30:
        return None
    
    def get_cs_weights(row):
        """횡단면 랭킹 기반 가중치"""
        ranks = row.rank(ascending=False)
        weights = pd.Series(0.0, index=row.index)
        weights[ranks <= TOP_N] = 1.0 / TOP_N
        weights[ranks > (n_factors - TOP_N)] = -1.0 / TOP_N
        return weights
    
    cs_weights = trade_signals.loc[valid_idx].apply(get_cs_weights, axis=1)
    cs_portfolio = (cs_weights * factor_returns.loc[valid_idx]).sum(axis=1)
    
    # 통계 계산
    ann_ret = cs_portfolio.mean() * 52
    ann_vol = cs_portfolio.std() * np.sqrt(52)
    sharpe = ann_ret / ann_vol if ann_vol > 0 else 0
    t_stat, p_val = stats.ttest_1samp(cs_portfolio, 0)
    
    return {
        'n_obs': len(valid_idx),
        'Ann_Return': ann_ret,
        'Ann_Vol': ann_vol,
        'Sharpe': sharpe,
        't_stat': t_stat,
        'p_value': p_val,
        'significant': p_val < 0.05 and sharpe > 0
    }


# ===== 6. 국면별 분석 실행 =====
def analyze_all_regimes(merged_data: pd.DataFrame) -> pd.DataFrame:
    """모든 국면 상태별 모멘텀 분석 (개별 Lookback 적용)"""
    
    print("\n" + "=" * 70)
    print("[Step 5] 국면별 모멘텀 분석 (개별 Lookback)")
    print("=" * 70)
    
    results = []
    
    for regime in ['DRAI', 'MACRO_GROWTH', 'MACRO_INFLATION']:
        print(f"\n  ▶ {regime} 분석")
        
        if regime not in merged_data.columns:
            print(f"    → 데이터 없음")
            continue
        
        for state in [1, 0, -1]:
            state_name = STATE_MAPPING.get(state, 'Unknown')
            state_desc = REGIME_STATE_DESC.get(regime, {}).get(state, '')
            
            # 개별 파라미터 가져오기
            params = REGIME_PARAMS.get((regime, state), {'lookback': 10, 'skip': 5})
            lookback = params['lookback']
            skip = params['skip']
            
            print(f"    - {state_name} ({state_desc}): Lookback={lookback}, Skip={skip}")
            
            # 연속성 고려한 모멘텀 계산
            factor_returns, trade_signals, valid_mask = calculate_momentum_with_continuity_check(
                merged_data, regime, state, lookback, skip
            )
            
            n_obs = len(factor_returns)
            print(f"      관측 수: {n_obs}주")
            
            if n_obs < 30:
                print(f"      → 관측 수 부족 (최소 30주 필요)")
                results.append({
                    'Regime': regime,
                    'State': state,
                    'StateName': state_name,
                    'StateDesc': state_desc,
                    'Lookback': lookback,
                    'Skip': skip,
                    'N_Obs': n_obs,
                    'TSFM_Return': np.nan,
                    'TSFM_Sharpe': np.nan,
                    'TSFM_pval': np.nan,
                    'TSFM_Sig': '-',
                    'CSFM_Return': np.nan,
                    'CSFM_Sharpe': np.nan,
                    'CSFM_pval': np.nan,
                    'CSFM_Sig': '-'
                })
                continue
            
            # 시계열 모멘텀 분석
            ts_stats, ts_portfolio = time_series_momentum(factor_returns, trade_signals)
            
            # 횡단면 모멘텀 분석
            cs_stats = cross_sectional_momentum(factor_returns, trade_signals)
            
            # 결과 저장
            result = {
                'Regime': regime,
                'State': state,
                'StateName': state_name,
                'StateDesc': state_desc,
                'Lookback': lookback,
                'Skip': skip,
                'N_Obs': ts_portfolio['n_obs'] if ts_portfolio else n_obs,
            }
            
            if ts_portfolio:
                result['TSFM_Return'] = ts_portfolio['Ann_Return']
                result['TSFM_Sharpe'] = ts_portfolio['Sharpe']
                result['TSFM_pval'] = ts_portfolio['p_value']
                result['TSFM_Sig'] = '✓' if ts_portfolio['significant'] else '✗'
            else:
                result['TSFM_Return'] = np.nan
                result['TSFM_Sharpe'] = np.nan
                result['TSFM_pval'] = np.nan
                result['TSFM_Sig'] = '-'
            
            if cs_stats:
                result['CSFM_Return'] = cs_stats['Ann_Return']
                result['CSFM_Sharpe'] = cs_stats['Sharpe']
                result['CSFM_pval'] = cs_stats['p_value']
                result['CSFM_Sig'] = '✓' if cs_stats['significant'] else '✗'
            else:
                result['CSFM_Return'] = np.nan
                result['CSFM_Sharpe'] = np.nan
                result['CSFM_pval'] = np.nan
                result['CSFM_Sig'] = '-'
            
            results.append(result)
    
    return pd.DataFrame(results)


# ===== 7. 결과 출력 =====
def print_results(results_df: pd.DataFrame):
    """결과 테이블 출력"""
    
    print("\n" + "=" * 110)
    print("[Summary] 국면별 팩터 모멘텀 유의성 분석 결과")
    print("=" * 110)
    print(f"  유의 수준: p < 0.05 & Sharpe > 0")
    print(f"  Top/Bottom N: {TOP_N}개")
    print("-" * 110)
    
    # 테이블 포맷팅
    display_df = results_df.copy()
    display_df['State_Display'] = display_df.apply(
        lambda x: f"{x['StateName']} ({x['StateDesc']})", axis=1
    )
    display_df['Params'] = display_df.apply(
        lambda x: f"L={x['Lookback']}, S={x['Skip']}", axis=1
    )
    display_df['TSFM_Return'] = display_df['TSFM_Return'].apply(
        lambda x: f"{x:.2%}" if pd.notna(x) else "-"
    )
    display_df['TSFM_Sharpe'] = display_df['TSFM_Sharpe'].apply(
        lambda x: f"{x:.3f}" if pd.notna(x) else "-"
    )
    display_df['TSFM_pval'] = display_df['TSFM_pval'].apply(
        lambda x: f"{x:.4f}" if pd.notna(x) else "-"
    )
    display_df['CSFM_Return'] = display_df['CSFM_Return'].apply(
        lambda x: f"{x:.2%}" if pd.notna(x) else "-"
    )
    display_df['CSFM_Sharpe'] = display_df['CSFM_Sharpe'].apply(
        lambda x: f"{x:.3f}" if pd.notna(x) else "-"
    )
    display_df['CSFM_pval'] = display_df['CSFM_pval'].apply(
        lambda x: f"{x:.4f}" if pd.notna(x) else "-"
    )
    
    # 국면별로 구분하여 출력
    for regime in ['DRAI', 'MACRO_GROWTH', 'MACRO_INFLATION']:
        regime_df = display_df[display_df['Regime'] == regime]
        if len(regime_df) > 0:
            print(f"\n[{regime}]")
            print("-" * 110)
            
            # 출력용 컬럼
            print(f"{'State':<25} {'Params':<12} {'N_Obs':>6} | "
                  f"{'TSFM_Ret':>10} {'TSFM_SR':>8} {'TSFM_p':>8} {'Sig':>4} | "
                  f"{'CSFM_Ret':>10} {'CSFM_SR':>8} {'CSFM_p':>8} {'Sig':>4}")
            print("-" * 110)
            
            for _, row in regime_df.iterrows():
                print(f"{row['State_Display']:<25} {row['Params']:<12} {row['N_Obs']:>6} | "
                      f"{row['TSFM_Return']:>10} {row['TSFM_Sharpe']:>8} {row['TSFM_pval']:>8} {row['TSFM_Sig']:>4} | "
                      f"{row['CSFM_Return']:>10} {row['CSFM_Sharpe']:>8} {row['CSFM_pval']:>8} {row['CSFM_Sig']:>4}")
    
    print("\n" + "-" * 110)
    print("[범례]")
    print("  TSFM: 시계열 모멘텀 (Time-Series Factor Momentum)")
    print("  CSFM: 횡단면 모멘텀 (Cross-Sectional Factor Momentum)")
    print("  L: Lookback (주), S: Skip Period (주)")
    print("  SR: Sharpe Ratio, Ret: 연율 수익률, p: p-value")
    print("  ✓: 통계적으로 유의 (p < 0.05 & Sharpe > 0)")
    print("  ✗: 통계적으로 유의하지 않음")
    print("  -: 관측 수 부족 (< 30주)")
    
    # 요약 통계
    print("\n" + "=" * 110)
    print("[요약]")
    print("=" * 110)
    
    valid_results = results_df[results_df['N_Obs'] >= 30]
    
    if len(valid_results) > 0:
        tsfm_sig = (valid_results['TSFM_Sig'] == '✓').sum()
        csfm_sig = (valid_results['CSFM_Sig'] == '✓').sum()
        total = len(valid_results)
        
        print(f"  분석 가능 국면 상태: {total}개 (총 9개 중)")
        print(f"  시계열 모멘텀(TSFM) 유의: {tsfm_sig}/{total}개 ({tsfm_sig/total*100:.1f}%)")
        print(f"  횡단면 모멘텀(CSFM) 유의: {csfm_sig}/{total}개 ({csfm_sig/total*100:.1f}%)")
        
        # 국면별 상세
        print("\n[국면별 유의성 요약]")
        for regime in ['DRAI', 'MACRO_GROWTH', 'MACRO_INFLATION']:
            regime_results = valid_results[valid_results['Regime'] == regime]
            if len(regime_results) > 0:
                tsfm_cnt = (regime_results['TSFM_Sig'] == '✓').sum()
                csfm_cnt = (regime_results['CSFM_Sig'] == '✓').sum()
                print(f"  {regime:20s}: TSFM {tsfm_cnt}/{len(regime_results)}개, CSFM {csfm_cnt}/{len(regime_results)}개")
                
                # 유의한 상태 표시
                tsfm_sig_states = regime_results[regime_results['TSFM_Sig'] == '✓']['StateDesc'].tolist()
                csfm_sig_states = regime_results[regime_results['CSFM_Sig'] == '✓']['StateDesc'].tolist()
                if tsfm_sig_states:
                    print(f"    → TSFM 유의: {', '.join(tsfm_sig_states)}")
                if csfm_sig_states:
                    print(f"    → CSFM 유의: {', '.join(csfm_sig_states)}")
    else:
        print("  분석 가능한 국면 상태가 없습니다.")


def print_params_summary():
    """현재 설정된 파라미터 요약 출력"""
    
    print("\n" + "=" * 70)
    print("[현재 설정된 국면별 파라미터]")
    print("=" * 70)
    
    for regime in ['DRAI', 'MACRO_GROWTH', 'MACRO_INFLATION']:
        print(f"\n  {regime}:")
        for state in [1, 0, -1]:
            params = REGIME_PARAMS.get((regime, state), {'lookback': 10, 'skip': 5})
            state_desc = REGIME_STATE_DESC.get(regime, {}).get(state, '')
            print(f"    {STATE_MAPPING[state]:8s} ({state_desc:12s}): "
                  f"Lookback={params['lookback']:2d}주, Skip={params['skip']:2d}주")


# ===== 메인 실행 =====
if __name__ == '__main__':
    
    print("\n" + "=" * 70)
    print("국면별 팩터 모멘텀 분석 (개별 Lookback 설정)")
    print("=" * 70)
    print(f"  분석 국면: DRAI, MACRO_GROWTH, MACRO_INFLATION")
    print(f"  각 국면 상태: Positive(1), Neutral(0), Negative(-1)")
    print(f"  Top/Bottom N: {TOP_N}개")
    
    # 파라미터 요약 출력
    print_params_summary()
    
    # 1. 데이터 로드
    factor_returns = load_factor_returns()
    regime_data = load_regime_data()
    
    # 2. 전체 데이터 병합
    merged_data = merge_full_data(factor_returns, regime_data)
    
    # 3. 국면별 분석 (개별 Lookback 적용)
    results_df = analyze_all_regimes(merged_data)
    
    # 4. 결과 출력
    print_results(results_df)
    
    print("\n" + "=" * 70)
    print("분석 완료!")
    print("=" * 70)