"""
MSCI 편입/편출 이벤트 일별 누적 초과수익률 분석
- D-30 ~ D+30 매일의 누적수익률을 열(column)로 표시
- 발표일(Announcement) 기준 / 리밸런싱일(Effective) 기준을 행으로 구분
- 초과수익률 = 종목 누적수익률 - KOSPI200 누적수익률

실행 방법:
    pip install pandas pykrx
    python msci_daily_excess_return.py

입력 파일: msci_kr_history_full.csv
출력 파일: msci_daily_excess_return.csv
"""

import pandas as pd
import numpy as np
from pykrx import stock
from datetime import datetime, timedelta
import time
import warnings
warnings.filterwarnings('ignore')


def get_trading_days(start_date: str, end_date: str) -> list:
    """거래일 목록 조회"""
    try:
        df = stock.get_market_ohlcv(start_date, end_date, "069500")
        return df.index.tolist()
    except:
        return []


def get_price_series(ticker: str, start_date: str, end_date: str) -> pd.Series:
    """종목의 일별 종가 시리즈 반환"""
    try:
        df = stock.get_market_ohlcv(start_date, end_date, ticker)
        return df['종가']
    except:
        return pd.Series()


def get_kospi200_price_series(start_date: str, end_date: str) -> pd.Series:
    """KOSPI200 (KODEX200 ETF) 일별 종가 시리즈 반환"""
    try:
        df = stock.get_market_ohlcv(start_date, end_date, "069500")
        return df['종가']
    except:
        return pd.Series()


def format_ticker(ticker) -> str:
    """티커를 6자리 문자열로 포맷"""
    if pd.isna(ticker):
        return None
    try:
        ticker_int = int(float(ticker))
        return f"{ticker_int:06d}"
    except:
        return None


def find_closest_trading_day_idx(target_date: datetime, trading_days: list) -> int:
    """target_date 이후의 가장 가까운 거래일 인덱스 반환"""
    for i, d in enumerate(trading_days):
        d_dt = pd.Timestamp(d).to_pydatetime()
        if d_dt >= target_date:
            return i
    return None


def calculate_daily_cumulative_returns(
    ticker: str, 
    event_date: datetime, 
    trading_days: list,
    stock_prices: pd.Series,
    kospi_prices: pd.Series,
    window: int = 30
) -> dict:
    """
    이벤트 기준일로부터 D-window ~ D+window까지 일별 누적 초과수익률 계산
    
    Returns:
        dict: {'D-30': value, 'D-29': value, ..., 'D+30': value}
    """
    result = {}
    
    # D-window ~ D+window 열 초기화
    for d in range(-window, window + 1):
        col_name = f"D{d:+d}" if d != 0 else "D0"
        result[col_name] = np.nan
    
    # 이벤트일의 거래일 인덱스 찾기
    event_idx = find_closest_trading_day_idx(event_date, trading_days)
    if event_idx is None:
        return result
    
    # 기준일 (D-window) 인덱스
    base_idx = event_idx - window
    if base_idx < 0:
        return result
    
    # 기준일 가격
    base_date = trading_days[base_idx]
    
    if base_date not in stock_prices.index or base_date not in kospi_prices.index:
        return result
    
    base_stock_price = stock_prices.loc[base_date]
    base_kospi_price = kospi_prices.loc[base_date]
    
    if base_stock_price == 0 or base_kospi_price == 0:
        return result
    
    # D-window ~ D+window 일별 누적수익률 계산
    for d in range(-window, window + 1):
        target_idx = event_idx + d
        
        if target_idx < 0 or target_idx >= len(trading_days):
            continue
        
        target_date = trading_days[target_idx]
        
        if target_date not in stock_prices.index or target_date not in kospi_prices.index:
            continue
        
        stock_price = stock_prices.loc[target_date]
        kospi_price = kospi_prices.loc[target_date]
        
        # 누적수익률: 기준일(D-window) 대비
        stock_cum_ret = (stock_price / base_stock_price) - 1
        kospi_cum_ret = (kospi_price / base_kospi_price) - 1
        
        # 초과수익률
        excess_ret = stock_cum_ret - kospi_cum_ret
        
        col_name = f"D{d:+d}" if d != 0 else "D0"
        result[col_name] = excess_ret
    
    return result


