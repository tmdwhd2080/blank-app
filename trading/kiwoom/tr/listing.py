# -*- coding: utf-8 -*-
"""
종목 리스트 TR
================
- ka10099: 시장별 전종목 리스트
- ka10100: 종목정보 단건

ka10099 의 응답 필드명/리스트 키는 키움 문서 버전에 따라 조금씩 다를 수 있어
방어적으로 여러 키를 시도한다.
"""

from __future__ import annotations

from typing import Any

from trading.kiwoom.http_client import KiwoomClient


# ============================================================
#  시장 코드
# ============================================================

MARKET_CODE = {
    "kospi": "0",
    "kosdaq": "10",
    "etf": "8",
    "elw": "3",
    "konex": "50",
    "reit": "6",
}

# 코드 → 사람이 읽을 라벨
MARKET_LABEL = {v: k.upper() for k, v in MARKET_CODE.items()}


# ============================================================
#  ka10099 — 전종목 리스트
# ============================================================


def stock_list(client: KiwoomClient, market: str = "kospi") -> list[dict[str, str]]:
    """시장별 전 종목.

    Args:
        market: 'kospi' / 'kosdaq' / 'etf' / 'elw' / 'konex' / 'reit'.
                또는 키움 시장코드 문자열 직접 입력 가능.

    Returns:
        [{'code': '005930', 'name': '삼성전자', 'market': 'KOSPI'}, ...]
    """
    code = MARKET_CODE.get(market.lower(), market)
    rows: list[dict[str, Any]] = []
    cont_yn, next_key = "N", ""

    for _ in range(30):  # 안전 상한
        resp = client.call("ka10099", {"mrkt_tp": code},
                           cont_yn=cont_yn, next_key=next_key)
        chunk = _extract_list(resp.body)
        rows.extend(chunk)
        if not resp.has_next:
            break
        cont_yn, next_key = "Y", resp.next_key

    label = MARKET_LABEL.get(code, code)
    return [_normalize(r, label) for r in rows]


# ============================================================
#  내부
# ============================================================


def _extract_list(body: dict) -> list[dict]:
    """응답 본문에서 row 리스트가 들어있는 키를 자동으로 찾는다.

    문서 버전에 따라 'list' / 'output' / 'stk_lst' 등 다양하게 옴.
    """
    for k in ("list", "output", "stk_lst", "items", "data"):
        v = body.get(k)
        if isinstance(v, list):
            return v
    # 마지막 폴백: 본문에서 list-of-dict 형태 첫 번째 필드
    for v in body.values():
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return v
    return []


def _normalize(row: dict, market_label: str) -> dict[str, str]:
    """필드명을 통일. 키움 응답에서 자주 쓰이는 후보들을 모두 시도."""
    code = (
        row.get("code")
        or row.get("stk_cd")
        or row.get("symbol")
        or row.get("isu_cd")
        or ""
    )
    name = (
        row.get("name")
        or row.get("stk_nm")
        or row.get("isu_nm")
        or row.get("stk_nm_kr")
        or ""
    )
    return {
        "code": str(code).strip(),
        "name": str(name).strip(),
        "market": market_label,
    }
