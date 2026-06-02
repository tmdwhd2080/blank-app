# -*- coding: utf-8 -*-
"""
데이터 로드 모듈
- 코스피200 수정주가
- 코스피200 지수
- 환율 (USD/KRW)
"""

import pandas as pd
from pathlib import Path


def load_stock_prices(filepath):
    """
    코스피200 수정주가 로드
    
    Parameters
    ----------
    filepath : str or Path
        엑셀 파일 경로
    
    Returns
    -------
    df_stock : DataFrame
        수정주가 (index: date, columns: 종목코드)
    code_name_map : dict
        종목코드 → 종목명 매핑
    """
    df = pd.read_excel(filepath, header=None)
    
    # 헤더 위치
    code_row = 7
    name_row = 8
    data_start = 14
    
    # 종목 코드와 이름 추출
    codes = df.iloc[code_row, 1:].tolist()
    names = df.iloc[name_row, 1:].tolist()
    
    # 데이터 추출
    data = df.iloc[data_start:, :].copy()
    data.columns = ['date'] + codes
    data['date'] = pd.to_datetime(data['date'])
    data = data.set_index('date')
    
    # 종목코드 컬럼만 선택 (A로 시작)
    stock_cols = [c for c in data.columns if isinstance(c, str) and c.startswith('A')]
    df_stock = data[stock_cols].copy()
    
    # 숫자형 변환
    df_stock = df_stock.apply(pd.to_numeric, errors='coerce')
    df_stock = df_stock.dropna(how='all', axis=1)
    df_stock = df_stock.sort_index()
    
    # 종목코드-종목명 매핑
    code_name_map = dict(zip(codes, names))
    
    return df_stock, code_name_map


def load_index(filepath):
    """
    코스피200 지수 로드
    
    Parameters
    ----------
    filepath : str or Path
        CSV 파일 경로
    
    Returns
    -------
    df_index : DataFrame
        지수 데이터 (index: date, columns: close, open, high, low)
    """
    df = pd.read_csv(filepath)
    df.columns = ['date', 'close', 'open', 'high', 'low', 'volume', 'change']
    
    # 날짜 정리 (공백 제거)
    df['date'] = df['date'].str.replace(' ', '')
    df['date'] = pd.to_datetime(df['date'])
    
    # 숫자형 변환
    for col in ['close', 'open', 'high', 'low']:
        df[col] = pd.to_numeric(df[col].astype(str).str.replace(',', ''), errors='coerce')
    
    df = df.set_index('date')
    df = df.sort_index()
    
    return df[['close', 'open', 'high', 'low']]


def load_fx(filepath):
    """
    환율 데이터 로드
    
    Parameters
    ----------
    filepath : str or Path
        엑셀 파일 경로
    
    Returns
    -------
    df_fx : DataFrame
        환율 데이터 (index: date, columns: fx)
    """
    df = pd.read_excel(filepath, header=None)
    
    # 데이터 시작 행
    data_start = 14
    
    data = df.iloc[data_start:, [0, 1]].copy()
    data.columns = ['date', 'fx']
    data['date'] = pd.to_datetime(data['date'])
    data['fx'] = pd.to_numeric(data['fx'], errors='coerce')
    data = data.set_index('date')
    data = data.sort_index()
    
    return data


def load_all_data(data_dir):
    """
    모든 데이터 로드
    
    Parameters
    ----------
    data_dir : str or Path
        데이터 폴더 경로
    
    Returns
    -------
    df_stock : DataFrame
        수정주가
    df_index : DataFrame
        지수
    df_fx : DataFrame
        환율
    code_name_map : dict
        종목코드 → 종목명
    """
    data_dir = Path(data_dir)
    
    print("[데이터 로드]")
    
    # 수정주가
    df_stock, code_name_map = load_stock_prices(data_dir / '코스피_200_수정주가.xlsx')
    print(f"    주식: {df_stock.shape[0]}일 × {df_stock.shape[1]}종목")
    
    # 지수
    df_index = load_index(data_dir / '코스피_200_지수.csv')
    print(f"    지수: {len(df_index)}일")
    
    # 환율
    df_fx = load_fx(data_dir / '환율_종가_.xlsx')
    print(f"    환율: {len(df_fx)}일")
    
    # 기간 출력
    common_start = max(df_stock.index.min(), df_index.index.min(), df_fx.index.min())
    common_end = min(df_stock.index.max(), df_index.index.max(), df_fx.index.max())
    print(f"    공통 기간: {common_start.strftime('%Y-%m-%d')} ~ {common_end.strftime('%Y-%m-%d')}")
    
    return df_stock, df_index, df_fx, code_name_map


if __name__ == '__main__':
    print("="*60)
    print("데이터 로드 테스트")
    print("="*60)
    
    data_dir = Path(__file__).parent / 'data'
    
    try:
        df_stock, df_index, df_fx, code_name_map = load_all_data(data_dir)
        
        print("\n[주식 데이터 샘플]")
        print(df_stock.iloc[:5, :5])
        
        print("\n[지수 데이터 샘플]")
        print(df_index.head())
        
        print("\n[환율 데이터 샘플]")
        print(df_fx.head())
        
        print("\n[종목명 매핑 샘플]")
        for code in list(df_stock.columns)[:5]:
            print(f"    {code}: {code_name_map.get(code, 'N/A')}")
            
    except Exception as e:
        import traceback
        print(f"오류: {e}")
        traceback.print_exc()