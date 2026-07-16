from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping


def clean_code(value: object) -> str:
    text = str(value or "").strip().upper()
    if text.startswith("A") and len(text) == 7:
        text = text[1:]
    return text.zfill(6) if text.isdigit() and len(text) <= 6 else text


def to_float(value: object, *, abs_value: bool = False) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        out = float(text)
    except ValueError:
        return None
    if abs_value:
        out = abs(out)
    return out if math.isfinite(out) else None


def bps(numerator: float, denominator: float | None) -> float:
    if not denominator:
        return 0.0
    return numerator / denominator * 10000.0


@dataclass(frozen=True)
class PdfHolding:
    code: str
    name: str = ""
    shares: float = 0.0
    amount: float = 0.0
    weight_pct: float | None = None


@dataclass(frozen=True)
class EtfStatic:
    etf_code: str
    trade_date: str
    holdings: tuple[PdfHolding, ...]
    creation_unit: float | None = None
    cash_minus_fee: float = 0.0
    cash_like_amount: float = 0.0
    source: str = "unknown"
    confidence: str = "unverified"

    @property
    def pdf_equity_amount(self) -> float:
        return sum(row.amount for row in self.holdings)

    @property
    def pdf_total_amount(self) -> float:
        return self.pdf_equity_amount + self.cash_like_amount

    def basket_value(self, price_map: Mapping[str, float] | None = None) -> float:
        if not price_map:
            return self.pdf_equity_amount
        total = 0.0
        for row in self.holdings:
            price = price_map.get(row.code)
            if price is not None and row.shares:
                total += price * row.shares
        return total

    def nav_from_prices(self, price_map: Mapping[str, float]) -> float | None:
        if not self.creation_unit:
            return None
        return (self.basket_value(price_map) + self.cash_minus_fee) / self.creation_unit


@dataclass(frozen=True)
class OrderBook:
    code: str
    ask_prices: Mapping[int, float] = field(default_factory=dict)
    bid_prices: Mapping[int, float] = field(default_factory=dict)
    ask_sizes: Mapping[int, float] = field(default_factory=dict)
    bid_sizes: Mapping[int, float] = field(default_factory=dict)
    timestamp: str = ""
    source: str = ""

    @property
    def ask1(self) -> float | None:
        return self.ask_prices.get(1)

    @property
    def bid1(self) -> float | None:
        return self.bid_prices.get(1)

    @property
    def mid(self) -> float | None:
        if self.ask1 and self.bid1:
            return (self.ask1 + self.bid1) / 2.0
        return None

    @property
    def spread(self) -> float | None:
        if self.ask1 and self.bid1:
            return self.ask1 - self.bid1
        return None

    @property
    def spread_bps(self) -> float:
        return bps(self.spread or 0.0, self.mid)

    def depth_imbalance(self, levels: int = 1) -> float:
        bid_depth = sum(float(self.bid_sizes.get(i, 0.0)) for i in range(1, levels + 1))
        ask_depth = sum(float(self.ask_sizes.get(i, 0.0)) for i in range(1, levels + 1))
        denom = bid_depth + ask_depth
        return (bid_depth - ask_depth) / denom if denom else 0.0

    def micro_price(self, levels: int = 1) -> float | None:
        if not self.ask1 or not self.bid1:
            return self.mid
        bid_qty = sum(float(self.bid_sizes.get(i, 0.0)) for i in range(1, levels + 1))
        ask_qty = sum(float(self.ask_sizes.get(i, 0.0)) for i in range(1, levels + 1))
        denom = bid_qty + ask_qty
        if denom <= 0:
            return self.mid
        return (self.ask1 * bid_qty + self.bid1 * ask_qty) / denom


@dataclass(frozen=True)
class FairValueSignal:
    code: str
    nav: float
    etf_fair_value: float
    etf_mid: float
    etf_micro: float
    expected_basis_bps: float
    current_basis_bps: float
    fair_gap_bps: float
    executable_buy_gap_bps: float
    executable_sell_gap_bps: float
    spread_bps: float
    etf_obi: float
    decision: str
    reason: str
