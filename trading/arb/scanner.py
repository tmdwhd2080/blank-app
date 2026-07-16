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

    # 5) SK하이닉스 주문 계획/키움 모의 현물 주문 스모크 테스트
    python -m trading.arb.scanner trade-hynix --paper-order
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

from trading.kis import KisClient, KisError
from trading.kis import stocks, stock_futures
from trading.arb.theory import (
    CostModel,
    ArbResult,
    evaluate_vendor,
    implied_r,
)
from trading.arb.ou import OUPosition, RollingOU


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
    codes: set[str] | None = None,
) -> list[tuple[ArbResult, float | None, float | None]]:
    """KIS 괴리율(dprt)을 신뢰하는 스캔. 선물 1콜/종목.

    my_r 지정 시: 종목별로 KIS 내재금리를 역산해 (my_r − KIS r) 만큼 조달보정.
    None 이면 KIS 괴리율을 100% 그대로 신뢰.
    """
    out: list[tuple[ArbResult, float | None, float | None]] = []
    for code, name in UNIVERSE.items():
        if codes is not None and code not in codes:
            continue
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


def _build_ou_rows(
    rows: list[tuple[ArbResult, float | None, float | None]],
    *,
    models: dict[str, RollingOU],
    positions: dict[str, OUPosition],
    args: argparse.Namespace,
    now: float,
) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for res, vol, oi in rows:
        x_bps = res.net_edge_rate * 10000.0
        model = models.setdefault(
            res.code,
            RollingOU(window_sec=args.ou_window_sec, min_samples=args.ou_min_samples),
        )
        model.add(now, x_bps)
        params = model.estimate()

        z = None
        half_life = None
        pnl_bps = None
        action = "WARMUP"
        reason = f"samples={model.sample_count}/{args.ou_min_samples}"

        if params is not None:
            z = params.z_score(x_bps)
            half_life = params.half_life_sec
            pos = positions.get(res.code)
            valid_z = z is not None
            max_half_life_ok = (
                args.ou_max_half_life_sec <= 0
                or (half_life is not None and half_life <= args.ou_max_half_life_sec)
            )

            if pos is None:
                if not valid_z:
                    action = "WAIT"
                    reason = "z_unavailable"
                elif x_bps <= args.min_edge * 10000.0:
                    action = "WAIT"
                    reason = "net_edge_below_min"
                elif z < args.ou_entry_z:
                    action = "WAIT"
                    reason = "z_below_entry"
                elif args.require_reversion and (not params.valid or half_life is None):
                    action = "WAIT"
                    reason = "reversion_invalid"
                elif args.require_reversion and half_life < args.ou_min_half_life_sec:
                    action = "WAIT"
                    reason = "half_life_too_short"
                elif args.require_reversion and not max_half_life_ok:
                    action = "WAIT"
                    reason = "half_life_too_long"
                else:
                    positions[res.code] = OUPosition(
                        entry_ts=now,
                        entry_z=z,
                        entry_x_bps=x_bps,
                        entry_half_life_sec=half_life,
                    )
                    action = "ENTER_BUY_CARRY"
                    reason = "entry_z_and_reversion" if args.require_reversion else "entry_z_only"
            else:
                held_sec = now - pos.entry_ts
                pnl_bps = pos.entry_x_bps - x_bps
                if valid_z and z <= args.ou_exit_z:
                    positions.pop(res.code, None)
                    action = "EXIT_Z"
                    reason = "z_reverted"
                elif held_sec >= args.ou_max_hold_sec:
                    positions.pop(res.code, None)
                    action = "EXIT_TIME"
                    reason = "max_hold_elapsed"
                else:
                    action = "HOLD_POSITION"
                    reason = f"held_sec={held_sec:.0f}"

        out.append(
            {
                "code": res.code,
                "name": res.name,
                "spot": res.spot,
                "futures": res.futures,
                "theo": res.theo,
                "net_edge_bps": x_bps,
                "pnl_bps": pnl_bps,
                "z": z,
                "half_life_sec": half_life,
                "volume": vol,
                "open_interest": oi,
                "action": action,
                "reason": reason,
            }
        )

    out.sort(key=lambda row: float(row["net_edge_bps"]), reverse=True)
    return out


def _print_ou_table(rows: list[dict[str, object]]) -> None:
    print(
        "\n"
        f"{'code':<8}{'name':<16}{'net_bps':>9}{'pnl_bps':>9}{'z':>8}{'hl_sec':>9}"
        f"{'spot':>11}{'future':>11}{'volume':>11}  action"
    )
    for row in rows:
        z = row["z"]
        half_life = row["half_life_sec"]
        pnl = row["pnl_bps"]
        z_text = "" if z is None else f"{float(z):.2f}"
        hl_text = "" if half_life is None else f"{float(half_life):.0f}"
        pnl_text = "" if pnl is None else f"{float(pnl):.2f}"
        print(
            f"{str(row['code']):<8}{str(row['name'])[:15]:<16}"
            f"{float(row['net_edge_bps']):>9.2f}{pnl_text:>9}{z_text:>8}{hl_text:>9}"
            f"{float(row['spot']):>11,.0f}{float(row['futures']):>11,.0f}"
            f"{float(row['volume'] or 0):>11,.0f}  {row['action']} ({row['reason']})"
        )


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


