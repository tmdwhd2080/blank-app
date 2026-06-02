# -*- coding: utf-8 -*-
"""키움 API 호출 시 발생할 수 있는 예외 계층.

모든 예외는 KiwoomError 를 상속하므로 호출 측에서
`except KiwoomError` 한 줄로 일괄 처리 가능.
"""

from __future__ import annotations


class KiwoomError(Exception):
    """키움 API 관련 모든 예외의 베이스."""


class AuthError(KiwoomError):
    """토큰 발급/만료/거부."""


class TRError(KiwoomError):
    """업무 응답이 실패(return_code != 0)."""

    def __init__(self, tr_code: str, return_code: int, return_msg: str):
        self.tr_code = tr_code
        self.return_code = return_code
        self.return_msg = return_msg
        super().__init__(f"[{tr_code}] {return_code}: {return_msg}")


class RateLimitError(KiwoomError):
    """레이트리밋 초과 — 호출 측에서 backoff 후 재시도."""


class OrderRejected(TRError):
    """주문 거부 (호가단위/예수금부족 등)."""
