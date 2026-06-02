# -*- coding: utf-8 -*-
"""
시세 TR
========
- ka10081: 주식 일봉
- ka10080: 주식 분봉
- ka10004: 주식 호가

각 함수는 KiwoomClient 를 받고, 비즈니스에 친숙한 형태(DTO 리스트, dict 등)로 반환.
연속조회 / 수정주가 같은 잡일은 여기서 흡수한다.
"""

from __future__ import annotations

from datetime import date

from trading.kiwoom.http_client import KiwoomClient
from trading.kiwoom.models import DailyCandle


# ============================================================
#  ka10081 — 주식 일봉
# ============================================================


def daily_chart(
    client: KiwoomClient,
    stk_cd: str,
    *,
    base_dt: date | str | None = None,
    adjusted: bool = True,
    max_pages: int = 10,
) -> list[DailyCandle]:
    """일봉 차트.

    Args:
        stk_cd: 종목코드 6자리 (예: '005930').
        base_dt: 기준일. 응답은 이 날짜에서 과거 방향으로 내려감.
            None 이면 오늘. date 객체 또는 'YYYYMMDD' 문자열.
        adjusted: 수정주가 적용 여부. 백테스트 데이터는 항상 True 권장.
        max_pages: 연속조회 페이지 상한 (1페이지 ≈ 600봉).

    Returns:
        오래된 → 최근 순으로 정렬된 DailyCandle 리스트.
    """
    if isinstance(base_dt, date):
        base_dt = base_dt.strftime("%Y%m%d")
    elif base_dt is None:
        from datetime import datetime as _dt
        base_dt = _dt.now().strftime("%Y%m%d")

    body = {
        "stk_cd": stk_cd,
        "base_dt": base_dt,
        "upd_stkpc_tp": "1" if adjusted else "0",
    }
    rows = client.call_paginated(
        "ka10081",
        body,
        list_key="stk_dt_pole_chart_qry",
        max_pages=max_pages,
    )
    candles = [DailyCandle.from_tr(r) for r in rows]
    candles.sort(key=lambda c: c.dt)
    return candles


# ============================================================
#  ka10080 — 주식 분봉
# ============================================================


_VALID_TIC = {"1", "3", "5", "10", "15", "30", "45", "60"}


def minute_chart(
    client: KiwoomClient,
    stk_cd: str,
    *,
    tic_scope: str = "1",
    adjusted: bool = True,
    max_pages: int = 5,
) -> list[dict]:
    """분봉 차트. 응답 그대로 반환(필드가 일봉과 달라 별도 DTO 정의 생략)."""
    if tic_scope not in _VALID_TIC:
        raise ValueError(f"tic_scope must be one of {_VALID_TIC}, got {tic_scope!r}")

    body = {
        "stk_cd": stk_cd,
        "tic_scope": tic_scope,
        "upd_stkpc_tp": "1" if adjusted else "0",
    }
    return client.call_paginated(
        "ka10080",
        body,
        list_key="stk_min_pole_chart_qry",
        max_pages=max_pages,
    )


# ============================================================
#  ka10004 — 주식 호가
# ============================================================


def order_book(client: KiwoomClient, stk_cd: str) -> dict:
    """매수/매도 10호가. 단건 호출이라 페이지네이션 없음."""
    resp = client.call("ka10004", {"stk_cd": stk_cd})
    return resp.body


# ============================================================
#  ka20006 — 업종 일봉 (코스피/코스닥 지수 등)
# ============================================================


def sector_daily_chart(
    client: KiwoomClient,
    upjong_cd: str = "001",
    *,
    base_dt: date | str | None = None,
    max_pages: int = 10,
) -> list[DailyCandle]:
    """업종(지수) 일봉 차트.

    Args:
        upjong_cd: 업종코드. '001'=코스피, '101'=코스닥.
        base_dt: 기준일 (None=오늘). 과거 방향으로 조회.
        max_pages: 연속조회 페이지 상한.

    Returns:
        오래된 → 최근 순 DailyCandle 리스트.
    """
    if isinstance(base_dt, date):
        base_dt = base_dt.strftime("%Y%m%d")
    elif base_dt is None:
        from datetime import datetime as _dt
        base_dt = _dt.now().strftime("%Y%m%d")

    body = {
        "inds_cd": upjong_cd,
        "base_dt": base_dt,
    }
    rows = client.call_paginated(
        "ka20006",
        body,
        list_key="upjong_dt_pole_chart_qry",
        max_pages=max_pages,
    )
    candles = [DailyCandle.from_tr(r) for r in rows]
    candles.sort(key=lambda c: c.dt)
    return candles