def cmd_scan_ou_aware(args) -> int:
    client = KisClient()
    cost = CostModel(
        commission_rate=args.commission,
        tax_rate=args.tax,
        slippage_rate=args.slippage,
    )
    codes = {code.strip().zfill(6) for code in args.codes.split(",") if code.strip()} if args.codes else None
    mode = "KIS disparity" if args.my_r is None else f"funding adjusted my_r={args.my_r:.4f}"
    print(
        f"[settings] mode={mode} roundtrip_cost={cost.roundtrip_rate()*100:.3f}% "
        f"min_edge={args.min_edge*100:.3f}% min_volume={args.min_volume:,.0f}"
    )
    print(f"[cost] {cost.breakdown()}")
    if args.use_ou:
        print(
            "[OU] enabled "
            f"window={args.ou_window_sec:.0f}s min_samples={args.ou_min_samples} "
            f"entry_z={args.ou_entry_z:.2f} exit_z={args.ou_exit_z:.2f} "
            f"require_reversion={args.require_reversion} "
            f"min_half_life={args.ou_min_half_life_sec:.0f}s"
        )
        if not args.loop:
            print("[OU] one-shot scan has no history; it will usually print WARMUP only.")

    print("[master] loading stock futures map...", file=sys.stderr)
    rows = stock_futures.download_stock_futures_master()
    future_map = stock_futures.front_future_by_underlying(rows)
    print(f"[master] mapped underlyings={len(future_map)}", file=sys.stderr)

    ou_models: dict[str, RollingOU] = {}
    ou_positions: dict[str, OUPosition] = {}
    while True:
        rows_out = scan_once(
            client,
            future_map,
            cost=cost,
            min_edge=args.min_edge,
            min_volume=args.min_volume,
            my_r=args.my_r,
            codes=codes,
        )
        if args.use_ou:
            ou_rows = _build_ou_rows(
                rows_out,
                models=ou_models,
                positions=ou_positions,
                args=args,
                now=time.time(),
            )
            _print_ou_table(ou_rows)
        else:
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


def _stock_future_multiplier(name: str) -> int:
    import re

    match = re.search(r"\(\s*(\d+)\s*\)", name or "")
    return int(match.group(1)) if match else 10


