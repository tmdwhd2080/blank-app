# -*- coding: utf-8 -*-
"""
KiwoomClient — REST 호출의 단일 진입점
========================================
- 토큰은 TokenManager 가 자동 주입
- 레이트리밋: 1초당 max 5건 (키움 공시 한도) → 슬라이딩 윈도우 큐
- 연속조회: cont-yn / next-key 헤더를 자동으로 페이지네이션
- 응답 헤더 + 본문을 같이 반환 (페이지네이션/디버깅에 필요)

모든 TR 호출은 이 클래스를 통과한다. TR 모듈은 단순히
self.client.call("ka10081", body) 만 호출.
"""

from __future__ import annotations

import collections
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

import requests

from trading.config import TR_ROUTES, KiwoomConfig, load_config
from trading.kiwoom.auth import TokenManager
from trading.kiwoom.exceptions import KiwoomError, RateLimitError, TRError


log = logging.getLogger(__name__)


# ============================================================
#  레이트리밋 — 슬라이딩 윈도우
# ============================================================


class _RateLimiter:
    """초당 max_per_sec 호출 보장. 초과 시 잠깐 sleep."""

    def __init__(self, max_per_sec: int = 5):
        self._max = max_per_sec
        self._calls: collections.deque[float] = collections.deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            while self._calls and now - self._calls[0] > 1.0:
                self._calls.popleft()
            if len(self._calls) >= self._max:
                sleep_for = 1.0 - (now - self._calls[0]) + 0.01
                time.sleep(max(sleep_for, 0))
                # 재정리
                now = time.monotonic()
                while self._calls and now - self._calls[0] > 1.0:
                    self._calls.popleft()
            self._calls.append(time.monotonic())


# ============================================================
#  응답 컨테이너
# ============================================================


@dataclass(frozen=True)
class TRResponse:
    """업무 호출 응답. 페이지네이션을 위해 헤더 키도 노출."""

    body: dict[str, Any]
    cont_yn: str  # 'Y' / 'N'
    next_key: str

    @property
    def has_next(self) -> bool:
        return self.cont_yn == "Y"


# ============================================================
#  메인 클라이언트
# ============================================================


class KiwoomClient:
    def __init__(self, config: KiwoomConfig | None = None):
        self._cfg = config or load_config()
        self._token_mgr = TokenManager(self._cfg)
        self._session = requests.Session()
        self._rate = _RateLimiter(max_per_sec=5)

    # --------------------------------------------------------
    #  단건 호출
    # --------------------------------------------------------

    def call(
        self,
        tr_code: str,
        body: dict[str, Any],
        *,
        cont_yn: str = "N",
        next_key: str = "",
        timeout: float = 10.0,
    ) -> TRResponse:
        """TR 1회 호출.

        Args:
            tr_code: 'ka10081' 같은 TR 코드. TR_ROUTES 에 등록돼 있어야 함.
            body: 요청 본문. TR 별 입력 파라미터.
            cont_yn / next_key: 연속조회 시에만 채움. 보통 call_paginated 사용.
        """
        if tr_code not in TR_ROUTES:
            raise KiwoomError(f"unknown tr_code: {tr_code}")

        self._rate.acquire()

        url = self._cfg.rest_base + TR_ROUTES[tr_code]
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": f"Bearer {self._token_mgr.get_token()}",
            "api-id": tr_code,
            "cont-yn": cont_yn,
            "next-key": next_key,
        }

        try:
            r = self._session.post(url, headers=headers, json=body, timeout=timeout)
        except requests.RequestException as e:
            raise KiwoomError(f"network error on {tr_code}: {e}") from e

        if r.status_code == 429:
            raise RateLimitError(f"[{tr_code}] rate limit hit")
        if r.status_code != 200:
            raise KiwoomError(f"[{tr_code}] HTTP {r.status_code}: {r.text}")

        data = r.json()

        # 키움 공통 응답: return_code 0 = 성공
        rc = data.get("return_code")
        if rc is not None and rc != 0:
            raise TRError(tr_code, rc, data.get("return_msg", ""))

        return TRResponse(
            body=data,
            cont_yn=r.headers.get("cont-yn", "N"),
            next_key=r.headers.get("next-key", ""),
        )

    # --------------------------------------------------------
    #  페이지네이션 자동 처리
    # --------------------------------------------------------

    def call_paginated(
        self,
        tr_code: str,
        body: dict[str, Any],
        *,
        list_key: str,
        max_pages: int = 50,
        timeout: float = 10.0,
    ) -> list[dict[str, Any]]:
        """연속조회를 끝까지 따라가며 list_key 의 모든 row 를 누적해 반환.

        Args:
            list_key: 응답 본문에서 데이터 배열이 들어있는 키.
                예) ka10081 의 경우 'stk_dt_pole_chart_qry'
            max_pages: 안전장치. 무한루프 방지용 상한.
        """
        rows: list[dict[str, Any]] = []
        cont_yn = "N"
        next_key = ""

        for page in range(max_pages):
            resp = self.call(
                tr_code,
                body,
                cont_yn=cont_yn,
                next_key=next_key,
                timeout=timeout,
            )
            chunk = resp.body.get(list_key) or []
            rows.extend(chunk)
            if not resp.has_next:
                break
            cont_yn = "Y"
            next_key = resp.next_key
        else:
            log.warning("[%s] max_pages=%d reached, data may be truncated", tr_code, max_pages)

        return rows

    # --------------------------------------------------------
    #  편의 접근자
    # --------------------------------------------------------

    @property
    def config(self) -> KiwoomConfig:
        return self._cfg

    @property
    def token(self) -> str:
        return self._token_mgr.get_token()
