"""
네이버 금융 테마 크롤링 + 베타 조정 수익률
=============================================
- 테마별 베타값(2025.11.04~2026.02.04 평균)을 하드코딩으로 보유
- 모든 테마 크롤링 + 코스피 지수 크롤링 → 코스피 수익률 계산
- 베타 조정 수익률 = 테마 등락률 - Beta × 코스피 등락률
- 베타 조정 수익률 기준 하락 테마 / 반등 테마 + 구성 종목 저장
- 저장 위치/파일명: 기존과 동일 (./theme_data/)
"""

import requests
import pandas as pd
from bs4 import BeautifulSoup
import time
import os
import sys
import glob
from datetime import datetime
import re
import logging

# repo root 를 sys.path 에 추가 (직접 실행 시에도 settings_real import 가능)
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from Theme_real.settings_real import THEME_DATA_DIR

# ─── 설정 ──────────────────────────────────────────────
SAVE_DIR = THEME_DATA_DIR  # repo/theme_data 로 통일 (cwd 무관)
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) '
                  'Chrome/125.0.0.0 Safari/537.36',
    'Referer': 'https://finance.naver.com/sise/theme.naver',
}
SLEEP_SEC = 0.5
MAX_PAGES = 20

logging.basicConfig(level=logging.INFO, format='%(asctime)s  %(message)s')
log = logging.getLogger(__name__)

os.makedirs(SAVE_DIR, exist_ok=True)


