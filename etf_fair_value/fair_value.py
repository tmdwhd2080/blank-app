from __future__ import annotations

from dataclasses import dataclass

from etf_fair_value.models import FairValueSignal, OrderBook, bps


@dataclass(frozen=True)
class FairValueConfig:
    obi_levels: int = 3
    min_edge_bps: float = 40.0
    max_spread_bps: float = 12.0
    use_spread_filter: bool = True
    obi_weight_bps: float = 6.0
    spread_penalty_weight: float = 0.3
    adj_cap_bps: float = 6.0


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def fair_value_adjustment_bps(
    *,
    etf_obi: float,
    spread_bps: float,
    cfg: FairValueConfig = FairValueConfig(),
) -> float:
    raw = cfg.obi_weight_bps * etf_obi - cfg.spread_penalty_weight * spread_bps
    return _clip(raw, -cfg.adj_cap_bps, cfg.adj_cap_bps)


def expected_basis_bps(
    *,
    etf_obi: float,
    spread_bps: float,
    cfg: FairValueConfig = FairValueConfig(),
) -> float:
    return fair_value_adjustment_bps(
        etf_obi=etf_obi,
        spread_bps=spread_bps,
        cfg=cfg,
    )


def build_signal(
    *,
    code: str,
    nav: float,
    etf_book: OrderBook,
    etf_ofi_z: float = 0.0,
    trade_imbalance_z: float = 0.0,
    basket_obi: float = 0.0,
    basis_momentum_bps: float = 0.0,
    cfg: FairValueConfig = FairValueConfig(),
) -> FairValueSignal:
    etf_micro = etf_book.micro_price()
    etf_mid = etf_book.mid
    if not nav or not etf_mid or not etf_micro:
        raise ValueError("nav and ETF book mid/micro price are required")

    etf_obi = etf_book.depth_imbalance(cfg.obi_levels)
    current_basis = bps(etf_mid - nav, nav)
    exp_basis = expected_basis_bps(
        etf_obi=etf_obi,
        spread_bps=etf_book.spread_bps,
        cfg=cfg,
    )
    fair = nav * (1.0 + exp_basis / 10000.0)
    fair_gap = bps(fair - etf_micro, fair)
    buy_gap = bps(fair - (etf_book.ask1 or etf_micro), fair)
    sell_gap = bps((etf_book.bid1 or etf_micro) - fair, fair)

    if cfg.use_spread_filter and etf_book.spread_bps > cfg.max_spread_bps:
        decision = "HOLD"
        reason = "spread_too_wide"
    elif etf_book.ask1 and fair > etf_book.ask1 and buy_gap > cfg.min_edge_bps:
        decision = "BUY"
        reason = "fair_value_above_executable_ask"
    else:
        decision = "HOLD"
        reason = "edge_below_threshold"

    return FairValueSignal(
        code=code,
        nav=nav,
        etf_fair_value=fair,
        etf_mid=etf_mid,
        etf_micro=etf_micro,
        expected_basis_bps=exp_basis,
        current_basis_bps=current_basis,
        fair_gap_bps=fair_gap,
        executable_buy_gap_bps=buy_gap,
        executable_sell_gap_bps=sell_gap,
        spread_bps=etf_book.spread_bps,
        etf_obi=etf_obi,
        decision=decision,
        reason=reason,
    )
