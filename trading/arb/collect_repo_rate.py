# -*- coding: utf-8 -*-
"""KIS 내재 조달금리(r_b) 일별 누적 수집기 — 단독 실행.

KIS 이론가(hts_thpr)는 '스냅샷'만 제공해 과거 r_b 시계열을 받을 수 없다.
그래서 이 파일을 '매일(장중/장마감 후) 한 번' 실행해 r_b 를 역산·누적한다.
여러 만기를 함께 찍어, 만기 간 r_b 일치 여부로 KIS 사용금리를 확정할 수 있다.

역산식(D=0 기본):
    r_b = ((hts_thpr + D)/S - 1) × 365/τ          # 이론가 기반 = 정밀
    r_mkt = ((futs_prpr + D)/S - 1) × 365/τ        # 시장가 기반 = 노이즈(참고)

이 파일만 돌리면 동작한다 (키: trading/.kis.env 또는 환경변수):
    python -m trading.arb.collect_repo_rate                 # 기본 000660, 모든 만기
    python -m trading.arb.collect_repo_rate 000660 005930   # 여러 종목
    python -m trading.arb.collect_repo_rate --front-only     # 근월물만
    python -m trading.arb.collect_repo_rate --out out/my_log.csv

매일 실행하면 같은 CSV 에 '한 줄씩' append 되어 r_b 시계열이 쌓인다.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from datetime import datetime
from pathlib import Path

from trading.kis import KisClient, KisError
from trading.kis import stock_futures
from trading.arb.theory import implied_r


# 종목명(표시용). 없어도 동작.
try:
    from trading.arb.scanner import UNIVERSE as _NAMES  # type: ignore
except Exception:  # noqa: BLE001
    _NAMES = {}


DEFAULT_CODES = ["000660"]
DEFAULT_OUT = Path("out/kis_repo_rate_log.csv")

FIELDS = [
    "collected_at", "date", "code", "name", "short_code", "expiry",
    "days", "spot", "futures", "kis_theo", "dprt",
    "r_b_pct", "r_mkt_pct",
]


def collect_rows(
    client: KisClient,
    codes: list[str],
    *,
    front_only: bool,
    dividend: float,
    sleep_sec: float = 0.08,
) -> list[dict]:
    master = stock_futures.download_stock_futures_master()
    if front_only:
        fmap = stock_futures.front_future_by_underlying(master)
        contracts = {c: [fmap[c]] for c in codes if c in fmap}
    else:
        amap = stock_futures.alive_futures_by_underlying(master)
        contracts = {c: amap[c] for c in codes if c in amap}

    now = datetime.now()
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    today = now.strftime("%Y%m%d")

    rows: list[dict] = []
    for code in codes:
        futs = contracts.get(code)
        if not futs:
            print(f"[skip] {code}: 주식선물 매핑 실패", file=sys.stderr)
            continue
        name = _NAMES.get(code, "")
        for fut in futs:
            try:
                q = stock_futures.parse_quote(
                    stock_futures.inquire_price(client, fut.short_code)
                )
            except (KisError, ValueError) as exc:
                print(f"[skip] {code}/{fut.short_code}: {exc}", file=sys.stderr)
                continue

            spot, theo, days = q["spot"], q["vendor_theo"], q["days"]
            futures = q["futures"]
            if not (spot and theo and days):
                continue  # 유동성 없는 원월물 등 → 이론가/현물 없음
            days = int(days)

            r_b = implied_r(spot=spot, theo_price=theo, days=days, dividend=dividend)
            r_mkt = (
                implied_r(spot=spot, theo_price=futures, days=days, dividend=dividend)
                if futures else None
            )
            rows.append({
                "collected_at": stamp,
                "date": today,
                "code": code,
                "name": name,
                "short_code": fut.short_code,
                "expiry": q["expiry"] or fut.expiry_yyyymm,
                "days": days,
                "spot": f"{spot:.0f}",
                "futures": f"{futures:.0f}" if futures else "",
                "kis_theo": f"{theo:.0f}",
                "dprt": q["vendor_disparity"],
                "r_b_pct": round(r_b * 100, 4),
                "r_mkt_pct": round(r_mkt * 100, 4) if r_mkt is not None else "",
            })
            time.sleep(sleep_sec)  # KIS 유량제한 여유
    return rows


def append_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    with path.open("a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    p = argparse.ArgumentParser(
        prog="collect_repo_rate",
        description="KIS 내재 조달금리 r_b 일별 누적 수집",
    )
    p.add_argument("codes", nargs="*", default=DEFAULT_CODES,
                   help="6자리 종목코드(여러 개). 생략시 000660")
    p.add_argument("--front-only", action="store_true",
                   help="근월물만(기본은 만료 안 된 전 만기)")
    p.add_argument("--dividend", type=float, default=0.0,
                   help="만기내 배당락 DPS 가정(기본 0)")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT,
                   help=f"누적 CSV 경로(기본 {DEFAULT_OUT})")
    args = p.parse_args()

    try:
        client = KisClient()
        rows = collect_rows(
            client, args.codes,
            front_only=args.front_only, dividend=args.dividend,
        )
    except KisError as exc:
        print(f"[kis] {exc}", file=sys.stderr)
        return 3

    if not rows:
        print("수집된 행이 없습니다(유동성/매핑 확인).", file=sys.stderr)
        return 1

    append_csv(args.out, rows)
    print(f"appended {len(rows)} rows → {args.out}")
    for r in rows:
        nm = r["name"] or r["code"]
        print(f"  {r['expiry']}  {nm:<10} 잔존{r['days']:>3}일  "
              f"r_b={r['r_b_pct']:>7}%   r_mkt={r['r_mkt_pct']}%   dprt={r['dprt']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
