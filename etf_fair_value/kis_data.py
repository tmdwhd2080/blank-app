from __future__ import annotations

from datetime import datetime
import time
from typing import Iterable

from etf_fair_value.models import EtfStatic, OrderBook, PdfHolding, clean_code, to_float
from trading.kis import KisClient
from trading.kis import stocks as kis_stocks


DOMESTIC_STOCK_ORDERBOOK_PATH = (
    "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn"
)
DOMESTIC_STOCK_ORDERBOOK_TR_ID = "FHKST01010200"


class KisMarketData:
    def __init__(self, client: KisClient | None = None) -> None:
        self.client = client or KisClient()

    def etf_nav(self, code: str) -> float | None:
        return kis_stocks.etf_nav(self.client, clean_code(code))

    def etf_price_output(self, code: str) -> dict:
        return kis_stocks.etf_inquire_price(self.client, clean_code(code)).get("output") or {}

    def etf_components(self, code: str) -> list[dict]:
        return kis_stocks.etf_component_stocks(self.client, clean_code(code))

    def pseudo_static_from_kis_components(
        self,
        code: str,
        *,
        official_nav: float,
        trade_date: str,
    ) -> EtfStatic:
        """Build a per-share approximate basket from KIS component weights.

        KIS component rows are useful when KRX PDF login/data is unavailable, but
        they usually expose weights rather than PDF contract quantities. We turn
        each weight into a pseudo per-ETF-share quantity:

            pseudo_shares_i = NAV * weight_i / component_price_i

        This is good enough for research/probing and OBI weighting. It is not a
        substitute for the exact KRX PDF when calculating C-F and U.
        """
        rows = self.etf_components(code)
        holdings: list[PdfHolding] = []
        for row in rows:
            component_code = clean_code(row.get("stck_shrn_iscd") or row.get("mksc_shrn_iscd"))
            price = to_float(row.get("stck_prpr"), abs_value=True)
            weight = to_float(row.get("etf_cnfg_issu_rlim") or row.get("cnfg_issu_rlim"))
            if not (component_code.isdigit() and len(component_code) == 6 and price and weight):
                continue
            amount = official_nav * weight / 100.0
            holdings.append(
                PdfHolding(
                    code=component_code,
                    name=str(row.get("hts_kor_isnm") or row.get("kor_isnm") or "").strip(),
                    shares=amount / price,
                    amount=amount,
                    weight_pct=weight,
                )
            )
        if not holdings:
            raise ValueError(f"KIS component fallback returned no usable rows for {code}")
        return EtfStatic(
            etf_code=clean_code(code),
            trade_date=trade_date,
            holdings=tuple(holdings),
            creation_unit=1.0,
            cash_minus_fee=0.0,
            cash_like_amount=0.0,
            source="KIS inquire-component-stock-price pseudo basket",
            confidence="kis_weight_pseudo_per_share",
        )

    def last_price(self, code: str) -> float:
        return kis_stocks.last_price(self.client, clean_code(code))

    def last_price_map(self, codes: Iterable[str], *, sleep_sec: float = 0.0) -> dict[str, float]:
        out: dict[str, float] = {}
        code_list = list(codes)
        for i, code in enumerate(code_list):
            cleaned = clean_code(code)
            try:
                out[cleaned] = self.last_price(cleaned)
            except Exception:
                continue
            if sleep_sec > 0 and i < len(code_list) - 1:
                time.sleep(sleep_sec)
        return out

    def micro_price(self, code: str, *, levels: int = 1) -> float | None:
        return self.orderbook(code).micro_price(levels=levels)

    def micro_price_map(
        self,
        codes: Iterable[str],
        *,
        sleep_sec: float = 0.0,
        levels: int = 1,
    ) -> dict[str, float]:
        out: dict[str, float] = {}
        code_list = list(codes)
        for i, code in enumerate(code_list):
            cleaned = clean_code(code)
            try:
                price = self.micro_price(cleaned, levels=levels)
            except Exception:
                price = None
            if price:
                out[cleaned] = price
            if sleep_sec > 0 and i < len(code_list) - 1:
                time.sleep(sleep_sec)
        return out

    def orderbook(self, code: str) -> OrderBook:
        cleaned = clean_code(code)
        data = self.client.get(
            DOMESTIC_STOCK_ORDERBOOK_PATH,
            tr_id=DOMESTIC_STOCK_ORDERBOOK_TR_ID,
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": cleaned,
            },
        )
        raw = data.get("output1") or data.get("output") or {}
        ask_prices: dict[int, float] = {}
        bid_prices: dict[int, float] = {}
        ask_sizes: dict[int, float] = {}
        bid_sizes: dict[int, float] = {}
        for level in range(1, 11):
            ask_p = to_float(raw.get(f"askp{level}"), abs_value=True)
            bid_p = to_float(raw.get(f"bidp{level}"), abs_value=True)
            ask_q = to_float(raw.get(f"askp_rsqn{level}"), abs_value=True)
            bid_q = to_float(raw.get(f"bidp_rsqn{level}"), abs_value=True)
            if ask_p:
                ask_prices[level] = ask_p
            if bid_p:
                bid_prices[level] = bid_p
            if ask_q is not None:
                ask_sizes[level] = ask_q
            if bid_q is not None:
                bid_sizes[level] = bid_q
        return OrderBook(
            code=cleaned,
            ask_prices=ask_prices,
            bid_prices=bid_prices,
            ask_sizes=ask_sizes,
            bid_sizes=bid_sizes,
            timestamp=datetime.now().isoformat(timespec="milliseconds"),
            source="KIS inquire-asking-price-exp-ccn",
        )
