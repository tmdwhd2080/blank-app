# -*- coding: utf-8 -*-
"""
KiwoomWS — 실시간 데이터 (asyncio + websockets)
================================================
용도:
  - 시세 실시간 (0B 체결, 0D 호가 등): 종목 등록 후 푸시 수신
  - 주문체결 실시간 (00, 04): 토큰만 있으면 자동 수신 — 주문 매니저 핵심

설계:
  - 단일 연결로 시세/주문 모두 처리
  - 콜백 등록 방식 (on_message / on_real_event)
  - 재연결 자동 (ping 실패, 끊김 감지)
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

import websockets
from websockets.exceptions import ConnectionClosed

from trading.config import KiwoomConfig, load_config
from trading.kiwoom.auth import TokenManager


log = logging.getLogger(__name__)


RealCallback = Callable[[dict[str, Any]], Awaitable[None]]


class KiwoomWS:
    def __init__(self, config: KiwoomConfig | None = None):
        self._cfg = config or load_config()
        self._token_mgr = TokenManager(self._cfg)
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._handlers: dict[str, list[RealCallback]] = {}
        self._stop = asyncio.Event()

    # --------------------------------------------------------
    #  콜백 등록
    # --------------------------------------------------------

    def on(self, real_type: str, handler: RealCallback) -> None:
        """실시간 TR 코드별 핸들러 등록.

        real_type 예시:
            '0B' 주식 체결 / '0D' 호가 / '0A' 시고저
            '00' 주문체결 / '04' 잔고
        """
        self._handlers.setdefault(real_type, []).append(handler)

    # --------------------------------------------------------
    #  연결 / 메인 루프
    # --------------------------------------------------------

    async def run(self) -> None:
        """연결 + 로그인 + 수신 루프. 끊기면 자동 재연결."""
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(self._cfg.ws_url) as ws:
                    self._ws = ws
                    await self._login()
                    backoff = 1.0
                    await self._recv_loop()
            except (ConnectionClosed, OSError) as e:
                log.warning("ws disconnected: %s — reconnect in %.1fs", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def stop(self) -> None:
        self._stop.set()

    async def _login(self) -> None:
        assert self._ws is not None
        msg = {"trnm": "LOGIN", "token": self._token_mgr.get_token()}
        await self._ws.send(json.dumps(msg))
        # 응답 1회 처리
        raw = await self._ws.recv()
        data = json.loads(raw)
        if data.get("return_code") != 0:
            raise RuntimeError(f"ws login failed: {data}")
        log.info("ws login ok (env=%s)", self._cfg.env)

    async def _recv_loop(self) -> None:
        assert self._ws is not None
        async for raw in self._ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("non-json frame: %s", raw[:200])
                continue

            trnm = msg.get("trnm")
            if trnm == "PING":
                # 키움 서버가 주기적으로 PING 보냄 → 그대로 echo
                await self._ws.send(raw)
                continue

            if trnm == "REAL":
                # data 는 보통 list. 실시간 패킷 하나마다 type / item / values
                for item in msg.get("data", []):
                    real_type = item.get("type")
                    for h in self._handlers.get(real_type, []):
                        try:
                            await h(item)
                        except Exception:
                            log.exception("handler for %s failed", real_type)
                continue

            log.debug("ws msg ignored: %s", msg)

    # --------------------------------------------------------
    #  실시간 종목 등록 / 해제
    # --------------------------------------------------------

    async def register(
        self,
        group_no: str,
        items: list[str],
        types: list[str],
        *,
        refresh: bool = True,
    ) -> None:
        """시세 실시간 등록.

        Args:
            group_no: 화면번호 (4자리). 그룹별로 해제 가능.
            items: 종목코드 리스트.
            types: 실시간 TR 코드 리스트 (예: ['0B','0D']).
            refresh: True 면 기존 등록 유지 + 추가, False 면 교체.
        """
        assert self._ws is not None
        await self._ws.send(
            json.dumps(
                {
                    "trnm": "REG",
                    "grp_no": group_no,
                    "refresh": "1" if refresh else "0",
                    "data": [{"item": items, "type": types}],
                }
            )
        )

    async def unregister(self, group_no: str) -> None:
        assert self._ws is not None
        await self._ws.send(json.dumps({"trnm": "REMOVE", "grp_no": group_no}))
