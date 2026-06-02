# -*- coding: utf-8 -*-
"""
FX Risk Regime 분석 모듈
- 대칭 OLS + HAC (3개 레짐)
- 비대칭 OLS + HAC (6개 레짐 × 방향)
- Wald Test
- 예측 기능

레짐 해석 (수정됨!):
    STATES = 1  → Risk-On  → 안정기 (위험자산 선호)
    STATES = 0  → Neutral  → 평범
    STATES = -1 → Risk-Off → 위기 (안전자산 선호)
"""

import pandas as pd
import numpy as np
import statsmodels.api as sm
from typing import Optional, Dict, List, Tuple
import warnings
warnings.filterwarnings('ignore')


# 레짐 매핑 (수정됨!)
REGIME_MAP = {
    1: '안정',      # Risk-On = 위험자산 선호 = 시장 좋음
    0: '평범',      
    -1: '위기'      # Risk-Off = 안전자산 선호 = 시장 불안
}


# ============================================================
# 1. 데이터 전처리
# ============================================================

def prepare_data(df_stock, df_index, df_fx, df_regime):
    """
    데이터 전처리 및 병합
    """
    # 수익률 계산
    stock_returns = df_stock.pct_change()
    index_returns = df_index['close'].pct_change()
    fx_returns = df_fx['fx'].pct_change()
    
    # 기본 데이터프레임
    df = pd.DataFrame({
        'R_mkt': index_returns,
        'ΔFX': fx_returns
    })
    
    # 레짐 병합
    df = df.join(df_regime[['risk_regime']], how='left')
    
    # 주식 수익률 병합
    df = pd.concat([df, stock_returns], axis=1)
    
    # 결측치 제거
    df = df.dropna(subset=['R_mkt', 'ΔFX', 'risk_regime'])
    
    return df


def create_regime_dummies(df):
    """
    레짐별 FX 더미 변수 생성 (대칭)
    
    수정됨:
        FX_stable = 안정기 (STATES=1, Risk-On)
        FX_normal = 평범 (STATES=0)
        FX_crisis = 위기 (STATES=-1, Risk-Off)
    """
    df = df.copy()
    
    df['FX_stable'] = df['ΔFX'] * (df['risk_regime'] == 1)   # 안정 (Risk-On)
    df['FX_normal'] = df['ΔFX'] * (df['risk_regime'] == 0)   # 평범
    df['FX_crisis'] = df['ΔFX'] * (df['risk_regime'] == -1)  # 위기 (Risk-Off)
    
    return df


def create_asymmetric_dummies(df):
    """
    레짐별 + 방향별 FX 더미 변수 생성 (비대칭)
    6개: 3레짐 × 2방향 (상승/하락)
    """
    df = df.copy()
    
    # 환율 상승/하락 구분
    fx_up = df['ΔFX'] > 0
    fx_down = df['ΔFX'] <= 0
    
    # 안정 레짐 (1, Risk-On)
    df['FX_stable_up'] = df['ΔFX'] * (df['risk_regime'] == 1) * fx_up
    df['FX_stable_down'] = df['ΔFX'] * (df['risk_regime'] == 1) * fx_down
    
    # 평범 레짐 (0)
    df['FX_normal_up'] = df['ΔFX'] * (df['risk_regime'] == 0) * fx_up
    df['FX_normal_down'] = df['ΔFX'] * (df['risk_regime'] == 0) * fx_down
    
    # 위기 레짐 (-1, Risk-Off)
    df['FX_crisis_up'] = df['ΔFX'] * (df['risk_regime'] == -1) * fx_up
    df['FX_crisis_down'] = df['ΔFX'] * (df['risk_regime'] == -1) * fx_down
    
    return df


# ============================================================
# 2. 대칭 OLS + HAC
# ============================================================

def run_ols_hac(df, stock_col, maxlags=5):
    """
    단일 종목 대칭 OLS + HAC 분석
    """
    required_cols = ['R_mkt', 'FX_stable', 'FX_normal', 'FX_crisis', stock_col]
    
    df_subset = df[required_cols].copy()
    
    if df_subset.columns.duplicated().any():
        df_subset = df_subset.loc[:, ~df_subset.columns.duplicated()]
    
    df_clean = df_subset.dropna()
    
    if len(df_clean) < 100:
        return None
    
    try:
        X = sm.add_constant(df_clean[['R_mkt', 'FX_stable', 'FX_normal', 'FX_crisis']])
        y = df_clean[stock_col]
        
        if isinstance(y, pd.DataFrame):
            y = y.iloc[:, 0]
        
        model = sm.OLS(y, X).fit(cov_type='HAC', cov_kwds={'maxlags': maxlags})
        return model
    except Exception as e:
        return None


