# -*- coding: utf-8 -*-
"""Domestic futures/options quotation helpers for KIS Open API."""

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


INDEX_FUTURE_MASTER_URL = (
    "https://new.real.download.dws.co.kr/common/master/fo_idx_code_mts.mst.zip"
)


@dataclass(frozen=True)
class FuturesMasterRow:
    product_type: str
    short_code: str
    standard_code: str
    korean_name: str
    atm_type: str
    strike: str
    month_code: str
    underlying_short_code: str
    underlying_name: str

    def as_dict(self) -> dict[str, str]:
        return {
            "product_type": self.product_type,
            "short_code": self.short_code,
            "standard_code": self.standard_code,
            "korean_name": self.korean_name,
            "atm_type": self.atm_type,
            "strike": self.strike,
            "month_code": self.month_code,
            "underlying_short_code": self.underlying_short_code,
            "underlying_name": self.underlying_name,
        }


def inquire_price(
    client: KisClient,
    code: str,
    *,
    market_code: str = "F",
) -> dict[str, Any]:
    """선물옵션 시세 조회. market_code F=지수선물, O=지수옵션."""

    return client.get(
        "/uapi/domestic-futureoption/v1/quotations/inquire-price",
        tr_id="FHMIF10000000",
        params={
            "FID_COND_MRKT_DIV_CODE": market_code,
            "FID_INPUT_ISCD": code,
        },
    )


def inquire_asking_price(
    client: KisClient,
    code: str,
    *,
    market_code: str = "F",
) -> dict[str, Any]:
    """선물옵션 시세호가 조회."""

    return client.get(
        "/uapi/domestic-futureoption/v1/quotations/inquire-asking-price",
        tr_id="FHMIF10010000",
        params={
            "FID_COND_MRKT_DIV_CODE": market_code,
            "FID_INPUT_ISCD": code,
        },
    )


def download_index_futures_master() -> list[FuturesMasterRow]:
    """Download and parse KIS index futures/options master rows."""

    tmp = Path(tempfile.mkdtemp(prefix="kis_futures_master_"))
    try:
        zip_path = tmp / "fo_idx_code_mts.mst.zip"
        ctx = ssl._create_unverified_context()
        with urllib.request.urlopen(INDEX_FUTURE_MASTER_URL, context=ctx, timeout=20) as src:
            zip_path.write_bytes(src.read())

        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(tmp)

        mst_path = tmp / "fo_idx_code_mts.mst"
        if not mst_path.exists():
            raise KisError("downloaded futures master did not contain fo_idx_code_mts.mst")

        rows: list[FuturesMasterRow] = []
        text = mst_path.read_text(encoding="cp949", errors="replace")
        reader = csv.reader(io.StringIO(text), delimiter="|")
        for parts in reader:
            if len(parts) < 9:
                continue
            values = [part.strip() for part in parts[:9]]
            rows.append(FuturesMasterRow(*values))
        return rows
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def pick_front_kospi200_future(rows: list[FuturesMasterRow]) -> FuturesMasterRow:
    """Pick the first KOSPI200 index futures row from the current master."""

    candidates = [
        row
        for row in rows
        if row.product_type == "1"
        and row.underlying_name == "KOSPI200"
        and row.korean_name.startswith("F ")
        and row.short_code.startswith("A01")
    ]
    if not candidates:
        candidates = [
            row
            for row in rows
            if row.underlying_name == "KOSPI200" and row.korean_name.startswith("F ")
        ]
    if not candidates:
        raise KisError("could not find a KOSPI200 futures code in the master file")
    candidates.sort(key=lambda row: row.korean_name)
    return candidates[0]
