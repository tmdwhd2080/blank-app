# -*- coding: utf-8 -*-
"""
theme_stock_return_analysis.py

GA 최적 포트폴리오 테마의 구성종목 수익률 분석

흐름
----
1. GA 최종 산출 THEME_ID 목록 입력 (아래 THEME_IDS 리스트에 직접 기입)
2. DB(THEME_INFO)에서 THEME_ID → THEME_NAME 매핑
3. DB(THEME_STOCKS)에서 THEME_NAME → STK_NAME, STK_CODE 조회
   (UPDATED_DT 기준: REBAL_DATE 이전 가장 최근 구성)
4. pykrx로 REBAL_DATE·HOLDING_DATE 종가 크롤링
5. 수익률 계산
   - lookback_rtn : REBAL_DATE 기준 J일 누적 수익률
   - holding_rtn  : REBAL_DATE 종가 → HOLDING_DATE 종가 수익률

분석
----
  Analysis 1 : 테마별 일치율(LOOKBACK 상승 종목 비율) → 구간별 HOLDING 수익률 검증
  Analysis 2 : 테마별 LOOKBACK 수익률 Q1~Q4 분위 → HOLDING 수익률
  Analysis 3 : LOOKBACK 기준 하락 / 0~5% 상승 / 5% 초과 상승 → HOLDING 수익률 비교
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


# ═══════════════════════════════════════════════════════════════
# ▶  단독 실행 시 설정  (Theme.py 통합 실행 시에는 settings.py 사용)
# ═══════════════════════════════════════════════════════════════
# THEME_ID 비어 있으면 실행 시 수동 입력 요청
STANDALONE_THEME_IDS:   list  = []
STANDALONE_REBAL_DATE:  str   = "20260227"
STANDALONE_HOLDING_DATE: str  = "20260304"
STANDALONE_J:            int  = 1
STANDALONE_CRAWL_DELAY:  float = 0.05
# ═══════════════════════════════════════════════════════════════


# ───────────────────────────────────────────────────────────────
# 유틸
# ───────────────────────────────────────────────────────────────

def _trading_date_buffer_start(date_str: str, n_days: int = 20) -> str:
    """
    YYYYMMDD 기준으로 캘린더상 n_days 앞선 날짜 반환.
    (주말·공휴일 무시한 단순 버퍼 — pykrx가 실제 거래일만 반환하므로 충분히 넉넉히)
    """
    dt = datetime.strptime(date_str, '%Y%m%d') - timedelta(days=n_days)
    return dt.strftime('%Y%m%d')


def _nearest_date(index_dates: list, target: str, direction: str = 'prev') -> str:
    """
    정렬된 날짜 리스트에서 target 에 가장 가까운 날짜 반환.
    direction='prev' : target 이하 최근 거래일
    direction='next' : target 이상 최초 거래일
    """
    if direction == 'prev':
        cands = [d for d in index_dates if d <= target]
        return cands[-1] if cands else index_dates[0]
    else:
        cands = [d for d in index_dates if d >= target]
        return cands[0] if cands else index_dates[-1]


# ───────────────────────────────────────────────────────────────
# DB 조회
# ───────────────────────────────────────────────────────────────

def fetch_theme_name_map(db: MSSQL, theme_ids: list, as_of_date: str) -> dict:
    """
    THEME_INFO 에서 THEME_ID → THEME_NAME 매핑 반환.
    as_of_date 이전 가장 최근 UPDATED_DT 기준.

    Returns
    -------
    {theme_id(str): theme_name(str)}
    """
    ids_csv = ", ".join(f"'{tid}'" for tid in theme_ids)
    query = f"""
        SELECT THEME_ID, THEME_NAME, UPDATED_DT
        FROM THEME_INFO
        WHERE THEME_ID IN ({ids_csv})
          AND UPDATED_DT <= '{as_of_date}'
        ORDER BY THEME_ID, UPDATED_DT
    """
    df = db.SELECT(query)
    if df is None or df.empty:
        print("  ⚠️  THEME_INFO 조회 결과 없음 (THEME_ID·날짜 범위 확인)")
        return {}

    df['THEME_ID']   = df['THEME_ID'].astype(str).str.strip()
    df['THEME_NAME'] = df['THEME_NAME'].astype(str).str.strip()
    df['UPDATED_DT'] = pd.to_datetime(df['UPDATED_DT']).dt.strftime('%Y%m%d')

    # 각 THEME_ID 에서 가장 최근 UPDATED_DT 1행만 유지
    df_latest = (df.sort_values('UPDATED_DT', ascending=False)
                   .groupby('THEME_ID', as_index=False)
                   .first())

    mapping = dict(zip(df_latest['THEME_ID'], df_latest['THEME_NAME']))
    print(f"  THEME_INFO 매핑 완료: {len(mapping)}개")
    for tid, tnm in mapping.items():
        print(f"    THEME_ID {tid:>6} → {tnm}")
    return mapping


def fetch_theme_stocks(db: MSSQL, theme_names: list, as_of_date: str) -> pd.DataFrame:
    """
    THEME_STOCKS 에서 THEME_NAME 목록에 해당하는 구성종목 반환.
    as_of_date 이전 가장 최근 UPDATED_DT 기준 구성으로 필터링.

    Returns
    -------
    DataFrame : [THEME_NAME, STK_NAME, STK_CODE, UPDATED_DT]
    """
    # SQL 인젝션 방지 ─ 작은따옴표 이스케이프
    escaped = [n.replace("'", "''") for n in theme_names]
    names_csv = ", ".join(f"'{n}'" for n in escaped)

    query = f"""
        SELECT THEME_NAME, STK_NAME, STK_CODE, UPDATED_DT
        FROM THEME_STOCKS
        WHERE THEME_NAME IN ({names_csv})
          AND UPDATED_DT <= '{as_of_date}'
        ORDER BY THEME_NAME, STK_CODE, UPDATED_DT
    """
    df = db.SELECT(query)
    if df is None or df.empty:
        print("  ⚠️  THEME_STOCKS 조회 결과 없음")
        return pd.DataFrame()

    df['THEME_NAME'] = df['THEME_NAME'].astype(str).str.strip()
    df['STK_NAME']   = df['STK_NAME'].astype(str).str.strip()
    df['STK_CODE']   = df['STK_CODE'].astype(str).str.strip().str.zfill(6)
    df['UPDATED_DT'] = pd.to_datetime(df['UPDATED_DT']).dt.strftime('%Y%m%d')

    # 각 테마의 가장 최근 UPDATED_DT 날짜 확인
    theme_latest_dt = (df.groupby('THEME_NAME')['UPDATED_DT']
                         .max()
                         .reset_index()
                         .rename(columns={'UPDATED_DT': 'max_dt'}))

    # 각 (THEME_NAME, STK_CODE) 중 가장 최근 행만 남김
    df_latest = (df.sort_values('UPDATED_DT', ascending=False)
                   .groupby(['THEME_NAME', 'STK_CODE'], as_index=False)
                   .first())

    # 테마별 최신 날짜의 구성만 남김
    df_latest = df_latest.merge(theme_latest_dt, on='THEME_NAME')
    df_latest = df_latest[df_latest['UPDATED_DT'] == df_latest['max_dt']].copy()
    df_latest = df_latest.drop(columns=['max_dt']).reset_index(drop=True)

    n_themes = df_latest['THEME_NAME'].nunique()
    n_stocks = df_latest['STK_CODE'].nunique()
    print(f"  THEME_STOCKS 로드 완료: {n_themes}개 테마, {n_stocks}개 종목 (as_of {as_of_date})")
    for tnm, grp in df_latest.groupby('THEME_NAME'):
        print(f"    {tnm}: {len(grp)}개 종목  (UPDATED_DT={grp['UPDATED_DT'].iloc[0]})")
    return df_latest


# ═══════════════════════════════════════════════════════════════
# ▶  DataFrame 기반 매핑 (Theme.py 통합 실행 시 사용 — DB 재연결 불필요)
# ═══════════════════════════════════════════════════════════════

def map_id_to_name_df(theme_info_df: pd.DataFrame,
                      theme_ids: list,
                      as_of_date: str,
                      _verbose: bool = True) -> dict:
    """
    이미 로드된 theme_info_df 에서 THEME_ID → THEME_NAME 매핑.
    as_of_date 이전 가장 최근 UPDATED_DT 기준.
    """
    ids = [str(t).strip() for t in theme_ids]
    df  = theme_info_df.copy()
    df['THEME_ID'] = df['THEME_ID'].astype(str).str.strip()
    sub = df[(df['THEME_ID'].isin(ids)) & (df['UPDATED_DT'] <= as_of_date)]
    if sub.empty:
        if _verbose:
            print("  ⚠️  THEME_INFO 매핑 대상 없음")
        return {}
    latest = (sub.sort_values('UPDATED_DT', ascending=False)
                 .groupby('THEME_ID', as_index=False).first())
    mapping = dict(zip(latest['THEME_ID'], latest['THEME_NAME']))
    if _verbose:
        print(f"  THEME_INFO 매핑: {len(mapping)}개")
        for tid, tnm in mapping.items():
            print(f"    ID {tid:>6} → {tnm}")
    return mapping


def filter_stocks_df(theme_stocks_df: pd.DataFrame,
                     theme_names: list,
                     as_of_date: str,
                     _verbose: bool = True) -> pd.DataFrame:
    """
    이미 로드된 theme_stocks_df 에서 THEME_NAME 목록의 구성종목 반환.
    as_of_date 이전 가장 최근 UPDATED_DT 기준 구성.
    """
    df = theme_stocks_df.copy()
    df['THEME_NAME'] = df['THEME_NAME'].astype(str).str.strip()
    df['STK_CODE']   = df['STK_CODE'].astype(str).str.strip().str.zfill(6)
    df['UPDATED_DT'] = df['UPDATED_DT'].astype(str)
    if 'STK_NAME' not in df.columns:
        df['STK_NAME'] = ''
    else:
        df['STK_NAME'] = df['STK_NAME'].astype(str).str.strip()

    sub = df[(df['THEME_NAME'].isin(theme_names)) & (df['UPDATED_DT'] <= as_of_date)]
    if sub.empty:
        print("  ⚠️  THEME_STOCKS 필터 결과 없음")
        return pd.DataFrame()

    theme_max = (sub.groupby('THEME_NAME')['UPDATED_DT']
                    .max().reset_index()
                    .rename(columns={'UPDATED_DT': 'max_dt'}))
    latest = (sub.sort_values('UPDATED_DT', ascending=False)
                 .groupby(['THEME_NAME', 'STK_CODE'], as_index=False).first())
    latest = latest.merge(theme_max, on='THEME_NAME')
    latest = latest[latest['UPDATED_DT'] == latest['max_dt']].copy()
    latest = latest.drop(columns=['max_dt']).reset_index(drop=True)

    nt = latest['THEME_NAME'].nunique()
    ns = latest['STK_CODE'].nunique()
    if _verbose:
        print(f"  구성종목 필터: {nt}개 테마, {ns}개 종목 (as_of {as_of_date})")
        for tnm, grp in latest.groupby('THEME_NAME'):
            print(f"    {tnm}: {len(grp)}개  (UPDATED_DT={grp['UPDATED_DT'].iloc[0]})")
    return latest


# ───────────────────────────────────────────────────────────────
# pykrx 가격 크롤링
# ───────────────────────────────────────────────────────────────

def crawl_close_prices(stk_codes: list,
                       start_date: str,
                       end_date: str,
                       delay: float = 0.05) -> pd.DataFrame:
    """
    pykrx 로 각 종목의 종가 크롤링.

    Returns
    -------
    DataFrame : index=YYYYMMDD(str), columns=STK_CODE, values=종가(float)
    """
    price_dict: dict = {}
    total = len(stk_codes)
    print(f"  총 {total}개 종목 크롤링 시작 ({start_date} ~ {end_date})")

    for i, code in enumerate(stk_codes, 1):
        if i % 30 == 0 or i == total:
            print(f"    진행: {i}/{total}")
        try:
            df = krx.get_market_ohlcv_by_date(start_date, end_date, code)
            if df is not None and not df.empty and '종가' in df.columns:
                df.index = pd.to_datetime(df.index).strftime('%Y%m%d')
                price_dict[code] = df['종가'].astype(float)
        except Exception as e:
            print(f"    ⚠️  {code} 크롤링 실패: {e}")
        time.sleep(delay)

    if not price_dict:
        return pd.DataFrame()

    price_df = pd.DataFrame(price_dict).sort_index()
    print(f"  크롤링 완료: {price_df.shape[1]}개 종목, {price_df.shape[0]}개 거래일")
    return price_df


# ───────────────────────────────────────────────────────────────
# 수익률 계산
# ───────────────────────────────────────────────────────────────

def compute_stock_returns(price_df: pd.DataFrame,
                          rebal_date: str,
                          holding_date: str,
                          j: int = 1,
                          _verbose: bool = True) -> pd.DataFrame:
    """
    각 종목의 lookback_rtn 과 holding_rtn 계산.

    - lookback_rtn : close[rebal_date - J 거래일] 대비 close[rebal_date] 수익률
    - holding_rtn  : close[rebal_date] 대비 close[holding_date] 수익률

    Returns
    -------
    DataFrame : [STK_CODE, lookback_rtn, holding_rtn, close_rebal, close_holding]
    """
    dates = sorted(price_df.index.tolist())

    rebal_d   = _nearest_date(dates, rebal_date,   direction='prev')
    holding_d = _nearest_date(dates, holding_date, direction='prev')

    # J 거래일 이전 인덱스
    try:
        rebal_idx = dates.index(rebal_d)
    except ValueError:
        print(f"  ⚠️  rebal_date {rebal_d} 가격 데이터에 없습니다.")
        return pd.DataFrame()

    lb_start_idx = max(0, rebal_idx - j)
    lb_start_d   = dates[lb_start_idx]

    if _verbose:
        print(f"\n  날짜 매핑:")
        print(f"    LOOKBACK 시작 : {lb_start_d}  (J={j} 거래일 이전)")
        print(f"    REBAL  날짜   : {rebal_d}")
        print(f"    HOLDING 날짜  : {holding_d}")

    p_lb_start = price_df.loc[lb_start_d]
    p_rebal    = price_df.loc[rebal_d]
    p_holding  = price_df.loc[holding_d]

    rows = []
    for code in price_df.columns:
        pls = p_lb_start.get(code, np.nan)
        prb = p_rebal.get(code, np.nan)
        phd = p_holding.get(code, np.nan)

        lookback_rtn = (prb / pls - 1) if (pd.notna(pls) and pd.notna(prb) and pls > 0) else np.nan
        holding_rtn  = (phd / prb - 1) if (pd.notna(prb) and pd.notna(phd) and prb > 0) else np.nan

        rows.append({
            'STK_CODE':     code,
            'lookback_rtn': lookback_rtn,
            'holding_rtn':  holding_rtn,
            'close_rebal':  prb,
            'close_holding': phd,
        })

    return pd.DataFrame(rows)


# ───────────────────────────────────────────────────────────────
# Analysis 1 : 일치율 구간별 HOLDING 수익률
# ───────────────────────────────────────────────────────────────

def analysis1_agreement_rate(stock_returns: pd.DataFrame,
                              stocks_meta: pd.DataFrame
                              ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    테마별 일치율 = (LOOKBACK 수익률 > 0 인 종목 수) / (테마 전체 종목 수)

    일치율 구간(5개)별로 테마의 HOLDING 평균 수익률을 집계한다.

    Returns
    -------
    (bin_summary DataFrame, theme_detail DataFrame)
    """
    merged = stocks_meta[['THEME_NAME', 'STK_CODE']].merge(
        stock_returns[['STK_CODE', 'lookback_rtn', 'holding_rtn']],
        on='STK_CODE', how='left'
    )

    rows = []
    for theme, grp in merged.groupby('THEME_NAME'):
        valid_lb  = grp.dropna(subset=['lookback_rtn'])
        valid_hld = grp.dropna(subset=['holding_rtn'])

        if valid_lb.empty:
            continue

        n_total    = len(valid_lb)
        n_up       = (valid_lb['lookback_rtn'] > 0).sum()
        agreement  = n_up / n_total
        holding_mn = valid_hld['holding_rtn'].mean() if not valid_hld.empty else np.nan

        # 테마의 단순 평균 LOOKBACK 수익률
        lb_mean = valid_lb['lookback_rtn'].mean()

        rows.append({
            'THEME_NAME':       theme,
            'n_stocks':         n_total,
            'n_up':             int(n_up),
            'agreement_rate':   agreement,
            'lb_mean_rtn':      lb_mean,
            'holding_rtn_mean': holding_mn,
        })

    if not rows:
        return pd.DataFrame(), pd.DataFrame()

    theme_df = pd.DataFrame(rows)

    bins   = [0.0, 0.2, 0.4, 0.6, 0.8, 1.001]
    labels = ['0~20%', '20~40%', '40~60%', '60~80%', '80~100%']
    theme_df['agreement_bin'] = pd.cut(
        theme_df['agreement_rate'], bins=bins, labels=labels, right=False
    )

    bin_summary = (
        theme_df.groupby('agreement_bin', observed=True)
        .agg(
            테마수             = ('THEME_NAME',       'count'),
            평균_일치율         = ('agreement_rate',   'mean'),
            평균_LB수익률       = ('lb_mean_rtn',      'mean'),
            평균_HOLDING수익률  = ('holding_rtn_mean', 'mean'),
            중앙값_HOLDING수익률 = ('holding_rtn_mean', 'median'),
        )
        .reset_index()
    )
    for col in ['평균_일치율', '평균_LB수익률', '평균_HOLDING수익률', '중앙값_HOLDING수익률']:
        bin_summary[col] *= 100

    return bin_summary, theme_df


