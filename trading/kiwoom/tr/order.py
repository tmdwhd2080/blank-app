# -*- coding: utf-8 -*-
"""
주문 TR
========
- kt10000: 주식 매수
- kt10001: 주식 매도
- kt10002: 주식 정정
- kt10003: 주식 취소

호출 결과는 OrderAck (주문번호) 만 반환한다.
*진짜 체결 여부는 WebSocket 의 '00' 실시간으로 추적해야 한다.*
"""

from __future__ import annotations

from typing import Literal

from trading.kiwoom.http_client import KiwoomClient
from trading.kiwoom.models import OrderAck


# ============================================================
#  매매 구분 (trde_tp) — 자주 쓰는 것만 enum 화
# ============================================================

TradeType = Literal[
    "0",   # 보통(지정가)
    "3",   # 시장가
    "5",   # 조건부지정가
    "6",   # 최유리지정가
    "7",   # 최우선지정가
    "10",  # 지정가 IOC
    "13",  # 시장가 IOC
    "20",  # 지정가 FOK
    "23",  # 시장가 FOK
    "61",  # 장전 시간외종가
    "62",  # 시간외 단일가
    "81",  # 장후 시간외종가
]

Exchange = Literal["KRX", "NXT", "SOR"]


def _needs_price(trde_tp: str) -> bool:
    """단가 필요한 매매 구분만 True (시장가 류는 단가 비움)."""
    return trde_tp in {"0", "5", "10", "20", "62"}


# ============================================================
#  매수 / 매도
# ============================================================


def _new_order(
    client: KiwoomClient,
    api_id: str,
    stk_cd: str,
    qty: int,
    price: int,
    trde_tp: TradeType,
    dmst_stex_tp: Exchange,
    cond_price: int | None,
) -> OrderAck:
    if qty <= 0:
        raise ValueError(f"qty must be positive, got {qty}")
    if _needs_price(trde_tp) and price <= 0:
        raise ValueError(f"trde_tp={trde_tp} requires positive price")

    body = {
        "dmst_stex_tp": dmst_stex_tp,
        "stk_cd": stk_cd,
        "ord_qty": str(qty),
        "ord_uv": str(price) if _needs_price(trde_tp) else "",
        "trde_tp": trde_tp,
        "cond_uv": str(cond_price) if cond_price else "",
    }
    resp = client.call(api_id, body)
    return OrderAck.from_tr(resp.body)


def buy(
    client: KiwoomClient,
    stk_cd: str,
    qty: int,
    price: int = 0,
    *,
    trde_tp: TradeType = "0",
    dmst_stex_tp: Exchange = "KRX",
    cond_price: int | None = None,
) -> OrderAck:
    """주식 매수. 기본 지정가."""
    return _new_order(client, "kt10000", stk_cd, qty, price, trde_tp, dmst_stex_tp, cond_price)


def sell(
    client: KiwoomClient,
    stk_cd: str,
    qty: int,
    price: int = 0,
    *,
    trde_tp: TradeType = "0",
    dmst_stex_tp: Exchange = "KRX",
    cond_price: int | None = None,
) -> OrderAck:
    """주식 매도. 기본 지정가."""
    return _new_order(client, "kt10001", stk_cd, qty, price, trde_tp, dmst_stex_tp, cond_price)


# ============================================================
#  정정 / 취소
# ============================================================


def modify(
    client: KiwoomClient,
    orig_ord_no: str,
    stk_cd: str,
    new_qty: int,
    new_price: int,
    *,
    trde_tp: TradeType = "0",
    dmst_stex_tp: Exchange = "KRX",
    new_cond_price: int | None = None,
) -> OrderAck:
    """주문 정정. orig_ord_no 는 [매수/매도] 응답의 ord_no."""
    body = {
        "dmst_stex_tp": dmst_stex_tp,
        "orig_ord_no": orig_ord_no,
        "stk_cd": stk_cd,
        "mdfy_qty": str(new_qty),
        "mdfy_uv": str(new_price),
        "trde_tp": trde_tp,
        "mdfy_cond_uv": str(new_cond_price) if new_cond_price else "",
    }
    resp = client.call("kt10002", body)
    return OrderAck.from_tr(resp.body)


def cancel(
    client: KiwoomClient,
    orig_ord_no: str,
    stk_cd: str,
    *,
    cancel_qty: int = 0,
    dmst_stex_tp: Exchange = "KRX",
) -> OrderAck:
    """주문 취소. cancel_qty=0 이면 잔량 전체 취소."""
    body = {
        "dmst_stex_tp": dmst_stex_tp,
        "orig_ord_no": orig_ord_no,
        "stk_cd": stk_cd,
        "cncl_qty": str(cancel_qty),
    }
    resp = client.call("kt10003", body)
    return OrderAck.from_tr(resp.body)
