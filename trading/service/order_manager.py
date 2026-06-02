# -*- coding: utf-8 -*-
"""
OrderManager — 주문 상태머신
=============================
HTTP 응답만으로는 주문이 어디까지 갔는지 모른다.
WebSocket '00' (주문체결) 실시간을 같이 들어야 진짜 상태가 보임.

상태 전이:
    SUBMITTING --(HTTP 200)--> ACCEPTED
    ACCEPTED   --(WS '접수')--> WORKING
    WORKING    --(WS '체결' 일부)--> PARTIAL
    PARTIAL    --(WS '체결' 잔량 0)--> FILLED
    WORKING    --(WS '거부')--> REJECTED
    WORKING    --(WS '취소')--> CANCELLED

설계 원칙:
  - Order 객체는 frozen dataclass 가 아니라 가변 (상태 변함)
  - 모든 변경은 단일 lock 안에서 — 멀티스레드/asyncio 혼용 안전
  - 외부에는 wait_filled() / wait_status() 같은 async 인터페이스 제공
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from trading.kiwoom.http_client import KiwoomClient
from trading.kiwoom.tr import order as ord_tr
from trading.kiwoom.websocket_client import KiwoomWS


log = logging.getLogger(__name__)


# ============================================================
#  상태 / 모델
# ============================================================


class OrderStatus(str, Enum):
    SUBMITTING = "SUBMITTING"  # HTTP 응답 전
    ACCEPTED = "ACCEPTED"      # HTTP 200 받았으나 거래소 접수 미확인
    WORKING = "WORKING"        # 거래소 접수됨, 미체결 잔량 존재
    PARTIAL = "PARTIAL"        # 일부 체결
    FILLED = "FILLED"          # 전량 체결
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


@dataclass
class Order:
    stk_cd: str
    side: str  # 'BUY' / 'SELL'
    qty: int
    price: int
    trde_tp: str
    ord_no: str = ""
    status: OrderStatus = OrderStatus.SUBMITTING
    filled_qty: int = 0
    filled_avg_price: float = 0.0
    rejected_reason: str = ""
    submitted_at: datetime = field(default_factory=datetime.now)
    last_event_at: datetime = field(default_factory=datetime.now)
    raw_events: list[dict[str, Any]] = field(default_factory=list)


# ============================================================
#  매니저
# ============================================================


class OrderManager:
    def __init__(self, client: KiwoomClient, ws: KiwoomWS):
        self._client = client
        self._ws = ws
        self._orders: dict[str, Order] = {}     # ord_no → Order
        self._waiters: dict[str, list[asyncio.Event]] = {}
        self._lock = asyncio.Lock()

        # 주문체결 실시간 구독
        ws.on("00", self._on_exec_event)

    # --------------------------------------------------------
    #  공개 API
    # --------------------------------------------------------

    async def buy(self, stk_cd: str, qty: int, price: int = 0, **kw) -> Order:
        ack = await asyncio.to_thread(
            ord_tr.buy, self._client, stk_cd, qty, price, **kw
        )
        order = Order(stk_cd=stk_cd, side="BUY", qty=qty, price=price,
                      trde_tp=kw.get("trde_tp", "0"), ord_no=ack.ord_no,
                      status=OrderStatus.ACCEPTED)
        async with self._lock:
            self._orders[ack.ord_no] = order
        return order

    async def sell(self, stk_cd: str, qty: int, price: int = 0, **kw) -> Order:
        ack = await asyncio.to_thread(
            ord_tr.sell, self._client, stk_cd, qty, price, **kw
        )
        order = Order(stk_cd=stk_cd, side="SELL", qty=qty, price=price,
                      trde_tp=kw.get("trde_tp", "0"), ord_no=ack.ord_no,
                      status=OrderStatus.ACCEPTED)
        async with self._lock:
            self._orders[ack.ord_no] = order
        return order

    async def cancel(self, ord_no: str, *, cancel_qty: int = 0) -> None:
        order = self._orders.get(ord_no)
        if not order:
            raise KeyError(f"unknown ord_no: {ord_no}")
        await asyncio.to_thread(
            ord_tr.cancel, self._client, ord_no, order.stk_cd,
            cancel_qty=cancel_qty,
        )

    async def wait_done(self, ord_no: str, timeout: float = 60.0) -> Order:
        """주문이 종결 상태(FILLED/CANCELLED/REJECTED)에 도달할 때까지 대기."""
        ev = asyncio.Event()
        async with self._lock:
            self._waiters.setdefault(ord_no, []).append(ev)
            if self._is_terminal(self._orders[ord_no].status):
                ev.set()

        await asyncio.wait_for(ev.wait(), timeout=timeout)
        return self._orders[ord_no]

    def get(self, ord_no: str) -> Order | None:
        return self._orders.get(ord_no)

    # --------------------------------------------------------
    #  WebSocket 이벤트 처리
    # --------------------------------------------------------

    async def _on_exec_event(self, packet: dict[str, Any]) -> None:
        """packet 은 키움 실시간 '00' 1건. values 안에 주문 상태가 들어옴.

        키움 실시간 필드(번호) 매핑은 문서 참조:
          9201: 계좌번호 / 9203: 주문번호 / 9001: 종목코드
          913: 주문상태 (접수/확인/체결/거부) / 911: 체결수량 / 910: 체결가
          900: 주문수량 / 901: 주문가격 / 902: 미체결수량
        """
        values = packet.get("values", {})
        ord_no = str(values.get("9203", "")).strip()
        if not ord_no:
            return

        async with self._lock:
            order = self._orders.get(ord_no)
            if not order:
                # 외부에서 들어온 주문(영웅문 수동 등) — 일단 무시
                return

            order.raw_events.append(values)
            order.last_event_at = datetime.now()

            status_raw = str(values.get("913", "")).strip()
            unfilled = _to_int(values.get("902"))
            filled_qty_total = _to_int(values.get("911"))
            filled_price = _to_float(values.get("910"))

            if "거부" in status_raw or status_raw == "REJECT":
                order.status = OrderStatus.REJECTED
                order.rejected_reason = status_raw
            elif "취소" in status_raw:
                order.status = OrderStatus.CANCELLED
            elif "체결" in status_raw:
                if filled_qty_total:
                    # 평균체결가 누적 갱신
                    if order.filled_qty == 0:
                        order.filled_avg_price = filled_price
                    else:
                        order.filled_avg_price = (
                            (order.filled_avg_price * order.filled_qty
                             + filled_price * (filled_qty_total - order.filled_qty))
                            / filled_qty_total
                        )
                    order.filled_qty = filled_qty_total
                order.status = (
                    OrderStatus.FILLED if unfilled == 0 else OrderStatus.PARTIAL
                )
            elif "접수" in status_raw or "확인" in status_raw:
                order.status = OrderStatus.WORKING

            # 종결 상태면 대기자들 깨움
            if self._is_terminal(order.status):
                for ev in self._waiters.pop(ord_no, []):
                    ev.set()

        log.info("[%s] %s status=%s filled=%d/%d",
                 ord_no, order.stk_cd, order.status.value,
                 order.filled_qty, order.qty)

    @staticmethod
    def _is_terminal(s: OrderStatus) -> bool:
        return s in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED)


def _to_int(v: Any) -> int:
    if v is None or v == "":
        return 0
    s = str(v).replace(",", "").lstrip("+")
    try:
        return int(s)
    except ValueError:
        return 0


def _to_float(v: Any) -> float:
    if v is None or v == "":
        return 0.0
    s = str(v).replace(",", "").lstrip("+")
    try:
        return float(s)
    except ValueError:
        return 0.0
