#파라미터 설정(541번째 줄 부터 수정 가능 합니다.)


import pandas as pd
import numpy as np
from pykrx import stock
from datetime import datetime, timedelta
import time
import matplotlib.pyplot as plt
import os

plt.rcParams['font.family'] = 'Malgun Gothic'  # Windows
plt.rcParams['axes.unicode_minus'] = False


INDEX_TICKER_MAP = {
    'KOSPI200': '1028',
    'KOSPI': '1001',
    'KOSDAQ': '2001',
    'KOSDAQ150': '1028',  # 필요시 수정
    'KRX100': '1034',
    'KRX300': '1035',
}

def load_and_preprocess_events(filepath, change_type='all'):
    df_change = pd.read_excel(filepath)
    df_change = df_change.iloc[1:].reset_index(drop=True)
    df_change.columns = ['순번', '반영일', '편입_ISIN', '편입_종목명', '편출_ISIN', '편출_종목명']   
    df_change['반영일'] = pd.to_datetime(df_change['반영일'])
    
    df_in = df_change[['반영일', '편입_ISIN', '편입_종목명']].dropna(subset=['편입_ISIN']).copy()
    df_in.columns = ['반영일', 'ISIN', '종목명']
    df_in['구분'] = '편입'
    
    df_out = df_change[['반영일', '편출_ISIN', '편출_종목명']].dropna(subset=['편출_ISIN']).copy()
    df_out.columns = ['반영일', 'ISIN', '종목명']
    df_out['구분'] = '편출'
    
    df_events = pd.concat([df_in, df_out], ignore_index=True)
    df_events['종목코드'] = df_events['ISIN'].str[3:9]
    
    exclude_date = pd.Timestamp('2025-12-12')
    df_events = df_events[df_events['반영일'] != exclude_date].reset_index(drop=True)
    
    df_events['월'] = df_events['반영일'].dt.month
    
    if change_type == 'regular':
        df_events = df_events[df_events['월'].isin([6, 12])].reset_index(drop=True)
        print(f"  → 정기 변경(6월, 12월)만 필터링")
    elif change_type == 'special':
        df_events = df_events[~df_events['월'].isin([6, 12])].reset_index(drop=True)
        print(f"  → 특수 변경(6월, 12월 제외)만 필터링")
    else:
        print(f"  → 전체 데이터 사용")
    
    df_events = df_events.drop(columns=['월'])
    
    return df_events

def fetch_adjusted_price_with_range(ticker, event_date, before_days=10, after_days=10):
    """pykrx를 사용하여 반영일 기준 전후 N영업일 수정 주가 조회"""
    try:
        start_date = event_date - timedelta(days=before_days * 2 + 10)
        end_date = event_date + timedelta(days=after_days * 2 + 10)
        
        start_str = start_date.strftime("%Y%m%d")
        end_str = end_date.strftime("%Y%m%d")
        df = stock.get_market_ohlcv_by_date(start_str, end_str, ticker, adjusted=True)
        
        if df.empty:
            return None
        
        df = df.reset_index()
        df = df.rename(columns={'날짜': '날짜', '종가': '수정주가'})
        business_days = df['날짜'].tolist()
        
        event_date_ts = pd.Timestamp(event_date)
        
        if event_date_ts in business_days:
            event_idx = business_days.index(event_date_ts)
        else:
            past_days = [d for d in business_days if d <= event_date_ts]
            if past_days:
                event_idx = business_days.index(past_days[-1])
            else:
                return None
        
        start_idx = max(0, event_idx - before_days)
        end_idx = min(len(business_days) - 1, event_idx + after_days)
        
        df_filtered = df.iloc[start_idx:end_idx + 1].copy()       
        df_filtered['상대영업일'] = range(start_idx - event_idx, end_idx - event_idx + 1)
        
        return df_filtered[['날짜', '수정주가', '상대영업일']]
    
    except Exception as e:
        print(f"  Error fetching {ticker}: {e}")
        return None

