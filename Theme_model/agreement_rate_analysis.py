# -*- coding: utf-8 -*-
"""
agreement_rate_analysis.py

테마 일치율(Agreement Rate) 범위별 수익률 백테스팅

흐름
----
1. Theme.py의 backtest_ga() 실행 -> portfolio_log 취득
2. 각 리밸런싱 시점마다:
   a. 선택된 테마의 구성종목 목록을 DB에서 조회
   b. pykrx로 종목 종가 크롤링 (전체 기간 1회 크롤링 후 캐시)
   c. 각 종목의 J일 lookback 수익률 양/음 판단 -> 테마별 일치율 계산
   d. 해당 테마의 K일 보유 수익률 기록
3. 일치율 범위별 집계 (평균, 중앙값, 승률 등)
"""

import sys
import os
import time
import warnings
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

warnings.filterwarnings('ignore')

sys.path.append(os.path.join(r'C:\Users\intern9', 'truston_quant_dev'))

from pykrx import stock as krx
from util.database2 import MSSQL, DBConfig

from Theme_model.BETA import (
    load_kospi_returns,
    get_theme_returns,
    compute_market_adjusted_returns,
)
from Theme_model.theme_filter import (
    get_theme_info,
    get_theme_stocks,
)
from Theme_model.theme_filter2 import (
    build_constituent_sets,
    remove_overlapping_themes,
)
from Theme_model.settings import (
    KOSPI_EXCEL_PATH,
    START_DATE, END_DATE,
    ROLLING_WINDOW,
    J, K, TOP_N, REBAL_COST_GA, GAP,
    USE_FILTER,
    OVERLAP_THRESHOLD,
    GA_LOOKBACK,
    PRE_FILTER_ENABLED, PRE_FILTER_N, PRE_FILTER_K,
    CRAWL_DELAY,
    get_filter_config,
)
from Theme_model.Theme import backtest_ga
from Theme_model.theme_stock_return_analysis import (
    map_id_to_name_df,
    filter_stocks_df,
    crawl_close_prices,
    _trading_date_buffer_start,
    _nearest_date,
)


# ============================================================
# 종목 수익률 계산 (lookback + holding)
# ============================================================
def compute_returns_for_date(price_df: pd.DataFrame,
                             rebal_date: str,
                             holding_date: str,
                             j: int = 1) -> pd.DataFrame:
    """
    각 종목의 lookback_rtn(J일)과 holding_rtn(K일) 계산.
    """
    dates = sorted(price_df.index.tolist())
    if not dates:
        return pd.DataFrame()

    rebal_d = _nearest_date(dates, rebal_date, direction='prev')
    holding_d = _nearest_date(dates, holding_date, direction='prev')

    try:
        rebal_idx = dates.index(rebal_d)
    except ValueError:
        return pd.DataFrame()

    lb_start_idx = max(0, rebal_idx - j)
    lb_start_d = dates[lb_start_idx]

    p_lb_start = price_df.loc[lb_start_d]
    p_rebal = price_df.loc[rebal_d]
    p_holding = price_df.loc[holding_d]

    rows = []
    for code in price_df.columns:
        pls = p_lb_start.get(code, np.nan)
        prb = p_rebal.get(code, np.nan)
        phd = p_holding.get(code, np.nan)

        lookback_rtn = (prb / pls - 1) if (pd.notna(pls) and pd.notna(prb) and pls > 0) else np.nan
        holding_rtn = (phd / prb - 1) if (pd.notna(prb) and pd.notna(phd) and prb > 0) else np.nan

        rows.append({
            'STK_CODE': code,
            'lookback_rtn': lookback_rtn,
            'holding_rtn': holding_rtn,
        })

    return pd.DataFrame(rows)