def run_ols_mkt_only(df, stock_col, maxlags=5):
    """
    시장만 포함한 OLS (부분 R² 계산용)
    R_stock = α + β_mkt × R_mkt + ε
    """
    required_cols = ['R_mkt', stock_col]
    
    df_subset = df[required_cols].copy()
    
    if df_subset.columns.duplicated().any():
        df_subset = df_subset.loc[:, ~df_subset.columns.duplicated()]
    
    df_clean = df_subset.dropna()
    
    if len(df_clean) < 100:
        return None
    
    try:
        X = sm.add_constant(df_clean[['R_mkt']])
        y = df_clean[stock_col]
        
        if isinstance(y, pd.DataFrame):
            y = y.iloc[:, 0]
        
        model = sm.OLS(y, X).fit(cov_type='HAC', cov_kwds={'maxlags': maxlags})
        return model
    except Exception as e:
        return None


def run_wald_test(model):
    """
    레짐 차이 Wald Test (대칭)
    """
    try:
        wald = model.wald_test('FX_stable = FX_normal = FX_crisis', scalar=True)
        return {
            'f_stat': wald.statistic,
            'p_value': wald.pvalue
        }
    except:
        return None


# ============================================================
# 3. 비대칭 OLS + HAC
# ============================================================

def run_asymmetric_ols_hac(df, stock_col, maxlags=5):
    """
    단일 종목 비대칭 OLS + HAC 분석 (6개 β_fx)
    """
    required_cols = ['R_mkt', 
                     'FX_stable_up', 'FX_stable_down',
                     'FX_normal_up', 'FX_normal_down',
                     'FX_crisis_up', 'FX_crisis_down',
                     stock_col]
    
    df_subset = df[required_cols].copy()
    
    if df_subset.columns.duplicated().any():
        df_subset = df_subset.loc[:, ~df_subset.columns.duplicated()]
    
    df_clean = df_subset.dropna()
    
    if len(df_clean) < 100:
        return None
    
    try:
        X_cols = ['R_mkt', 
                  'FX_stable_up', 'FX_stable_down',
                  'FX_normal_up', 'FX_normal_down',
                  'FX_crisis_up', 'FX_crisis_down']
        X = sm.add_constant(df_clean[X_cols])
        y = df_clean[stock_col]
        
        if isinstance(y, pd.DataFrame):
            y = y.iloc[:, 0]
        
        model = sm.OLS(y, X).fit(cov_type='HAC', cov_kwds={'maxlags': maxlags})
        return model
    except Exception as e:
        return None


def run_asymmetric_wald_tests(model):
    """
    비대칭 Wald Tests
    """
    results = {}
    
    # 1) 전체: 모든 β_fx가 동일한가?
    try:
        wald_all = model.wald_test(
            'FX_stable_up = FX_stable_down = FX_normal_up = FX_normal_down = FX_crisis_up = FX_crisis_down',
            scalar=True
        )
        results['all'] = {'f_stat': wald_all.statistic, 'p_value': wald_all.pvalue}
    except:
        results['all'] = None
    
    # 2) 안정 레짐 내 상승/하락 차이
    try:
        wald_stable = model.wald_test('FX_stable_up = FX_stable_down', scalar=True)
        results['stable_asym'] = {'f_stat': wald_stable.statistic, 'p_value': wald_stable.pvalue}
    except:
        results['stable_asym'] = None
    
    # 3) 평범 레짐 내 상승/하락 차이
    try:
        wald_normal = model.wald_test('FX_normal_up = FX_normal_down', scalar=True)
        results['normal_asym'] = {'f_stat': wald_normal.statistic, 'p_value': wald_normal.pvalue}
    except:
        results['normal_asym'] = None
    
    # 4) 위기 레짐 내 상승/하락 차이
    try:
        wald_crisis = model.wald_test('FX_crisis_up = FX_crisis_down', scalar=True)
        results['crisis_asym'] = {'f_stat': wald_crisis.statistic, 'p_value': wald_crisis.pvalue}
    except:
        results['crisis_asym'] = None
    
    return results


# ============================================================
# 4. 단일 종목 분석
# ============================================================

