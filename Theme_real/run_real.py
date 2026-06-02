# -*- coding: utf-8 -*-
"""
Theme_real 파이프라인 실행 스크립트 (실무용)
=============================================
0. (선택) Theme_crawling.py 크롤링 실행
1. theme_data/ 에 축적된 beta_rebound_detail_*.csv 를 유니버스로 사용
2. 구성종목 수 필터 적용
3. J일 모멘텀 기준 테마 랭킹 + 종목 중복 필터
4. GA 비중 최적화 → 최적 포트폴리오 비중 산출
5. 포트폴리오 비중 로그 저장 (날짜별 누적)

※ 백테스팅 없음. K(보유기간)를 사용하지 않으며
  현재 시점의 최적 비중만 산출하는 실무 도구.

Usage:
    python Theme_real/run_real.py
"""

import sys
import os
import glob
import re
import pandas as pd
import numpy as np
from datetime import datetime
import warnings
warnings.filterwarnings('ignore')

# repo root 를 sys.path 에 추가 (어떤 cwd 에서 실행해도 import 가능하도록)
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from Theme_real.settings_real import (
    THEME_DATA_DIR, RESULT_DIR,
    J, K, TOP_N, GAP,
    REBAL_COST_GA,
    USE_FILTER, MIN_CONSTITUENTS,
    OVERLAP_THRESHOLD,
    GA_LOOKBACK, GA_POP_SIZE, GA_GENERATIONS,
    GA_MUTATION_RATE,
    MIN_WEIGHT_THRESHOLD,
    MIN_HISTORY_DAYS,
    get_filter_config, get_ga_config,
)
from Theme_real.theme_filter_real import (
    build_constituent_counts_from_csv,
    summarize_filter_stats,
)
from Theme_real.theme_filter2_real import (
    build_constituent_sets_from_csv,
)
from Theme_real.Theme_GA_real import (
    compute_optimal_portfolio,
)

import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s  %(message)s')
log = logging.getLogger(__name__)


# ============================================================
# 데이터 로드: 축적된 CSV들을 날짜별로 읽어 피벗 테이블 구축
# ============================================================
# 데이터 로드
# ============================================================
def load_rebound_detail_history(data_dir: str) -> pd.DataFrame:
    """
    beta_rebound_detail_YYYYMMDD.csv 파일들을 모두 읽어 합친다.
    구성종목 정보 추출용.

    Returns
    -------
    DataFrame : (theme_no, 테마명, 종목코드, 등락률, date, ...)
    """
    pattern = os.path.join(data_dir, "beta_rebound_detail_*.csv")
    files = sorted(glob.glob(pattern))

    if not files:
        log.warning("beta_rebound_detail_*.csv 파일이 없습니다.")
        return pd.DataFrame()

    frames = []
    for f in files:
        m = re.search(r'beta_rebound_detail_(\d{8})\.csv', f)
        if not m:
            continue
        date_str = m.group(1)
        try:
            df = pd.read_csv(f, encoding='utf-8-sig')
            df['date'] = date_str
            frames.append(df)
        except Exception as e:
            log.warning(f"파일 로드 실패: {f} - {e}")

    if not frames:
        return pd.DataFrame()

    result = pd.concat(frames, ignore_index=True)
    log.info(f"beta_rebound_detail 로드: {len(files)}개 파일, "
             f"{result['date'].nunique()}일, {len(result)} rows")
    return result


def load_declining_detail_history(data_dir: str) -> pd.DataFrame:
    """beta_declining_detail_YYYYMMDD.csv 파일들을 모두 읽어 합친다."""
    pattern = os.path.join(data_dir, "beta_declining_detail_*.csv")
    files = sorted(glob.glob(pattern))

    if not files:
        return pd.DataFrame()

    frames = []
    for f in files:
        m = re.search(r'beta_declining_detail_(\d{8})\.csv', f)
        if not m:
            continue
        date_str = m.group(1)
        try:
            df = pd.read_csv(f, encoding='utf-8-sig')
            df['date'] = date_str
            frames.append(df)
        except Exception as e:
            log.warning(f"파일 로드 실패: {f} - {e}")

    if not frames:
        return pd.DataFrame()

    result = pd.concat(frames, ignore_index=True)
    log.info(f"beta_declining_detail 로드: {len(files)}개 파일, "
             f"{result['date'].nunique()}일, {len(result)} rows")
    return result