# ============================================================
# 테마별 일치율 계산
# ============================================================
def compute_agreement_rate(stock_returns: pd.DataFrame,
                           stocks_meta: pd.DataFrame) -> pd.DataFrame:
    """
    테마별 일치율 = (lookback 수익률 > 0인 종목 수) / (전체 종목 수)
    + 해당 테마 구성종목의 평균 holding 수익률
    """
    merged = stocks_meta[['THEME_NAME', 'THEME_ID', 'STK_CODE']].merge(
        stock_returns[['STK_CODE', 'lookback_rtn', 'holding_rtn']],
        on='STK_CODE', how='left'
    )

    rows = []
    for (theme_name, theme_id), grp in merged.groupby(['THEME_NAME', 'THEME_ID']):
        valid_lb = grp.dropna(subset=['lookback_rtn'])
        valid_hld = grp.dropna(subset=['holding_rtn'])

        if valid_lb.empty:
            continue

        n_total = len(valid_lb)
        n_up = (valid_lb['lookback_rtn'] > 0).sum()
        agreement = n_up / n_total
        holding_mean = valid_hld['holding_rtn'].mean() if not valid_hld.empty else np.nan

        rows.append({
            'THEME_NAME': theme_name,
            'THEME_ID': theme_id,
            'n_stocks': n_total,
            'n_up': int(n_up),
            'agreement_rate': agreement,
            'holding_rtn_mean': holding_mean,
        })

    return pd.DataFrame(rows)


