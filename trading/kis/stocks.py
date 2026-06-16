# -*- coding: utf-8 -*-
"""국내주식 현물 시세 헬퍼 (KIS Open API)."""

from __future__ import annotations

from typing import Any

from trading.kis.client import KisClient


def inquire_price(client: KisClient, code: str) -> dict[str, Any]:
    """국내주식 현재가 조회.

    code : 6자리 종목코드 (예: 005930)
    응답 output 의 주요 필드: stck_prpr(현재가), stck_oprc, stck_hgpr, ...
    """
    return client.get(
        "/uapi/domestic-stock/v1/quotations/inquire-price",
        tr_id="FHKST01010100",
        params={
            "FID_COND_MRKT_DIV_CODE": "J",   # J=주식/ETF
            "FID_INPUT_ISCD": code,
        },
    )


def last_price(client: KisClient, code: str) -> float:
    """현물 현재가(체결가)만 float 로 반환."""
    data = inquire_price(client, code)
    output = data.get("output") or {}
    raw = output.get("stck_prpr")
    if raw in (None, ""):
        raise ValueError(f"no stck_prpr in response for {code}: {data}")
    return float(raw)


def etf_inquire_price(client: KisClient, code: str) -> dict[str, Any]:
    """ETF/ETN 현재가 조회 — 실시간 NAV 포함.

    code : ETF 6자리 종목코드 (예: 457990)
    응답 output 주요 필드: nav(실시간 NAV), stck_prpr(현재가),
        nav_prdy_vrss / nav_prdy_ctrt, prdy_last_nav(전일 NAV).
    Kiwoom REST 에는 실시간 NAV 가 없어 이 KIS 엔드포인트로 대체한다.
    """
    return client.get(
        "/uapi/etfetn/v1/quotations/inquire-price",
        tr_id="FHPST02400000",
        params={
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": code,
        },
    )


def etf_nav(client: KisClient, code: str) -> float | None:
    """ETF 실시간 NAV 만 float 로 반환 (없으면 None)."""
    output = etf_inquire_price(client, code).get("output") or {}
    raw = output.get("nav")
    if raw in (None, ""):
        return None
    try:
        return float(str(raw).replace(",", ""))
    except ValueError:
        return None


def etf_component_stocks(client: KisClient, code: str) -> list[dict[str, Any]]:
    """ETF 구성종목(PDF) 시세 목록.

    code : ETF 6자리 종목코드.
    응답 output2 가 구성종목 배열. 지수형 ETF·장중/장마감 시점에 따라 빈
    배열이 올 수 있으므로 호출부에서 빈 결과를 허용해야 한다.
    각 행 주요 필드: stck_shrn_iscd(종목코드), hts_kor_isnm(종목명),
        stck_prpr(현재가), etf_cnfg_issu_rlim(구성비중%), prdy_ctrt(등락률).
    """
    data = client.get(
        "/uapi/etfetn/v1/quotations/inquire-component-stock-price",
        tr_id="FHKST121600C0",
        params={
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_COND_SCR_DIV_CODE": "11216",
            "FID_INPUT_ISCD": code,
        },
    )
    rows = data.get("output2") or []
    return rows if isinstance(rows, list) else []


def inquire_daily_chart(
    client: KisClient,
    code: str,
    *,
    start: str,
    end: str,
    period: str = "D",
    market_div: str = "J",
) -> list[dict[str, Any]]:
    """국내주식/ETF 일·주·월봉 시세.

    start, end : YYYYMMDD. period : D/W/M. ETF 도 market_div='J' 로 동작.
    응답 output2 가 캔들 배열(최신→과거). 각 행: stck_bsop_date,
        stck_clpr(종가), stck_oprc, stck_hgpr, stck_lwpr, acml_vol.
    """
    data = client.get(
        "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
        tr_id="FHKST03010100",
        params={
            "FID_COND_MRKT_DIV_CODE": market_div,
            "FID_INPUT_ISCD": code,
            "FID_INPUT_DATE_1": start,
            "FID_INPUT_DATE_2": end,
            "FID_PERIOD_DIV_CODE": period,
            "FID_ORG_ADJ_PRC": "0",
        },
    )
    rows = data.get("output2") or []
    return [row for row in rows if isinstance(row, dict) and row.get("stck_clpr")]


def inquire_index_daily_chart(
    client: KisClient,
    index_code: str = "0001",
    *,
    start: str,
    end: str,
    period: str = "D",
) -> list[dict[str, Any]]:
    """업종(지수) 일봉 시세. index_code 0001=KOSPI 종합.

    응답 output2 캔들 배열. 각 행: stck_bsop_date, bstp_nmix_prpr(지수 종가).
    """
    data = client.get(
        "/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice",
        tr_id="FHKUP03500100",
        params={
            "FID_COND_MRKT_DIV_CODE": "U",
            "FID_INPUT_ISCD": index_code,
            "FID_INPUT_DATE_1": start,
            "FID_INPUT_DATE_2": end,
            "FID_PERIOD_DIV_CODE": period,
        },
    )
    rows = data.get("output2") or []
    return [row for row in rows if isinstance(row, dict) and row.get("bstp_nmix_prpr")]
