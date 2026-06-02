# -*- coding: utf-8 -*-
"""
팩터 모멘텀 분석 (6개월)
- 시계열 모멘텀 (TSFM): 과거 6개월 수익률 부호 기반
- 횡단면 모멘텀 (CSFM): 팩터 간 상대 순위 기반
- SKIP_PERIOD: 직전 N주 제외 옵션
# 26 주 계산 + 1주씩 롤링 / 거래 비용 x
"""

import os
import sys
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import scipy.stats as stats
from datetime import datetime

sys.path.append(r'C:\Users\intern9\truston_quant_dev')
from util.database2 import MSSQL, FACTOR_MAPPING

# ===== 설정 =====
START_DATE = '2003-01-01'
END_DATE = '2025-12-19'
LOOKBACK = 10  # 모멘텀 계산 기간 (52주 = 1년)
SKIP_PERIOD =5  # 스킵 기간 (4주 = 1개월) - 직전 N주 제외
TOP_N = 2          # 횡단면 모멘텀: 상위/하위 N개 팩터


# ===== 1. 데이터 로드 =====
def load_factor_returns():
    """DB에서 팩터 수익률 로드 (Rtn_L_S 사용)"""
    
    print("=" * 60)
    print("[Step 1] 팩터 수익률 데이터 로드")
    print("=" * 60)
    
    db = MSSQL()
    
    # PFM_FCTR 테이블에서 팩터 수익률 조회
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
        raise ValueError("데이터가 없습니다.")
    
    # 피벗: 날짜 x 팩터
    df['BaseDate'] = pd.to_datetime(df['BaseDate'])
    df['Rtn_L_S'] = pd.to_numeric(df['Rtn_L_S'], errors='coerce')
    
    df_pivot = df.pivot_table(
        index='BaseDate',
        columns='FLD_NAME',
        values='Rtn_L_S',
        aggfunc='first'
    )
    
    # 팩터명 변환
    df_pivot = df_pivot.rename(columns=FACTOR_MAPPING)
    df_pivot = df_pivot.sort_index()
    
    # 결측치 처리
    df_pivot = df_pivot.dropna(how='all')
    
    print(f"  기간: {df_pivot.index.min().date()} ~ {df_pivot.index.max().date()}")
    print(f"  관측 수: {len(df_pivot)}주")
    print(f"  팩터: {list(df_pivot.columns)}")
    print(f"\n  기초 통계 (주간 수익률):")
    print(df_pivot.describe().round(4))
    
    return df_pivot


# ===== 2. 모멘텀 시그널 계산 =====
def calculate_momentum_signals(factor_returns):
    """모멘텀 시그널 계산 (SKIP_PERIOD 적용)"""
    
    print("\n" + "=" * 60)
    print("[Step 2] 모멘텀 시그널 계산")
    print("=" * 60)
    
    # 누적 수익률 (LOOKBACK 주)
    factor_mom = (1 + factor_returns).rolling(window=LOOKBACK).apply(np.prod, raw=True) - 1
    
    # t+1 투자를 위한 shift(1) + 스킵 기간 shift(SKIP_PERIOD)
    # 총 shift = 1 + SKIP_PERIOD
    # 예: SKIP_PERIOD=4 → 5주 전까지의 52주 모멘텀으로 현재 주 투자
    trade_signals = factor_mom.shift(1 + SKIP_PERIOD)
    
    print(f"  Lookback: {LOOKBACK}주")
    print(f"  Skip Period: {SKIP_PERIOD}주 (직전 {SKIP_PERIOD}주 제외)")
    print(f"  시그널 기준: t-{1+SKIP_PERIOD}주 ~ t-{1+SKIP_PERIOD+LOOKBACK}주 수익률")
    print(f"  유효 시그널 시작: {trade_signals.dropna().index.min().date()}")
    
    return trade_signals


