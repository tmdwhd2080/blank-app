# -*- coding: utf-8 -*-
"""코스피 대형주 개별주식선물 매수차익 실시간 괴리율 스캐너 (KIS REST 폴링).

매수차 신호 = 선물이 '본인 조달금리 기준 이론가'보다, 왕복 거래비용을 넘는
만큼 고평가일 때. (현물 매수 + 선물 매도)

명령:
    # 1) 주식선물 마스터 원시 덤프 — 컬럼/기초자산 매핑 확인용
    python -m trading.arb.scanner probe-master --limit 30

    # 2) 종목 1개 원시 응답 덤프 — 선물 시세 TR/필드 확정용
    python -m trading.arb.scanner probe 000660

    # 3) 1회 스캔
    python -m trading.arb.scanner scan --r 0.035 --min-edge 0.0

    # 4) 실시간 반복 (15초 간격)
    python -m trading.arb.scanner scan --r 0.035 --loop 15
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from trading.kis import KisClient, KisError
from trading.kis import stocks, stock_futures
from trading.arb.theory import (
    CostModel,
    ArbResult,
    evaluate_vendor,
    implied_r,
)


# ============================================================
# 유니버스 — 코스피 대형주 (시총 상위, 주식선물 상장 다수)
#   ※ 우선주/선물 미상장 종목은 자동으로 매핑 실패 → 스킵됨.
#   ※ 정확한 '시총 상위 50'은 KIS 순위 API(시가총액상위)로 동적 조회 가능.
# ============================================================
UNIVERSE: dict[str, str] = {
    "005930": "삼성전자",     "000660": "SK하이닉스",   "373220": "LG에너지솔루션",
    "207940": "삼성바이오로직스", "005380": "현대차",     "000270": "기아",
    "068270": "셀트리온",     "005490": "POSCO홀딩스",  "035420": "NAVER",
    "051910": "LG화학",       "006400": "삼성SDI",      "035720": "카카오",
    "028260": "삼성물산",     "105560": "KB금융",       "055550": "신한지주",
    "012330": "현대모비스",   "066570": "LG전자",       "003670": "포스코퓨처엠",
    "096770": "SK이노베이션", "032830": "삼성생명",     "015760": "한국전력",
    "086790": "하나금융지주", "011200": "HMM",          "009150": "삼성전기",
    "010130": "고려아연",     "259960": "크래프톤",     "316140": "우리금융지주",
    "034730": "SK",           "018260": "삼성에스디에스", "010950": "S-Oil",
    "051900": "LG생활건강",   "024110": "기업은행",     "030200": "KT",
    "033780": "KT&G",         "017670": "SK텔레콤",     "090430": "아모레퍼시픽",
    "011070": "LG이노텍",     "161390": "한국타이어앤테크놀로지", "097950": "CJ제일제당",
    "036570": "엔씨소프트",   "047050": "포스코인터내셔널", "078930": "GS",
    "267260": "HD현대일렉트릭", "010140": "삼성중공업", "009540": "HD한국조선해양",
    "012450": "한화에어로스페이스", "042660": "한화오션", "000810": "삼성화재",
    "001570": "금양",         "302440": "SK바이오사이언스",
}


# ============================================================
# CD91 조회 (compare-r 의 KIS 내재금리 비교용)
# ============================================================
ECOS_RATE_TABLE = "817Y002"   # 시장금리(일별)


def _fetch_cd91_from_ecos() -> float:
    """한국은행 ECOS 에서 CD(91일) 최근치 조회 → 연율 소수 반환.

    환경변수 ECOS_API_KEY 필요 (https://ecos.bok.or.kr/api 에서 무료 발급).
    항목코드 표기가 바뀔 수 있어, 항목 리스트를 먼저 조회해 'CD'+'91' 이
    포함된 항목을 자동으로 찾는다. ECOS 값은 % 단위라 /100 해서 소수로 반환.
    """
    import json as _json
    import os
    import urllib.request
    from datetime import date, timedelta

    key = os.environ.get("ECOS_API_KEY", "")
    if not key:
        raise RuntimeError("ECOS_API_KEY 환경변수 없음 (ecos.bok.or.kr/api 에서 발급)")

    def _get(url: str) -> dict:
        with urllib.request.urlopen(url, timeout=15) as resp:
            return _json.loads(resp.read().decode("utf-8"))

    # 1) 817Y002 의 항목 목록에서 CD(91일) 항목코드 찾기
    list_url = f"https://ecos.bok.or.kr/api/StatisticItemList/{key}/json/kr/1/200/{ECOS_RATE_TABLE}"
    items = _get(list_url).get("StatisticItemList", {}).get("row", [])
    item_code = None
    for it in items:
        name = (it.get("ITEM_NAME") or "")
        if "CD" in name.upper() and "91" in name:
            item_code = it.get("ITEM_CODE")
            break
    if not item_code:
        raise RuntimeError(f"{ECOS_RATE_TABLE} 에서 CD91 항목을 못 찾음 (항목명 확인 필요)")

    # 2) 최근 약 2주 일별 데이터 → 가장 최신값
    end = date.today()
    start = end - timedelta(days=14)
    search_url = (
        f"https://ecos.bok.or.kr/api/StatisticSearch/{key}/json/kr/1/100/"
        f"{ECOS_RATE_TABLE}/D/{start:%Y%m%d}/{end:%Y%m%d}/{item_code}"
    )
    rows = _get(search_url).get("StatisticSearch", {}).get("row", [])
    if not rows:
        raise RuntimeError("ECOS CD91 데이터가 비어있음 (기간/항목코드 확인)")
    latest = sorted(rows, key=lambda r: r.get("TIME", ""))[-1]
    value = latest.get("DATA_VALUE")
    if value in (None, ""):
        raise RuntimeError("ECOS CD91 DATA_VALUE 없음")
    return float(value) / 100.0  # % → 소수


# ============================================================
# 스캔 1회 (잔존일수/만기는 KIS 선물 응답에서 직접 사용)
# ============================================================
def scan_once(
    client: KisClient,
    future_map: dict[str, stock_futures.StockFutureRow],
    *,
    cost: CostModel,
    min_edge: float,
    min_volume: float,
    my_r: float | None,
) -> list[tuple[ArbResult, float | None, float | None]]:
    """KIS 괴리율(dprt)을 신뢰하는 스캔. 선물 1콜/종목.

    my_r 지정 시: 종목별로 KIS 내재금리를 역산해 (my_r − KIS r) 만큼 조달보정.
    None 이면 KIS 괴리율을 100% 그대로 신뢰.
    """
    out: list[tuple[ArbResult, float | None, float | None]] = []
    for code, name in UNIVERSE.items():
        fut_row = future_map.get(code)
        if fut_row is None:
            continue  # 주식선물 미상장/매핑 실패 → 스킵
        try:
            q = stock_futures.parse_quote(stock_futures.inquire_price(client, fut_row.short_code))
        except (KisError, ValueError) as exc:
            print(f"[skip] {code} {name}: {exc}", file=sys.stderr)
            continue

        if q["futures"] is None or q["vendor_disparity"] is None or q["vendor_theo"] is None:
            continue
        if q["volume"] is None or (min_volume and q["volume"] < min_volume):
            continue  # 유동성 필터

        days = int(q["days"] or 0)
        funding_adjust = 0.0
        if my_r is not None and days > 0:
            kis_r = implied_r(spot=q["spot"], theo_price=q["vendor_theo"], days=days)
            funding_adjust = my_r - kis_r  # 본인이 더 비싸게 조달하면 양수→차익 차감

        res = evaluate_vendor(
            code=code,
            name=name,
            spot=q["spot"],
            futures=q["futures"],
            vendor_theo=q["vendor_theo"],
            vendor_disparity_pct=q["vendor_disparity"],
            days=days,
            cost=cost,
            min_edge_rate=min_edge,
            funding_adjust_annual=funding_adjust,
        )
        out.append((res, q["volume"], q["open_interest"]))
        time.sleep(0.06)  # KIS 유량제한(초당 호출 제한) 여유

    out.sort(key=lambda t: t[0].net_edge_rate, reverse=True)
    return out


def _print_table(rows: list[tuple[ArbResult, float | None, float | None]]) -> None:
    print(f"\n{'종목':<16}{'현물':>11}{'선물':>11}{'KIS이론가':>12}"
          f"{'KIS괴리%':>9}{'순차익%':>9}{'거래량':>11}  신호")
    for res, vol, _oi in rows:
        flag = "★ 매수차" if res.signal else ""
        print(f"{res.name[:15]:<16}{res.spot:>11,.0f}{res.futures:>11,.0f}{res.theo:>12,.0f}"
              f"{res.disparity_rate*100:>9.3f}{res.net_edge_rate*100:>9.3f}"
              f"{(vol or 0):>11,.0f}  {flag}")
    signals = [r for r, _, _ in rows if r.signal]
    print(f"\n신호 {len(signals)}건 / 평가 {len(rows)}종목")


# ============================================================
# CLI
# ============================================================
def cmd_scan(args) -> int:
    client = KisClient()
    cost = CostModel(
        commission_rate=args.commission,
        tax_rate=args.tax,
        slippage_rate=args.slippage,
    )
    mode = "KIS괴리율 그대로 신뢰" if args.my_r is None else f"조달보정(my_r={args.my_r:.4f})"
    print(f"[설정] 모드={mode}  왕복비용={cost.roundtrip_rate()*100:.3f}%  "
          f"min_edge={args.min_edge*100:.3f}%  min_volume={args.min_volume:,.0f}")
    print(f"[비용내역] {cost.breakdown()}")

    print("[마스터] 주식선물 매핑 로드 중...", file=sys.stderr)
    rows = stock_futures.download_stock_futures_master()
    future_map = stock_futures.front_future_by_underlying(rows)
    print(f"[마스터] 기초자산 {len(future_map)}개 매핑", file=sys.stderr)

    while True:
        rows_out = scan_once(
            client, future_map,
            cost=cost, min_edge=args.min_edge,
            min_volume=args.min_volume, my_r=args.my_r,
        )
        _print_table(rows_out)
        if not args.loop:
            return 0
        time.sleep(args.loop)


def cmd_probe(args) -> int:
    """종목 1개의 현물/선물 원시 응답을 덤프 → 필드명·TR 확정용."""
    client = KisClient()
    code = args.code

    print(f"--- 현물 {code} ---")
    print(json.dumps(stocks.inquire_price(client, code).get("output", {}),
                     ensure_ascii=False, indent=2))

    print("\n[마스터] 주식선물 매핑 로드 중...", file=sys.stderr)
    rows = stock_futures.download_stock_futures_master()
    fut = stock_futures.front_future_by_underlying(rows).get(code)
    if fut is None:
        print(f"기초자산 {code} 의 주식선물을 마스터에서 못 찾음.", file=sys.stderr)
        return 2
    print(f"--- 선물 {fut.short_code} ({fut.korean_name}) ---")
    raw = stock_futures.inquire_price(client, fut.short_code)
    print(json.dumps(raw.get("output", raw), ensure_ascii=False, indent=2))
    print("\n[파싱결과]", stock_futures.parse_quote(raw))
    return 0


def cmd_compare_r(args) -> int:
    """KIS 이론가에서 내재 r 역산 → CD91 과 비교.

    python -m trading.arb.scanner compare-r 000660
    """
    client = KisClient()
    code = args.code

    rows = stock_futures.download_stock_futures_master()
    fut = stock_futures.front_future_by_underlying(rows).get(code)
    if fut is None:
        print(f"기초자산 {code} 주식선물 매핑 실패", file=sys.stderr)
        return 2
    q = stock_futures.parse_quote(stock_futures.inquire_price(client, fut.short_code))
    vendor_theo = q["vendor_theo"]
    spot = q["spot"]
    # ★ KIS가 이론가 계산에 쓴 잔존일수(hts_rmnn_dynu)로 역산해야 내재금리가 정확
    days = int(q["days"] or 0)

    print(f"종목 {code}  현물 {spot:,.0f}  선물 {q['futures']}  "
          f"잔존 {days}일(KIS기준)  만기 {q['expiry']}")
    if not vendor_theo:
        print("KIS 응답에 theor_pric 가 없음 → probe 로 실제 필드명 확인 필요.", file=sys.stderr)
        return 2

    div = args.dividend
    r_imp = implied_r(spot=spot, theo_price=vendor_theo, days=days, dividend=div)
    print(f"KIS 이론가 theor_pric = {vendor_theo:,.2f}  (배당가정 D={div})")
    print(f"→ 내재 조달금리 r_implied = {r_imp*100:.3f}%")

    try:
        cd91 = _fetch_cd91_from_ecos()
        print(f"ECOS CD91          = {cd91*100:.3f}%")
        print(f"차이 (r_implied − CD91) = {(r_imp - cd91)*100:+.3f}%p")
        verdict = "CD91 기반으로 보임" if abs(r_imp - cd91) < 0.003 else "CD91 과 다른 금리 사용 추정"
        print(f"판정: {verdict}")
    except Exception as exc:  # noqa: BLE001
        print(f"[CD91 비교 생략] {exc}", file=sys.stderr)
        print("→ CD91 은 ECOS_API_KEY 설정 후 다시 실행하거나, kofiabond.or.kr 에서 수동 확인.")
    return 0


def cmd_probe_master(args) -> int:
    """주식선물 마스터 원시 행 덤프 → 컬럼 레이아웃 확정용."""
    rows = stock_futures.download_stock_futures_master()
    for r in rows[: args.limit]:
        print(r.short_code, "|", r.korean_name, "|", r.underlying_code, "|", r.raw[:8])
    print(f"\n총 {len(rows)}행")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="arb.scanner", description="주식선물 매수차익 스캐너")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("scan", help="KIS 괴리율 신뢰 스캔")
    sp.add_argument("--my-r", type=float, default=None,
                    help="본인 조달금리(연율 소수). 지정시 종목별 KIS내재금리와의 차이를 보정. "
                         "미지정시 KIS 괴리율 100%% 신뢰")
    sp.add_argument("--min-edge", type=float, default=0.0, help="순차익률 임계(소수). 예 0.001=0.1%%")
    sp.add_argument("--min-volume", type=float, default=100, help="최소 선물 거래량(유동성 필터)")
    sp.add_argument("--commission", type=float, default=0.00015, help="편도 수수료율")
    sp.add_argument("--tax", type=float, default=0.0018, help="증권거래세(현물 매도)")
    sp.add_argument("--slippage", type=float, default=0.0005, help="편도 슬리피지")
    sp.add_argument("--loop", type=int, default=0, help="반복 간격(초). 0=1회")
    sp.set_defaults(handler=cmd_scan)

    sp = sub.add_parser("probe", help="종목1개 원시 응답 덤프")
    sp.add_argument("code", help="현물 6자리 종목코드")
    sp.set_defaults(handler=cmd_probe)

    sp = sub.add_parser("probe-master", help="주식선물 마스터 덤프")
    sp.add_argument("--limit", type=int, default=30)
    sp.set_defaults(handler=cmd_probe_master)

    sp = sub.add_parser("compare-r", help="KIS 내재 r 역산 → CD91 비교")
    sp.add_argument("code", help="현물 6자리 종목코드")
    sp.add_argument("--dividend", type=float, default=0.0, help="만기내 배당락 DPS 가정")
    sp.set_defaults(handler=cmd_compare_r)

    return p


def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.handler(args))
    except KisError as exc:
        print(f"[kis] {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