def fetch_index_with_range(event_date, index_ticker, before_days=10, after_days=10):
    """pykrx를 사용하여 반영일 기준 전후 N영업일 지수 조회"""
    try:
        start_date = event_date - timedelta(days=before_days * 2 + 10)
        end_date = event_date + timedelta(days=after_days * 2 + 10)
        
        start_str = start_date.strftime("%Y%m%d")
        end_str = end_date.strftime("%Y%m%d")
        
        df = stock.get_index_ohlcv_by_date(start_str, end_str, index_ticker)
        
        if df.empty:
            return None
        
        df = df.reset_index()
        df = df.rename(columns={'날짜': '날짜', '종가': '지수종가'})        
        business_days = df['날짜'].tolist()       
        event_date_ts = pd.Timestamp(event_date)
        
        if event_date_ts in business_days:
            event_idx = business_days.index(event_date_ts)
        else:
            past_days = [d for d in business_days if d <= event_date_ts]
            if past_days:
                event_idx = business_days.index(past_days[-1])
            else:
                return None
        
        start_idx = max(0, event_idx - before_days)
        end_idx = min(len(business_days) - 1, event_idx + after_days)
        
        df_filtered = df.iloc[start_idx:end_idx + 1].copy()
        df_filtered['상대영업일'] = range(start_idx - event_idx, end_idx - event_idx + 1)
        
        return df_filtered[['날짜', '지수종가', '상대영업일']]
    
    except Exception as e:
        print(f"  Error fetching index {index_ticker}: {e}")
        return None


def fetch_all_events_prices(df_events, index_ticker, before_days=10, after_days=10, sleep_time=0.1):
    """모든 편입/편출 이벤트에 대해 수정 주가 및 지수 조회"""
    print(f"\n=== 수정 주가 조회 시작 ===")
    print(f"총 이벤트 수: {len(df_events)}")
    print(f"지수 티커: {index_ticker}")
    print(f"조회 범위: D-{before_days} ~ D+{after_days}")
    
    result_list = []
    success_count = 0
    fail_count = 0    
    index_cache = {}
    
    for i, event in df_events.iterrows():
        event_date = event['반영일']
        ticker = event['종목코드']

        price_data = fetch_adjusted_price_with_range(
            ticker, event_date, 
            before_days=before_days, 
            after_days=after_days
        )
        
        if price_data is None or len(price_data) == 0:
            print(f"  [{i+1}/{len(df_events)}] {ticker} {event['종목명']} - 데이터 없음")
            fail_count += 1
            continue

        event_date_ts = pd.Timestamp(event_date)
        if event_date_ts not in index_cache:
            index_data = fetch_index_with_range(
                event_date, index_ticker,
                before_days=before_days, after_days=after_days
            )
            if index_data is not None:
                index_cache[event_date_ts] = index_data

        if event_date_ts in index_cache:
            index_data = index_cache[event_date_ts]
            price_data = price_data.merge(
                index_data[['상대영업일', '지수종가']], 
                on='상대영업일', 
                how='left'
            )
        else:
            price_data['지수종가'] = np.nan
        
        price_data = price_data.copy()
        price_data['반영일'] = event_date
        price_data['종목코드'] = ticker
        price_data['종목명'] = event['종목명']
        price_data['구분'] = event['구분']
        price_data['ISIN'] = event['ISIN']
        
        result_list.append(price_data)
        success_count += 1
        
        if (i + 1) % 20 == 0:
            print(f"  진행 중: {i + 1}/{len(df_events)} (성공: {success_count}, 실패: {fail_count})")
        
        time.sleep(sleep_time)
    
    print(f"\n조회 완료: 성공 {success_count}개, 실패 {fail_count}개")
    
    if not result_list:
        return pd.DataFrame()
    
    df_result = pd.concat(result_list, ignore_index=True)
    df_result = df_result[['구분', 'ISIN', '종목코드', '종목명', '반영일', '날짜', '상대영업일', '수정주가', '지수종가']]
    df_result = df_result.sort_values(['구분', '종목코드', '반영일', '날짜']).reset_index(drop=True)
    
    return df_result