# ===== 3. 시계열 모멘텀 (TSFM) =====
def time_series_momentum(factor_returns, trade_signals):
    """
    시계열 모멘텀: 각 팩터별로 과거 수익률 부호에 따라 Long/Short
    """
    
    print("\n" + "=" * 60)
    print("[Step 3] 시계열 모멘텀 (TSFM) 분석")
    print("=" * 60)
    
    valid_idx = trade_signals.dropna().index
    n_factors = len(factor_returns.columns)
    
    # 시그널 부호 (+1 또는 -1)
    ts_weights = np.sign(trade_signals.loc[valid_idx])
    
    # 개별 팩터별 TSFM 수익률
    ts_factor_returns = ts_weights * factor_returns.loc[valid_idx]
    
    # 전체 포트폴리오 (Equal Weight)
    ts_portfolio = ts_factor_returns.mean(axis=1)
    
    # 팩터별 TSFM 성과
    print("\n  [팩터별 시계열 모멘텀 성과]")
    print("-" * 70)
    
    ts_stats = pd.DataFrame(index=factor_returns.columns)
    
    for col in factor_returns.columns:
        ret = ts_factor_returns[col].dropna()
        ann_ret = ret.mean() * 52
        ann_vol = ret.std() * np.sqrt(52)
        sharpe = ann_ret / ann_vol if ann_vol > 0 else 0
        t_stat, p_val = stats.ttest_1samp(ret, 0)
        
        # 모멘텀 지속성: 시그널 방향 적중률
        correct = (ts_weights[col] * factor_returns.loc[valid_idx, col] > 0).mean()
        
        ts_stats.loc[col, 'Ann_Return'] = ann_ret
        ts_stats.loc[col, 'Ann_Vol'] = ann_vol
        ts_stats.loc[col, 'Sharpe'] = sharpe
        ts_stats.loc[col, 't_stat'] = t_stat
        ts_stats.loc[col, 'p_value'] = p_val
        ts_stats.loc[col, 'Hit_Rate'] = correct
    
    print(ts_stats.round(4).to_string())
    
    # 통계적 유의성 요약
    significant = ts_stats[ts_stats['p_value'] < 0.05]
    print(f"\n  통계적으로 유의한 팩터 (p < 0.05): {len(significant)}개")
    if len(significant) > 0:
        print(f"    → {list(significant.index)}")
    
    return ts_portfolio, ts_factor_returns, ts_stats


# ===== 4. 횡단면 모멘텀 (CSFM) =====
def cross_sectional_momentum(factor_returns, trade_signals):
    """
    횡단면 모멘텀: 팩터 간 상대 순위에 따라 상위 N개 Long, 하위 N개 Short
    """
    
    print("\n" + "=" * 60)
    print("[Step 4] 횡단면 모멘텀 (CSFM) 분석")
    print("=" * 60)
    
    valid_idx = trade_signals.dropna().index
    n_factors = len(factor_returns.columns)
    
    def get_cs_weights(row):
        """횡단면 랭킹 기반 가중치"""
        ranks = row.rank(ascending=False)
        weights = pd.Series(0.0, index=row.index)
        weights[ranks <= TOP_N] = 1.0 / TOP_N          # Long Top N
        weights[ranks > (n_factors - TOP_N)] = -1.0 / TOP_N  # Short Bottom N
        return weights
    
    cs_weights = trade_signals.loc[valid_idx].apply(get_cs_weights, axis=1)
    cs_portfolio = (cs_weights * factor_returns.loc[valid_idx]).sum(axis=1)
    
    # 성과 분석
    ann_ret = cs_portfolio.mean() * 52
    ann_vol = cs_portfolio.std() * np.sqrt(52)
    sharpe = ann_ret / ann_vol if ann_vol > 0 else 0
    t_stat, p_val = stats.ttest_1samp(cs_portfolio, 0)
    
    print(f"\n  [횡단면 모멘텀 포트폴리오 성과]")
    print(f"    Top/Bottom: {TOP_N}개 팩터")
    print(f"    연율 수익률: {ann_ret:.4f} ({ann_ret*100:.2f}%)")
    print(f"    연율 변동성: {ann_vol:.4f} ({ann_vol*100:.2f}%)")
    print(f"    Sharpe Ratio: {sharpe:.4f}")
    print(f"    t-statistic: {t_stat:.4f}")
    print(f"    p-value: {p_val:.4f}")
    print(f"    통계적 유의성: {'Yes (p < 0.05)' if p_val < 0.05 else 'No'}")
    
    # 팩터별 Long/Short 빈도
    print(f"\n  [팩터별 Long/Short 빈도]")
    long_freq = (cs_weights > 0).mean()
    short_freq = (cs_weights < 0).mean()
    freq_df = pd.DataFrame({
        'Long_Freq': long_freq,
        'Short_Freq': short_freq,
        'Net_Exposure': long_freq - short_freq
    })
    print(freq_df.round(4).to_string())
    
    return cs_portfolio, cs_weights