def _get(url, *, headers=None, timeout=10, retries=3, backoff=1.5):
    """requests.get 래퍼 — 간헐적 타임아웃/연결오류 시 자동 재시도(백오프).

    기존 동작과 호환: 성공하면 동일하게 Response 를 반환하고,
    재시도(retries회)를 모두 실패하면 '마지막 예외를 그대로' raise 하므로
    호출부의 try/except·반환값 사용이 전혀 바뀌지 않는다.
    """
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            return requests.get(
                url,
                headers=headers if headers is not None else HEADERS,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < retries:
                wait = backoff * attempt
                log.warning(f"요청 실패 {attempt}/{retries} ({url}) → {wait:.1f}s 후 재시도: {exc}")
                time.sleep(wait)
    raise last_exc


# ═══════════════════════════════════════════════════════
#  테마별 베타값 (BETA.py 산출, 2025.11.04~2026.02.04 평균)
#  THEME_ID(=theme_no) → Beta
# ═══════════════════════════════════════════════════════
THEME_BETA_MAP = {
    "8": 0.425976, "9": 0.525413, "12": 0.836701, "13": 0.110766,
    "14": 0.786443, "16": 0.168139, "17": 0.46072, "18": 0.468353,
    "19": 0.184669, "27": 0.344112, "28": 0.206311, "30": 0.420477,
    "31": 0.275169, "32": 0.140377, "33": 0.455069, "36": 0.287513,
    "38": 0.069492, "40": 0.415149, "41": 0.394954, "42": 0.387229,
    "44": 0.161184, "45": 0.31585, "47": -0.018279, "48": 0.200322,
    "49": 0.654288, "52": 0.298331, "55": 0.449101, "56": 0.255145,
    "59": 0.196435, "60": 0.179797, "62": 0.210304, "63": 0.083505,
    "64": 0.624611, "66": 0.343164, "72": 0.203882, "76": 0.179819,
    "79": 0.461804, "82": 0.262217, "86": 0.103404, "90": 0.173335,
    "92": 0.60181, "94": 0.644782, "97": 0.592516, "98": 0.026165,
    "99": 0.597165, "104": 0.288624, "105": 0.029914, "106": 0.327651,
    "108": 0.171586, "110": 0.142937, "111": 0.395713, "112": 0.377729,
    "113": 0.09173, "119": 0.368216, "121": 0.350191, "123": 0.770416,
    "124": 0.430958, "126": 0.117303, "127": 0.208686, "128": 0.048797,
    "136": 0.147498, "139": 0.067107, "141": 0.204126, "144": 0.536835,
    "147": 0.063337, "149": 0.237293, "151": 0.761878, "152": 0.487755,
    "154": 0.784495, "155": 1.35572, "159": 0.510147, "164": 0.330746,
    "165": 0.44694, "166": 0.357297, "167": 0.299876, "170": 0.060068,
    "171": 0.359222, "172": 0.112189, "173": 0.903146, "174": 0.314147,
    "175": 0.281361, "176": 0.202386, "177": 0.490787, "178": 1.020705,
    "180": 0.490725, "181": 0.44185, "184": 0.401481, "185": 0.656208,
    "187": 0.717944, "188": 0.623109, "191": 0.622351, "197": 0.172243,
    "200": 0.724925, "204": 0.222217, "205": 0.73439, "206": 0.201703,
    "209": 0.333236, "210": 0.192109, "213": 0.550928, "223": 0.433156,
    "227": 0.560405, "228": 0.317304, "229": 0.50188, "232": 0.135226,
    "234": 0.367844, "237": 0.491396, "241": 0.177339, "242": 0.525903,
    "250": 0.35862, "265": 0.384521, "266": 0.149005, "268": 0.287817,
    "269": 0.732768, "270": 0.349837, "272": 0.458989, "276": 0.510725,
    "279": 0.379963, "283": 0.411472, "284": 0.053159, "285": 0.185793,
    "287": 0.632153, "288": 0.338846, "289": 0.514479, "290": 0.139531,
    "294": 0.008931, "297": 0.116663, "298": 0.148761, "302": 0.300744,
    "307": 0.824204, "310": 0.148746, "311": 0.23858, "313": 0.425606,
    "316": 0.133206, "317": 0.363899, "318": 0.059804, "319": 0.128588,
    "321": 0.651058, "322": 0.336376, "323": 0.603403, "324": 0.33736,
    "325": 0.346361, "326": 0.358761, "328": 0.274735, "329": 0.803023,
    "330": 0.111751, "331": 0.534372, "332": 0.453456, "334": 0.399013,
    "335": 0.334496, "341": 0.260825, "342": 0.473814, "343": 0.408249,
    "346": 0.157381, "348": 0.46179, "349": 0.531453, "352": 0.468461,
    "362": 0.542481, "370": 1.11508, "373": 0.568068, "374": 0.185518,
    "375": 0.541896, "376": 0.255605, "377": 0.081769, "378": 0.410589,
    "379": 0.563288, "380": 0.08202, "381": 0.574881, "382": 0.633484,
    "384": 0.08108, "385": 0.492421, "386": 0.542222, "387": 0.417908,
    "388": 0.624797, "389": 0.253966, "390": 0.575023, "392": 0.688181,
    "393": 0.551409, "397": 0.231463, "398": 0.302546, "400": 0.186854,
    "401": 0.808677, "402": 0.134931, "404": 0.551849, "405": 0.637938,
    "407": 0.070259, "408": 0.161438, "415": 0.333319, "417": 0.219328,
    "421": 0.133229, "422": 0.643653, "426": 0.458166, "427": 0.191334,
    "435": 0.287755, "436": 0.200259, "445": 0.623215, "446": 0.598749,
    "447": 0.184158, "448": 0.265703, "449": 0.950035, "452": 0.477986,
    "462": 0.511028, "464": 0.341238, "467": 0.27897, "468": 0.235245,
    "470": 0.214696, "472": 0.825888, "474": 0.261037, "480": 0.46086,
    "481": 0.28742, "482": 0.593077, "483": 0.408392, "487": 0.077185,
    "488": 0.504862, "489": 0.519386, "492": 0.546574, "493": -0.002494,
    "496": 0.257391, "497": 0.460279, "500": 0.624483, "501": 0.374867,
    "503": 0.571104, "504": 0.079321, "505": 0.532839, "506": 0.022729,
    "507": 0.305335, "511": 0.35488, "513": 0.212743, "514": 0.397477,
    "516": 0.278201, "517": 0.423375, "519": 0.378031, "520": 0.824895,
    "521": 0.193589, "523": 0.779965, "524": 0.402032, "525": 0.065427,
    "527": 0.390276, "529": 0.536537, "531": 0.620891, "534": 0.261044,
    "536": 0.981374, "537": 0.567394, "539": 0.200829, "540": 0.215839,
    "543": 0.174013, "545": 0.894704, "546": 0.145236, "547": 0.856396,
    "556": 1.083941, "557": 0.713941, "559": 0.953675, "560": 0.816544,
    "563": 0.373161, "564": 0.497987, "566": 0.457181, "567": 0.438052,
    "568": 0.542066, "571": 0.456112, "574": 0.448019, "575": 0.656144,
    "576": 0.558657, "579": 0.716009, "580": 0.369213, "581": 0.207469,
    "582": 0.66569, "583": 0.499296, "584": 1.139472, "585": 0.293976,
}
# 268개 테마 (베타 없는 테마는 기본값 0.5 사용)


# ═══════════════════════════════════════════════════════
#  코스피 지수 크롤링 (네이버 모바일 JSON API)
# ═══════════════════════════════════════════════════════

def fetch_kospi_index() -> dict:
    """
    코스피 지수 현재값을 가져온다.
    반환: {'코스피지수': '2,687.45', '코스피등락': '+12.34', '코스피등락률': '+0.46', '코스피조회시간': '2026-02-23 14:30:00'}
    JSON API 실패 시 HTML 폴백.
    """
    # 방식 1: 모바일 JSON API (가장 안정적)
    try:
        url = "https://m.stock.naver.com/api/index/KOSPI/basic"
        resp = _get(url, headers={
            **HEADERS,
            'Referer': 'https://m.stock.naver.com/',
        }, timeout=5)

        if resp.status_code == 200:
            data = resp.json()
            return {
                '코스피지수': data.get('closePrice', ''),
                '코스피등락': data.get('compareToPreviousClosePrice', ''),
                '코스피등락률': data.get('fluctuationsRatio', ''),
                '코스피조회시간': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            }
    except Exception as e:
        log.warning(f"코스피 JSON API 실패: {e}")

    # 방식 2: HTML 폴백
    try:
        url = "https://finance.naver.com/sise/sise_index.naver?code=KOSPI"
        resp = _get(url, headers=HEADERS, timeout=5)
        resp.encoding = 'euc-kr'
        soup = BeautifulSoup(resp.text, 'html.parser')

        now_val = soup.select_one('#now_value')
        change_val = soup.select_one('#change_value_and_rate')

        kospi_val = now_val.get_text(strip=True) if now_val else ''
        change_text = change_val.get_text(strip=True) if change_val else ''

        return {
            '코스피지수': kospi_val,
            '코스피등락': change_text,
            '코스피등락률': '',
            '코스피조회시간': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }
    except Exception as e:
        log.warning(f"코스피 HTML 폴백도 실패: {e}")

    return {
        '코스피지수': '',
        '코스피등락': '',
        '코스피등락률': '',
        '코스피조회시간': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }


# ═══════════════════════════════════════════════════════
#  테마 크롤링 공통 함수
# ═══════════════════════════════════════════════════════

def get_last_page() -> int:
    url = "https://finance.naver.com/sise/theme.naver?&page=1"
    resp = _get(url, headers=HEADERS, timeout=10)
    resp.encoding = 'euc-kr'
    soup = BeautifulSoup(resp.text, 'html.parser')

    paging = soup.select('td.pgRR a')
    if paging:
        href = paging[0].get('href', '')
        m = re.search(r'page=(\d+)', href)
        if m:
            return int(m.group(1))

    page_links = soup.select('table.Nnavi td a, table.Nnavi td strong')
    pages = [int(t.get_text(strip=True)) for t in page_links if t.get_text(strip=True).isdigit()]
    return max(pages) if pages else 1


def parse_theme_page(page: int) -> pd.DataFrame:
    url = f"https://finance.naver.com/sise/theme.naver?&page={page}"
    resp = _get(url, headers=HEADERS, timeout=10)
    resp.encoding = 'euc-kr'
    soup = BeautifulSoup(resp.text, 'html.parser')

    rows = []
    for tr in soup.select('table.type_1 tr'):
        tds = tr.find_all('td')
        if len(tds) < 6:
            continue

        a_tag = tds[0].find('a')
        if not a_tag:
            continue

        theme_name = a_tag.get_text(strip=True)
        href = a_tag.get('href', '')
        m = re.search(r'no=(\d+)', href)
        theme_no = int(m.group(1)) if m else None

        chg_rate_text = tds[1].get_text(strip=True).replace('%', '').replace(',', '')
        try:
            chg_rate = float(chg_rate_text)
        except ValueError:
            continue

        img = tds[1].find('img')
        if img:
            src = img.get('src', '')
            if 'down' in src:
                chg_rate = -abs(chg_rate)
            elif 'up' in src:
                chg_rate = abs(chg_rate)

        avg_chg_text = tds[2].get_text(strip=True).replace('%', '').replace(',', '')
        try:
            avg_chg = float(avg_chg_text)
        except ValueError:
            avg_chg = None

        up_cnt = tds[3].get_text(strip=True) if len(tds) > 3 else ''
        flat_cnt = tds[4].get_text(strip=True) if len(tds) > 4 else ''
        down_cnt = tds[5].get_text(strip=True) if len(tds) > 5 else ''

        rows.append({
            'theme_no': theme_no,
            '테마명': theme_name,
            '등락률': chg_rate,
            '평균등락률': avg_chg,
            '상승종목수': up_cnt,
            '보합종목수': flat_cnt,
            '하락종목수': down_cnt,
        })

    return pd.DataFrame(rows)


def crawl_all_themes() -> pd.DataFrame:
    last_page = min(get_last_page(), MAX_PAGES)
    log.info(f"총 {last_page} 페이지 크롤링 시작")

    frames = []
    for p in range(1, last_page + 1):
        log.info(f"  페이지 {p}/{last_page}")
        df = parse_theme_page(p)
        if df.empty:
            log.warning(f"  페이지 {p} 데이터 없음 - 중단")
            break
        frames.append(df)
        time.sleep(SLEEP_SEC)

    if not frames:
        return pd.DataFrame()

    result = pd.concat(frames, ignore_index=True).drop_duplicates(subset=['theme_no'])
    log.info(f"총 {len(result)}개 테마 수집 완료")
    return result


def get_theme_detail(theme_no: int) -> pd.DataFrame:
    url = f"https://finance.naver.com/sise/sise_group_detail.naver?type=theme&no={theme_no}"
    resp = _get(url, headers=HEADERS, timeout=10)
    resp.encoding = 'euc-kr'
    soup = BeautifulSoup(resp.text, 'html.parser')

    table = soup.find('table', {'class': 'type_5'})
    if table is None:
        return pd.DataFrame()

    df = pd.read_html(str(table), encoding='euc-kr')[0]
    df = df.dropna(how='all').dropna(subset=['종목명'])

    codes = []
    for a in table.select('a[href*="main.naver?code="]'):
        m = re.search(r'code=(\w+)', a.get('href', ''))
        codes.append(m.group(1) if m else '')
    if len(codes) == len(df):
        df.insert(0, '종목코드', codes)

    return df


def crawl_detail_for_themes(theme_df: pd.DataFrame) -> pd.DataFrame:
    all_details = []
    total = len(theme_df)
    for idx, (_, row) in enumerate(theme_df.iterrows()):
        tno = row['theme_no']
        tname = row['테마명']
        log.info(f"  상세 크롤링: {tname} (no={tno}) [{idx+1}/{total}]")

        detail = get_theme_detail(tno)
        if not detail.empty:
            detail.insert(0, 'theme_no', tno)
            detail.insert(1, '테마명', tname)
            detail.insert(2, '테마등락률', row['등락률'])
            all_details.append(detail)
        time.sleep(SLEEP_SEC)

    if not all_details:
        return pd.DataFrame()
    return pd.concat(all_details, ignore_index=True)


def save_csv(df: pd.DataFrame, prefix: str) -> str:
    today = datetime.now().strftime('%Y%m%d')
    fname = f"{prefix}_{today}.csv"
    path = os.path.join(SAVE_DIR, fname)
    df.to_csv(path, index=False, encoding='utf-8-sig')
    log.info(f"저장 완료: {path}  ({len(df)} rows)")
    return path


def add_kospi_columns(df: pd.DataFrame, kospi_data: dict) -> pd.DataFrame:
    """DataFrame에 코스피 지수 열들을 추가한다."""
    if df.empty:
        return df
    for key, val in kospi_data.items():
        df[key] = val
    return df


# ═══════════════════════════════════════════════════════
#  베타 조정 수익률 계산
# ═══════════════════════════════════════════════════════

DEFAULT_BETA = 0.5  # 베타 맵에 없는 테마의 기본값


def compute_beta_adjusted_returns(
    all_themes: pd.DataFrame,
    kospi_chg_pct: float,
) -> pd.DataFrame:
    """
    모든 테마에 대해 베타 조정 수익률을 계산한다.
      베타조정수익률(%) = 테마등락률(%) - Beta × 코스피등락률(%)

    Parameters
    ----------
    all_themes : 크롤링된 전체 테마 DataFrame (theme_no, 등락률 등)
    kospi_chg_pct : 코스피 등락률 (%, 예: +0.46 → 0.46)

    Returns
    -------
    all_themes 에 'Beta', '베타조정수익률' 열이 추가된 DataFrame
    """
    df = all_themes.copy()
    df['Beta'] = df['theme_no'].astype(str).map(THEME_BETA_MAP).fillna(DEFAULT_BETA)
    df['베타조정수익률'] = df['등락률'] - df['Beta'] * kospi_chg_pct
    return df


# ═══════════════════════════════════════════════════════
#  전일 하락 CSV 탐색
# ═══════════════════════════════════════════════════════

def find_prev_declining_csv() -> str | None:
    """전일 베타조정 하락 CSV (beta_declining_detail_*.csv) 를 찾는다."""
    today = datetime.now().date()
    pattern = os.path.join(SAVE_DIR, "beta_declining_detail_*.csv")
    files = sorted(glob.glob(pattern), reverse=True)

    for f in files:
        m = re.search(r'beta_declining_detail_(\d{8})\.csv', f)
        if m:
            file_date = datetime.strptime(m.group(1), '%Y%m%d').date()
            if file_date < today:
                log.info(f"전일 베타조정 하락 CSV 발견: {f}")
                return f

    log.info("전일 베타조정 하락 CSV 파일 없음 → 반등 스킵")
    return None


# ═══════════════════════════════════════════════════════
#  전일 하락 → 당일 반등 필터링 (베타 조정 기준)
# ═══════════════════════════════════════════════════════

def find_rebound_themes(
    prev_declining_path: str,
    today_all_themes: pd.DataFrame,
) -> pd.DataFrame:
    """
    전일 베타조정 하락 테마 중 당일 베타조정 수익률 > 0 인 테마를 반등으로 분류한다.
    """
    prev_df = pd.read_csv(prev_declining_path, encoding='utf-8-sig')
    prev_theme_nos = set(prev_df['theme_no'].unique())
    log.info(f"전일 베타조정 하락 테마 수: {len(prev_theme_nos)}")

    rebound_themes = today_all_themes[
        (today_all_themes['theme_no'].isin(prev_theme_nos)) &
        (today_all_themes['베타조정수익률'] > 0)
    ].copy()
    rebound_themes = rebound_themes.sort_values('베타조정수익률', ascending=False)
    log.info(f"전일하락 → 당일반등(베타조정) 테마 수: {len(rebound_themes)}")

    if rebound_themes.empty:
        return pd.DataFrame()

    detail_df = crawl_detail_for_themes(rebound_themes)
    if detail_df.empty:
        return pd.DataFrame()

    # 구성 종목 등락률 파싱
    chg_col = '등락률'
    if chg_col in detail_df.columns:
        detail_df[chg_col] = pd.to_numeric(
            detail_df[chg_col].astype(str).str.replace('%', '').str.replace(',', ''),
            errors='coerce'
        )

    # 테마 레벨 베타 조정 수익률 정보 병합
    theme_info = rebound_themes[['theme_no', 'Beta', '베타조정수익률']].copy()
    detail_df = detail_df.merge(theme_info, on='theme_no', how='left')

    return detail_df


# ═══════════════════════════════════════════════════════
#  메인
# ═══════════════════════════════════════════════════════

def main():
    log.info("=" * 60)
    log.info("네이버 금융 테마 크롤링 + 베타 조정 수익률")
    log.info("=" * 60)

    # ── 코스피 지수 먼저 수집 ──
    log.info("코스피 지수 조회 중...")
    kospi_data = fetch_kospi_index()

    kospi_chg_str = kospi_data.get('코스피등락률', '0')
    try:
        kospi_chg_pct = float(str(kospi_chg_str).replace('+', '').replace('%', '').replace(',', ''))
    except (ValueError, TypeError):
        kospi_chg_pct = 0.0
        log.warning("코스피 등락률 파싱 실패 → 0으로 처리")

    log.info(
        f"코스피: {kospi_data['코스피지수']}  "
        f"등락: {kospi_data['코스피등락']}  "
        f"등락률: {kospi_chg_pct}%  "
        f"조회시간: {kospi_data['코스피조회시간']}"
    )

    # ── 전체 테마 수집 ──
    all_themes = crawl_all_themes()
    if all_themes.empty:
        log.error("테마 데이터를 수집하지 못했습니다.")
        return

    # ── 베타 조정 수익률 계산 ──
    log.info("-" * 60)
    log.info("[계산] 모든 테마 베타 조정 수익률 산출")
    all_themes = compute_beta_adjusted_returns(all_themes, kospi_chg_pct)
    all_themes = add_kospi_columns(all_themes, kospi_data)

    n_with_beta = all_themes['theme_no'].astype(str).isin(THEME_BETA_MAP).sum()
    log.info(f"베타 매핑 완료: {n_with_beta}/{len(all_themes)} 테마 (나머지 기본값 {DEFAULT_BETA})")
    log.info(f"코스피 등락률 적용: {kospi_chg_pct:+.2f}%")

    # 전체 테마 요약 저장 (베타 조정 수익률 포함)
    save_csv(all_themes, "beta_all_themes")

    # ══════════════════════════════════════════════════
    #  Step 1: 베타 조정 수익률 < 0 → 당일 하락 테마
    # ══════════════════════════════════════════════════
    log.info("-" * 60)
    log.info("[Step 1] 베타 조정 수익률 기준 당일 하락 테마 상세 크롤링")

    declining = all_themes[all_themes['베타조정수익률'] < 0].copy()
    declining = declining.sort_values('베타조정수익률', ascending=True)
    log.info(f"베타조정 기준 당일 하락 테마 수: {len(declining)}")

    if declining.empty:
        log.info("오늘 베타 조정 기준 하락한 테마가 없습니다.")
    else:
        detail_df = crawl_detail_for_themes(declining)
        if not detail_df.empty:
            # 구성 종목 등락률 파싱
            chg_col = '등락률'
            if chg_col in detail_df.columns:
                detail_df[chg_col] = pd.to_numeric(
                    detail_df[chg_col].astype(str).str.replace('%', '').str.replace(',', ''),
                    errors='coerce'
                )
            # 테마 레벨 베타 조정 수익률 정보 병합
            theme_info = declining[['theme_no', 'Beta', '베타조정수익률']].copy()
            detail_df = detail_df.merge(theme_info, on='theme_no', how='left')
            detail_df = add_kospi_columns(detail_df, kospi_data)
            save_csv(detail_df, "beta_declining_detail")

    # ══════════════════════════════════════════════════
    #  Step 2: 전일 하락 → 당일 반등 (베타 조정 기준)
    # ══════════════════════════════════════════════════
    log.info("-" * 60)
    log.info("[Step 2] 전일 베타조정 하락 → 당일 반등 필터링")

    prev_csv = find_prev_declining_csv()
    if prev_csv is None:
        log.info("전일 베타조정 하락 CSV 없음 → Step 2 스킵")
    else:
        rebound_df = find_rebound_themes(prev_csv, all_themes)
        if rebound_df.empty:
            log.info("전일하락 → 당일반등(베타조정) 해당 종목 없음")
        else:
            rebound_df = add_kospi_columns(rebound_df, kospi_data)
            save_csv(rebound_df, "beta_rebound_detail")

    log.info("=" * 60)
    log.info("크롤링 완료")
    log.info("=" * 60)


if __name__ == '__main__':
    main()

# python Theme_real/Theme_crawling.py