# ============================================================
# 피벗 테이블 구축 (beta_rebound_detail 기반)
# ============================================================
def build_pivot_tables(rebound_detail_df: pd.DataFrame) -> tuple:
    """
    beta_rebound_detail 히스토리에서 테마 레벨 피벗 테이블 3종 생성.
    detail 데이터에서 (date, theme_no) 별 첫 행을 추출하여
    테마등락률 / 베타조정수익률 / 코스피등락률을 사용한다.

    Returns
    -------
    adj_pivot : (date x theme_no) 베타조정수익률 (%)→소수 변환)
    theme_pivot : (date x theme_no) 테마등락률 (%)→소수 변환)
    kospi_pivot : (date) 코스피 등락률 (%)→소수 변환)
    """
    df = rebound_detail_df.copy()
    df['theme_no'] = df['theme_no'].astype(str)

    # ── 테마 레벨 행 추출 (종목별 중복 제거) ──
    theme_level = df.drop_duplicates(subset=['date', 'theme_no'], keep='first').copy()

    # %단위를 소수로 변환 (테마등락률, 베타조정수익률은 테마 레벨 값)
    theme_level['베타조정수익률_dec'] = pd.to_numeric(
        theme_level['베타조정수익률'], errors='coerce'
    ) / 100.0

    # 테마등락률 컬럼 파싱
    if '테마등락률' in theme_level.columns:
        theme_level['테마등락률_dec'] = pd.to_numeric(
            theme_level['테마등락률'], errors='coerce'
        ) / 100.0
    elif '등락률' in theme_level.columns:
        # fallback: 등락률 사용
        theme_level['테마등락률_dec'] = pd.to_numeric(
            theme_level['등락률'], errors='coerce'
        ) / 100.0
    else:
        theme_level['테마등락률_dec'] = 0.0

    # 코스피 등락률 파싱
    if '코스피등락률' in theme_level.columns:
        theme_level['코스피등락률_dec'] = pd.to_numeric(
            theme_level['코스피등락률'].astype(str)
                .str.replace('+', '').str.replace('%', '').str.replace(',', ''),
            errors='coerce'
        ) / 100.0
    else:
        theme_level['코스피등락률_dec'] = 0.0

    # 베타조정수익률 피벗
    adj_pivot = theme_level.pivot_table(
        index='date', columns='theme_no', values='베타조정수익률_dec', aggfunc='first'
    ).sort_index()

    # 테마 등락률 피벗
    theme_pivot = theme_level.pivot_table(
        index='date', columns='theme_no', values='테마등락률_dec', aggfunc='first'
    ).sort_index()

    # 코스피 등락률 시리즈
    kospi_pivot = (theme_level.drop_duplicates('date')
                   .set_index('date')['코스피등락률_dec']
                   .sort_index())

    return adj_pivot, theme_pivot, kospi_pivot


# ============================================================
# 구성종목 정보 구축 (최신 rebound + declining detail 기반)
# ============================================================
def build_constituent_info(rebound_df: pd.DataFrame,
                           declining_df: pd.DataFrame) -> tuple:
    """
    rebound + declining detail에서 구성종목 수 / 구성종목 set 구축.
    가장 최근 파일의 데이터를 기준으로 한다.

    Returns
    -------
    constituent_counts : {theme_no(str) → 종목수(int)}
    constituent_sets : {theme_no(str) → set(종목코드)}
    """
    # rebound와 declining을 합침
    frames = []
    if not rebound_df.empty:
        frames.append(rebound_df)
    if not declining_df.empty:
        frames.append(declining_df)

    if not frames:
        return {}, {}

    combined = pd.concat(frames, ignore_index=True)

    # 가장 최근 날짜의 데이터 사용
    if 'date' in combined.columns:
        latest_date = combined['date'].max()
        combined = combined[combined['date'] == latest_date]
        log.info(f"구성종목 정보: {latest_date} 기준")

    constituent_counts = build_constituent_counts_from_csv(combined)
    constituent_sets = build_constituent_sets_from_csv(combined)

    return constituent_counts, constituent_sets


def build_constituent_counts_from_csv(detail_df: pd.DataFrame) -> dict:
    """CSV에서 테마별 구성종목 수 계산"""
    if detail_df.empty:
        return {}

    code_col = '종목코드' if '종목코드' in detail_df.columns else '종목명'
    counts = detail_df.groupby('theme_no')[code_col].nunique().to_dict()
    return {str(k): v for k, v in counts.items()}