# ===== 5. 모멘텀 지속성 분석 =====
def analyze_momentum_persistence(factor_returns):
    """팩터 수익률의 자기상관 분석"""
    
    print("\n" + "=" * 60)
    print("[Step 5] 모멘텀 지속성 분석 (자기상관)")
    print("=" * 60)
    
    lags = [1, 4, 13, 26, 52]  # 1주, 1개월, 3개월, 6개월, 1년
    
    autocorr_df = pd.DataFrame(index=factor_returns.columns, columns=[f'Lag_{l}w' for l in lags])
    
    for col in factor_returns.columns:
        for lag in lags:
            ac = factor_returns[col].autocorr(lag=lag)
            autocorr_df.loc[col, f'Lag_{lag}w'] = ac
    
    print("\n  [팩터별 자기상관 계수]")
    print(autocorr_df.round(4).to_string())
    
    # 평균 자기상관
    print(f"\n  [평균 자기상관]")
    for lag in lags:
        avg_ac = autocorr_df[f'Lag_{lag}w'].astype(float).mean()
        print(f"    {lag}주 시차: {avg_ac:.4f}")
    
    return autocorr_df


# ===== 6. 결과 시각화 =====
def visualize_results(factor_returns, ts_portfolio, cs_portfolio, ts_stats, autocorr_df):
    """결과 시각화"""
    
    print("\n" + "=" * 60)
    print("[Step 6] 결과 시각화")
    print("=" * 60)
    
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    
    # (1) 누적 수익률 비교
    ax1 = axes[0, 0]
    results = pd.DataFrame({
        'TSFM': ts_portfolio,
        'CSFM': cs_portfolio,
        'EqualWeight': factor_returns.loc[ts_portfolio.index].mean(axis=1)
    })
    cum_ret = (1 + results).cumprod()
    cum_ret.plot(ax=ax1)
    ax1.set_title(f'Cumulative Returns (Lookback={LOOKBACK}w, Skip={SKIP_PERIOD}w)')
    ax1.set_xlabel('Date')
    ax1.set_ylabel('Cumulative Return')
    ax1.legend()
    ax1.grid(True, alpha=0.3)
    
    # (2) 팩터별 TSFM Sharpe Ratio
    ax2 = axes[0, 1]
    ts_stats['Sharpe'].plot(kind='bar', ax=ax2, color='steelblue', edgecolor='black')
    ax2.axhline(y=0, color='red', linestyle='--')
    ax2.set_title('Time-Series Momentum: Sharpe Ratio by Factor')
    ax2.set_xlabel('Factor')
    ax2.set_ylabel('Sharpe Ratio')
    ax2.tick_params(axis='x', rotation=45)
    ax2.grid(True, alpha=0.3, axis='y')
    
    # (3) 자기상관 히트맵
    ax3 = axes[1, 0]
    autocorr_numeric = autocorr_df.astype(float)
    im = ax3.imshow(autocorr_numeric.values, cmap='RdBu_r', vmin=-0.3, vmax=0.3, aspect='auto')
    ax3.set_xticks(range(len(autocorr_df.columns)))
    ax3.set_xticklabels(autocorr_df.columns, rotation=45)
    ax3.set_yticks(range(len(autocorr_df.index)))
    ax3.set_yticklabels(autocorr_df.index)
    ax3.set_title('Factor Autocorrelation (Momentum Persistence)')
    plt.colorbar(im, ax=ax3, shrink=0.8)
    
    # (4) 롤링 Sharpe
    ax4 = axes[1, 1]
    rolling_window = 52  # 1년
    rolling_sharpe_ts = (ts_portfolio.rolling(rolling_window).mean() / 
                         ts_portfolio.rolling(rolling_window).std()) * np.sqrt(52)
    rolling_sharpe_cs = (cs_portfolio.rolling(rolling_window).mean() / 
                         cs_portfolio.rolling(rolling_window).std()) * np.sqrt(52)
    
    rolling_sharpe_ts.plot(ax=ax4, label='TSFM', alpha=0.8)
    rolling_sharpe_cs.plot(ax=ax4, label='CSFM', alpha=0.8)
    ax4.axhline(y=0, color='red', linestyle='--')
    ax4.set_title(f'Rolling {rolling_window}-Week Sharpe Ratio')
    ax4.set_xlabel('Date')
    ax4.set_ylabel('Sharpe Ratio')
    ax4.legend()
    ax4.grid(True, alpha=0.3)
    
    plt.tight_layout()
    plt.savefig(r'C:\Users\intern9\truston_quant_dev\results\factor_momentum_analysis.png', 
                dpi=150, bbox_inches='tight')
    plt.show()
    
    print("  차트 저장 완료")