def main(input_file: str = r'C:\Users\intern9\kospi 200\msci_kr_history_full.csv', 
         output_file: str = 'msci_daily_excess_return.csv',
         window: int = 30):
    """
    메인 실행 함수
    """
    
    print("=" * 70)
    print("MSCI 편입/편출 이벤트 일별 누적 초과수익률 분석")
    print("=" * 70)
    
    # 1. 데이터 로드
    df = pd.read_csv(input_file, encoding='utf-8-sig')
    print(f"📊 총 {len(df)}개 이벤트 로드")
    
    # 유효한 티커만 필터링
    df['Ticker_fmt'] = df['Ticker'].apply(format_ticker)
    df_valid = df[df['Ticker_fmt'].notna()].copy()
    print(f"📊 유효한 이벤트: {len(df_valid)}개")
    
    # 2. 전체 기간 설정
    min_date = pd.to_datetime(df_valid['Announcement_Date']).min() - timedelta(days=60)
    max_date = pd.to_datetime(df_valid['Effective_Date']).max() + timedelta(days=60)
    
    today = datetime.now()
    if max_date > today:
        max_date = today
    
    print(f"📅 분석 기간: {min_date.strftime('%Y-%m-%d')} ~ {max_date.strftime('%Y-%m-%d')}")
    
    # 3. 거래일 목록 조회
    print("거래일 목록 조회 중...")
    trading_days = get_trading_days(
        min_date.strftime('%Y%m%d'), 
        max_date.strftime('%Y%m%d')
    )
    trading_days = sorted(trading_days)
    print(f"✅ 총 {len(trading_days)}개 거래일")
    
    # 4. KOSPI200 전체 기간 가격 데이터 미리 로드
    print("KOSPI200 가격 데이터 로드 중...")
    kospi_prices = get_kospi200_price_series(
        min_date.strftime('%Y%m%d'),
        max_date.strftime('%Y%m%d')
    )
    print(f"✅ KOSPI200 데이터: {len(kospi_prices)}일")
    
    # 5. 각 이벤트별 계산
    print(f"\n일별 누적 초과수익률 계산 중 (윈도우: D-{window} ~ D+{window})...")
    
    results = []
    
    for idx, (i, row) in enumerate(df_valid.iterrows()):
        ticker = row['Ticker_fmt']
        name = row['Name']
        event_type = row['Type']
        ann_date = pd.to_datetime(row['Announcement_Date'])
        eff_date = pd.to_datetime(row['Effective_Date'])
        
        # 종목 가격 데이터 로드
        stock_start = (ann_date - timedelta(days=60)).strftime('%Y%m%d')
        stock_end = (eff_date + timedelta(days=60)).strftime('%Y%m%d')
        if pd.to_datetime(stock_end) > today:
            stock_end = today.strftime('%Y%m%d')
        
        stock_prices = get_price_series(ticker, stock_start, stock_end)
        
        if len(stock_prices) == 0:
            print(f"  ⚠️ {ticker} ({name}) 데이터 없음, 스킵")
            continue
        
        # 발표일 기준 일별 초과수익률
        ann_returns = calculate_daily_cumulative_returns(
            ticker, ann_date, trading_days, stock_prices, kospi_prices, window
        )
        
        ann_row = {
            'Announcement_Date': row['Announcement_Date'],
            'Effective_Date': row['Effective_Date'],
            'Ticker': ticker,
            'Name': name,
            'Type': event_type,
            'Base': 'Announcement',  # 기준일 구분
            **ann_returns
        }
        results.append(ann_row)
        
        # 리밸런싱일 기준 일별 초과수익률
        eff_returns = calculate_daily_cumulative_returns(
            ticker, eff_date, trading_days, stock_prices, kospi_prices, window
        )
        
        eff_row = {
            'Announcement_Date': row['Announcement_Date'],
            'Effective_Date': row['Effective_Date'],
            'Ticker': ticker,
            'Name': name,
            'Type': event_type,
            'Base': 'Effective',  # 기준일 구분
            **eff_returns
        }
        results.append(eff_row)
        
        if (idx + 1) % 10 == 0:
            print(f"  {idx + 1}/{len(df_valid)} 완료...")
        
        time.sleep(0.05)
    
    # 6. 결과 DataFrame 생성
    result_df = pd.DataFrame(results)
    
    # 컬럼 순서 정리
    meta_cols = ['Announcement_Date', 'Effective_Date', 'Ticker', 'Name', 'Type', 'Base']
    day_cols = [f"D{d:+d}" if d != 0 else "D0" for d in range(-window, window + 1)]
    result_df = result_df[meta_cols + day_cols]
    
    # 7. 저장
    result_df.to_csv(output_file, index=False, encoding='utf-8-sig')
    
    # 8. 결과 요약
    print("\n" + "=" * 70)
    print("📈 분석 결과 요약")
    print("=" * 70)
    
    print(f"\n총 {len(result_df)}개 행 (이벤트당 2행: Announcement/Effective)")
    
    # Add 이벤트 평균
    add_ann = result_df[(result_df['Type'] == 'Add') & (result_df['Base'] == 'Announcement')]
    add_eff = result_df[(result_df['Type'] == 'Add') & (result_df['Base'] == 'Effective')]
    
    if len(add_ann) > 0:
        print(f"\n🔺 편입(Add) 이벤트: {len(add_ann)}개")
        print(f"   발표일 기준 D+30 평균 초과수익률: {add_ann['D+30'].mean()*100:+.2f}%")
        print(f"   리밸런싱일 기준 D+30 평균 초과수익률: {add_eff['D+30'].mean()*100:+.2f}%")
    
    # Delete 이벤트 평균
    del_ann = result_df[(result_df['Type'] == 'Delete') & (result_df['Base'] == 'Announcement')]
    del_eff = result_df[(result_df['Type'] == 'Delete') & (result_df['Base'] == 'Effective')]
    
    if len(del_ann) > 0:
        print(f"\n🔻 편출(Delete) 이벤트: {len(del_ann)}개")
        print(f"   발표일 기준 D+30 평균 초과수익률: {del_ann['D+30'].mean()*100:+.2f}%")
        print(f"   리밸런싱일 기준 D+30 평균 초과수익률: {del_eff['D+30'].mean()*100:+.2f}%")
    
    print(f"\n✅ 결과 저장 완료: {output_file}")
    print("=" * 70)
    
    return result_df


if __name__ == "__main__":
    result = main(
        input_file='msci_kr_history_full.csv',
        output_file='msci_daily_excess_return.csv',
        window=30
    )