def convert_to_wide_and_calculate_returns(df_long, before_days=10):
    """Wide format 변환 후 누적수익률 및 초과수익률 계산"""
    df_price = df_long.pivot_table(
        index=['구분', 'ISIN', '종목코드', '종목명', '반영일'],
        columns='상대영업일',
        values='수정주가'
    ).reset_index()
    
    df_index = df_long.pivot_table(
        index=['구분', 'ISIN', '종목코드', '종목명', '반영일'],
        columns='상대영업일',
        values='지수종가'
    ).reset_index()
    
    day_cols = [col for col in df_price.columns if isinstance(col, (int, float, np.integer)) and not pd.isna(col)]
    day_cols = sorted(day_cols)
    
    info_cols = ['구분', 'ISIN', '종목코드', '종목명', '반영일']
    
    df_result = df_price[info_cols].copy()
    
    for day in day_cols:
        col_name = f"D{int(day):+d}"
        df_result[col_name] = df_price[day].values   
    base_col = -before_days if -before_days in day_cols else day_cols[0]
    
    for day in day_cols:
        col_name = f"D{int(day):+d}_cumret(%)"
        base_price = df_price[base_col].values
        current_price = df_price[day].values
        df_result[col_name] = (current_price / base_price - 1) * 100

    for day in day_cols:
        col_name = f"D{int(day):+d}_idx_cumret(%)"
        base_idx = df_index[base_col].values
        current_idx = df_index[day].values
        df_result[col_name] = (current_idx / base_idx - 1) * 100
    

    for day in day_cols:
        stock_col = f"D{int(day):+d}_cumret(%)"
        idx_col = f"D{int(day):+d}_idx_cumret(%)"
        excess_col = f"D{int(day):+d}_excess(%)"
        df_result[excess_col] = df_result[stock_col] - df_result[idx_col]
    
    return df_result