def analyze_single_stock(df, stock_col, stock_name=None, print_result=True):
    """
    단일 종목 대칭 분석
    """
    model = run_ols_hac(df, stock_col)
    
    if model is None:
        if print_result:
            print(f"[{stock_col}] 데이터 부족 (N < 100)")
        return None
    
    wald = run_wald_test(model)
    name = stock_name if stock_name else stock_col
    
    # 부분 R² 계산 (시장만 모델)
    model_mkt = run_ols_mkt_only(df, stock_col)
    r2_mkt_only = model_mkt.rsquared if model_mkt else None
    r2_full = model.rsquared
    r2_fx_partial = (r2_full - r2_mkt_only) if r2_mkt_only is not None else None
    
    result = {
        'code': stock_col,
        'name': name,
        'n_obs': int(model.nobs),
        'r_squared': r2_full,
        'r2_mkt_only': r2_mkt_only,
        'r2_fx_partial': r2_fx_partial,
        'beta_mkt': model.params['R_mkt'],
        'beta_fx_stable': model.params['FX_stable'],
        'beta_fx_normal': model.params['FX_normal'],
        'beta_fx_crisis': model.params['FX_crisis'],
        'se_fx_stable': model.bse['FX_stable'],
        'se_fx_normal': model.bse['FX_normal'],
        'se_fx_crisis': model.bse['FX_crisis'],
        'tstat_fx_stable': model.tvalues['FX_stable'],
        'tstat_fx_normal': model.tvalues['FX_normal'],
        'tstat_fx_crisis': model.tvalues['FX_crisis'],
        'pval_fx_stable': model.pvalues['FX_stable'],
        'pval_fx_normal': model.pvalues['FX_normal'],
        'pval_fx_crisis': model.pvalues['FX_crisis'],
        'wald_f': wald['f_stat'] if wald else None,
        'wald_p': wald['p_value'] if wald else None,
    }
    
    if print_result:
        print(f"\n{'='*60}")
        print(f"종목: {name} ({stock_col})")
        print(f"{'='*60}")
        print(f"N = {result['n_obs']}, R² = {result['r_squared']:.4f}")
        
        print(f"\n{'레짐':<15} {'β_fx':>10} {'SE':>10} {'t-stat':>10} {'p-value':>10}")
        print("-" * 57)
        
        # 순서 변경: 안정 → 평범 → 위기
        for regime, suffix in [('안정(1,RiskOn)', 'stable'), ('평범(0)', 'normal'), ('위기(-1,RiskOff)', 'crisis')]:
            beta = result[f'beta_fx_{suffix}']
            se = result[f'se_fx_{suffix}']
            tstat = result[f'tstat_fx_{suffix}']
            pval = result[f'pval_fx_{suffix}']
            sig = '***' if pval < 0.01 else '**' if pval < 0.05 else '*' if pval < 0.1 else ''
            print(f"{regime:<15} {beta:>10.4f} {se:>10.4f} {tstat:>10.2f} {pval:>10.4f} {sig}")
        
        if wald:
            print(f"\n[Wald Test] H0: 모든 레짐 β_fx 동일")
            print(f"F-stat = {wald['f_stat']:.2f}, p-value = {wald['p_value']:.4f}")
            if wald['p_value'] < 0.05:
                print("→ 레짐별 β_fx 유의하게 다름 ✅")
            else:
                print("→ 레짐별 β_fx 차이 없음")
    
    return result


