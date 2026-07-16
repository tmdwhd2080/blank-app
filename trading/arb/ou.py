from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from statistics import median
from typing import Deque


@dataclass(frozen=True)
class OUParams:
    mean_bps: float
    std_bps: float
    phi: float
    kappa_per_sec: float
    half_life_sec: float
    residual_std_bps: float
    sample_count: int
    valid: bool

    def z_score(self, value_bps: float) -> float | None:
        if self.std_bps <= 0:
            return None
        return (value_bps - self.mean_bps) / self.std_bps


@dataclass(frozen=True)
class OUPosition:
    entry_ts: float
    entry_z: float
    entry_x_bps: float
    entry_half_life_sec: float


class RollingOU:
    """Rolling AR(1) estimator for a mean-reverting basis series.

    The input series should already be expressed in bps. For the stock-futures
    cash-and-carry scanner we feed cost-adjusted net edge bps, so positive
    values mean the futures leg is rich enough for a buy-carry candidate.
    """

    def __init__(
        self,
        *,
        window_sec: float,
        min_samples: int,
        min_std_bps: float = 0.01,
    ) -> None:
        self.window_sec = float(window_sec)
        self.min_samples = int(min_samples)
        self.min_std_bps = float(min_std_bps)
        self._samples: Deque[tuple[float, float]] = deque()

    @property
    def sample_count(self) -> int:
        return len(self._samples)

    def add(self, ts: float, value_bps: float) -> None:
        if not math.isfinite(value_bps):
            return
        self._samples.append((float(ts), float(value_bps)))
        cutoff = float(ts) - self.window_sec
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def estimate(self) -> OUParams | None:
        samples = list(self._samples)
        if len(samples) < self.min_samples:
            return None

        ts = [row[0] for row in samples]
        xs = [row[1] for row in samples]
        n = len(xs)
        rolling_mean = sum(xs) / n
        variance = sum((x - rolling_mean) ** 2 for x in xs) / max(n - 1, 1)
        rolling_std = math.sqrt(variance)
        if rolling_std < self.min_std_bps:
            return OUParams(
                mean_bps=rolling_mean,
                std_bps=rolling_std,
                phi=float("nan"),
                kappa_per_sec=0.0,
                half_life_sec=float("inf"),
                residual_std_bps=0.0,
                sample_count=n,
                valid=False,
            )

        x0 = xs[:-1]
        x1 = xs[1:]
        m = len(x0)
        mean0 = sum(x0) / m
        mean1 = sum(x1) / m
        var0 = sum((x - mean0) ** 2 for x in x0)
        if var0 <= 0:
            return None

        cov01 = sum((a - mean0) * (b - mean1) for a, b in zip(x0, x1))
        phi = cov01 / var0
        intercept = mean1 - phi * mean0
        residuals = [b - (intercept + phi * a) for a, b in zip(x0, x1)]
        residual_var = sum(e * e for e in residuals) / max(m - 2, 1)
        residual_std = math.sqrt(residual_var)

        deltas = [b - a for a, b in zip(ts[:-1], ts[1:]) if b > a]
        dt_sec = median(deltas) if deltas else 1.0

        valid = 0.0 < phi < 1.0 and dt_sec > 0.0
        if valid:
            mean_bps = intercept / (1.0 - phi)
            kappa = -math.log(phi) / dt_sec
            half_life = math.log(2.0) / kappa if kappa > 0 else float("inf")
        else:
            mean_bps = rolling_mean
            kappa = 0.0
            half_life = float("inf")

        return OUParams(
            mean_bps=mean_bps,
            std_bps=rolling_std,
            phi=phi,
            kappa_per_sec=kappa,
            half_life_sec=half_life,
            residual_std_bps=residual_std,
            sample_count=n,
            valid=valid,
        )
