from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence


DEFAULT_CREATION_UNIT_CANDIDATES = (
    1000.0,
    5000.0,
    10000.0,
    20000.0,
    50000.0,
    100000.0,
    200000.0,
    500000.0,
    1000000.0,
)


@dataclass(frozen=True)
class UnitCashEstimate:
    creation_unit: float
    cash_minus_fee: float
    raw_creation_unit: float
    method: str
    max_relative_rounding_error: float
    sample_count: int


def round_creation_unit(
    raw_unit: float,
    candidates: Sequence[float] = DEFAULT_CREATION_UNIT_CANDIDATES,
    *,
    max_rel_error: float = 0.03,
) -> tuple[float, float, str]:
    if raw_unit <= 0:
        raise ValueError(f"raw_unit must be positive, got {raw_unit}")
    nearest = min(candidates, key=lambda x: abs(x - raw_unit))
    rel_error = abs(nearest - raw_unit) / raw_unit
    if rel_error <= max_rel_error:
        return nearest, rel_error, "rounded_to_common_creation_unit"
    return raw_unit, 0.0, "unrounded_raw_creation_unit"


def estimate_from_single_nav(
    *,
    basket_value: float,
    official_nav: float,
    cash_like_amount: float = 0.0,
    creation_unit: float | None = None,
) -> UnitCashEstimate:
    if official_nav <= 0:
        raise ValueError(f"official_nav must be positive, got {official_nav}")
    raw_unit = (basket_value + cash_like_amount) / official_nav
    unit, rel_error, method = (
        (creation_unit, 0.0, "user_supplied_creation_unit")
        if creation_unit
        else round_creation_unit(raw_unit)
    )
    cash_minus_fee = official_nav * unit - basket_value
    return UnitCashEstimate(
        creation_unit=unit,
        cash_minus_fee=cash_minus_fee,
        raw_creation_unit=raw_unit,
        method=method,
        max_relative_rounding_error=rel_error,
        sample_count=1,
    )


def estimate_from_intraday_samples(
    samples: Iterable[tuple[float, float]],
    *,
    creation_unit: float | None = None,
) -> UnitCashEstimate:
    """Estimate U and C-F from (official_nav, basket_value) samples.

    The regression is:

        basket_value_t = U * official_nav_t - (C - F)

    so slope is U and intercept is -(C-F).
    """
    xs: list[float] = []
    ys: list[float] = []
    for official_nav, basket_value in samples:
        if official_nav > 0 and basket_value > 0:
            xs.append(float(official_nav))
            ys.append(float(basket_value))
    if len(xs) < 2:
        raise ValueError("at least two valid samples are required")

    if creation_unit:
        cash_values = [x * creation_unit - y for x, y in zip(xs, ys)]
        return UnitCashEstimate(
            creation_unit=creation_unit,
            cash_minus_fee=sum(cash_values) / len(cash_values),
            raw_creation_unit=creation_unit,
            method="user_unit_mean_cash_from_samples",
            max_relative_rounding_error=0.0,
            sample_count=len(xs),
        )

    n = float(len(xs))
    sx = sum(xs)
    sy = sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys))
    denom = n * sxx - sx * sx
    if abs(denom) < 1e-9:
        raise ValueError("NAV samples have too little variation to estimate U")
    raw_unit = (n * sxy - sx * sy) / denom
    intercept = (sy - raw_unit * sx) / n
    unit, rel_error, method = round_creation_unit(raw_unit)
    if method.startswith("rounded"):
        cash_values = [x * unit - y for x, y in zip(xs, ys)]
        cash_minus_fee = sum(cash_values) / len(cash_values)
    else:
        cash_minus_fee = -intercept
    return UnitCashEstimate(
        creation_unit=unit,
        cash_minus_fee=cash_minus_fee,
        raw_creation_unit=raw_unit,
        method=f"{method}_regression",
        max_relative_rounding_error=rel_error,
        sample_count=len(xs),
    )