def analyze_single_stock_asymmetric(df, stock_col, stock_name=None, print_result=True):
    """
    단일 종목 비대칭 분석 (6개 β_fx)
    """
    model = run_asymmetric_ols_hac(df, stock_col)
    
    if model is None:
        if print_result:
            print(f"[{stock_col}] 데이터 부족 (N < 100)")
        return None
    
    wald_tests = run_asymmetric_wald_tests(model)
    name = stock_name if stock_name else stock_col
    
    result = {
        'code': stock_col,
        'name': name,
        'n_obs': int(model.nobs),
        'r_squared': model.rsquared,
        'beta_mkt': model.params['R_mkt'],
    }
    
    # 6개 β_fx 추가
    for var in ['FX_stable_up', 'FX_stable_down', 
                'FX_normal_up', 'FX_normal_down',
                'FX_crisis_up', 'FX_crisis_down']:
        key = var.lower()
        result[f'beta_{key}'] = model.params[var]
        result[f'se_{key}'] = model.bse[var]
        result[f'tstat_{key}'] = model.tvalues[var]
        result[f'pval_{key}'] = model.pvalues[var]
    
    # Wald test 결과
    for test_name, test_result in wald_tests.items():
        if test_result:
            result[f'wald_{test_name}_f'] = test_result['f_stat']
            result[f'wald_{test_name}_p'] = test_result['p_value']
        else:
            result[f'wald_{test_name}_f'] = None
            result[f'wald_{test_name}_p'] = None
    
    if print_result:
        print(f"\n{'='*60}")
        print(f"종목: {name} ({stock_col}) [비대칭]")
        print(f"{'='*60}")
        print(f"N = {result['n_obs']}, R² = {result['r_squared']:.4f}")
        
        print(f"\n{'레짐':<15} {'방향':<6} {'β_fx':>10} {'SE':>10} {'t-stat':>10} {'p-value':>10}")
        print("-" * 65)
        
        # 순서 변경: 안정 → 평범 → 위기
        for regime, regime_name in [('stable', '안정(RiskOn)'), ('normal', '평범'), ('crisis', '위기(RiskOff)')]:
            for direction, dir_name in [('up', '상승'), ('down', '하락')]:
                key = f'fx_{regime}_{direction}'
                beta = result[f'beta_{key}']
                se = result[f'se_{key}']
                tstat = result[f'tstat_{key}']
                pval = result[f'pval_{key}']
                sig = '***' if pval < 0.01 else '**' if pval < 0.05 else '*' if pval < 0.1 else ''
                print(f"{regime_name:<15} {dir_name:<6} {beta:>10.4f} {se:>10.4f} {tstat:>10.2f} {pval:>10.4f} {sig}")
        
        print(f"\n[Wald Tests]")
        if wald_tests['all']:
            print(f"  전체 동일: F={wald_tests['all']['f_stat']:.2f}, p={wald_tests['all']['p_value']:.4f}")
        
        for regime, regime_name in [('stable', '안정'), ('normal', '평범'), ('crisis', '위기')]:
            test = wald_tests.get(f'{regime}_asym')
            if test:
                sig = "✅ 비대칭" if test['p_value'] < 0.05 else "대칭"
                print(f"  {regime_name} 상승=하락: F={test['f_stat']:.2f}, p={test['p_value']:.4f} → {sig}")
    
    return result


# ============================================================
# 5. 전체 종목 분석
# ============================================================

def analyze_all_stocks(df, stock_cols, code_name_map=None, verbose=True):
    """
    전체 종목 대칭 분석
    """
    results = []
    total = len(stock_cols)
    
    for i, col in enumerate(stock_cols):
        if verbose and (i + 1) % 50 == 0:
            print(f"    진행: {i+1}/{total}")
        
        name = code_name_map.get(col, col) if code_name_map else col
        result = analyze_single_stock(df, col, name, print_result=False)
        
        if result:
            results.append(result)
    
    df_results = pd.DataFrame(results)
    
    if verbose:
        print(f"\n분석 완료: {len(df_results)}/{total} 종목")
    
    return df_results


def analyze_all_stocks_asymmetric(df, stock_cols, code_name_map=None, verbose=True):
    """
    전체 종목 비대칭 분석
    """
    results = []
    total = len(stock_cols)
    
    for i, col in enumerate(stock_cols):
        if verbose and (i + 1) % 50 == 0:
            print(f"    진행: {i+1}/{total}")
        
        name = code_name_map.get(col, col) if code_name_map else col
        result = analyze_single_stock_asymmetric(df, col, name, print_result=False)
        
        if result:
            results.append(result)
    
    df_results = pd.DataFrame(results)
    
    if verbose:
        print(f"\n분석 완료: {len(df_results)}/{total} 종목")
    
    return df_results


# ============================================================
# 6. 예측 기능 (Step 3)
# ============================================================

def get_current_regime(df_regime):
    """
    현재 레짐 확인
    """
    latest = df_regime.iloc[-1]
    
    return {
        'date': df_regime.index[-1],
        'regime': int(latest['risk_regime']),
        'regime_name': REGIME_MAP.get(int(latest['risk_regime']), 'Unknown'),
        'prob_stable': latest.get('prob_stable', None),
        'prob_normal': latest.get('prob_normal', None),
        'prob_crisis': latest.get('prob_crisis', None),
    }


