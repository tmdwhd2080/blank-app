# -*- coding: utf-8 -*-
"""한국투자증권(KIS) API — 종목별 투자자 매매동향(순매수/순매도 수량) 수집기.

라이브 검증 완료 엔드포인트:
    /uapi/domestic-stock/v1/quotations/inquire-investor   (tr_id=FHKST01010900)
    한 종목의 '일자별' 개인/외국인/기관 매수·매도·순매수 수량을 반환(약 30영업일).

투자자 구분 접두어:  prsn=개인  frgn=외국인  orgn=기관
수량 필드:
    *_shnu_vol   매수 수량        *_seln_vol   매도 수량
    *_ntby_qty   순매수 수량(= 매수 − 매도, 음수면 순매도)

검산(삼성전자 20260602 외국인): 8,263,993 − 19,280,905 = −11,016,912 ✓

사용법:
    # 키 (trading/.kis.env 또는 환경변수 KIS_APP_KEY/KIS_APP_SECRET/KIS_ENV)
    python -m trading.kis_investor 005930                 # 삼성전자 한 종목
    python -m trading.kis_investor 005930 000660 --days 5
    python -m trading.kis_investor 005930 --out out/investor_005930.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from trading.kis import KisClient, KisError


# 종목명 매핑(있으면 표시용). 없어도 동작.
try:
    from trading.arb.scanner import UNIVERSE as _NAMES  # type: ignore
except Exception:  # noqa: BLE001
    _NAMES = {}


INVESTOR_PATH = "/uapi/domestic-stock/v1/quotations/inquire-investor"
INVESTOR_TR_ID = "FHKST01010900"

# 투자자 구분: (접두어, 한글명)
INVESTORS = [("prsn", "개인"), ("frgn", "외국인"), ("orgn", "기관")]


@dataclass(frozen=True)
class InvestorFlow:
    date: str          # 일자 YYYYMMDD
    code: str
    name: str
    investor: str      # 개인/외국인/기관
    buy_qty: int       # 매수 수량
    sell_qty: int      # 매도 수량
    net_qty: int       # 순매수 수량(+순매수 / -순매도)


def _to_int(v: Any) -> int:
    if v in (None, ""):
        return 0
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def fetch_investor(client: KisClient, code: str) -> dict[str, Any]:
    """종목별 투자자 매매동향 원시 응답(일자별 ~30행)."""
    return client.get(
        INVESTOR_PATH,
        tr_id=INVESTOR_TR_ID,
        params={
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": code,
        },
    )


def parse_flows(code: str, data: dict[str, Any], *, days: int | None) -> list[InvestorFlow]:
    rows = data.get("output") or data.get("output1") or []
    if isinstance(rows, dict):
        rows = [rows]
    name = _NAMES.get(code, "")

    cutoff = None
    if days is not None:
        cutoff = (date.today() - timedelta(days=days)).strftime("%Y%m%d")

    out: list[InvestorFlow] = []
    for r in rows:
        d = r.get("stck_bsop_date", "")
        if cutoff and d < cutoff:
            continue
        for prefix, label in INVESTORS:
            out.append(
                InvestorFlow(
                    date=d,
                    code=code,
                    name=name,
                    investor=label,
                    buy_qty=_to_int(r.get(f"{prefix}_shnu_vol")),
                    sell_qty=_to_int(r.get(f"{prefix}_seln_vol")),
                    net_qty=_to_int(r.get(f"{prefix}_ntby_qty")),
                )
            )
    return out


def collect(
    client: KisClient,
    codes: list[str],
    *,
    days: int | None,
    sleep_sec: float = 0.06,
) -> list[InvestorFlow]:
    out: list[InvestorFlow] = []
    for code in codes:
        try:
            data = fetch_investor(client, code)
            out.extend(parse_flows(code, data, days=days))
        except KisError as exc:
            print(f"[skip] {code}: {exc}", file=sys.stderr)
        time.sleep(sleep_sec)  # KIS 유량제한 여유
    out.sort(key=lambda f: (f.date, f.code), reverse=True)
    return out


def _write_csv(path: Path, rows: list[InvestorFlow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["date", "code", "name", "investor",
                        "buy_qty", "sell_qty", "net_qty"],
        )
        writer.writeheader()
        for r in rows:
            writer.writerow(asdict(r))


def _print_table(rows: list[InvestorFlow]) -> None:
    print(f"\n{'일자':<10}{'종목':<9}{'투자자':<8}"
          f"{'매수수량':>13}{'매도수량':>13}{'순매수수량':>14}")
    for r in rows:
        nm = r.name or r.code
        print(f"{r.date:<10}{nm[:8]:<9}{r.investor:<8}"
              f"{r.buy_qty:>13,}{r.sell_qty:>13,}{r.net_qty:>14,}")
    print(f"\n총 {len(rows)}행")


def main() -> int:
    p = argparse.ArgumentParser(
        prog="kis_investor",
        description="KIS 종목별 투자자 순매수/순매도 수량 수집",
    )
    p.add_argument("codes", nargs="*", default=["005930"],
                   help="6자리 종목코드(여러 개 가능). 생략시 005930(삼성전자)")
    p.add_argument("--days", type=int, default=None,
                   help="최근 N일만 필터(생략시 응답 전체 ~30영업일)")
    p.add_argument("--out", type=Path, help="CSV 저장 경로(생략시 콘솔 출력)")
    args = p.parse_args()

    try:
        client = KisClient()
        rows = collect(client, args.codes, days=args.days)
    except KisError as exc:
        print(f"[kis] {exc}", file=sys.stderr)
        return 3

    if args.out:
        _write_csv(args.out, rows)
        print(f"saved {args.out} ({len(rows)} rows)")
    else:
        _print_table(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