def build_constituent_sets_from_csv(detail_df: pd.DataFrame) -> dict:
    """CSV에서 테마별 구성종목 set 구축"""
    if detail_df.empty:
        return {}

    code_col = '종목코드' if '종목코드' in detail_df.columns else '종목명'
    result = {}
    for theme_no, group in detail_df.groupby('theme_no'):
        stk_set = set(group[code_col].dropna().astype(str).unique())
        result[str(theme_no)] = stk_set
    return result


# ============================================================
# 테마별 일치율 산출
# ============================================================
def compute_theme_agreement_rate(rebound_df: pd.DataFrame,
                                  latest_date: str) -> dict:
    """
    테마별 일치율 계산:
    일치율 = (종목별 베타조정수익률 > 0 인 종목수) / (전체 종목수) * 100

    종목별 베타조정수익률 = 종목등락률(%) - Beta * 코스피등락률(%)

    Parameters
    ----------
    rebound_df : beta_rebound_detail 전체 히스토리
    latest_date : 최신 날짜 (str)

    Returns
    -------
    dict : {theme_no(str) → 일치율(float, %)}
    """
    df = rebound_df[rebound_df['date'] == latest_date].copy()
    if df.empty:
        return {}

    df['theme_no'] = df['theme_no'].astype(str)

    # 종목별 등락률 파싱
    df['stock_return'] = pd.to_numeric(df['등락률'], errors='coerce').fillna(0)

    # Beta (테마 레벨)
    df['beta'] = pd.to_numeric(df['Beta'], errors='coerce').fillna(0)

    # 코스피등락률 파싱
    df['kospi_chg'] = pd.to_numeric(
        df['코스피등락률'].astype(str)
            .str.replace('+', '').str.replace('%', '').str.replace(',', ''),
        errors='coerce'
    ).fillna(0)

    # 종목별 베타 조정 수익률 = 종목등락률(%) - Beta × 코스피등락률(%)
    df['stock_beta_adj'] = df['stock_return'] - df['beta'] * df['kospi_chg']

    result = {}
    for theme_no, group in df.groupby('theme_no'):
        total = len(group)
        positive = (group['stock_beta_adj'] > 0).sum()
        rate = (positive / total * 100) if total > 0 else 0.0
        result[str(theme_no)] = round(rate, 1)

    return result


def build_theme_name_map(rebound_df: pd.DataFrame,
                         latest_date: str) -> dict:
    """
    최신 날짜의 rebound_detail에서 theme_no → 테마명 매핑 구축.
    """
    df = rebound_df[rebound_df['date'] == latest_date].copy()
    if df.empty or '테마명' not in df.columns:
        return {}
    df['theme_no'] = df['theme_no'].astype(str)
    return df.drop_duplicates('theme_no').set_index('theme_no')['테마명'].to_dict()


# ============================================================
# 포트폴리오 비중 로그 저장 (누적 append 방식)
# ============================================================
PORTFOLIO_LOG_FILE = os.path.join(RESULT_DIR, "portfolio_weight_log.csv")


def save_portfolio_log(weight_dict: dict,
                       run_date: str,
                       n_days_used: int,
                       filter_stats: dict,
                       n_removed: int,
                       n_weight_excluded: int,
                       agreement_rates: dict,
                       result_dir: str) -> None:
    """
    포트폴리오 비중을 로그 파일에 누적 저장.
    실행할 때마다 새 행이 추가되어 이력이 남는다.
    """
    os.makedirs(result_dir, exist_ok=True)

    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    rows = []
    for theme_id, weight in sorted(weight_dict.items(), key=lambda x: -x[1]):
        rows.append({
            'timestamp': timestamp,
            'date': run_date,
            'theme_no': theme_id,
            'weight': round(weight, 6),
            'agreement_rate': agreement_rates.get(str(theme_id), 0),
            'n_themes': len(weight_dict),
            'n_days_used': n_days_used,
            'filter_total': filter_stats.get('total', 0),
            'filter_passed': filter_stats.get('passed', 0),
            'n_overlap_removed': n_removed,
            'n_weight_excluded': n_weight_excluded,
        })

    new_df = pd.DataFrame(rows)

    log_path = os.path.join(result_dir, "portfolio_weight_log.csv")
    if os.path.exists(log_path):
        new_df.to_csv(log_path, mode='a', header=False,
                      index=False, encoding='utf-8-sig')
    else:
        new_df.to_csv(log_path, index=False, encoding='utf-8-sig')

    log.info(f"포트폴리오 비중 로그 저장: {log_path}")


