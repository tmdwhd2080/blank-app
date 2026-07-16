from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal


Side = Literal["BUY", "SELL"]
Env = Literal["paper", "real"]


@dataclass(frozen=True)
class OrderRequest:
    side: Side
    code: str
    qty: int
    price: int
    order_type: str = "0"
    exchange: str = "KRX"


@dataclass(frozen=True)
class OrderResult:
    dry_run: bool
    request: OrderRequest
    order_no: str = ""
    message: str = ""
    env: str = ""


class KiwoomOrderRouter:
    def __init__(
        self,
        *,
        dry_run: bool = True,
        env: Env = "paper",
        require_paper: bool = True,
    ) -> None:
        self.dry_run = dry_run
        self.env = env
        self.require_paper = require_paper
        self._client = None

    def _client_or_create(self):
        if self._client is None:
            from trading.kiwoom.http_client import KiwoomClient

            self._client = KiwoomClient(_load_config_for_env(self.env, self.require_paper))
        return self._client

    @property
    def client(self):
        return self._client_or_create()

    def place(self, request: OrderRequest) -> OrderResult:
        request = OrderRequest(
            side=request.side,
            code=_clean_code(request.code),
            qty=int(request.qty),
            price=int(request.price),
            order_type=str(request.order_type),
            exchange=str(request.exchange or "KRX"),
        )
        if request.qty <= 0:
            raise ValueError("qty must be positive")
        if request.price <= 0 and request.order_type in {"0", "5", "10", "20", "62"}:
            raise ValueError("limit order requires positive price")
        if self.dry_run:
            return OrderResult(
                dry_run=True,
                request=request,
                message="dry_run: Kiwoom order was not sent",
                env=self.env,
            )

        from trading.kiwoom.tr import order

        client = self._client_or_create()
        if request.side == "BUY":
            ack = order.buy(
                client,
                request.code,
                request.qty,
                request.price,
                trde_tp=request.order_type,  # type: ignore[arg-type]
                dmst_stex_tp=request.exchange,  # type: ignore[arg-type]
            )
        else:
            ack = order.sell(
                client,
                request.code,
                request.qty,
                request.price,
                trde_tp=request.order_type,  # type: ignore[arg-type]
                dmst_stex_tp=request.exchange,  # type: ignore[arg-type]
            )
        return OrderResult(
            dry_run=False,
            request=request,
            order_no=ack.ord_no,
            message="sent",
            env=client.config.env,
        )


def _clean_code(code: str) -> str:
    value = str(code).strip().upper()
    if value.startswith("A") and len(value) == 7 and value[1:].isdigit():
        value = value[1:]
    if value.isdigit() and len(value) <= 6:
        return value.zfill(6)
    return value


def _load_config_for_env(env: Env, require_paper: bool):
    old_env = os.environ.get("KIWOOM_ENV")
    os.environ["KIWOOM_ENV"] = env
    try:
        from trading.config import load_config

        cfg = load_config()
    finally:
        if old_env is None:
            os.environ.pop("KIWOOM_ENV", None)
        else:
            os.environ["KIWOOM_ENV"] = old_env

    if cfg.env != env:
        cache = cfg.token_cache_path
        if not os.environ.get("KIWOOM_TOKEN_CACHE"):
            cache = Path.home() / ".kiwoom" / f"token_{env}.json"
        cfg = replace(cfg, env=env, token_cache_path=cache)

    if require_paper and cfg.env != "paper":
        raise RuntimeError(
            f"refusing to send Kiwoom order to env={cfg.env}; use paper env or disable require_paper"
        )
    return cfg
