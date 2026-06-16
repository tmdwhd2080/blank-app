# -*- coding: utf-8 -*-
"""개별주식선물 마스터 + 시세 헬퍼 (KIS Open API).

⚠️ 검증 필요 2가지 (KIS 문서가 부실해 실측으로 확정):
  (1) 주식선물 마스터 파일 URL/컬럼 레이아웃
  (2) 주식선물 시세 TR_ID / FID_COND_MRKT_DIV_CODE

기본값은 합리적 추정치이며, scanner.py 의 `probe` 명령으로 실제 응답을
1회 덤프해 확정한 뒤 아래 상수만 고치면 된다. 순수 계산 로직(theory.py)은
이 검증과 무관하게 정확하다.
"""

from __future__ import annotations

import csv
import io
import shutil
import ssl
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trading.kis.client import KisClient, KisError


# ── (1) 마스터 파일 ────────────────────────────────────────
# 지수선물옵션 마스터는 fo_idx_code_mts.mst (기존 futures.py 가 사용).
# 주식선물 마스터는 통상 아래 이름. 실패하면 KIS 자료실에서 정확한 파일명 확인.
STOCK_FUTURE_MASTER_URL = (
    "https://new.real.download.dws.co.kr/common/master/fo_stk_code_mts.mst.zip"
)

# ── (2) 시세 TR ────────────────────────────────────────────
# 지수선물은 FHMIF10000000 / 시장코드 'F'. 주식선물은 시장코드 'JF' 로 추정.
# probe 로 theor_pric/dprt 필드가 정상적으로 오는지 확인 후 확정.
STOCK_FUTURE_TR_ID = "FHMIF10000000"
STOCK_FUTURE_MARKET_CODE = "JF"


@dataclass(frozen=True)
class StockFutureRow:
    """주식선물 마스터 한 행.

    실측 확인된 9컬럼 레이아웃 (지수 마스터와 동일):
      [0]상품구분 [1]단축코드 [2]표준코드 [3]한글명('금양 F 202506 ( 10)')
      [4]ATM구분 [5]행사가 [6]월물순번 [7]기초자산코드 [8]기초자산명
    """
    short_code: str            # 선물 단축코드 (시세 호출에 사용)
    standard_code: str         # 표준코드
    korean_name: str           # 한글종목명 (만기 YYYYMM 포함)
    underlying_code: str       # 기초자산(현물) 6자리 코드
    expiry_yyyymm: str         # 한글명에서 추출한 만기 YYYYMM
    strike: str                # 행사가 ('00000.00'=선물, 그 외=옵션)
    raw: tuple[str, ...]

    @classmethod
    def from_parts(cls, parts: list[str]) -> "StockFutureRow | None":
        vals = [p.strip() for p in parts]
        if len(vals) < 8:
            return None
        return cls(
            short_code=vals[1],
            standard_code=vals[2],
            korean_name=vals[3],
            underlying_code=vals[7],
            expiry_yyyymm=_extract_yyyymm(vals[3]),
            strike=vals[5] if len(vals) > 5 else "",
            raw=tuple(vals),
        )

    @property
    def is_future(self) -> bool:
        """행사가 0 = 선물, 그 외 = 옵션(콜/풋). 마스터에 옵션도 섞여 있어 구분 필요."""
        try:
            return float(self.strike) == 0.0
        except (TypeError, ValueError):
            return self.strike in ("", "00000.00")

    def expiry_date(self):
        """만기일 = 만기월 둘째 목요일."""
        from trading.arb.theory import second_thursday
        ym = self.expiry_yyyymm
        return second_thursday(int(ym[:4]), int(ym[4:6]))


def _extract_yyyymm(name: str) -> str:
    """'금양 F 202506 ( 10)' → '202506'."""
    import re
    m = re.search(r"(20\d{2})(0[1-9]|1[0-2])", name)
    return (m.group(1) + m.group(2)) if m else ""


