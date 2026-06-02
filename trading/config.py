# -*- coding: utf-8 -*-
"""
trading 모듈 공통 설정
======================
- 환경(모의/실전) 토글
- API 키는 .env 또는 환경변수에서만 로드 (하드코딩 금지)
- 엔드포인트/도메인은 코드 한 곳에서 관리
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

try:
    from dotenv import load_dotenv

    # .env 우선, 없으면 .env.example 도 사용 (사용자가 그쪽에 키를 넣어둔 경우)
    _here = Path(__file__).parent
    for _candidate in (".env", ".env.example"):
        _p = _here / _candidate
        if _p.exists():
            load_dotenv(_p, override=False)  # 먼저 로드된 값을 덮어쓰지 않음
except ImportError:
    pass


Env = Literal["paper", "real"]


# ============================================================
#  도메인 — 모의/실전에서 도메인만 다르고 path 는 동일
# ============================================================

_DOMAINS: dict[Env, dict[str, str]] = {
    "real": {
        "rest": "https://api.kiwoom.com",
        "ws": "wss://api.kiwoom.com:10000/api/dostk/websocket",
    },
    "paper": {
        "rest": "https://mockapi.kiwoom.com",
        "ws": "wss://mockapi.kiwoom.com:10000/api/dostk/websocket",
    },
}


# ============================================================
#  TR 코드 → 엔드포인트 path 매핑
#  키움 REST 는 카테고리별로 path 가 다르므로 라우팅 테이블이 필요함
# ============================================================

TR_ROUTES: dict[str, str] = {
    # 종목 정보
    "ka10001": "/api/dostk/stkinfo",
    "ka10002": "/api/dostk/stkinfo",
    "ka10099": "/api/dostk/stkinfo",  # 전종목 리스트 (시장별)
    "ka10100": "/api/dostk/stkinfo",  # 종목정보 단건
    # 시세 / 호가
    "ka10003": "/api/dostk/mrkcond",
    "ka10004": "/api/dostk/mrkcond",
    # 차트 (틱/분/일/주/월)
    "ka10079": "/api/dostk/chart",
    "ka10080": "/api/dostk/chart",
    "ka10081": "/api/dostk/chart",
    "ka10082": "/api/dostk/chart",
    "ka10083": "/api/dostk/chart",
    "ka10060": "/api/dostk/chart",    # 종목별투자자기관별차트
    # 업종 차트
    "ka20006": "/api/dostk/chart",    # 업종일봉 (코스피 001, 코스닥 101 등)
    # 외인/기관
    "ka10059": "/api/dostk/stkinfo",
    # 계좌
    "kt00001": "/api/dostk/acnt",
    "kt00007": "/api/dostk/acnt",
    "kt00018": "/api/dostk/acnt",
    # 주문
    "kt10000": "/api/dostk/ordr",  # 매수
    "kt10001": "/api/dostk/ordr",  # 매도
    "kt10002": "/api/dostk/ordr",  # 정정
    "kt10003": "/api/dostk/ordr",  # 취소
}


# ============================================================
#  설정 객체
# ============================================================


@dataclass(frozen=True)
class KiwoomConfig:
    """런타임 설정. 어플리케이션 시작 시 한 번만 만들어 의존 주입."""

    app_key: str
    app_secret: str
    env: Env
    account_no: str
    token_cache_path: Path

    @property
    def rest_base(self) -> str:
        return _DOMAINS[self.env]["rest"]

    @property
    def ws_url(self) -> str:
        return _DOMAINS[self.env]["ws"]


class ConfigError(Exception):
    """환경변수 미설정/오설정."""


def load_config() -> KiwoomConfig:
    """환경변수에서 설정 로드. 누락된 항목이 있으면 ConfigError 로 fail-fast."""
    # 본 운영은 실전(real). 미설정 시에도 real 로 진행.
    env_str = os.environ.get("KIWOOM_ENV", "real")
    if env_str not in ("paper", "real"):
        raise ConfigError(f"KIWOOM_ENV must be 'paper' or 'real', got {env_str!r}")
    env: Env = env_str  # type: ignore[assignment]

    missing = [k for k in ("KIWOOM_APP_KEY", "KIWOOM_APP_SECRET") if not os.environ.get(k)]
    if missing:
        raise ConfigError(
            "누락된 환경변수: "
            + ", ".join(missing)
            + "\n  → trading/.env.example 을 trading/.env 로 복사하고 키 채워주세요."
            + "\n  → 키움증권 OpenAPI 사이트에서 appkey/secretkey 한 쌍을 발급받습니다."
        )

    cache = os.environ.get("KIWOOM_TOKEN_CACHE") or str(
        Path.home() / ".kiwoom" / f"token_{env}.json"
    )

    return KiwoomConfig(
        app_key=os.environ["KIWOOM_APP_KEY"],
        app_secret=os.environ["KIWOOM_APP_SECRET"],
        env=env,
        account_no=os.environ.get("KIWOOM_ACCOUNT_NO", ""),
        token_cache_path=Path(cache),
    )
