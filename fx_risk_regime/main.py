# -*- coding: utf-8 -*-
"""
FX Risk Regime 분석 - 메인 실행 파일 (v3)

기능:
- Part 1: 대칭 OLS + HAC
- Part 1-C: 비대칭 OLS (환율 상승/하락 분리)
- Step 3: 현재 레짐 기반 예측
- 엑셀 리포트 + 그래프 시각화

실행 방법:
    python main.py
"""

import sys
from pathlib import Path

# truston_quant_dev 경로 추가
sys.path.insert(0, r'C:\Users\intern6\trst_dev\truston_quant_dev')

# 현재 폴더를 path에 추가
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
import numpy as np

# 로컬 모듈
from data_loader import load_all_data
from r_regime import load_risk_regime, get_regime_summary
from analysis import (
    prepare_data,
    create_regime_dummies,
    create_asymmetric_dummies,
    analyze_single_stock,
    analyze_single_stock_asymmetric,
    analyze_all_stocks,
    analyze_all_stocks_asymmetric,
    get_top_fx_sensitive_stocks,
    print_summary,
    print_asymmetric_summary,
    get_current_regime,
    predict_fx_impact,
    predict_fx_impact_asymmetric,
    print_prediction_report,
)
from visualization import generate_all_visualizations


def main():
    """메인 실행 함수"""
    
    print("="*60)
    print("FX Risk Regime 분석 (v3)")
    print("대칭 + 비대칭 OLS + HAC + 예측 + 시각화")
    print("="*60)
    
    # ============================================================
    # 1. 데이터 로드
    # ============================================================
    print("\n[1] 데이터 로드")
    print("-"*40)
    
    data_dir = Path(__file__).parent / 'data'
    
    # 주식/지수/환율 로드
    df_stock, df_index, df_fx, code_name_map = load_all_data(data_dir)
    
    # 레짐 로드 (DB에서)
    print("\n[레짐 데이터 로드]")
    df_regime = load_risk_regime('2020-01-01', '2025-12-31')
    print(f"    DB 레짐: {len(df_regime)}일")
    
    # ============================================================
    # 2. 데이터 전처리
    # ============================================================
    print("\n[2] 데이터 전처리")
    print("-"*40)
    
    df = prepare_data(df_stock, df_index, df_fx, df_regime)
    df = create_regime_dummies(df)
    df = create_asymmetric_dummies(df)
    
    print(f"    병합 후: {len(df)}일")
    print(f"    기간: {df.index.min().strftime('%Y-%m-%d')} ~ {df.index.max().strftime('%Y-%m-%d')}")
    
    # 레짐 분포
    get_regime_summary(df[['risk_regime']])
    
    # 종목 컬럼 추출
    stock_cols = [col for col in df.columns if col.startswith('A')]
    
    # ============================================================
    # 3. 대칭 분석 (Part 1)
    # ============================================================
    print("\n" + "="*60)
    print("[Part 1] 대칭 분석 (3개 레짐)")
    print("="*60)
    
    # 삼성전자 단일 분석
    if 'A005930' in stock_cols:
        analyze_single_stock(df, 'A005930', code_name_map.get('A005930', '삼성전자'))
    
    # 전체 종목 분석
    print("\n[전체 종목 대칭 분석]")
    df_results = analyze_all_stocks(df, stock_cols, code_name_map, verbose=True)
    print_summary(df_results)
    
    # 최종 판단
    wald_sig_pct = (df_results['wald_p'] < 0.05).mean() * 100
    wald_sig_cnt = (df_results['wald_p'] < 0.05).sum()
    total_cnt = len(df_results)
    
    print("\n" + "-"*40)
    print("[대칭 분석 판단]")
    print(f"Wald Test 유의 비율: {wald_sig_pct:.1f}% ({wald_sig_cnt}/{total_cnt})")
    
    if wald_sig_pct >= 30:
        print("→ ✅ 레짐 분리 효과적!")
    elif wald_sig_pct >= 10:
        print("→ ⚠️ 일부 종목만 유효")
    else:
        print("→ ❌ 레짐 효과 없음")
    
    # ============================================================
    # 4. 비대칭 분석 (Part 1-C)
    # ============================================================
    print("\n" + "="*60)
    print("[Part 1-C] 비대칭 분석 (6개: 3레짐 × 2방향)")
    print("="*60)
    
    # 삼성전자 비대칭 분석
    if 'A005930' in stock_cols:
        analyze_single_stock_asymmetric(df, 'A005930', code_name_map.get('A005930', '삼성전자'))
    
    # 전체 종목 비대칭 분석
    print("\n[전체 종목 비대칭 분석]")
    df_results_asym = analyze_all_stocks_asymmetric(df, stock_cols, code_name_map, verbose=True)
    print_asymmetric_summary(df_results_asym)
    
    # 비대칭 판단
    print("\n" + "-"*40)
    print("[비대칭 분석 판단]")
    
    for regime, name in [('crisis', '위기'), ('normal', '평범'), ('stable', '안정')]:
        col = f'wald_{regime}_asym_p'
        if col in df_results_asym.columns:
            asym_pct = (df_results_asym[col] < 0.05).mean() * 100
            asym_cnt = (df_results_asym[col] < 0.05).sum()
            
            if asym_pct >= 20:
                verdict = "✅ 비대칭 효과 있음"
            elif asym_pct >= 10:
                verdict = "⚠️ 일부 비대칭"
            else:
                verdict = "❌ 대칭으로 충분"
            
            print(f"  {name} 레짐: {asym_pct:.1f}% ({asym_cnt}개) → {verdict}")
    
    # ============================================================
    # 5. Step 3: 현재 레짐 기반 예측
    # ============================================================
    print("\n" + "="*60)
    print("[Step 3] 현재 레짐 기반 예측")
    print("="*60)
    
    # 현재 레짐 확인
    current = get_current_regime(df_regime)
    print(f"\n[현재 레짐 정보]")
    print(f"    날짜: {current['date']}")
    print(f"    레짐: {current['regime_name']} ({current['regime']})")
    if current['prob_crisis']:
        print(f"    확률: 위기={current['prob_crisis']:.2f}, 평범={current['prob_normal']:.2f}, 안정={current['prob_stable']:.2f}")
    
    # 환율 1% 상승 시 예측 (대칭)
    print("\n[환율 1% 상승 시 영향 예측 - 대칭]")
    df_pred = predict_fx_impact(df_results, current['regime'], fx_change=0.01)
    print_prediction_report(df_pred, current, fx_change=0.01, top_n=10)
    
    # 환율 1% 상승 시 예측 (비대칭)
    print("\n[환율 1% 상승 시 영향 예측 - 비대칭]")
    df_pred_asym = predict_fx_impact_asymmetric(df_results_asym, current['regime'], fx_change=0.01)
    print_prediction_report(df_pred_asym, current, fx_change=0.01, top_n=10)
    
    # ============================================================
    # 6. 시각화 및 엑셀 리포트
    # ============================================================
    print("\n" + "="*60)
    print("[시각화 및 리포트 생성]")
    print("="*60)
    
    results_dir = Path(__file__).parent / 'results'
    generate_all_visualizations(df_results, df_results_asym, df_regime, results_dir)
    
    # CSV도 저장
    print("\n[CSV 저장]")
    df_results.to_csv(results_dir / 'fx_regime_results_symmetric.csv', index=False, encoding='utf-8-sig')
    print(f"    대칭 결과: {results_dir / 'fx_regime_results_symmetric.csv'}")
    
    df_results_asym.to_csv(results_dir / 'fx_regime_results_asymmetric.csv', index=False, encoding='utf-8-sig')
    print(f"    비대칭 결과: {results_dir / 'fx_regime_results_asymmetric.csv'}")
    
    print("\n" + "="*60)
    print("분석 완료!")
    print("="*60)
    print(f"\n결과 폴더: {results_dir}")
    print("  - fx_regime_report.xlsx (엑셀 리포트)")
    print("  - fig1~5_*.png (그래프)")
    print("  - fx_regime_results_*.csv (CSV)")
    
    return df_results, df_results_asym


if __name__ == '__main__':
    df_results, df_results_asym = main()