# ============================================================
# 메인 분석 함수
# ============================================================
def run_agreement_rate_backtest():
    """
    1) backtest_ga() 실행 -> portfolio_log
    2) 전 종목 1회 크롤링
    3) 롤링별 일치율 + holding 수익률 산출
    4) 일치율 범위별 집계
    """
    print("=" * 65)
    print("  테마 일치율(Agreement Rate) 범위별 수익률 백테스팅")
    print(f"  J={J} / K={K} / TOP_N={TOP_N}")
    print(f"  기간: {START_DATE} ~ {END_DATE}")
    print("=" * 65)

    # ── Step 1: 데이터 로드 + 백테스트 ──
    print("\n[Step 1] 데이터 로드")
    kospi_df = load_kospi_returns(KOSPI_EXCEL_PATH, START_DATE, END_DATE)

    db = MSSQL(database=DBConfig.TRSTDEV_DB)
    try:
        theme_df = get_theme_returns(db, START_DATE, END_DATE)
        theme_info_df = get_theme_info(db)
        theme_stocks_df = get_theme_stocks(db)
    finally:
        db.close()

    print("\n[Step 2] 시장조정 수익률 계산")
    result_df = compute_market_adjusted_returns(theme_df, kospi_df, START_DATE, ROLLING_WINDOW)

    adj_pivot = result_df.pivot_table(
        index='PfmDate', columns='THEME_ID', values='AdjRtn', aggfunc='first'
    ).sort_index()

    theme_pivot = result_df.pivot_table(
        index='PfmDate', columns='THEME_ID', values='ThemeRtn', aggfunc='first'
    ).sort_index()

    kospi_pivot = (result_df.drop_duplicates('PfmDate')
                   .set_index('PfmDate')['KOSPI_Rtn'].sort_index())

    filter_config = get_filter_config()

    print("\n[Step 3] backtest_ga() 실행")
    bt_df, filter_log, portfolio_log = backtest_ga(
        adj_pivot, theme_pivot, kospi_pivot,
        J=J, K=K, top_n=TOP_N, rebal_cost=REBAL_COST_GA, gap=GAP,
        use_filter=USE_FILTER,
        theme_info_df=theme_info_df,
        theme_stocks_df=theme_stocks_df,
        filter_config=filter_config,
        overlap_threshold=OVERLAP_THRESHOLD,
        ga_lookback=GA_LOOKBACK,
        pre_filter_enabled=PRE_FILTER_ENABLED,
        pre_filter_n=PRE_FILTER_N,
        pre_filter_k=PRE_FILTER_K,
    )

    if not portfolio_log:
        print("  portfolio_log 비어있음. 종료.")
        return

    n_rollings = len(portfolio_log)
    print(f"  리밸런싱 횟수: {n_rollings}")

    # ── Step 4: 전체 종목 코드 수집 + 1회 크롤링 ──
    print(f"\n[Step 4] 구성종목 수집 및 pykrx 1회 크롤링")
    all_codes = set()
    min_rebal = None
    max_holding = None
    rolling_meta = []  # [(rebal_date, holding_date, stocks_meta_df, weight_dict)]

    for entry in portfolio_log:
        rebal_date = entry['date']
        holding_date = entry['holding_end']
        theme_ids = list(entry['weights'].keys())
        weight_dict = entry['weights']

        id_to_name = map_id_to_name_df(theme_info_df, theme_ids, rebal_date,
                                        _verbose=False)
        if not id_to_name:
            rolling_meta.append((rebal_date, holding_date, pd.DataFrame(), weight_dict))
            continue

        theme_names = list(id_to_name.values())
        stocks_meta = filter_stocks_df(theme_stocks_df, theme_names, rebal_date,
                                       _verbose=False)
        if stocks_meta.empty:
            rolling_meta.append((rebal_date, holding_date, pd.DataFrame(), weight_dict))
            continue

        # THEME_ID 추가
        name_to_id = {v: k for k, v in id_to_name.items()}
        stocks_meta = stocks_meta.copy()
        stocks_meta['THEME_ID'] = stocks_meta['THEME_NAME'].map(name_to_id)

        codes = stocks_meta['STK_CODE'].unique().tolist()
        all_codes.update(codes)

        if min_rebal is None or rebal_date < min_rebal:
            min_rebal = rebal_date
        if max_holding is None or holding_date > max_holding:
            max_holding = holding_date

        rolling_meta.append((rebal_date, holding_date, stocks_meta, weight_dict))

    if not all_codes or min_rebal is None:
        print("  분석할 종목 없음. 종료.")
        return

    print(f"  전체 고유 종목: {len(all_codes)}개")
    print(f"  기간: {min_rebal} ~ {max_holding}")

    crawl_start = _trading_date_buffer_start(min_rebal, n_days=(J + 5) * 3)
    price_df = crawl_close_prices(sorted(all_codes), crawl_start, max_holding,
                                  delay=CRAWL_DELAY)

    if price_df.empty:
        print("  가격 데이터 없음. 종료.")
        return

    # ── Step 5: 롤링별 일치율 + holding 수익률 산출 ──
    print(f"\n[Step 5] 롤링별 일치율 계산 ({n_rollings}개)")
    all_records = []

    for i, (rebal_date, holding_date, stocks_meta, weight_dict) in enumerate(rolling_meta):
        if stocks_meta.empty:
            continue

        avail_codes = [c for c in stocks_meta['STK_CODE'].unique() if c in price_df.columns]
        if not avail_codes:
            continue

        price_sub = price_df[avail_codes]
        stock_ret = compute_returns_for_date(price_sub, rebal_date, holding_date,
                                              j=J)
        if stock_ret.empty:
            continue

        agreement_df = compute_agreement_rate(stock_ret, stocks_meta)
        if agreement_df.empty:
            continue

        # 리밸런싱 날짜 추가
        agreement_df['rebal_date'] = rebal_date
        agreement_df['holding_date'] = holding_date

        # GA 비중 추가
        agreement_df['ga_weight'] = agreement_df['THEME_ID'].map(weight_dict).fillna(0)

        all_records.append(agreement_df)

        if (i + 1) % 50 == 0 or (i + 1) == n_rollings:
            print(f"  진행: {i + 1}/{n_rollings}")

    if not all_records:
        print("  일치율 계산 결과 없음. 종료.")
        return

    full_df = pd.concat(all_records, ignore_index=True)
    print(f"\n  전체 레코드: {len(full_df)}개 (테마-리밸런싱 조합)")

    # ── Step 6: 일치율 범위별 집계 ──
    print(f"\n[Step 6] 일치율 범위별 집계")

    bins = [0.0, 0.3, 0.5, 0.7, 1.001]
    labels = ['0~30%', '30~50%', '50~70%', '70~100%']
    full_df['agreement_bin'] = pd.cut(
        full_df['agreement_rate'], bins=bins, labels=labels, right=False
    )

    # holding_rtn_mean이 NaN인 행 제외
    valid_df = full_df.dropna(subset=['holding_rtn_mean'])

    summary = (
        valid_df.groupby('agreement_bin', observed=True)
        .agg(
            표본수=('holding_rtn_mean', 'count'),
            평균_일치율=('agreement_rate', 'mean'),
            평균_수익률=('holding_rtn_mean', 'mean'),
            중앙값_수익률=('holding_rtn_mean', 'median'),
            표준편차=('holding_rtn_mean', 'std'),
            승률=('holding_rtn_mean', lambda x: (x > 0).mean()),
            평균_종목수=('n_stocks', 'mean'),
        )
        .reset_index()
    )

    # GA 비중 가중 수익률도 계산
    weighted_summary_rows = []
    for label in labels:
        sub = valid_df[valid_df['agreement_bin'] == label]
        if sub.empty:
            weighted_summary_rows.append({
                'agreement_bin': label,
                'GA가중_평균수익률': np.nan,
            })
            continue
        # 각 리밸런싱 내에서 GA 비중으로 가중평균
        total_w = sub['ga_weight'].sum()
        if total_w > 0:
            w_rtn = (sub['holding_rtn_mean'] * sub['ga_weight']).sum() / total_w
        else:
            w_rtn = sub['holding_rtn_mean'].mean()
        weighted_summary_rows.append({
            'agreement_bin': label,
            'GA가중_평균수익률': w_rtn,
        })

    weighted_df = pd.DataFrame(weighted_summary_rows)
    summary = summary.merge(weighted_df, on='agreement_bin', how='left')

    # ── 출력 ──
    print(f"\n{'=' * 75}")
    print(f"  일치율(Agreement Rate) 범위별 수익률 분석 결과")
    print(f"  J={J}일 lookback / K={K}일 holding / 리밸런싱 {n_rollings}회")
    print(f"{'=' * 75}")

    print(f"\n  {'일치율 범위':>12}  {'표본수':>6}  {'평균일치율':>9}  "
          f"{'평균수익률':>9}  {'중앙값':>8}  {'표준편차':>8}  "
          f"{'승률':>6}  {'평균종목수':>8}  {'GA가중수익률':>11}")
    print(f"  {'-' * 12}  {'-' * 6}  {'-' * 9}  "
          f"{'-' * 9}  {'-' * 8}  {'-' * 8}  "
          f"{'-' * 6}  {'-' * 8}  {'-' * 11}")

    for _, row in summary.iterrows():
        bin_label = row['agreement_bin']
        n = int(row['표본수'])
        avg_agr = row['평균_일치율'] * 100
        avg_rtn = row['평균_수익률'] * 100
        med_rtn = row['중앙값_수익률'] * 100
        std_rtn = row['표준편차'] * 100 if pd.notna(row['표준편차']) else 0
        wr = row['승률'] * 100
        avg_stk = row['평균_종목수']
        ga_rtn = row['GA가중_평균수익률'] * 100 if pd.notna(row['GA가중_평균수익률']) else float('nan')

        print(f"  {str(bin_label):>12}  {n:>6}  {avg_agr:>8.1f}%  "
              f"{avg_rtn:>+8.3f}%  {med_rtn:>+7.3f}%  {std_rtn:>7.3f}%  "
              f"{wr:>5.1f}%  {avg_stk:>7.1f}  {ga_rtn:>+10.3f}%")

    # 전체 요약
    total_n = len(valid_df)
    total_avg = valid_df['holding_rtn_mean'].mean() * 100
    total_med = valid_df['holding_rtn_mean'].median() * 100
    total_wr = (valid_df['holding_rtn_mean'] > 0).mean() * 100

    print(f"  {'=' * 100}")
    print(f"  {'전체':>12}  {total_n:>6}  {'':>9}  "
          f"{total_avg:>+8.3f}%  {total_med:>+7.3f}%  {'':>8}  "
          f"{total_wr:>5.1f}%")

    # ── CSV 저장 ──
    out_dir = os.path.dirname(os.path.abspath(__file__))
    csv_path = os.path.join(out_dir, 'agreement_rate_backtest_result.csv')

    # summary를 퍼센트 표시로 변환하여 저장
    save_df = summary.copy()
    save_df['평균_일치율'] = save_df['평균_일치율'] * 100
    save_df['평균_수익률'] = save_df['평균_수익률'] * 100
    save_df['중앙값_수익률'] = save_df['중앙값_수익률'] * 100
    save_df['표준편차'] = save_df['표준편차'] * 100
    save_df['승률'] = save_df['승률'] * 100
    save_df['GA가중_평균수익률'] = save_df['GA가중_평균수익률'] * 100

    save_df.to_csv(csv_path, index=False, encoding='utf-8-sig')
    print(f"\n  CSV 저장: {csv_path}")

    # 상세 데이터도 저장
    detail_path = os.path.join(out_dir, 'agreement_rate_detail.csv')
    detail_save = full_df.copy()
    detail_save['agreement_rate_pct'] = detail_save['agreement_rate'] * 100
    detail_save['holding_rtn_pct'] = detail_save['holding_rtn_mean'] * 100
    detail_save.to_csv(detail_path, index=False, encoding='utf-8-sig')
    print(f"  상세 CSV 저장: {detail_path}")

    return summary, full_df


# ============================================================
# main
# ============================================================
def main():
    run_agreement_rate_backtest()


if __name__ == '__main__':
    main()

# python Theme_model/agreement_rate_analysis.py
