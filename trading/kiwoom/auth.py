# -*- coding: utf-8 -*-
"""
TokenManager — access token 발급 / 캐시 / 자동 갱신
====================================================
키움 토큰은 24시간 유효. 매 스크립트 실행마다 재발급하면
한도/지연만 늘어나므로 파일에 캐시해 재사용한다.

전략:
  1. 메모리 캐시 hit → 그대로 사용
  2. 디스크 캐시 hit + 만료 5분 전 이상 → 사용
  3. 그 외 → 신규 발급 + 디스크 저장
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import requests

from trading.kiwoom.exceptions import AuthError

if TYPE_CHECKING:
    from trading.config import KiwoomConfig


log = logging.getLogger(__name__)

# 만료 5분 전부터는 미리 갱신해 호출 중 만료 사고 방지
_REFRESH_MARGIN = timedelta(minutes=5)


class TokenManager:
    def __init__(self, config: "KiwoomConfig"):
        self._cfg = config
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at: datetime | None = None

    # --------------------------------------------------------
    #  공개 API
    # --------------------------------------------------------

    def get_token(self) -> str:
        """현재 유효한 토큰 반환. 필요 시 자동 갱신."""
        with self._lock:
            if self._is_valid():
                return self._token  # type: ignore[return-value]

            if self._load_from_disk() and self._is_valid():
                return self._token  # type: ignore[return-value]

            self._issue_new()
            return self._token  # type: ignore[return-value]

    def revoke(self) -> None:
        """토큰 폐기 (테스트/재로그인 용도)."""
        with self._lock:
            if not self._token:
                return
            try:
                requests.post(
                    f"{self._cfg.rest_base}/oauth2/revoke",
                    json={
                        "appkey": self._cfg.app_key,
                        "secretkey": self._cfg.app_secret,
                        "token": self._token,
                    },
                    timeout=10,
                )
            except requests.RequestException as e:
                log.warning("revoke failed: %s", e)
            self._token = None
            self._expires_at = None
            self._cfg.token_cache_path.unlink(missing_ok=True)

    # --------------------------------------------------------
    #  내부
    # --------------------------------------------------------

    def _is_valid(self) -> bool:
        if not self._token or not self._expires_at:
            return False
        return datetime.now() + _REFRESH_MARGIN < self._expires_at

    def _load_from_disk(self) -> bool:
        path = self._cfg.token_cache_path
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self._token = data["token"]
            self._expires_at = datetime.fromisoformat(data["expires_at"])
            return True
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            log.warning("token cache corrupt, ignoring: %s", e)
            return False

    def _save_to_disk(self) -> None:
        path = self._cfg.token_cache_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "token": self._token,
                    "expires_at": self._expires_at.isoformat()  # type: ignore[union-attr]
                    if self._expires_at
                    else None,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def _issue_new(self) -> None:
        url = f"{self._cfg.rest_base}/oauth2/token"
        body = {
            "grant_type": "client_credentials",
            "appkey": self._cfg.app_key,
            "secretkey": self._cfg.app_secret,
        }
        log.info("issuing new kiwoom token (env=%s)", self._cfg.env)
        try:
            r = requests.post(url, json=body, timeout=10)
        except requests.RequestException as e:
            raise AuthError(f"network error during token issue: {e}") from e

        if r.status_code != 200:
            raise AuthError(f"token endpoint returned {r.status_code}: {r.text}")

        data = r.json()

        # 키움은 200 OK 응답에 본문으로 에러를 담아 보내는 경우가 많음.
        # 가장 흔한 케이스를 친절하게 처리한다.
        rc = data.get("return_code")
        if rc is not None and rc != 0:
            msg = data.get("return_msg", "") or ""
            if "8050" in msg or "지정단말기" in msg:
                raise AuthError(
                    "지정단말기 인증 실패 (키움 계정 보안 설정).\n"
                    f"  서버 응답: {msg}\n"
                    "  해결 방법:\n"
                    "    1) 키움증권 홈페이지 → 마이페이지 → 보안센터 → "
                    "지정단말기 관리 → 현재 PC 등록\n"
                    "    2) 또는 동일 메뉴에서 지정단말기 기능 해지\n"
                    "    3) 또는 고객센터 1544-9000 문의"
                )
            if "8005" in msg or "appkey" in msg.lower() or "secret" in msg.lower():
                raise AuthError(
                    f"appkey/secretkey 가 거부됨: {msg}\n"
                    "  → 키 오타/공백 확인, 키 발급 환경(실전 vs 모의) 일치 여부 확인"
                )
            raise AuthError(f"token issuance failed (return_code={rc}): {msg}")

        token = data.get("token")
        expires_dt = data.get("expires_dt")  # 'YYYYMMDDHHMMSS'
        if not token or not expires_dt:
            raise AuthError(f"unexpected token response: {data}")

        self._token = token
        self._expires_at = datetime.strptime(expires_dt, "%Y%m%d%H%M%S")
        self._save_to_disk()
        log.info("token issued, expires at %s", self._expires_at)