def cmd_trade_hynix(args) -> int:
    """Build a SK Hynix cash-and-carry order plan and optionally place the stock leg."""
    code = "000660"
    client = KisClient()
    cost = CostModel(
        commission_rate=args.commission,
        tax_rate=args.tax,
        slippage_rate=args.slippage,
    )
    rows = stock_futures.download_stock_futures_master()
    future_map = stock_futures.front_future_by_underlying(rows)
    fut_row = future_map.get(code)
    if fut_row is None:
        print("[trade-hynix] SK Hynix stock future was not found in the KIS master.", file=sys.stderr)
        return 2

    rows_out = scan_once(
        client,
        future_map,
        cost=cost,
        min_edge=args.min_edge,
        min_volume=args.min_volume,
        my_r=args.my_r,
        codes={code},
    )
    if not rows_out:
        print("[trade-hynix] no usable SK Hynix quote/result after filters.", file=sys.stderr)
        return 2

    res, volume, open_interest = rows_out[0]
    multiplier = _stock_future_multiplier(fut_row.korean_name)
    stock_qty = args.stock_qty if args.stock_qty is not None else multiplier * args.future_qty
    estimated_underlying_price = res.spot
    estimated_contract_value = res.spot * multiplier if multiplier else res.spot
    stock_price = (
        0
        if args.stock_order_type in {"3", "13", "23"}
        else int(args.stock_price or round(estimated_underlying_price))
    )
    payload: dict[str, object] = {
        "strategy": "sk_hynix_cash_and_carry",
        "true_arbitrage_supported_by_kiwoom_rest": False,
        "can_verify_arbitrage_profit_now": False,
        "profit_check_note": (
            "A true profit check requires both stock BUY and stock-future SELL fills, "
            "then an exit or mark-to-market on both legs. The current Kiwoom REST "
            "client only has domestic stock order TRs."
        ),
        "signal": res.as_row(),
        "signal_bool": res.signal,
        "volume": volume,
        "open_interest": open_interest,
        "estimated_underlying_price": estimated_underlying_price,
        "estimated_contract_value": estimated_contract_value,
        "future_contract": {
            "short_code": fut_row.short_code,
            "standard_code": fut_row.standard_code,
            "name": fut_row.korean_name,
            "expiry_yyyymm": fut_row.expiry_yyyymm,
            "multiplier": multiplier,
        },
        "legs": [
            {
                "asset": "stock",
                "side": "BUY",
                "code": code,
                "qty": stock_qty,
                "order_type": args.stock_order_type,
                "price": stock_price,
                "status": "planned",
            },
            {
                "asset": "stock_future",
                "side": "SELL",
                "code": fut_row.short_code,
                "qty": args.future_qty,
                "status": "unsupported",
                "reason": "Kiwoom REST domestic-stock guide exposes stock order TRs, not stock-future order TRs.",
            },
        ],
    }

    should_send_stock = args.paper_order and (res.signal or args.force_stock_leg)
    if args.paper_order and not should_send_stock:
        payload["stock_order_result"] = {
            "dry_run": True,
            "message": "skipped: no arbitrage signal; pass --force-stock-leg for a connectivity smoke order",
        }
    elif should_send_stock and not args.allow_unhedged_stock_leg:
        payload["stock_order_result"] = {
            "dry_run": True,
            "message": "skipped: futures leg is unsupported; pass --allow-unhedged-stock-leg to test stock-only order routing",
        }
    elif should_send_stock:
        from trading.kiwoom.order_router import KiwoomOrderRouter, OrderRequest

        router = KiwoomOrderRouter(
            dry_run=False,
            env=args.kiwoom_env,
            require_paper=not args.allow_real_order,
        )
        result = router.place(
            OrderRequest(
                side="BUY",
                code=code,
                qty=stock_qty,
                price=stock_price,
                order_type=args.stock_order_type,
                exchange=args.exchange,
            )
        )
        payload["stock_order_result"] = asdict(result)
    else:
        payload["stock_order_result"] = {
            "dry_run": True,
            "message": "dry_run: pass --paper-order to attempt Kiwoom paper stock order routing",
        }

    text = json.dumps(payload, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
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
    sp.add_argument("--codes", default="", help="Comma-separated underlyings, e.g. 000660.")
    sp.add_argument("--use-ou", action="store_true", help="Use rolling OU z-score entry/exit logic.")
    sp.add_argument("--ou-window-sec", type=float, default=3600.0, help="Rolling OU lookback window.")
    sp.add_argument("--ou-min-samples", type=int, default=20, help="Samples required before OU signals.")
    sp.add_argument("--ou-entry-z", type=float, default=1.0, help="Buy-carry entry z threshold.")
    sp.add_argument("--ou-exit-z", type=float, default=0.3, help="Exit when z reverts below this.")
    sp.add_argument("--require-reversion", action="store_true", help="Require OU-valid half-life filter for entry.")
    sp.add_argument("--ou-min-half-life-sec", type=float, default=60.0, help="Minimum half-life for entry.")
    sp.add_argument("--ou-max-half-life-sec", type=float, default=0.0, help="Optional max half-life. 0 disables.")
    sp.add_argument("--ou-max-hold-sec", type=float, default=600.0, help="Force exit after this many seconds.")
    sp.set_defaults(handler=cmd_scan_ou_aware)

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

    sp = sub.add_parser("trade-hynix", help="SK하이닉스 매수차익 계획 및 키움 모의 현물 주문")
    sp.add_argument("--my-r", type=float, default=None)
    sp.add_argument("--min-edge", type=float, default=0.0)
    sp.add_argument("--min-volume", type=float, default=100)
    sp.add_argument("--commission", type=float, default=0.00015)
    sp.add_argument("--tax", type=float, default=0.0018)
    sp.add_argument("--slippage", type=float, default=0.0005)
    sp.add_argument("--stock-qty", type=int, help="Default is future multiplier * --future-qty.")
    sp.add_argument("--future-qty", type=int, default=1)
    sp.add_argument("--stock-price", type=int, default=0, help="Limit price override. 0 uses current KIS spot.")
    sp.add_argument("--stock-order-type", default="3", help="Kiwoom trde_tp. 3=market, 0=limit.")
    sp.add_argument("--paper-order", action="store_true", help="Attempt Kiwoom paper stock-leg order routing.")
    sp.add_argument("--force-stock-leg", action="store_true", help="Send stock leg even without an arbitrage signal.")
    sp.add_argument("--allow-unhedged-stock-leg", action="store_true", help="Permit stock-only order despite unsupported futures leg.")
    sp.add_argument("--kiwoom-env", choices=["paper", "real"], default="paper")
    sp.add_argument("--exchange", choices=["KRX", "NXT", "SOR"], default="KRX")
    sp.add_argument("--allow-real-order", action="store_true")
    sp.add_argument("--out", type=Path)
    sp.set_defaults(handler=cmd_trade_hynix)

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