# ============================================================
# 메인
# ============================================================
def run_crawling():
    """Theme_crawling.py 크롤링 실행"""
    print("\n[크롤링] Theme_crawling.py 실행 중...")
    try:
        from Theme_real.Theme_crawling import main as crawl_main
        crawl_main()
        print("[크롤링] 완료\n")
    except Exception as e:
        log.error(f"크롤링 실패: {e}")
        import traceback
        traceback.print_exc()
        return False
    return True


def main():
    print("=" * 65)
    print("  Theme_real 파이프라인 (실무 모드) — 방안 A")
    print(f"  J일 모멘텀 랭킹 + 중복필터 + GA 비중 최적화")
    print(f"  J={J} / K={K} / Gap={GAP} / Top {TOP_N}")
    print(f"  중복 임계값: {OVERLAP_THRESHOLD*100:.0f}%")
    print(f"  최소 비중: {MIN_WEIGHT_THRESHOLD*100:.2f}%")
    print(f"  거래비용(GA, 편도): {REBAL_COST_GA*100:.2f}% (왕복 {REBAL_COST_GA*200:.2f}%)")
    print(f"  GA: lookback(변동성)={GA_LOOKBACK}일, 수익률=J({J}일)")
    print(f"  GA: pop={GA_POP_SIZE}, gen={GA_GENERATIONS}, mut={GA_MUTATION_RATE}")
    print(f"  필터: 구성종목 >= {MIN_CONSTITUENTS}개")
    print(f"  유니버스: beta_rebound_detail (반등 테마)")
    print("=" * 65)

    # ═══════════════════════════════════════════════
    # Step 0: 크롤링 실행 여부 선택
    # ═══════════════════════════════════════════════
    print("\n[실행 모드 선택]")
    print("  1) 크롤링 실행 후 분석 진행")
    print("  2) 기존 파일로 분석만 진행")
    try:
        choice = input("  선택 (1 또는 2): ").strip()
    except EOFError:
        choice = '2'

    if choice == '1':
        ok = run_crawling()
        if not ok:
            print("크롤링에 실패하여 중단합니다.")
            return
    elif choice != '2':
        print(f"  잘못된 입력 '{choice}' → 기존 파일로 분석 진행")

    # ═══════════════════════════════════════════════
    # Step 1: 데이터 로드 (beta_rebound_detail 기반)
    # ═══════════════════════════════════════════════
    print("\n[Step 1] 축적 데이터 로드 (beta_rebound_detail)")

    rebound_df = load_rebound_detail_history(THEME_DATA_DIR)
    declining_df = load_declining_detail_history(THEME_DATA_DIR)

    if rebound_df.empty:
        log.error("beta_rebound_detail 데이터가 없습니다. "
                  "크롤링을 먼저 실행하세요 (모드 1).")
        return

    n_days = rebound_df['date'].nunique()
    latest_date = rebound_df['date'].max()
    log.info(f"beta_rebound_detail 축적 데이터: {n_days}일, 최신: {latest_date}")

    if n_days < MIN_HISTORY_DAYS:
        log.warning(f"데이터 축적일 ({n_days}일) < 최소 요구 ({MIN_HISTORY_DAYS}일). "
                    f"더 많은 데이터 축적 후 실행하세요.")
        return

    # ═══════════════════════════════════════════════
    # Stale-data 가드: latest_date 가 오늘이 아니면 경고
    # ═══════════════════════════════════════════════
    today_str = datetime.now().strftime('%Y%m%d')
    if latest_date != today_str:
        print("\n" + "!" * 65)
        print(f"  ⚠ 경고: latest_date={latest_date} 이 오늘({today_str})이 아닙니다.")
        print(f"  → 오늘자 beta_rebound_detail_{today_str}.csv 가 없어")
        print(f"     {latest_date} 데이터로 신호가 산출됩니다 (stale output).")
        print(f"  → Theme_crawling.py 를 먼저 실행하거나, 모드 1 로 재실행하세요.")
        print("!" * 65)
        try:
            ans = input("  그래도 진행하시겠습니까? (y/N): ").strip().lower()
        except EOFError:
            ans = 'n'
        if ans != 'y':
            print("  중단합니다.")
            return

    # 날짜별 테마 수 표시
    print(f"\n  축적된 날짜 (beta_rebound_detail):")
    for d in sorted(rebound_df['date'].unique()):
        n_themes = rebound_df[rebound_df['date'] == d]['theme_no'].nunique()
        print(f"    {d}: {n_themes}개 반등 테마")

    # ═══════════════════════════════════════════════
    # Step 2: 피벗 테이블 + 구성종목 정보 구축
    # ═══════════════════════════════════════════════
    print(f"\n[Step 2] 피벗 테이블 + 구성종목 정보 구축")

    adj_pivot, theme_pivot, kospi_pivot = build_pivot_tables(rebound_df)
    log.info(f"피벗: {adj_pivot.shape[0]}일 x {adj_pivot.shape[1]}개 테마 (반등 유니버스)")

    constituent_counts, constituent_sets = build_constituent_info(
        rebound_df, declining_df
    )
    log.info(f"구성종목 정보: {len(constituent_counts)}개 테마")

    # ═══════════════════════════════════════════════
    # Step 3: 최적 포트폴리오 비중 산출
    # ═══════════════════════════════════════════════
    print(f"\n[Step 3] 최적 포트폴리오 비중 산출")
    print(f"  J={J}일 모멘텀 → Top {TOP_N} → 중복필터 → GA 최적화")
    if n_days < 3:
        print(f"  ※ 축적 데이터 {n_days}일 → GA 변동성 추정 불가 → 동일비중 적용")

    weight_dict, filter_stats, n_removed, n_weight_excluded, ranked_info = \
        compute_optimal_portfolio(
            adj_pivot, theme_pivot, kospi_pivot,
            constituent_counts=constituent_counts,
            constituent_sets=constituent_sets,
            J=J, top_n=TOP_N,
            use_filter=USE_FILTER,
            min_constituents=MIN_CONSTITUENTS,
            overlap_threshold=OVERLAP_THRESHOLD,
            ga_lookback=GA_LOOKBACK,
        )

    if not weight_dict:
        log.warning("최적 비중 산출 실패 (유효 테마 부족)")
        print(f"\n  피벗: {adj_pivot.shape[0]}일 x {adj_pivot.shape[1]}개 테마")
        print(f"  필터 통과: {filter_stats.get('passed', 0)}개")
        return

    # ── 일치율 + 테마명 매핑 구축 ──
    agreement_rates = compute_theme_agreement_rate(rebound_df, latest_date)
    theme_names = build_theme_name_map(rebound_df, latest_date)

    # ── 필터 통계 출력 ──
    print(f"\n  📊 필터 통계")
    print(f"  {'─'*40}")
    print(f"  전체 테마:       {filter_stats.get('total', 0)}개")
    print(f"  구성종목 미달:   {filter_stats.get('f1_constituents', 0)}개 "
          f"(< {MIN_CONSTITUENTS}개)")
    print(f"  필터 통과:       {filter_stats.get('passed', 0)}개")
    print(f"  Top {TOP_N} 선정 후 중복 제거: {n_removed}개")
    print(f"  비중제한 제외:   {n_weight_excluded}개")
    print(f"  최종 포트폴리오: {len(weight_dict)}개 테마")

    # ── Top N 모멘텀 랭킹 결과 + 일치율 출력 ──
    print(f"\n{'='*80}")
    print(f"  📊 Top {len(ranked_info)} 모멘텀 랭킹 (중복필터 전)  [{latest_date}]")
    print(f"{'='*80}")
    print(f"  {'No':>4}  {'theme_no':>10}  {'테마명':<16}  {'AdjRtn':>10}  {'일치율':>8}")
    print(f"  {'-'*4}  {'-'*10}  {'-'*16}  {'-'*10}  {'-'*8}")

    score_map = {r['theme_no']: r['adj_score'] for r in ranked_info}
    survivor_set = set(weight_dict.keys())

    for i, info in enumerate(ranked_info, 1):
        th = info['theme_no']
        adj_score = info['adj_score']
        rate = agreement_rates.get(str(th), 0)
        name = theme_names.get(str(th), '')
        # 테마명 16자 맞춤 (한글은 2칸)
        name_disp = name[:8] if len(name) > 8 else name
        marker = '✓' if str(th) in survivor_set else '✗'
        print(f"  {i:>4}  {th:>10}  {name_disp:<16}  "
              f"{adj_score*100:>9.4f}%  {rate:>7.1f}%  {marker}")

    print(f"  {'─'*70}")
    print(f"  ✓ = 최종 포트폴리오 편입  |  ✗ = 중복/비중제한 제거")

    # ── 최적 포트폴리오 비중 출력 ──
    print(f"\n{'='*80}")
    print(f"  📊 최적 포트폴리오 비중  [{latest_date}]")
    print(f"{'='*80}")
    print(f"  {'No':>4}  {'theme_no':>10}  {'테마명':<16}  {'비중':>10}  "
          f"{'AdjRtn':>10}  {'일치율':>8}")
    print(f"  {'-'*4}  {'-'*10}  {'-'*16}  {'-'*10}  {'-'*10}  {'-'*8}")

    sorted_w = sorted(weight_dict.items(), key=lambda x: -x[1])
    for i, (theme_id, w) in enumerate(sorted_w, 1):
        adj_score = score_map.get(theme_id, 0)
        rate = agreement_rates.get(str(theme_id), 0)
        name = theme_names.get(str(theme_id), '')
        name_disp = name[:8] if len(name) > 8 else name
        print(f"  {i:>4}  {theme_id:>10}  {name_disp:<16}  "
              f"{w*100:>9.4f}%  {adj_score*100:>9.4f}%  {rate:>7.1f}%")

    total_w = sum(weight_dict.values())
    top3_w = sum(w for _, w in sorted_w[:3])
    top5_w = sum(w for _, w in sorted_w[:5])
    print(f"  {'─'*70}")
    print(f"  합계: {total_w*100:.2f}%  |  "
          f"Top3: {top3_w*100:.1f}%  |  Top5: {top5_w*100:.1f}%")

    # ═══════════════════════════════════════════════
    # Step 4: 포트폴리오 비중 로그 저장
    # ═══════════════════════════════════════════════
    print(f"\n[Step 4] 포트폴리오 비중 로그 저장")
    save_portfolio_log(
        weight_dict=weight_dict,
        run_date=latest_date,
        n_days_used=n_days,
        filter_stats=filter_stats,
        n_removed=n_removed,
        n_weight_excluded=n_weight_excluded,
        agreement_rates=agreement_rates,
        result_dir=RESULT_DIR,
    )

    # ═══════════════════════════════════════════════
    # Step 5: 통합 전략 — 일치율 기반 테마 2개 선택
    # ═══════════════════════════════════════════════
    print(f"\n[Step 5] 통합 전략 — 일치율 기반 테마 2개 선택")

    integrated_themes = select_themes_by_agreement(
        weight_dict=weight_dict,
        agreement_rates=agreement_rates,
        n_select=2,
    )

    if not integrated_themes:
        log.warning("통합 전략: 선택 가능한 테마 없음")
    else:
        print(f"\n  {'='*70}")
        print(f"  📊 통합 전략 — 선택된 테마  [{latest_date}]")
        print(f"  {'='*70}")
        print(f"  {'No':>4}  {'theme_no':>10}  {'테마명':<16}  {'일치율':>8}  {'구간':>10}")
        print(f"  {'-'*4}  {'-'*10}  {'-'*16}  {'-'*8}  {'-'*10}")
        for i, th in enumerate(integrated_themes, 1):
            rate = agreement_rates.get(str(th), 0)
            name = theme_names.get(str(th), '')
            name_disp = name[:8] if len(name) > 8 else name
            band = _agreement_band_label(rate)
            print(f"  {i:>4}  {th:>10}  {name_disp:<16}  {rate:>7.1f}%  {band:>10}")
        print(f"  {'─'*70}")

        # ═══════════════════════════════════════════════
        # Step 6: 통합 전략 — 종목 선별 (당일 수익률 기준)
        # ═══════════════════════════════════════════════
        print(f"\n[Step 6] 통합 전략 — 종목 선별 (당일 수익률 기준)")

        stock_portfolio = select_stocks_by_return(
            rebound_df=rebound_df,
            selected_themes=integrated_themes,
            latest_date=latest_date,
        )

        if stock_portfolio.empty:
            log.warning("통합 전략: 선별된 종목 없음")
        else:
            n_stocks = len(stock_portfolio)
            print(f"\n  {'='*70}")
            print(f"  📊 통합 전략 — 최종 종목 포트폴리오  [{latest_date}]")
            print(f"  {'='*70}")
            print(f"  {'No':>4}  {'종목코드':>10}  {'종목명':<14}  {'테마명':<12}  "
                  f"{'등락률':>8}  {'비중':>8}  {'기준':>10}")
            print(f"  {'-'*4}  {'-'*10}  {'-'*14}  {'-'*12}  "
                  f"{'-'*8}  {'-'*8}  {'-'*10}")

            for i, row in stock_portfolio.iterrows():
                name_disp = str(row['종목명'])[:7] if len(str(row['종목명'])) > 7 else str(row['종목명'])
                theme_disp = str(row['테마명'])[:6] if len(str(row['테마명'])) > 6 else str(row['테마명'])
                print(f"  {row['no']:>4}  {row['종목코드']:>10}  {name_disp:<14}  "
                      f"{theme_disp:<12}  {row['등락률']:>7.2f}%  "
                      f"{row['비중']*100:>7.2f}%  {row['선별기준']:>10}")

            total_w = stock_portfolio['비중'].sum()
            print(f"  {'─'*70}")
            print(f"  종목수: {n_stocks}개  |  비중합계: {total_w*100:.2f}%")

            # ── 통합 전략 결과 로그 저장 ──
            save_integrated_portfolio_log(
                selected_themes=integrated_themes,
                agreement_rates=agreement_rates,
                theme_names=theme_names,
                stock_portfolio=stock_portfolio,
                run_date=latest_date,
                result_dir=RESULT_DIR,
            )

    print(f"\n{'='*65}")
    print(f"  파이프라인 완료 — {len(weight_dict)}개 테마 비중 산출")
    if integrated_themes:
        print(f"  통합 전략 — {len(integrated_themes)}개 테마, "
              f"{len(stock_portfolio) if not stock_portfolio.empty else 0}개 종목 선별")
    print(f"{'='*65}")