# ===== 7. 요약 리포트 =====
def print_summary(ts_stats, cs_portfolio, autocorr_df, factor_returns, ts_portfolio):
    """분석 요약"""
    
    print("\n" + "=" * 60)
    print("[Summary] 팩터 모멘텀 분석 결과")
    print("=" * 60)
    print(f"  설정: Lookback={LOOKBACK}주, Skip={SKIP_PERIOD}주, Top/Bottom={TOP_N}개")
    
    # Equal Weight 성과
    valid_idx = ts_portfolio.index
    ew_returns = factor_returns.loc[valid_idx].mean(axis=1)
    ew_ann_ret = ew_returns.mean() * 52
    ew_ann_vol = ew_returns.std() * np.sqrt(52)
    ew_sharpe = ew_ann_ret / ew_ann_vol if ew_ann_vol > 0 else 0
    ew_t_stat, ew_p_val = stats.ttest_1samp(ew_returns, 0)
    
    print("\n0. Equal Weight (단순 팩터 평균) 성과:")
    print(f"    연율 수익률: {ew_ann_ret:.4f} ({ew_ann_ret*100:.2f}%)")
    print(f"    연율 변동성: {ew_ann_vol:.4f} ({ew_ann_vol*100:.2f}%)")
    print(f"    Sharpe Ratio: {ew_sharpe:.4f}")
    print(f"    t-statistic: {ew_t_stat:.4f}")
    print(f"    p-value: {ew_p_val:.4f}")
    
    print("\n1. 시계열 모멘텀 (TSFM) 존재 여부:")
    for factor in ts_stats.index:
        p_val = ts_stats.loc[factor, 'p_value']
        sharpe = ts_stats.loc[factor, 'Sharpe']
        hit_rate = ts_stats.loc[factor, 'Hit_Rate']
        exists = "✓ 존재" if p_val < 0.05 and sharpe > 0 else "✗ 미확인"
        print(f"    {factor:12} : {exists} (Sharpe={sharpe:.3f}, Hit={hit_rate:.1%}, p={p_val:.4f})")
    
    # TSFM 포트폴리오 전체 성과
    ts_ann_ret = ts_portfolio.mean() * 52
    ts_ann_vol = ts_portfolio.std() * np.sqrt(52)
    ts_sharpe = ts_ann_ret / ts_ann_vol if ts_ann_vol > 0 else 0
    ts_t_stat, ts_p_val = stats.ttest_1samp(ts_portfolio, 0)
    
    print(f"\n    [TSFM 포트폴리오 전체]")
    print(f"    연율 수익률: {ts_ann_ret:.4f} ({ts_ann_ret*100:.2f}%)")
    print(f"    연율 변동성: {ts_ann_vol:.4f} ({ts_ann_vol*100:.2f}%)")
    print(f"    Sharpe Ratio: {ts_sharpe:.4f}")
    print(f"    p-value: {ts_p_val:.4f}")
    
    print("\n2. 횡단면 모멘텀 (CSFM) 존재 여부:")
    cs_ret = cs_portfolio.mean() * 52
    cs_vol = cs_portfolio.std() * np.sqrt(52)
    cs_sharpe = cs_ret / cs_vol if cs_vol > 0 else 0
    t_stat, p_val = stats.ttest_1samp(cs_portfolio, 0)
    exists = "✓ 존재" if p_val < 0.05 and cs_sharpe > 0 else "✗ 미확인"
    print(f"    CSFM 포트폴리오: {exists}")
    print(f"    연율 수익률: {cs_ret:.4f} ({cs_ret*100:.2f}%)")
    print(f"    연율 변동성: {cs_vol:.4f} ({cs_vol*100:.2f}%)")
    print(f"    Sharpe Ratio: {cs_sharpe:.4f}")
    print(f"    p-value: {p_val:.4f}")
    
    print("\n3. 모멘텀 강도 (26주 자기상관):")
    lag_col = 'Lag_26w'
    for factor in autocorr_df.index:
        ac = float(autocorr_df.loc[factor, lag_col])
        strength = "강함" if ac > 0.1 else ("약함" if ac > 0 else "없음/반전")
        print(f"    {factor:12} : {strength} (AC={ac:.4f})")
    
    # 전략 비교 요약
    print("\n" + "-" * 60)
    print("[전략 비교 요약]")
    print("-" * 60)
    comparison = pd.DataFrame({
        'Strategy': ['Equal Weight', 'TSFM', 'CSFM'],
        'Ann_Return': [ew_ann_ret, ts_ann_ret, cs_ret],
        'Ann_Vol': [ew_ann_vol, ts_ann_vol, cs_vol],
        'Sharpe': [ew_sharpe, ts_sharpe, cs_sharpe],
        'p_value': [ew_p_val, ts_p_val, p_val]
    })
    comparison = comparison.set_index('Strategy')
    print(comparison.round(4).to_string())