def predict_fx_impact(df_results, current_regime, fx_change):
    """
    현재 레짐과 환율 변화에 따른 주식 영향 예측 (대칭)
    
    레짐 매핑 (수정됨):
        1 = 안정 (Risk-On)
        0 = 평범
        -1 = 위기 (Risk-Off)
    """
    regime_col_map = {1: 'beta_fx_stable', 0: 'beta_fx_normal', -1: 'beta_fx_crisis'}
    beta_col = regime_col_map.get(current_regime)
    
    if beta_col is None:
        raise ValueError(f"잘못된 레짐: {current_regime}")
    
    df_pred = df_results[['code', 'name', beta_col]].copy()
    df_pred['fx_change'] = fx_change
    df_pred['predicted_impact'] = df_pred[beta_col] * fx_change
    df_pred = df_pred.rename(columns={beta_col: 'beta_fx'})
    
    return df_pred.sort_values('predicted_impact')


def predict_fx_impact_asymmetric(df_results, current_regime, fx_change):
    """
    현재 레짐과 환율 변화에 따른 주식 영향 예측 (비대칭)
    """
    regime_map = {1: 'stable', 0: 'normal', -1: 'crisis'}
    regime_name = regime_map.get(current_regime)
    
    if regime_name is None:
        raise ValueError(f"잘못된 레짐: {current_regime}")
    
    # 환율 상승/하락에 따라 다른 β 사용
    if fx_change > 0:
        beta_col = f'beta_fx_{regime_name}_up'
    else:
        beta_col = f'beta_fx_{regime_name}_down'
    
    df_pred = df_results[['code', 'name', beta_col]].copy()
    df_pred['fx_change'] = fx_change
    df_pred['predicted_impact'] = df_pred[beta_col] * fx_change
    df_pred = df_pred.rename(columns={beta_col: 'beta_fx'})
    
    return df_pred.sort_values('predicted_impact')


def print_prediction_report(df_pred, current_regime_info, fx_change, top_n=10):
    """
    예측 결과 리포트 출력
    """
    regime_name = current_regime_info['regime_name']
    
    print("\n" + "="*60)
    print("FX 영향 예측 리포트")
    print("="*60)
    
    print(f"\n[현재 상황]")
    print(f"    날짜: {current_regime_info['date']}")
    print(f"    레짐: {regime_name} ({current_regime_info['regime']})")
    print(f"    환율 변화: {fx_change*100:+.2f}%")
    
    direction = "상승" if fx_change > 0 else "하락"
    
    print(f"\n[환율 {direction} 시 가장 부정적 영향 종목 (Top {top_n})]")
    df_worst = df_pred.head(top_n)
    for _, row in df_worst.iterrows():
        impact_pct = row['predicted_impact'] * 100
        print(f"    {row['name']:<15} β={row['beta_fx']:>7.4f} → 예상 {impact_pct:>+6.2f}%")
    
    print(f"\n[환율 {direction} 시 가장 긍정적 영향 종목 (Top {top_n})]")
    df_best = df_pred.tail(top_n).iloc[::-1]
    for _, row in df_best.iterrows():
        impact_pct = row['predicted_impact'] * 100
        print(f"    {row['name']:<15} β={row['beta_fx']:>7.4f} → 예상 {impact_pct:>+6.2f}%")


# ============================================================
# 7. 결과 요약
# ============================================================

def get_top_fx_sensitive_stocks(df_results, regime='crisis', top_n=20, ascending=True):
    """
    FX 민감도 상위/하위 종목 추출 (대칭)
    """
    col = f'beta_fx_{regime}'
    pval_col = f'pval_fx_{regime}'
    
    df_sig = df_results[df_results[pval_col] < 0.1].copy()
    df_top = df_sig.nsmallest(top_n, col) if ascending else df_sig.nlargest(top_n, col)
    
    return df_top[['code', 'name', col, pval_col, 'wald_p']]


def summarize_results(df_results):
    """
    대칭 분석 결과 요약
    """
    summary = {
        'total_stocks': len(df_results),
        'avg_r_squared': df_results['r_squared'].mean(),
        'wald_significant': (df_results['wald_p'] < 0.05).sum(),
        'wald_significant_pct': (df_results['wald_p'] < 0.05).mean() * 100,
    }
    
    # 순서: stable, normal, crisis
    for regime in ['stable', 'normal', 'crisis']:
        col = f'beta_fx_{regime}'
        pval_col = f'pval_fx_{regime}'
        
        summary[f'{regime}_avg_beta'] = df_results[col].mean()
        summary[f'{regime}_significant'] = (df_results[pval_col] < 0.05).sum()
        summary[f'{regime}_negative'] = (df_results[col] < 0).sum()
    
    return summary