# ============================================================
# 통합 전략: 일치율 기반 테마 선택 (Step A)
# ============================================================
def _agreement_band_label(rate: float) -> str:
    """일치율 구간 라벨 반환"""
    if 70 <= rate <= 100:
        return "70~100%"
    elif 0 <= rate <= 30:       return "0~30%"
    else:
        return "30~70%"


def select_themes_by_agreement(weight_dict: dict,
                                agreement_rates: dict,
                                n_select: int = 2) -> list:
    """
    GA 최적화 결과(weight_dict)에 포함된 테마들 중
    일치율 기준으로 n_select개 테마를 선택한다.

    선택 우선순위:
    1) 일치율 70~100% 구간 (일치율 내림차순)
    2) 부족하면 0~30% 구간에서 보충 (일치율 오름차순)
    3) 그래도 부족하면 일치율 상/하위로 보충

    Parameters
    ----------
    weight_dict : {theme_no → weight} GA 최적화 결과
    agreement_rates : {theme_no(str) → 일치율(float, %)}
    n_select : 선택할 테마 수

    Returns
    -------
    list : 선택된 theme_no 리스트
    """
    if not weight_dict:
        return []

    # weight_dict의 테마 목록과 일치율
    candidates = []
    for th in weight_dict.keys():
        rate = agreement_rates.get(str(th), 50.0)
        candidates.append((str(th), rate))

    # 구간별 분류
    high_band = [(th, r) for th, r in candidates if 70 <= r <= 100]
    low_band = [(th, r) for th, r in candidates if 0 <= r <= 30]

    # 정렬: 70~100%는 일치율 내림차순, 0~30%는 일치율 오름차순
    high_band.sort(key=lambda x: -x[1])
    low_band.sort(key=lambda x: x[1])

    selected = []

    # 1) 70~100% 구간 우선
    for th, r in high_band:
        if len(selected) >= n_select:
            break
        selected.append(th)

    # 2) 부족하면 0~30% 보충
    for th, r in low_band:
        if len(selected) >= n_select:
            break
        if th not in selected:
            selected.append(th)

    # 3) 그래도 부족하면 일치율 상/하위로 보충
    if len(selected) < n_select:
        # 일치율 내림차순 (상위)과 오름차순 (하위)를 번갈아 보충
        remaining = [(th, r) for th, r in candidates if th not in selected]
        remaining.sort(key=lambda x: -abs(x[1] - 50))  # 50%에서 먼 순서
        for th, r in remaining:
            if len(selected) >= n_select:
                break
            selected.append(th)

    return selected[:n_select]