# ───────────────────────────────────────────────────────────────
# Analysis 2 : LOOKBACK Q1~Q4 구간별 HOLDING 수익률
# ───────────────────────────────────────────────────────────────

def analysis2_quartile(stock_returns: pd.DataFrame,
                        stocks_meta: pd.DataFrame
                        ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    각 테마 내부에서 LOOKBACK 수익률을 Q1(하위)~Q4(상위) 분위 할당.
    전 테마 합산 및 테마별 분위별 HOLDING 평균 수익률 산출.

    Returns
    -------
    (by_theme DataFrame, overall DataFrame)
    """
    merged = (
        stocks_meta[['THEME_NAME', 'STK_CODE', 'STK_NAME']]
        .merge(stock_returns[['STK_CODE', 'lookback_rtn', 'holding_rtn']],
               on='STK_CODE', how='left')
        .dropna(subset=['lookback_rtn', 'holding_rtn'])
    )

    if merged.empty:
        return pd.DataFrame(), pd.DataFrame()

    q_labels = ['Q1(하위)', 'Q2', 'Q3', 'Q4(상위)']
    merged['quartile'] = np.nan

    for theme, grp in merged.groupby('THEME_NAME'):
        if len(grp) < 4:
            # 종목수 4 미만 → 동일 간격 4구간 시도
            try:
                q = pd.cut(grp['lookback_rtn'], bins=4,
                           labels=q_labels, duplicates='drop')
                merged.loc[grp.index, 'quartile'] = q.values
            except Exception:
                pass
        else:
            try:
                q = pd.qcut(grp['lookback_rtn'], q=4,
                            labels=q_labels, duplicates='drop')
                merged.loc[grp.index, 'quartile'] = q.values
            except Exception:
                pass

    valid = merged.dropna(subset=['quartile'])

    overall = (
        valid
        .groupby('quartile', observed=True)
        .agg(
            종목수            = ('STK_CODE',     'count'),
            평균_LB수익률      = ('lookback_rtn',  'mean'),
            평균_HOLDING수익률  = ('holding_rtn',   'mean'),
            중앙값_HOLDING수익률 = ('holding_rtn',   'median'),
            양수HOLDING비율    = ('holding_rtn',
                                  lambda x: (x > 0).mean() * 100),
        )
        .reset_index()
    )
    for col in ['평균_LB수익률', '평균_HOLDING수익률', '중앙값_HOLDING수익률']:
        overall[col] *= 100

    by_theme = (
        valid
        .groupby(['THEME_NAME', 'quartile'], observed=True)
        .agg(
            종목수           = ('STK_CODE',    'count'),
            평균_LB수익률     = ('lookback_rtn', 'mean'),
            평균_HOLDING수익률 = ('holding_rtn',  'mean'),
        )
        .reset_index()
    )
    for col in ['평균_LB수익률', '평균_HOLDING수익률']:
        by_theme[col] *= 100

    return by_theme, overall


# ───────────────────────────────────────────────────────────────
# Analysis 3 : 하락 / 5%이하 / 5%초과 카테고리별 HOLDING 수익률
# ───────────────────────────────────────────────────────────────

def analysis3_return_category(stock_returns: pd.DataFrame,
                               stocks_meta: pd.DataFrame
                               ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    LOOKBACK 수익률 3구간 분류:
      A: 하락       (lookback_rtn  < 0)
      B: 5%이하 상승 (0 <= lookback_rtn <= 0.05)
      C: 5%초과 상승  (lookback_rtn  > 0.05)

    Returns
    -------
    (by_theme DataFrame, overall DataFrame)
    """
    merged = (
        stocks_meta[['THEME_NAME', 'STK_CODE', 'STK_NAME']]
        .merge(stock_returns[['STK_CODE', 'lookback_rtn', 'holding_rtn']],
               on='STK_CODE', how='left')
        .dropna(subset=['lookback_rtn', 'holding_rtn'])
    )

    if merged.empty:
        return pd.DataFrame(), pd.DataFrame()

    def _cat(r: float) -> str:
        if r < 0:
            return 'A: 하락(<0%)'
        elif r <= 0.1:
            return 'B: 0~5% 상승'
        else:
            return 'C: 5%초과 상승'

    merged['category'] = merged['lookback_rtn'].apply(_cat)
    cat_order = ['A: 하락(<0%)', 'B: 0~5% 상승', 'C: 5%초과 상승']

    overall = (
        merged
        .groupby('category')
        .agg(
            종목수            = ('STK_CODE',    'count'),
            평균_LB수익률      = ('lookback_rtn', 'mean'),
            평균_HOLDING수익률  = ('holding_rtn',  'mean'),
            중앙값_HOLDING수익률 = ('holding_rtn',  'median'),
            양수HOLDING비율   = ('holding_rtn',
                                 lambda x: (x > 0).mean() * 100),
        )
        .reindex(cat_order)
        .reset_index()
    )
    for col in ['평균_LB수익률', '평균_HOLDING수익률', '중앙값_HOLDING수익률']:
        overall[col] *= 100

    by_theme = (
        merged
        .groupby(['THEME_NAME', 'category'])
        .agg(
            종목수           = ('STK_CODE',    'count'),
            평균_LB수익률     = ('lookback_rtn', 'mean'),
            평균_HOLDING수익률 = ('holding_rtn',  'mean'),
        )
        .reset_index()
    )
    for col in ['평균_LB수익률', '평균_HOLDING수익률']:
        by_theme[col] *= 100

    return by_theme, overall


# ───────────────────────────────────────────────────────────────
# 출력 유틸
# ───────────────────────────────────────────────────────────────

def _pct(v, fmt='+.2f') -> str:
    """float → 백분율 문자열. NaN 이면 '  N/A'"""
    if pd.isna(v):
        return '   N/A'
    return f"{v:{fmt}}%"


def _save_excel(results: dict, rebal_date: str, holding_date: str, out_dir: str) -> None:
    """분석 결과 Excel 저장"""
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(
        out_dir,
        f"stock_return_analysis_{rebal_date}_{holding_date}.xlsx"
    )
    try:
        with pd.ExcelWriter(out_path, engine='openpyxl') as writer:
            sheets = {
                '종목별_수익률':          results.get('stock_returns'),
                '분석1_일치율_구간':      results.get('a1_bin'),
                '분석1_테마별':           results.get('a1_theme'),
                '분석2_Q분위_전체':       results.get('a2_overall'),
                '분석2_Q분위_테마별':     results.get('a2_by_theme'),
                '분석3_카테고리_전체':    results.get('a3_overall'),
                '분석3_카테고리_테마별':  results.get('a3_by_theme'),
            }
            for sheet_name, df_ in sheets.items():
                if df_ is not None and not df_.empty:
                    df_.to_excel(writer, sheet_name=sheet_name, index=False)
        print(f"  📁 결과 저장: {out_path}")
    except Exception as e:
        print(f"  ⚠️ Excel 저장 실패: {e}")


# ═══════════════════════════════════════════════════════════════
# ▶  PUBLIC API — Theme.py 에서 리밸런싱마다 자동 호출
# ═══════════════════════════════════════════════════════════════

def run_stock_return_analysis(
    theme_ids:       list,
    rebal_date:      str,
    holding_date:    str,
    theme_info_df:   pd.DataFrame,
    theme_stocks_df: pd.DataFrame,
    j:               int   = 1,
    crawl_delay:     float = 0.05,
    save_excel:      bool  = True,
    out_dir:         str   = None,
) -> dict:
    """
    GA 최적 포트폴리오의 구성종목 3가지 분석 실행 (DataFrame 기반 — DB 재연결 없음).

    Parameters
    ----------
    theme_ids       : GA 선정 THEME_ID 목록
    rebal_date      : 리밸런싱 날짜 YYYYMMDD (LOOKBACK 기준일)
    holding_date    : 보유 종료 날짜 YYYYMMDD
    theme_info_df   : Theme.py 에서 로드된 THEME_INFO DataFrame
    theme_stocks_df : Theme.py 에서 로드된 THEME_STOCKS DataFrame
    j               : Lookback 일수 (settings.J)
    crawl_delay     : pykrx 딜레이(초)  (settings.CRAWL_DELAY)
    save_excel      : True 이면 Excel 저장
    out_dir         : 저장 디렉토리 (None → Theme_model/)

    Returns
    -------
    dict : a1_bin, a1_theme, a2_overall, a2_by_theme,
           a3_overall, a3_by_theme, stock_returns
    """
    print(f"\n{'─'*72}")
    print(f"  🔍 구성종목 수익률 분석  REBAL={rebal_date} → HOLDING={holding_date}  J={j}")
    print(f"     THEME_IDS = {theme_ids}")
    print(f"{'─'*72}")

    _empty = {
        'a1_bin': pd.DataFrame(), 'a1_theme': pd.DataFrame(),
        'a2_overall': pd.DataFrame(), 'a2_by_theme': pd.DataFrame(),
        'a3_overall': pd.DataFrame(), 'a3_by_theme': pd.DataFrame(),
        'stock_returns': pd.DataFrame(),
    }

    # 1. THEME_ID → THEME_NAME
    id_to_name = map_id_to_name_df(theme_info_df, theme_ids, rebal_date)
    if not id_to_name:
        print("  ⚠️  THEME_NAME 매핑 실패 — 건너뜀")
        return _empty

    theme_names = list(id_to_name.values())

    # 2. 구성종목 조회
    stocks_meta = filter_stocks_df(theme_stocks_df, theme_names, rebal_date)
    if stocks_meta.empty:
        print("  ⚠️  구성종목 없음 — 건너뜀")
        return _empty
    name_to_id = {v: k for k, v in id_to_name.items()}
    stocks_meta = stocks_meta.copy()
    stocks_meta['THEME_ID'] = stocks_meta['THEME_NAME'].map(name_to_id)

    # 3. pykrx 크롤링
    stk_codes   = stocks_meta['STK_CODE'].unique().tolist()
    crawl_start = _trading_date_buffer_start(rebal_date, n_days=(j + 5) * 3)
    price_df    = crawl_close_prices(stk_codes, crawl_start, holding_date,
                                     delay=crawl_delay)
    if price_df.empty:
        print("  ⚠️  가격 데이터 없음 — 건너뜀")
        return _empty

    # 4. 수익률 계산
    stock_ret = compute_stock_returns(price_df, rebal_date, holding_date, j=j)
    if stock_ret.empty:
        return _empty

    stock_ret_full = stocks_meta.merge(
        stock_ret[['STK_CODE', 'lookback_rtn', 'holding_rtn',
                   'close_rebal', 'close_holding']],
        on='STK_CODE', how='left'
    )
    valid = stock_ret_full.dropna(subset=['lookback_rtn', 'holding_rtn'])
    print(f"  유효 종목: {valid['STK_CODE'].nunique()}/{len(stk_codes)}개")
    if not valid.empty:
        print(f"  LOOKBACK 평균 수익률 : {valid['lookback_rtn'].mean()*100:+.2f}%")
        print(f"  HOLDING  평균 수익률 : {valid['holding_rtn'].mean()*100:+.2f}%")

    # 5. 3가지 분석
    a1_bin,    a1_theme   = analysis1_agreement_rate(stock_ret_full, stocks_meta)
    a2_by,     a2_overall = analysis2_quartile(stock_ret_full, stocks_meta)
    a3_by,     a3_overall = analysis3_return_category(stock_ret_full, stocks_meta)

    results = {
        'a1_bin': a1_bin, 'a1_theme': a1_theme,
        'a2_overall': a2_overall, 'a2_by_theme': a2_by,
        'a3_overall': a3_overall, 'a3_by_theme': a3_by,
        'stock_returns': stock_ret_full,
    }

    # 6. 출력 (단독 실행 시에만)
    if save_excel or out_dir:
        _print_analysis_results(a1_bin, a1_theme, a2_overall, a2_by,
                                a3_overall, a3_by, rebal_date, holding_date)
        if save_excel:
            _dir = out_dir or os.path.dirname(os.path.abspath(__file__))
            _save_excel(results, rebal_date, holding_date, _dir)

    return results


def _print_analysis_results(a1_bin, a1_theme, a2_overall, a2_by,
                             a3_overall, a3_by,
                             rebal_date: str, holding_date: str):
    """분석 결과 콘솔 출력 (run_stock_return_analysis 와 main 공용)"""
    # ── Analysis 1 ──
    print(f"\n{'='*72}")
    print(f"  📊 Analysis 1: 테마별 일치율 구간 → HOLDING 평균 수익률  [{rebal_date}→{holding_date}]")
    print(f"{'='*72}")
    if not a1_theme.empty:
        print(f"  ▶ 테마별 상세")
        print(f"  {'THEME_NAME':<24} {'종목':>4} {'상승':>4} {'일치율':>8}  {'LB수익률':>10}  {'HOLDING':>12}")
        print(f"  {'-'*24} {'-'*4} {'-'*4} {'-'*8}  {'-'*10}  {'-'*12}")
        for _, r in a1_theme.sort_values('agreement_rate', ascending=False).iterrows():
            print(f"  {r['THEME_NAME']:<24} {int(r['n_stocks']):>4} {int(r['n_up']):>4}"
                  f"  {r['agreement_rate']*100:>7.1f}%"
                  f"  {_pct(r['lb_mean_rtn']*100, '+.2f'):>11}"
                  f"  {_pct(r['holding_rtn_mean']*100, '+.2f'):>12}")
    if not a1_bin.empty:
        print(f"\n  ▶ 일치율 구간별 요약")
        print(f"  {'구간':<10} {'테마수':>5} {'평균일치율':>9}  {'평균HOLD':>10}  {'중앙HOLD':>10}")
        print(f"  {'-'*10} {'-'*5} {'-'*9}  {'-'*10}  {'-'*10}")
        for _, r in a1_bin.iterrows():
            print(f"  {str(r['agreement_bin']):<10} {int(r['테마수']):>5}"
                  f"  {r['평균_일치율']:>8.1f}%"
                  f"  {_pct(r['평균_HOLDING수익률'], '+.2f'):>11}"
                  f"  {_pct(r['중앙값_HOLDING수익률'], '+.2f'):>11}")

    # ── Analysis 2 ──
    print(f"\n{'='*72}")
    print(f"  📊 Analysis 2: LOOKBACK Q1~Q4 분위 → HOLDING 평균 수익률")
    print(f"{'='*72}")
    if not a2_overall.empty:
        print(f"  ▶ 전체 합산")
        print(f"  {'분위':<12} {'종목수':>5} {'평균LB':>10}  {'평균HOLD':>10}  {'중앙HOLD':>10}  {'양수%':>7}")
        print(f"  {'-'*12} {'-'*5} {'-'*10}  {'-'*10}  {'-'*10}  {'-'*7}")
        for _, r in a2_overall.iterrows():
            print(f"  {r['quartile']:<12} {int(r['종목수']):>5}"
                  f"  {_pct(r['평균_LB수익률'], '+.2f'):>11}"
                  f"  {_pct(r['평균_HOLDING수익률'], '+.2f'):>11}"
                  f"  {_pct(r['중앙값_HOLDING수익률'], '+.2f'):>11}"
                  f"  {r['양수HOLDING비율']:>6.1f}%")
    if not a2_by.empty:
        print(f"\n  ▶ 테마별 Q분위 HOLDING 수익률(%) 피벗")
        pivot = a2_by.pivot_table(
            index='THEME_NAME', columns='quartile',
            values='평균_HOLDING수익률', aggfunc='first'
        )
        print(pivot.to_string())

    # ── Analysis 3 ──
    print(f"\n{'='*72}")
    print(f"  📊 Analysis 3: LOOKBACK 3구간 → HOLDING 평균 수익률")
    print(f"     A: 하락(<0%)  |  B: 0~5% 상승  |  C: 5%초과 상승")
    print(f"{'='*72}")
    if not a3_overall.empty:
        print(f"  ▶ 전체 합산")
        print(f"  {'카테고리':<20} {'종목수':>5} {'평균LB':>10}  {'평균HOLD':>10}  {'중앙HOLD':>10}  {'양수%':>7}")
        print(f"  {'-'*20} {'-'*5} {'-'*10}  {'-'*10}  {'-'*10}  {'-'*7}")
        for _, r in a3_overall.iterrows():
            print(f"  {r['category']:<20} {int(r['종목수']):>5}"
                  f"  {_pct(r['평균_LB수익률'], '+.2f'):>11}"
                  f"  {_pct(r['평균_HOLDING수익률'], '+.2f'):>11}"
                  f"  {_pct(r['중앙값_HOLDING수익률'], '+.2f'):>11}"
                  f"  {r['양수HOLDING비율']:>6.1f}%")
    if not a3_by.empty:
        print(f"\n  ▶ 테마별 카테고리별 HOLDING 수익률(%) 피벗")
        pivot = a3_by.pivot_table(
            index='THEME_NAME', columns='category',
            values='평균_HOLDING수익률', aggfunc='first'
        )
        print(pivot.to_string())


# ═══════════════════════════════════════════════════════════════
# ▶  배치 실행 — 전체 롤링을 단 1회 크롤링으로 처리
# ═══════════════════════════════════════════════════════════════

def run_all_rolling_analysis(
    portfolio_log:   list,
    theme_info_df:   pd.DataFrame,
    theme_stocks_df: pd.DataFrame,
    j:               int   = 1,
    crawl_delay:     float = 0.05,
) -> list:
    """
    portfolio_log 의 모든 롤링에 대해 구성종목 수익률 분석 실행.
    pykrx 크롤링을 단 1회 수행하여 실행 시간 최소화.

    Returns
    -------
    list[dict] : 각 롤링별 결과 dict (stock_returns, a1_bin, a2_overall, a3_overall 등)
    """
    _empty_res = lambda: {
        'a1_bin': pd.DataFrame(), 'a1_theme': pd.DataFrame(),
        'a2_overall': pd.DataFrame(), 'a2_by_theme': pd.DataFrame(),
        'a3_overall': pd.DataFrame(), 'a3_by_theme': pd.DataFrame(),
        'stock_returns': pd.DataFrame(),
    }

    if not portfolio_log:
        return []

    n_rollings = len(portfolio_log)
    print(f"\n{'='*72}")
    print(f"  [구성종목 분석] 총 {n_rollings}개 롤링 — pykrx 1회 크롤링 모드")
    print(f"  J={j}일 LOOKBACK → HOLDING 수익률")
    print(f"{'='*72}")

    # ── Step 1: 전체 종목 코드 · 날짜 범위 수집 (출력 억제) ──
    print("\n  [1/3] 전체 종목 코드 수집 중...")
    all_codes: set = set()
    min_rebal:  str | None = None
    max_holding: str | None = None
    rolling_meta = []  # [(rebal_date, holding_date, stocks_meta_df)]

    for entry in portfolio_log:
        rebal_date   = entry['date']
        holding_date = entry['holding_end']
        theme_ids    = list(entry['weights'].keys())

        id_to_name = map_id_to_name_df(theme_info_df, theme_ids, rebal_date,
                                        _verbose=False)
        if not id_to_name:
            rolling_meta.append((rebal_date, holding_date, pd.DataFrame()))
            continue

        theme_names = list(id_to_name.values())
        stocks_meta = filter_stocks_df(theme_stocks_df, theme_names, rebal_date,
                                       _verbose=False)
        if stocks_meta.empty:
            rolling_meta.append((rebal_date, holding_date, pd.DataFrame()))
            continue

        name_to_id  = {v: k for k, v in id_to_name.items()}
        stocks_meta = stocks_meta.copy()
        stocks_meta['THEME_ID'] = stocks_meta['THEME_NAME'].map(name_to_id)

        codes = stocks_meta['STK_CODE'].unique().tolist()
        all_codes.update(codes)

        if min_rebal is None or rebal_date < min_rebal:
            min_rebal = rebal_date
        if max_holding is None or holding_date > max_holding:
            max_holding = holding_date

        rolling_meta.append((rebal_date, holding_date, stocks_meta))

    if not all_codes or min_rebal is None:
        print("  ⚠️  분석할 종목 없음")
        return [_empty_res() for _ in portfolio_log]

    print(f"  전체 고유 종목: {len(all_codes)}개, 기간: {min_rebal} ~ {max_holding}")

    # ── Step 2: 가격 데이터 1회 크롤링 ──
    print(f"\n  [2/3] pykrx 종가 크롤링 (1회) ...")
    crawl_start = _trading_date_buffer_start(min_rebal, n_days=(j + 5) * 3)
    price_df = crawl_close_prices(sorted(all_codes), crawl_start, max_holding,
                                  delay=crawl_delay)

    if price_df.empty:
        print("  ⚠️  가격 데이터 없음")
        return [_empty_res() for _ in portfolio_log]

    # ── Step 3: 롤링별 수익률 계산 및 분석 ──
    print(f"\n  [3/3] 롤링별 수익률 계산 ({n_rollings}개)...")
    all_results = []

    for i, (rebal_date, holding_date, stocks_meta) in enumerate(rolling_meta, 1):
        if stocks_meta.empty:
            all_results.append(_empty_res())
            continue

        avail_codes = [c for c in stocks_meta['STK_CODE'].unique() if c in price_df.columns]
        if not avail_codes:
            all_results.append(_empty_res())
            continue

        price_sub  = price_df[avail_codes]
        stock_ret  = compute_stock_returns(price_sub, rebal_date, holding_date,
                                           j=j, _verbose=False)
        if stock_ret.empty:
            all_results.append(_empty_res())
            continue

        stock_ret_full = stocks_meta.merge(
            stock_ret[['STK_CODE', 'lookback_rtn', 'holding_rtn',
                       'close_rebal', 'close_holding']],
            on='STK_CODE', how='left'
        )

        a1_bin,  a1_theme   = analysis1_agreement_rate(stock_ret_full, stocks_meta)
        a2_by,   a2_overall = analysis2_quartile(stock_ret_full, stocks_meta)
        a3_by,   a3_overall = analysis3_return_category(stock_ret_full, stocks_meta)

        all_results.append({
            'a1_bin': a1_bin, 'a1_theme': a1_theme,
            'a2_overall': a2_overall, 'a2_by_theme': a2_by,
            'a3_overall': a3_overall, 'a3_by_theme': a3_by,
            'stock_returns': stock_ret_full,
        })

    n_ok = sum(1 for r in all_results if not r['stock_returns'].empty)
    print(f"  완료: {n_ok}/{n_rollings}개 롤링 분석 성공")
    return all_results


def aggregate_and_print_rolling_results(all_results_list: list) -> None:
    """
    모든 롤링 결과를 집계하여 최종 평균 수익률 3종 요약 출력.
      1) 일치율 구간별 최종 평균 HOLDING 수익률
      2) 테마내 Quantile별 최종 평균 HOLDING 수익률
      3) 하락/상승/강상승 분류별 최종 평균 HOLDING 수익률
    """
    parts = []
    for i, res in enumerate(all_results_list, 1):
        sr = res.get('stock_returns')
        if sr is not None and not sr.empty:
            sr = sr.copy()
            sr['_rolling'] = i
            parts.append(sr)

    if not parts:
        print("\n  ⚠️  집계할 데이터 없음")
        return

    combined = pd.concat(parts, ignore_index=True)
    combined = combined.dropna(subset=['lookback_rtn', 'holding_rtn'])

    n_rollings = combined['_rolling'].nunique()
    n_obs      = len(combined)

    print(f"\n{'='*72}")
    print(f"  📊 구성종목 수익률 최종 집계  ({n_rollings}개 리밸런싱, 총 {n_obs}건)")
    print(f"{'='*72}")

    # ── Analysis 1: 일치율 구간별 ──
    print(f"\n  ━━━ Analysis 1: 일치율 구간별 최종 평균 HOLDING 수익률 ━━━")
    theme_rows = []
    for (roll, theme), grp in combined.groupby(['_rolling', 'THEME_NAME']):
        n_total = len(grp)
        if n_total == 0:
            continue
        n_up       = (grp['lookback_rtn'] > 0).sum()
        agreement  = n_up / n_total
        holding_mn = grp['holding_rtn'].mean()
        lb_mean    = grp['lookback_rtn'].mean()
        theme_rows.append({
            'agreement_rate':   agreement,
            'holding_rtn_mean': holding_mn,
            'lb_mean_rtn':      lb_mean,
        })

    if theme_rows:
        tdf    = pd.DataFrame(theme_rows)
        bins   = [0.0, 0.2, 0.4, 0.6, 0.8, 1.001]
        labels = ['0~20%', '20~40%', '40~60%', '60~80%', '80~100%']
        tdf['bin'] = pd.cut(tdf['agreement_rate'], bins=bins, labels=labels, right=False)
        bin_s = (
            tdf.groupby('bin', observed=True)
            .agg(
                관측수            = ('holding_rtn_mean', 'count'),
                평균_일치율        = ('agreement_rate',   'mean'),
                평균_LB수익률      = ('lb_mean_rtn',      'mean'),
                평균_HOLDING수익률 = ('holding_rtn_mean', 'mean'),
            )
            .reset_index()
        )
        print(f"  {'구간':<10} {'관측수':>6} {'평균일치율':>9}  {'평균LB':>10}  {'평균HOLD':>10}")
        print(f"  {'-'*10} {'-'*6} {'-'*9}  {'-'*10}  {'-'*10}")
        for _, r in bin_s.iterrows():
            print(f"  {str(r['bin']):<10} {int(r['관측수']):>6}"
                  f"  {r['평균_일치율']*100:>8.1f}%"
                  f"  {r['평균_LB수익률']*100:>+10.2f}%"
                  f"  {r['평균_HOLDING수익률']*100:>+10.2f}%")

    # ── Analysis 2: Quantile ──
    print(f"\n  ━━━ Analysis 2: 테마내 Quantile별 최종 평균 HOLDING 수익률 ━━━")
    q_labels = ['Q1(하위)', 'Q2', 'Q3', 'Q4(상위)']
    combined = combined.copy()
    combined['_quartile'] = np.nan

    for (roll, theme), grp in combined.groupby(['_rolling', 'THEME_NAME']):
        if len(grp) < 2:
            continue
        try:
            if len(grp) >= 4:
                q = pd.qcut(grp['lookback_rtn'], q=4, labels=q_labels, duplicates='drop')
            else:
                q = pd.cut(grp['lookback_rtn'], bins=4, labels=q_labels, duplicates='drop')
            combined.loc[grp.index, '_quartile'] = q.values
        except Exception:
            pass

    valid_q = combined.dropna(subset=['_quartile'])
    if not valid_q.empty:
        overall_q = (
            valid_q.groupby('_quartile', observed=True)
            .agg(
                종목수            = ('STK_CODE',     'count'),
                평균_LB수익률      = ('lookback_rtn',  'mean'),
                평균_HOLDING수익률 = ('holding_rtn',   'mean'),
                중앙값_HOLDING     = ('holding_rtn',   'median'),
                양수비율           = ('holding_rtn',
                                      lambda x: (x > 0).mean() * 100),
            )
            .reset_index()
        )
        print(f"  {'분위':<12} {'종목수':>6} {'평균LB':>10}  {'평균HOLD':>10}  {'중앙HOLD':>10}  {'양수%':>7}")
        print(f"  {'-'*12} {'-'*6} {'-'*10}  {'-'*10}  {'-'*10}  {'-'*7}")
        for _, r in overall_q.iterrows():
            print(f"  {str(r['_quartile']):<12} {int(r['종목수']):>6}"
                  f"  {r['평균_LB수익률']*100:>+10.2f}%"
                  f"  {r['평균_HOLDING수익률']*100:>+10.2f}%"
                  f"  {r['중앙값_HOLDING']*100:>+10.2f}%"
                  f"  {r['양수비율']:>6.1f}%")

    # ── Analysis 3: Category ──
    print(f"\n  ━━━ Analysis 3: LOOKBACK 구간 분류별 최종 평균 HOLDING 수익률 ━━━")
    print(f"     A: 하락(<0%)  |  B: 0~5% 상승  |  C: 5%초과 상승")

    def _cat(r: float) -> str:
        if r < 0:
            return 'A: 하락(<0%)'
        elif r <= 0.05:
            return 'B: 0~5% 상승'
        else:
            return 'C: 5%초과 상승'

    combined['_category'] = combined['lookback_rtn'].apply(_cat)
    cat_order = ['A: 하락(<0%)', 'B: 0~5% 상승', 'C: 5%초과 상승']
    overall_cat = (
        combined.groupby('_category')
        .agg(
            종목수            = ('STK_CODE',     'count'),
            평균_LB수익률      = ('lookback_rtn',  'mean'),
            평균_HOLDING수익률 = ('holding_rtn',   'mean'),
            중앙값_HOLDING     = ('holding_rtn',   'median'),
            양수비율           = ('holding_rtn',
                                  lambda x: (x > 0).mean() * 100),
        )
        .reindex(cat_order)
        .reset_index()
    )
    print(f"  {'카테고리':<20} {'종목수':>6} {'평균LB':>10}  {'평균HOLD':>10}  {'중앙HOLD':>10}  {'양수%':>7}")
    print(f"  {'-'*20} {'-'*6} {'-'*10}  {'-'*10}  {'-'*10}  {'-'*7}")
    for _, r in overall_cat.iterrows():
        print(f"  {str(r['_category']):<20} {int(r['종목수']):>6}"
              f"  {r['평균_LB수익률']*100:>+10.2f}%"
              f"  {r['평균_HOLDING수익률']*100:>+10.2f}%"
              f"  {r['중앙값_HOLDING']*100:>+10.2f}%"
              f"  {r['양수비율']:>6.1f}%")

    print(f"\n{'='*72}")


# ───────────────────────────────────────────────────────────────
# 단독 실행 main (DB 직접 연결)
# ───────────────────────────────────────────────────────────────

def main():
    pd.set_option('display.max_columns', None)
    pd.set_option('display.width', 160)
    pd.set_option('display.float_format', '{:.4f}'.format)

    print("=" * 72)
    print("  테마 구성종목 수익률 분석  (단독 실행 모드)")
    print(f"  REBAL_DATE   : {STANDALONE_REBAL_DATE}")
    print(f"  HOLDING_DATE : {STANDALONE_HOLDING_DATE}")
    print(f"  J            : {STANDALONE_J}")
    print("=" * 72)

    # THEME_ID 확인
    theme_ids = [str(t).strip() for t in STANDALONE_THEME_IDS] if STANDALONE_THEME_IDS else []
    if not theme_ids:
        raw = input(
            "\n  GA 최종 THEME_ID를 쉼표로 구분하여 입력하세요\n"
            "  (예: 38,113,319,447): "
        ).strip()
        theme_ids = [t.strip() for t in raw.split(',') if t.strip()]
    if not theme_ids:
        print("  ⚠️  THEME_ID 없음 — 종료")
        return
    print(f"\n  분석 대상 ({len(theme_ids)}개): {theme_ids}")

    # DB 연결
    print(f"\n[DB] THEME_INFO + THEME_STOCKS 로드...")
    db = MSSQL(database=DBConfig.TRSTDEV_DB)
    try:
        id_to_name = fetch_theme_name_map(db, theme_ids, STANDALONE_REBAL_DATE)
        if not id_to_name:
            return
        theme_names = list(id_to_name.values())
        stocks_meta = fetch_theme_stocks(db, theme_names, STANDALONE_REBAL_DATE)
    finally:
        db.close()
        print("  ✅ DB 연결 종료")

    if stocks_meta.empty:
        return
    name_to_id = {v: k for k, v in id_to_name.items()}
    stocks_meta = stocks_meta.copy()
    stocks_meta['THEME_ID'] = stocks_meta['THEME_NAME'].map(name_to_id)

    # 크롤링
    stk_codes   = stocks_meta['STK_CODE'].unique().tolist()
    crawl_start = _trading_date_buffer_start(STANDALONE_REBAL_DATE,
                                             n_days=(STANDALONE_J + 5) * 3)
    price_df    = crawl_close_prices(stk_codes, crawl_start, STANDALONE_HOLDING_DATE,
                                     delay=STANDALONE_CRAWL_DELAY)
    if price_df.empty:
        return

    stock_ret = compute_stock_returns(price_df, STANDALONE_REBAL_DATE,
                                      STANDALONE_HOLDING_DATE, j=STANDALONE_J)
    if stock_ret.empty:
        return

    stock_ret_full = stocks_meta.merge(
        stock_ret[['STK_CODE', 'lookback_rtn', 'holding_rtn',
                   'close_rebal', 'close_holding']],
        on='STK_CODE', how='left'
    )

    a1_bin, a1_theme   = analysis1_agreement_rate(stock_ret_full, stocks_meta)
    a2_by,  a2_overall = analysis2_quartile(stock_ret_full, stocks_meta)
    a3_by,  a3_overall = analysis3_return_category(stock_ret_full, stocks_meta)

    results = {
        'a1_bin': a1_bin, 'a1_theme': a1_theme,
        'a2_overall': a2_overall, 'a2_by_theme': a2_by,
        'a3_overall': a3_overall, 'a3_by_theme': a3_by,
        'stock_returns': stock_ret_full,
    }
    _print_analysis_results(a1_bin, a1_theme, a2_overall, a2_by,
                            a3_overall, a3_by,
                            STANDALONE_REBAL_DATE, STANDALONE_HOLDING_DATE)
    out_dir = os.path.dirname(os.path.abspath(__file__))
    _save_excel(results, STANDALONE_REBAL_DATE, STANDALONE_HOLDING_DATE, out_dir)

    print(f"\n{'='*72}")
    print("  분석 완료")
    print(f"{'='*72}")


if __name__ == '__main__':
    main()