def download_stock_futures_master() -> list[StockFutureRow]:
    """주식선물 마스터 다운로드 + 파싱 (best-effort)."""
    tmp = Path(tempfile.mkdtemp(prefix="kis_stk_fut_master_"))
    try:
        zip_path = tmp / "fo_stk_code_mts.mst.zip"
        ctx = ssl._create_unverified_context()
        with urllib.request.urlopen(STOCK_FUTURE_MASTER_URL, context=ctx, timeout=20) as src:
            zip_path.write_bytes(src.read())
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(tmp)
        mst = next(tmp.glob("*.mst"), None)
        if mst is None:
            raise KisError("stock futures master .mst not found in archive")

        rows: list[StockFutureRow] = []
        text = mst.read_text(encoding="cp949", errors="replace")
        for parts in csv.reader(io.StringIO(text), delimiter="|"):
            row = StockFutureRow.from_parts(parts)
            if row is not None:
                rows.append(row)
        return rows
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def front_future_by_underlying(
    rows: list[StockFutureRow],
    today=None,
) -> dict[str, StockFutureRow]:
    """기초자산(현물 6자리) → 근월물(만료 안 된 가장 가까운 월물) 1개 매핑."""
    from datetime import date

    today = today or date.today()
    by_underlying: dict[str, list[StockFutureRow]] = {}
    for r in rows:
        if r.underlying_code and r.expiry_yyyymm and r.is_future:
            by_underlying.setdefault(r.underlying_code, []).append(r)

    front: dict[str, StockFutureRow] = {}
    for code, cands in by_underlying.items():
        # 만기가 오늘 이후인 것 중 가장 가까운 월물
        alive = [c for c in cands if c.expiry_date() >= today]
        if not alive:
            continue
        alive.sort(key=lambda c: c.expiry_yyyymm)
        front[code] = alive[0]
    return front


def alive_futures_by_underlying(
    rows: list[StockFutureRow],
    today=None,
) -> dict[str, list[StockFutureRow]]:
    """기초자산 → 만료 안 된 모든 월물 리스트(만기 오름차순)."""
    from datetime import date

    today = today or date.today()
    by_underlying: dict[str, list[StockFutureRow]] = {}
    for r in rows:
        if r.underlying_code and r.expiry_yyyymm and r.is_future:
            by_underlying.setdefault(r.underlying_code, []).append(r)

    out: dict[str, list[StockFutureRow]] = {}
    for code, cands in by_underlying.items():
        alive = [c for c in cands if c.expiry_date() >= today]
        if alive:
            alive.sort(key=lambda c: c.expiry_yyyymm)
            out[code] = alive
    return out


# ── 시세 조회 ──────────────────────────────────────────────
def inquire_price(
    client: KisClient,
    future_code: str,
    *,
    tr_id: str = STOCK_FUTURE_TR_ID,
    market_code: str = STOCK_FUTURE_MARKET_CODE,
) -> dict[str, Any]:
    """주식선물 현재가 조회. 응답에 theor_pric(이론가)/dprt(괴리율) 포함 기대."""
    return client.get(
        "/uapi/domestic-futureoption/v1/quotations/inquire-price",
        tr_id=tr_id,
        params={
            "FID_COND_MRKT_DIV_CODE": market_code,
            "FID_INPUT_ISCD": future_code,
        },
    )


def parse_quote(data: dict[str, Any]) -> dict[str, Any]:
    """선물 응답에서 핵심 수치 추출.

    실측 확인: 데이터는 output1 에 들어오며 필드명은 아래와 같다.
      futs_prpr(선물현재가)  hts_thpr(이론가)  dprt(괴리율%)
      basis(이론베이시스)    mrkt_basis(시장베이시스=선물-현물)
      acml_vol(거래량)       hts_otst_stpl_qty(미결제약정)
      hts_rmnn_dynu(잔존일수) futs_last_tr_date(최종거래일)
    """
    o = data.get("output1") or data.get("output") or {}

    def num(key: str) -> float | None:
        v = o.get(key)
        if v in (None, ""):
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    futures = num("futs_prpr")
    mrkt_basis = num("mrkt_basis")
    # 현물 = 선물 - 시장베이시스 (KIS 동기화 현물). 현물 별도 호출 불필요.
    spot = (futures - mrkt_basis) if (futures is not None and mrkt_basis is not None) else None

    return {
        "futures": futures,                          # 선물 현재가
        "spot": spot,                                # 파생 현물가
        "vendor_theo": num("hts_thpr"),              # KIS 이론가
        "vendor_disparity": num("dprt"),             # KIS 괴리율(%)  ★ 신뢰해서 쓸 값
        "theo_basis": num("basis"),                  # 이론베이시스(이론가-현물)
        "mrkt_basis": mrkt_basis,                    # 시장베이시스(선물-현물)
        "volume": num("acml_vol"),                   # 누적 거래량
        "open_interest": num("hts_otst_stpl_qty"),   # 미결제약정
        "days": num("hts_rmnn_dynu"),                # 잔존일수
        "expiry": o.get("futs_last_tr_date"),        # 최종거래일(YYYYMMDD)
        "name": o.get("hts_kor_isnm"),
    }
