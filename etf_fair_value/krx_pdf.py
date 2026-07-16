from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from etf_fair_value.estimator import UnitCashEstimate, estimate_from_single_nav
from etf_fair_value.models import EtfStatic, PdfHolding, clean_code, to_float


_KRX_LOGIN_OK = False


def load_env_files() -> None:
    package_dir = Path(__file__).resolve().parent
    for path in (
        Path(".env"),
        Path("trading/.kis.env"),
        Path("trading/.kis.env.local"),
        Path("etf_fair_value/.env"),
        Path("etf_fair_value/.krx.env"),
        package_dir / ".env",
        package_dir / ".krx.env",
        package_dir / ".krx.env.local",
    ):
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def ensure_krx_login() -> bool:
    """Best-effort login for pykrx builds that support KRX_ID/KRX_PW."""
    global _KRX_LOGIN_OK
    if _KRX_LOGIN_OK:
        return True
    load_env_files()
    login_id = os.getenv("KRX_ID")
    login_pw = os.getenv("KRX_PW")
    if not (login_id and login_pw):
        return False
    try:
        from pykrx.website.comm.auth import build_krx_session, set_auth_session

        session = build_krx_session(login_id, login_pw)
        if session is None:
            return False
        set_auth_session(session)
        _KRX_LOGIN_OK = True
        return True
    except Exception:
        return False


def _today() -> str:
    return datetime.now().strftime("%Y%m%d")


def _scalar(value: Any) -> Any:
    if hasattr(value, "iloc"):
        try:
            if len(value) == 1:
                return value.iloc[0]
        except Exception:
            pass
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return value


def fetch_top_etfs_by_value(date: str | None = None, *, limit: int = 30) -> list[dict[str, Any]]:
    load_env_files()
    ensure_krx_login()
    from pykrx import stock

    trade_date = date or _today()
    df = stock.get_etf_ohlcv_by_ticker(trade_date)
    if df.empty:
        return []
    if "거래대금" not in df.columns:
        raise ValueError(f"pykrx ETF OHLCV did not include 거래대금: {list(df.columns)}")
    df = df.sort_values("거래대금", ascending=False).head(limit)
    rows: list[dict[str, Any]] = []
    for ticker, row in df.iterrows():
        code = clean_code(ticker)
        try:
            name = _scalar(stock.get_etf_ticker_name(code))
        except Exception:
            name = ""
        rows.append(
            {
                "date": trade_date,
                "code": code,
                "name": str(name),
                "nav": to_float(_scalar(row.get("NAV"))),
                "close": to_float(_scalar(row.get("종가"))),
                "volume": to_float(_scalar(row.get("거래량"))),
                "trading_value": to_float(_scalar(row.get("거래대금"))),
            }
        )
    return rows


def fetch_pdf_dataframe(etf_code: str, date: str | None = None):
    load_env_files()
    ensure_krx_login()
    from pykrx import stock

    return stock.get_etf_portfolio_deposit_file(clean_code(etf_code), date or _today())


def parse_pdf_dataframe(etf_code: str, date: str, df) -> EtfStatic:
    holdings: list[PdfHolding] = []
    cash_like_amount = 0.0
    for idx, row in df.iterrows():
        code = clean_code(idx)
        shares = to_float(row.get("계약수")) or 0.0
        # KRX PDF often exposes both "금액" and intraday "시가총액".
        # For same-time U/C-F calibration and NAV checks, prefer the current
        # valuation when present; fall back to static PDF amount otherwise.
        amount = to_float(row.get("시가총액"))
        if amount is None:
            amount = to_float(row.get("금액")) or 0.0
        weight = to_float(row.get("비중"))
        name = str(row.get("종목명") or row.get("한글명") or "").strip()

        if code.isdigit() and len(code) == 6 and shares:
            holdings.append(
                PdfHolding(
                    code=code,
                    name=name,
                    shares=shares,
                    amount=amount,
                    weight_pct=weight,
                )
            )
        else:
            cash_like_amount += amount

    return EtfStatic(
        etf_code=clean_code(etf_code),
        trade_date=date,
        holdings=tuple(holdings),
        cash_like_amount=cash_like_amount,
        source="pykrx.get_etf_portfolio_deposit_file",
        confidence="pdf_loaded_no_unit",
    )


def fetch_pdf_static(etf_code: str, date: str | None = None) -> EtfStatic:
    trade_date = date or _today()
    df = fetch_pdf_dataframe(etf_code, trade_date)
    if df.empty:
        raise ValueError(f"empty ETF PDF for {etf_code} on {trade_date}")
    return parse_pdf_dataframe(etf_code, trade_date, df)


def attach_unit_cash(
    static: EtfStatic,
    *,
    official_nav: float,
    creation_unit: float | None = None,
) -> tuple[EtfStatic, UnitCashEstimate]:
    estimate = estimate_from_single_nav(
        basket_value=static.pdf_equity_amount,
        cash_like_amount=static.cash_like_amount,
        official_nav=official_nav,
        creation_unit=creation_unit,
    )
    updated = replace(
        static,
        creation_unit=estimate.creation_unit,
        cash_minus_fee=estimate.cash_minus_fee,
        confidence=estimate.method,
    )
    return updated, estimate
