# -*- coding: utf-8 -*-
"""
DataLoader — quant 코드가 바로 쓰는 데이터 인터페이스
========================================================
TR 모듈은 키움 응답 형태에 묶여 있고, quant 코드는 DataFrame 으로 다룬다.
이 층이 그 사이 변환과 멀티 종목 적재를 담당.

연동 예:
    loader = DataLoader()
    df = loader.daily_close(['005930', '000660'], start='20240101')
    # → date 인덱스 + 종목코드 컬럼의 close DataFrame
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime
from typing import Iterable

import pandas as pd

from trading.kiwoom.http_client import KiwoomClient
from trading.kiwoom.tr import market_data as md


log = logging.getLogger(__name__)


class DataLoader:
    def __init__(self, client: KiwoomClient | None = None):
        self._client = client or KiwoomClient()

    # --------------------------------------------------------
    #  단일 종목 → DataFrame
    # --------------------------------------------------------

    def daily_ohlcv(
        self,
        stk_cd: str,
        *,
        start: str | date | None = None,
        end: str | date | None = None,
        adjusted: bool = True,
    ) -> pd.DataFrame:
        """단일 종목 일봉 OHLCV.

        Args:
            start / end: 'YYYYMMDD' 또는 date. 둘 다 inclusive.
                키움 TR 은 end 기준으로 과거 방향 조회만 지원 → end 부터 받아서 start 로 자른다.
        """
        candles = md.daily_chart(
            self._client,
            stk_cd,
            base_dt=end,
            adjusted=adjusted,
            max_pages=20,
        )
        if not candles:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "amount"])

        df = pd.DataFrame(
            [
                {
                    "date": c.dt,
                    "open": float(c.open),
                    "high": float(c.high),
                    "low": float(c.low),
                    "close": float(c.close),
                    "volume": c.volume,
                    "amount": c.amount,
                }
                for c in candles
            ]
        )
        df = df.set_index("date").sort_index()

        if start is not None:
            start_d = _to_date(start)
            df = df[df.index >= start_d]
        if end is not None:
            end_d = _to_date(end)
            df = df[df.index <= end_d]
        return df

    # --------------------------------------------------------
    #  멀티 종목 → 종가 매트릭스
    # --------------------------------------------------------

    def daily_close(
        self,
        stk_codes: Iterable[str],
        *,
        start: str | date | None = None,
        end: str | date | None = None,
        adjusted: bool = True,
        sleep_between: float = 0.25,
    ) -> pd.DataFrame:
        """여러 종목 종가만 묶어서 반환.

        반환 형식: date 인덱스, 종목코드 컬럼.
        Factor_Momentum / Theme_real 류에 그대로 입력 가능.
        """
        frames: dict[str, pd.Series] = {}
        for code in stk_codes:
            try:
                df = self.daily_ohlcv(code, start=start, end=end, adjusted=adjusted)
                frames[code] = df["close"]
            except Exception as e:
                log.warning("[%s] failed: %s", code, e)
            time.sleep(sleep_between)  # 초당 5건 한도 안전 마진

        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, axis=1).sort_index()


# ============================================================
#  헬퍼
# ============================================================


def _to_date(v: str | date) -> date:
    if isinstance(v, date):
        return v
    return datetime.strptime(v, "%Y%m%d").date()