def summarize_asymmetric_results(df_results):
    """
    비대칭 분석 결과 요약
    """
    summary = {
        'total_stocks': len(df_results),
        'avg_r_squared': df_results['r_squared'].mean(),
    }
    
    # 전체 Wald test
    if 'wald_all_p' in df_results.columns:
        summary['wald_all_significant'] = (df_results['wald_all_p'] < 0.05).sum()
        summary['wald_all_significant_pct'] = (df_results['wald_all_p'] < 0.05).mean() * 100
    
    # 레짐별 비대칭 Wald test
    for regime in ['stable', 'normal', 'crisis']:
        col = f'wald_{regime}_asym_p'
        if col in df_results.columns:
            summary[f'{regime}_asym_significant'] = (df_results[col] < 0.05).sum()
            summary[f'{regime}_asym_significant_pct'] = (df_results[col] < 0.05).mean() * 100
    
    # 레짐별 평균 β
    for regime in ['stable', 'normal', 'crisis']:
        for direction in ['up', 'down']:
            col = f'beta_fx_{regime}_{direction}'
            if col in df_results.columns:
                summary[f'{regime}_{direction}_avg_beta'] = df_results[col].mean()
    
    return summary


def print_summary(df_results):
    """
    대칭 결과 요약 출력
    """
    summary = summarize_results(df_results)
    
    print("\n" + "="*60)
    print("분석 결과 요약")
    print("="*60)
    
    print(f"\n총 분석 종목: {summary['total_stocks']}개")
    print(f"평균 R²: {summary['avg_r_squared']:.4f}")
    
    print(f"\n[Wald Test 결과]")
    print(f"레짐별 β_fx 유의하게 다른 종목: {summary['wald_significant']}개 ({summary['wald_significant_pct']:.1f}%)")
    
    print(f"\n[레짐별 β_fx 요약]")
    print(f"{'레짐':<15} {'평균 β':>10} {'유의(5%)':>10} {'음수':>10}")
    print("-" * 47)
    
    # 순서: 안정 → 평범 → 위기
    for regime, name in [('stable', '안정(RiskOn)'), ('normal', '평범'), ('crisis', '위기(RiskOff)')]:
        avg_beta = summary[f'{regime}_avg_beta']
        sig = summary[f'{regime}_significant']
        neg = summary[f'{regime}_negative']
        print(f"{name:<15} {avg_beta:>10.4f} {sig:>10}개 {neg:>10}개")


def print_asymmetric_summary(df_results):
    """
    비대칭 결과 요약 출력
    """
    summary = summarize_asymmetric_results(df_results)
    
    print("\n" + "="*60)
    print("비대칭 분석 결과 요약")
    print("="*60)
    
    print(f"\n총 분석 종목: {summary['total_stocks']}개")
    print(f"평균 R²: {summary['avg_r_squared']:.4f}")
    
    if 'wald_all_significant' in summary:
        print(f"\n[전체 Wald Test]")
        print(f"6개 β_fx 모두 다른 종목: {summary['wald_all_significant']}개 ({summary['wald_all_significant_pct']:.1f}%)")
    
    print(f"\n[레짐 내 비대칭 (상승≠하락) 종목 수]")
    for regime, name in [('stable', '안정'), ('normal', '평범'), ('crisis', '위기')]:
        key = f'{regime}_asym_significant'
        if key in summary:
            cnt = summary[key]
            pct = summary[f'{regime}_asym_significant_pct']
            print(f"    {name}: {cnt}개 ({pct:.1f}%)")
    
    print(f"\n[레짐별 × 방향별 평균 β_fx]")
    print(f"{'레짐':<15} {'상승(β)':>12} {'하락(β)':>12} {'차이':>12}")
    print("-" * 53)
    
    for regime, name in [('stable', '안정(RiskOn)'), ('normal', '평범'), ('crisis', '위기(RiskOff)')]:
        up_key = f'{regime}_up_avg_beta'
        down_key = f'{regime}_down_avg_beta'
        if up_key in summary and down_key in summary:
            up_beta = summary[up_key]
            down_beta = summary[down_key]
            diff = up_beta - down_beta
            print(f"{name:<15} {up_beta:>12.4f} {down_beta:>12.4f} {diff:>12.4f}")