# -*- coding: utf-8 -*-
"""응답 DTO. TR 응답의 dict 를 타입 있는 객체로 한 번 변환해 사용한다.

dict 그대로 쓰면 키 오타가 런타임에서만 터지므로,
조금이라도 자주 쓰는 응답은 dataclass 로 래핑한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal


# ============================================================
#  공통 헬퍼
# ============================================================


def _D(s: str | None) -> Decimal:
    """키움이 응답에서 부호 표시를 '+' / '-' 로 prefix 하는 경우가 있어
    Decimal 변환 시 부호 문자를 안전하게 처리.
    빈 문자열 / None → 0.
    """
    if not s:
        return Decimal(0)
    s = str(s).strip().replace(",", "")
    if s.startswith("+"):
        s = s[1:]
    return Decimal(s) if s else Decimal(0)


def _I(s: str | None) -> int:
    return int(_D(s))


def _date(s: str | None) -> date | None:
    if not s or len(s) != 8:
        return None
    return datetime.strptime(s, "%Y%m%d").date()


# ============================================================
#  시세 — 일봉
# ============================================================


@dataclass(frozen=True)
class DailyCandle:
    dt: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    amount: int  # 거래대금(원)

    @classmethod
    def from_tr(cls, row: dict) -> "DailyCandle":
        # ka10081 응답 필드명
        return cls(
            dt=_date(row.get("dt")) or date.min,
            open=_D(row.get("open_pric")),
            high=_D(row.get("high_pric")),
            low=_D(row.get("low_pric")),
            close=_D(row.get("cur_prc")),
            volume=_I(row.get("trde_qty")),
            amount=_I(row.get("trde_prica")),
        )


# ============================================================
#  계좌
# ============================================================


@dataclass(frozen=True)
class Deposit:
    """예수금 — kt00001 응답 핵심 필드만 추출."""

    deposit: int                    # 예수금
    d2_estimated_deposit: int       # D+2 추정예수금
    orderable_amount: int           # 주문가능금액

    @classmethod
    def from_tr(cls, body: dict) -> "Deposit":
        return cls(
            deposit=_I(body.get("entr")),
            d2_estimated_deposit=_I(body.get("d2_entra")),
            orderable_amount=_I(body.get("ord_alow_amt")),
        )


@dataclass(frozen=True)
class Holding:
    """보유 종목 — kt00018 응답 row 1건."""

    stk_cd: str
    stk_nm: str
    quantity: int               # 보유수량
    available_quantity: int     # 매도가능수량
    avg_price: Decimal          # 평균단가
    current_price: Decimal      # 현재가
    eval_amount: int            # 평가금액
    pnl_amount: int             # 평가손익
    pnl_ratio: Decimal          # 손익률(%)

    @classmethod
    def from_tr(cls, row: dict) -> "Holding":
        return cls(
            stk_cd=str(row.get("stk_cd", "")).strip(),
            stk_nm=str(row.get("stk_nm", "")).strip(),
            quantity=_I(row.get("rmnd_qty")),
            available_quantity=_I(row.get("trde_able_qty")),
            avg_price=_D(row.get("pur_pric")),
            current_price=_D(row.get("cur_prc")),
            eval_amount=_I(row.get("evlt_amt")),
            pnl_amount=_I(row.get("evltv_prft")),
            pnl_ratio=_D(row.get("prft_rt")),
        )


# ============================================================
#  주문
# ============================================================


@dataclass(frozen=True)
class OrderAck:
    """주문 접수 응답 — 주문번호만 들고 떠난다.

    *진짜 체결 여부는 WebSocket 에서 확인해야 함.*
    """

    ord_no: str
    dmst_stex_tp: str  # KRX / NXT / SOR

    @classmethod
    def from_tr(cls, body: dict) -> "OrderAck":
        return cls(
            ord_no=str(body.get("ord_no", "")).strip(),
            dmst_stex_tp=str(body.get("dmst_stex_tp", "KRX")).strip(),
        )
