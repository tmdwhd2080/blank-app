# -*- coding: utf-8 -*-
"""
레짐 데이터 로드 모듈
- DB에서 Risk 레짐 (DRAI) 조회
- CSV 저장 기능

레짐 해석:
    STATES = 1  → Risk-On  → 안정기 (위험자산 선호, 시장 좋음)
    STATES = 0  → Neutral  → 평범
    STATES = -1 → Risk-Off → 위기 (안전자산 선호, 시장 불안)
"""

import sys
import pandas as pd
from pathlib import Path

# truston_quant_dev 경로 추가
sys.path.insert(0, r'C:\Users\intern6\trst_dev\truston_quant_dev')

# DB 모듈 import
from util.database2 import MSSQL


# 레짐 매핑 (수정됨!)
REGIME_MAP = {
    1: '안정(Risk-On)',     # 위험자산 선호 = 시장 좋음
    0: '평범(Neutral)',      
    -1: '위기(Risk-Off)'    # 안전자산 선호 = 시장 불안
}


def load_risk_regime(start_date='2020-01-01', end_date='2025-12-31', save_csv=True, save_dir=None):
    """
    DB에서 Risk 레짐 (DRAI) 로드
    
    Parameters
    ----------
    start_date : str
        시작일 (YYYY-MM-DD)
    end_date : str
        종료일 (YYYY-MM-DD)
    save_csv : bool
        CSV 저장 여부
    save_dir : Path or str
        저장 경로 (None이면 results 폴더)
    
    Returns
    -------
    df_regime : DataFrame
        columns: risk_regime, prob_stable, prob_normal, prob_crisis
        index: date
    
    Notes
    -----
    risk_regime 값:
        1  = 안정 (Risk-On, 위험자산 선호)
        0  = 평범 (Neutral)
        -1 = 위기 (Risk-Off, 안전자산 선호)
    """
    db = MSSQL()
    
    query = f"""
    SELECT 
        LookBackDate as date,
        STATES as risk_regime,
        PROB_POSITIVE as prob_stable,
        PROB_NEUTRAL as prob_normal,
        PROB_NEGATIVE as prob_crisis
    FROM REGIME_QMS
    WHERE RegimeCode = 'RG00101'
      AND LookBackDate BETWEEN '{start_date}' AND '{end_date}'
      AND RECENT = 1
    ORDER BY LookBackDate
    """
    
    df_regime = db.SELECT(query)
    db.close()
    
    if df_regime is None or len(df_regime) == 0:
        raise ValueError(f"레짐 데이터 없음: {start_date} ~ {end_date}")
    
    # 데이터 타입 변환
    df_regime['date'] = pd.to_datetime(df_regime['date'])
    df_regime['risk_regime'] = df_regime['risk_regime'].astype(int)
    df_regime['prob_stable'] = pd.to_numeric(df_regime['prob_stable'], errors='coerce')
    df_regime['prob_normal'] = pd.to_numeric(df_regime['prob_normal'], errors='coerce')
    df_regime['prob_crisis'] = pd.to_numeric(df_regime['prob_crisis'], errors='coerce')
    
    # 레짐 이름 추가
    df_regime['regime_name'] = df_regime['risk_regime'].map(REGIME_MAP)
    
    # CSV 저장
    if save_csv:
        if save_dir is None:
            save_dir = Path(__file__).parent / 'results'
        else:
            save_dir = Path(save_dir)
        
        save_dir.mkdir(exist_ok=True)
        csv_path = save_dir / 'db_regime_data.csv'
        
        # 저장용 복사본
        df_save = df_regime.copy()
        df_save['date'] = df_save['date'].dt.strftime('%Y-%m-%d')
        df_save = df_save[['date', 'risk_regime', 'regime_name', 'prob_stable', 'prob_normal', 'prob_crisis']]
        df_save.to_csv(csv_path, index=False, encoding='utf-8-sig')
        print(f"    레짐 CSV 저장: {csv_path}")
    
    df_regime = df_regime.set_index('date')
    
    return df_regime


def get_regime_summary(df_regime):
    """
    레짐 분포 요약
    """
    print("\n[레짐 분포]")
    counts = df_regime['risk_regime'].value_counts().sort_index()
    total = len(df_regime)
    
    for regime, count in counts.items():
        regime_int = int(regime)
        name = REGIME_MAP.get(regime_int, str(regime_int))
        pct = count / total * 100
        print(f"    {name} ({regime_int:2d}): {count:5d}일 ({pct:5.1f}%)")
    
    return counts


def get_regime_transitions(df_regime):
    """
    레짐 전환 분석
    """
    df = df_regime.copy()
    if 'risk_regime' not in df.columns:
        return None
    
    df['prev_regime'] = df['risk_regime'].shift(1)
    df['transition'] = df['risk_regime'] != df['prev_regime']
    
    transitions = df[df['transition'] == True].copy()
    
    print(f"\n[레짐 전환 횟수]: {len(transitions)}회")
    
    return transitions


if __name__ == '__main__':
    print("="*60)
    print("Risk 레짐 데이터 로드 테스트")
    print("="*60)
    
    print("\n[레짐 해석]")
    print("    STATES = 1  → Risk-On  → 안정기 (위험자산 선호)")
    print("    STATES = 0  → Neutral  → 평범")
    print("    STATES = -1 → Risk-Off → 위기 (안전자산 선호)")
    
    try:
        df_regime = load_risk_regime('2020-01-01', '2025-12-31', save_csv=True)
        print(f"\n로드 완료: {len(df_regime)}일")
        print(f"기간: {df_regime.index.min()} ~ {df_regime.index.max()}")
        
        get_regime_summary(df_regime)
        get_regime_transitions(df_regime)
        
        print("\n[샘플 데이터]")
        print(df_regime.head(10))
        
    except Exception as e:
        import traceback
        print(f"오류: {e}")
        traceback.print_exc()