# ===== 메인 실행 =====
if __name__ == '__main__':
    
    print("\n" + "=" * 60)
    print("팩터 모멘텀 분석")
    print("=" * 60)
    print(f"  Lookback: {LOOKBACK}주")
    print(f"  Skip Period: {SKIP_PERIOD}주 (직전 {SKIP_PERIOD}주 제외)")
    print(f"  Top/Bottom N: {TOP_N}개")
    
    # 1. 데이터 로드
    factor_returns = load_factor_returns()
    
    # 2. 모멘텀 시그널 계산
    trade_signals = calculate_momentum_signals(factor_returns)
    
    # 3. 시계열 모멘텀 분석
    ts_portfolio, ts_factor_returns, ts_stats = time_series_momentum(factor_returns, trade_signals)
    
    # 4. 횡단면 모멘텀 분석
    cs_portfolio, cs_weights = cross_sectional_momentum(factor_returns, trade_signals)
    
    # 5. 모멘텀 지속성 분석
    autocorr_df = analyze_momentum_persistence(factor_returns)
    
    # 6. 시각화
    visualize_results(factor_returns, ts_portfolio, cs_portfolio, ts_stats, autocorr_df)
    
    # 7. 요약 리포트
    print_summary(ts_stats, cs_portfolio, autocorr_df, factor_returns, ts_portfolio)
    
    print("\n" + "=" * 60)
    print("분석 완료!")
    print("=" * 60)
    # python Factor_Momentum\Momentum.py