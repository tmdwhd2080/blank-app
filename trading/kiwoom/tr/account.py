# -*- coding: utf-8 -*-
"""
계좌 TR
========
- kt00001: 예수금 상세현황
- kt00018: 계좌평가잔고내역
- kt00007: 실시간 미체결요청
"""

from __future__ import annotations

from trading.kiwoom.http_client import KiwoomClient
from trading.kiwoom.models import Deposit, Holding


# ============================================================
#  kt00001 — 예수금
# ============================================================


def deposit(client: KiwoomClient, qry_tp: str = "3") -> Deposit:
    """예수금 상세현황.

    qry_tp: '1' 추정조회 / '2' 일반조회 / '3' 추정+일반(기본).
    """
    resp = client.call("kt00001", {"qry_tp": qry_tp})
    return Deposit.from_tr(resp.body)


# ============================================================
#  kt00018 — 계좌평가잔고
# ============================================================


def holdings(
    client: KiwoomClient,
    qry_tp: str = "1",
    dmst_stex_tp: str = "KRX",
) -> list[Holding]:
    """보유종목 평가내역.

    qry_tp: '1' 합산 / '2' 개별
    dmst_stex_tp: 'KRX' / 'NXT' / 'SOR'
    """
    body = {"qry_tp": qry_tp, "dmst_stex_tp": dmst_stex_tp}
    rows = client.call_paginated(
        "kt00018",
        body,
        list_key="acnt_evlt_remn_indv_tot",
        max_pages=20,
    )
    return [Holding.from_tr(r) for r in rows]


# ============================================================
#  kt00007 — 미체결
# ============================================================


def open_orders(
    client: KiwoomClient,
    *,
    all_stk_tp: str = "0",  # '0' 전체 / '1' 종목
    trde_tp: str = "0",     # '0' 전체 / '1' 매도 / '2' 매수
    stex_tp: str = "0",     # '0' 통합 / '1' KRX / '2' NXT
    stk_cd: str = "",
) -> list[dict]:
    """미체결 주문 조회. 주문 매니저가 정정/취소 결정 시 사용."""
    body = {
        "all_stk_tp": all_stk_tp,
        "trde_tp": trde_tp,
        "stex_tp": stex_tp,
        "stk_cd": stk_cd,
    }
    return client.call_paginated(
        "kt00007",
        body,
        list_key="oso",
        max_pages=10,
    )