# ============================================================
# 통합 전략: 종목 선별 (Step B)
# ============================================================
def select_stocks_by_return(rebound_df: pd.DataFrame,
                             selected_themes: list,
                             latest_date: str) -> pd.DataFrame:
    """
    선택된 테마의 구성종목 중 당일 수익률 기준으로 종목을 선별한다.

    선별 규칙:
    1) 등락률 -3% 이하 종목 (mean-reversion)
    2) -3% 이하 없으면 -1% 이하 종목
    3) 그래도 없으면 전체 종목 동일비중

    Parameters
    ----------
    rebound_df : beta_rebound_detail 전체 히스토리
    selected_themes : 선택된 theme_no 리스트
    latest_date : 최신 날짜 (str)

    Returns
    -------
    DataFrame : (no, 종목코드, 종목명, 테마명, 등락률, 비중, 선별기준)
    """
    df = rebound_df[rebound_df['date'] == latest_date].copy()
    if df.empty:
        return pd.DataFrame()

    df['theme_no'] = df['theme_no'].astype(str)
    df['stock_return'] = pd.to_numeric(df['등락률'], errors='coerce').fillna(0)

    # 선택된 테마의 구성종목만 필터
    theme_stocks = df[df['theme_no'].isin([str(t) for t in selected_themes])].copy()
    if theme_stocks.empty:
        return pd.DataFrame()

    # 테마명 매핑
    if '테마명' not in theme_stocks.columns:
        theme_stocks['테마명'] = ''

    # 종목코드 컬럼 확인
    code_col = '종목코드' if '종목코드' in theme_stocks.columns else '종목명'

    # 종목 중복 제거 (같은 종목이 두 테마에 있을 수 있음 → 등락률 낮은 쪽 유지)
    theme_stocks = theme_stocks.sort_values('stock_return', ascending=True)
    theme_stocks_dedup = theme_stocks.drop_duplicates(subset=[code_col], keep='first')

    # 선별 기준 적용
    tier1 = theme_stocks_dedup[theme_stocks_dedup['stock_return'] <= -3.0]
    tier2 = theme_stocks_dedup[theme_stocks_dedup['stock_return'] <= -1.0]

    if len(tier1) > 0:
        selected = tier1.copy()
        criterion = "<= -3%"
    elif len(tier2) > 0:
        selected = tier2.copy()
        criterion = "<= -1%"
    else:
        selected = theme_stocks_dedup.copy()
        criterion = "전체(동일비중)"

    log.info(f"통합 전략 종목 선별: 기준={criterion}, {len(selected)}개 종목")

    # 동일비중 배분
    n = len(selected)
    weight = 1.0 / n if n > 0 else 0.0

    result = []
    for idx, (_, row) in enumerate(selected.iterrows(), 1):
        result.append({
            'no': idx,
            '종목코드': str(row.get(code_col, '')),
            '종목명': str(row.get('종목명', '')),
            '테마명': str(row.get('테마명', '')),
            '등락률': row['stock_return'],
            '비중': weight,
            '선별기준': criterion,
        })

    return pd.DataFrame(result)