def plot_excess_return_graphs(df, output_dir="", index_name="KOSPI200", change_type="all"):
    """편입/편출 초과수익률 평균/중앙값 그래프 생성"""
    
    excess_cols = [col for col in df.columns if '_excess(%)' in col]
    excess_cols = sorted(excess_cols, key=lambda x: int(x.split('D')[1].split('_')[0].replace('+', '')))
    
    first_day = int(excess_cols[0].split('D')[1].split('_')[0].replace('+', ''))
    day_labels = [f"D{first_day-1:+d}"] + [col.replace('_excess(%)', '') for col in excess_cols]
    x = range(len(day_labels))
    
    def calculate_stats(df_subset):
        means = [0]
        medians = [0]
        for col in excess_cols:
            means.append(df_subset[col].mean())
            medians.append(df_subset[col].median())
        return means, medians
    
    df_in = df[df['구분'] == '편입']
    df_out = df[df['구분'] == '편출']
    
    in_means, in_medians = calculate_stats(df_in)
    out_means, out_medians = calculate_stats(df_out)
    
    in_count = len(df_in)
    out_count = len(df_out)
    
    change_label = {'all': '전체', 'regular': '정기변경', 'special': '특수변경'}[change_type]
    
    # 그래프 1: 평균
    fig2, ax = plt.subplots(figsize=(12, 6))
    ax.plot(x, in_means, 'b-o', linewidth=2.5, markersize=6, label=f'편입 평균 (n={in_count})')
    ax.plot(x, out_means, 'r-s', linewidth=2.5, markersize=6, label=f'편출 평균 (n={out_count})')
    ax.axvline(x=len(day_labels)//2, color='green', linestyle='--', linewidth=2, alpha=0.7, label='반영일 (D+0)')
    ax.axhline(y=0, color='gray', linestyle=':', linewidth=1, alpha=0.5)
    ax.set_xlabel('영업일 (반영일 대비)', fontsize=12)
    ax.set_ylabel('초과수익률 (%)', fontsize=12)
    ax.set_title(f'{index_name} 편입/편출 종목 평균 초과수익률 ({change_label})', fontsize=14, fontweight='bold')
    ax.set_xticks(x)
    ax.set_xticklabels(day_labels, rotation=45)
    ax.grid(True, alpha=0.3)
    ax.legend(loc='best', fontsize=10)
    plt.tight_layout()
    fig2.savefig(f'{output_dir}초과수익률_평균_{change_type}.png', dpi=150, bbox_inches='tight')
    plt.close()
    
    # 그래프 2: 중앙값
    fig3, ax = plt.subplots(figsize=(12, 6))
    ax.plot(x, in_medians, 'b-o', linewidth=2.5, markersize=6, label=f'편입 중앙값 (n={in_count})')
    ax.plot(x, out_medians, 'r-s', linewidth=2.5, markersize=6, label=f'편출 중앙값 (n={out_count})')
    ax.axvline(x=len(day_labels)//2, color='green', linestyle='--', linewidth=2, alpha=0.7, label='반영일 (D+0)')
    ax.axhline(y=0, color='gray', linestyle=':', linewidth=1, alpha=0.5)
    ax.set_xlabel('영업일 (반영일 대비)', fontsize=12)
    ax.set_ylabel('초과수익률 (%)', fontsize=12)
    ax.set_title(f'{index_name} 편입/편출 종목 중앙값 초과수익률 ({change_label})', fontsize=14, fontweight='bold')
    ax.set_xticks(x)
    ax.set_xticklabels(day_labels, rotation=45)
    ax.grid(True, alpha=0.3)
    ax.legend(loc='best', fontsize=10)
    plt.tight_layout()
    fig3.savefig(f'{output_dir}초과수익률_중앙값_{change_type}.png', dpi=150, bbox_inches='tight')
    plt.close()
    
    print("\n=== 그래프 저장 완료 ===")
    print(f"  1. {output_dir}초과수익률_평균_{change_type}.png")
    print(f"  2. {output_dir}초과수익률_중앙값_{change_type}.png")
    
    last_day_col = excess_cols[-1]
    last_day = last_day_col.replace('_excess(%)', '')
    
    print(f"\n=== 초과수익률 통계 ({last_day} 기준) ===")
    
    print(f"\n[편입] (n={in_count})")
    print(f"  평균: {in_means[-1]:.2f}%")
    print(f"  중앙값: {in_medians[-1]:.2f}%")
    
    print(f"\n[편출] (n={out_count})")
    print(f"  평균: {out_means[-1]:.2f}%")
    print(f"  중앙값: {out_medians[-1]:.2f}%")
    
    return in_means, in_medians, out_means, out_medians


def run_analysis_pipeline(
    input_filepath,
    before_days=10,
    after_days=10,
    index_ticker='1028',
    index_name='KOSPI200',
    change_type='all',
    output_dir=''
):

    
    print("=" * 70)
    print("KOSPI 200 편입/편출 종목 초과수익률 분석 파이프라인")
    print("=" * 70)
    
    print(f"\n[파라미터 설정]")
    print(f"  - 조회 범위: D-{before_days} ~ D+{after_days}")
    print(f"  - 대상 지수: {index_name} (티커: {index_ticker})")
    print(f"  - 변경 유형: {change_type}")
    print(f"  - 입력 파일: {input_filepath}")
    print(f"  - 출력 디렉토리: {output_dir if output_dir else '현재 디렉토리'}")
    
    # 출력 파일명 설정
    suffix = f"D{before_days}_{change_type}"
    output_long = f"{output_dir}편입편출_수정주가_지수포함_{suffix}.csv"
    output_wide = f"{output_dir}편입편출_초과수익률_{suffix}.csv"
    
    # 1. 데이터 로드
    print("\n" + "=" * 70)
    print("[1단계] 편입/편출 데이터 로드")
    print("=" * 70)
    df_events = load_and_preprocess_events(input_filepath, change_type=change_type)
    print(f"  총 이벤트 수: {len(df_events)}")
    print(f"  고유 종목 수: {df_events['종목코드'].nunique()}")
    print(f"  고유 반영일 수: {df_events['반영일'].nunique()}")
    
    if len(df_events) == 0:
        print("\n[경고] 필터링 조건에 맞는 데이터가 없습니다.")
        return None
    
    # 2. 수정 주가 및 지수 조회
    print("\n" + "=" * 70)
    print("[2단계] 수정 주가 및 지수 데이터 조회")
    print("=" * 70)
    df_long = fetch_all_events_prices(
        df_events, 
        index_ticker=index_ticker,
        before_days=before_days, 
        after_days=after_days
    )
    
    if len(df_long) == 0:
        print("\n[경고] 조회된 데이터가 없습니다.")
        return None
    
    # Long format 저장
    df_long.to_csv(output_long, index=False, encoding='utf-8-sig')
    print(f"\n  Long format 저장: {output_long}")
    
    # 3. 누적수익률 및 초과수익률 계산
    print("\n" + "=" * 70)
    print("[3단계] 누적수익률 및 초과수익률 계산")
    print("=" * 70)
    df_result = convert_to_wide_and_calculate_returns(df_long, before_days=before_days)
    
    # Wide format 저장
    df_result.to_csv(output_wide, index=False, encoding='utf-8-sig')
    print(f"  결과 저장: {output_wide}")
    
    # 4. 그래프 생성
    print("\n" + "=" * 70)
    print("[4단계] 그래프 생성")
    print("=" * 70)
    plot_excess_return_graphs(
        df_result, 
        output_dir=output_dir, 
        index_name=index_name, 
        change_type=change_type
    )
    
    # 결과 샘플 출력
    print("\n" + "=" * 70)
    print("[결과 샘플]")
    print("=" * 70)
    
    # 마지막 날짜 컬럼 찾기
    excess_cols = [col for col in df_result.columns if '_excess(%)' in col]
    last_day = excess_cols[-1].replace('_excess(%)', '')
    
    sample_cols = ['구분', '종목명', '반영일', 
                   'D+0_cumret(%)', 'D+0_idx_cumret(%)', 'D+0_excess(%)',
                   f'{last_day}_cumret(%)', f'{last_day}_idx_cumret(%)', f'{last_day}_excess(%)']
    available_cols = [col for col in sample_cols if col in df_result.columns]
    print(df_result[available_cols].head(10))
    
    print("\n" + "=" * 70)
    print("분석 완료!")
    print("=" * 70)
    
    return df_result



if __name__ == "__main__":
    
    # 파라미터 설정

    
    # 1. 입력 파일 경로
    INPUT_FILEPATH = r"C:\Users\intern9\kospi 200\코스피 200 자동화\편입 편출 종목.xlsx"
    
    # 2. 반영일 전후 영업일 수
    BEFORE_DAYS = 30  # D-10
    AFTER_DAYS = 30   # D+10
    
    # 3. 대상 지수 설정
    #    - KOSPI200: '1028'
    #    - KOSPI: '1001'
    #    - KOSDAQ: '2001'
    #    - KRX100: '1034'
    #    - KRX300: '1035'
    INDEX_TICKER = '1028'
    INDEX_NAME = 'KOSPI200'
    
    # 4. 변경 유형 설정
    #    - 'all': 전체
    #    - 'regular': 정기변경 (6월, 12월)
    #    - 'special': 특수변경 (그 외 월)
    CHANGE_TYPE = 'all'

    OUTPUT_DIR = ''
    
    
    df_result = run_analysis_pipeline(
        input_filepath=INPUT_FILEPATH,
        before_days=BEFORE_DAYS,
        after_days=AFTER_DAYS,
        index_ticker=INDEX_TICKER,
        index_name=INDEX_NAME,
        change_type=CHANGE_TYPE,
        output_dir=OUTPUT_DIR
    )