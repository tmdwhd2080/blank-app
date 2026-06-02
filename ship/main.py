import pandas as pd
import numpy as np
import json
import os

def export_data():
    base_path = os.path.dirname(os.path.abspath(__file__))
    data_path = os.path.join(base_path, 'data', '해운사_최종.xlsx')
    json_path = os.path.join(base_path, 'dashboard_data.json')

    print("엑셀 데이터 변환 중...")

    stocks = pd.read_excel(data_path, sheet_name='주가 숫자')
    factors = pd.read_excel(data_path, sheet_name='Factor')
    indices = pd.read_excel(data_path, sheet_name='Index 숫자')
    fx_df = pd.read_excel(data_path, sheet_name='환율')

    # DATE 컬럼 추출 후 인덱스로 설정
    dates = pd.to_datetime(factors['DATE'])

    # DATE 컬럼 제거 (숫자 데이터만 남기기)
    if 'DATE' in stocks.columns:
        stocks = stocks.drop(columns=['DATE'])
    factors = factors.drop(columns=['DATE'])
    factors.columns = [c.strip() for c in factors.columns]
    if 'DATE' in indices.columns:
        indices = indices.drop(columns=['DATE'])
    if 'DATE' in fx_df.columns:
        fx_df = fx_df.drop(columns=['DATE'])

    stocks.index = dates
    factors.index = dates
    indices.index = dates
    fx_df.index = dates

    # ── 매핑 ──
    vessel_map = {
        'HMM': '컨테이너', 'Maersk': '컨테이너', 'Hapag-Lloyd': '컨테이너', 'COSCO Shipping': '컨테이너',
        'ZIM': '컨테이너', 'Evergreen': '컨테이너', 'Yang Ming': '컨테이너', 'Wan Hai': '컨테이너',
        '팬오션': '벌크', '대한해운': '벌크', 'Star Bulk': '벌크', 'Golden Ocean': '벌크',
        'Genco Shipping': '벌크', 'Diana Shipping': '벌크', 'Safe Bulkers': '벌크', 'KSS해운': '벌크',
        'BW LPG': 'LPG', 'Dorian LPG': 'LPG', 'Navigator Holdings': 'LPG',
        'Frontline': '탱커', 'DHT Holdings': '탱커', 'Teekay Tankers': '탱커',
        'Intl Seaways': '탱커', 'Scorpio Tankers': '탱커', 'Torm': '탱커',
        'NYK Line': '종합', 'MOL': '종합', 'K-Line': '종합',
    }
    fx_map = {
        'HMM': 'USD/KRW', '팬오션': 'USD/KRW', '대한해운': 'USD/KRW', 'KSS해운': 'USD/KRW',
        'Maersk': 'USD/EUR', 'Hapag-Lloyd': 'USD/EUR',
        'COSCO Shipping': 'USD/HKD',
        'Evergreen': 'USD/TWD', 'Yang Ming': 'USD/TWD', 'Wan Hai': 'USD/TWD',
        'NYK Line': 'USD/JPY', 'MOL': 'USD/JPY', 'K-Line': 'USD/JPY',
    }
    benchmark_map = {
        'HMM': '한국 KOSPI', '팬오션': '한국 KOSPI', '대한해운': '한국 KOSPI', 'KSS해운': '한국 KOSPI',
        'Maersk': '유럽 STOXX 600', 'Hapag-Lloyd': '유럽 STOXX 600',
        'COSCO Shipping': '홍콩 HSI', 'ZIM': '미국 S&P 500',
        'Evergreen': '대만 TAIEX', 'Yang Ming': '대만 TAIEX', 'Wan Hai': '대만 TAIEX',
        'Star Bulk': '미국 S&P 500', 'Golden Ocean': '미국 S&P 500',
        'Genco Shipping': '미국 S&P 500', 'Diana Shipping': '미국 S&P 500', 'Safe Bulkers': '미국 S&P 500',
        'BW LPG': '미국 S&P 500', 'Dorian LPG': '미국 S&P 500', 'Navigator Holdings': '미국 S&P 500',
        'Frontline': '미국 S&P 500', 'DHT Holdings': '미국 S&P 500', 'Teekay Tankers': '미국 S&P 500',
        'Intl Seaways': '미국 S&P 500', 'Scorpio Tankers': '미국 S&P 500', 'Torm': '미국 S&P 500',
        'NYK Line': '일본 TOPIX', 'MOL': '일본 TOPIX', 'K-Line': '일본 TOPIX',
    }
    own_factor = {'컨테이너': 'CCFI', '벌크': 'BDI', '탱커': 'BDTI', 'LPG': 'Brent', '종합': 'BDI'}

    H = 4
    date_strs = [d.strftime('%Y-%m-%d') for d in dates]

    # ── 그룹 ──
    groups = {}
    for c in stocks.columns:
        vt = vessel_map.get(c, '기타')
        if vt not in groups: groups[vt] = []
        groups[vt].append(c)
    ordered_groups = [{'name': g, 'companies': groups[g]}
                      for g in ['컨테이너','벌크','탱커','LPG','종합'] if g in groups]

    def to_list(s):
        return [None if pd.isna(v) or np.isinf(v) else round(float(v), 4) for v in s]

    def get_usd(col):
        fx_key = fx_map.get(col)
        if fx_key and fx_key in fx_df.columns:
            return (stocks[col] / fx_df[fx_key]).replace([np.inf, -np.inf], np.nan)
        return stocks[col].replace([np.inf, -np.inf], np.nan).copy()

    # ── 시리즈 생성 ──
    series, momentum = {}, {}

    for c in stocks.columns:
        usd = get_usd(c)
        fv = usd.dropna().iloc[0] if not usd.dropna().empty else 1
        series[c] = to_list(usd / fv * 100)
        momentum[c] = to_list((usd / usd.shift(H) - 1) * 100)

    for f in factors.columns:
        vals = factors[f].replace([np.inf, -np.inf], np.nan)
        fv = vals.dropna().iloc[0] if not vals.dropna().empty else 1
        series[f] = to_list(vals / fv * 100)
        momentum[f] = to_list((vals / vals.shift(H) - 1) * 100)

    # 벤치마크 인덱스
    index_series = {}
    for idx_col in indices.columns:
        vals = indices[idx_col].replace([np.inf, -np.inf], np.nan)
        fv = vals.dropna().iloc[0] if not vals.dropna().empty else 1
        index_series[idx_col] = to_list(vals / fv * 100)

    # 초과수익률 (alpha)
    alphas = {}
    for c in stocks.columns:
        bm = benchmark_map.get(c)
        if bm and bm in indices.columns:
            usd = get_usd(c)
            sr = np.log(usd / usd.shift(H))
            br = np.log(indices[bm] / indices[bm].shift(H))
            alphas[c] = to_list(sr - br)

    # ── 출력 ──
    output = {
        'dates': date_strs,
        'groups': ordered_groups,
        'factors': list(factors.columns),
        'indices': list(indices.columns),
        'vessel_map': vessel_map,
        'own_factor': own_factor,
        'benchmark_map': benchmark_map,
        'series': series,
        'momentum': momentum,
        'index_series': index_series,
        'alphas': alphas,
    }

    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False)

    print(f"✅ dashboard_data.json 생성 완료!")
    print(f"   {len(stocks.columns)}개 해운사, {len(factors.columns)}개 팩터, {len(date_strs)}주")
    print(f"   stock_graph.html 과 같은 폴더에 두고 브라우저에서 열어주세요.")

if __name__ == "__main__":
    export_data()