# ============================================================
# 통합 전략: 로그 저장
# ============================================================
def save_integrated_portfolio_log(selected_themes: list,
                                   agreement_rates: dict,
                                   theme_names: dict,
                                   stock_portfolio: pd.DataFrame,
                                   run_date: str,
                                   result_dir: str) -> None:
    """통합 전략 결과를 CSV 로그에 누적 저장"""
    os.makedirs(result_dir, exist_ok=True)

    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    rows = []
    for _, row in stock_portfolio.iterrows():
        rows.append({
            'timestamp': timestamp,
            'date': run_date,
            'strategy': 'integrated',
            'theme_no': row.get('테마명', ''),
            'agreement_rate': agreement_rates.get(
                str([t for t in selected_themes
                     if theme_names.get(str(t), '') == row.get('테마명', '')][0])
                if [t for t in selected_themes
                    if theme_names.get(str(t), '') == row.get('테마명', '')]
                else '', 0),
            '종목코드': row.get('종목코드', ''),
            '종목명': row.get('종목명', ''),
            '등락률': row.get('등락률', 0),
            '비중': row.get('비중', 0),
            '선별기준': row.get('선별기준', ''),
        })

    new_df = pd.DataFrame(rows)
    log_path = os.path.join(result_dir, "integrated_portfolio_log.csv")

    if os.path.exists(log_path):
        new_df.to_csv(log_path, mode='a', header=False,
                      index=False, encoding='utf-8-sig')
    else:
        new_df.to_csv(log_path, index=False, encoding='utf-8-sig')

    log.info(f"통합 전략 포트폴리오 로그 저장: {log_path}")


if __name__ == '__main__':
    main()

# python Theme_real/run_real.py