# -*- coding: utf-8 -*-
"""Small KIS Open API REST client.

Secrets are read from environment variables or local-only env files:

    KIS_PROFILE=real|demo_stock|demo_derivatives

Or the legacy single-key form:

    KIS_APP_KEY
    KIS_APP_SECRET
    KIS_ENV=real|demo

Local files loaded automatically when present:

    trading/.kis.env
    trading/.kis.env.local

Do not commit those values to Git.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import requests


KisEnv = Literal["real", "demo"]
KisProfile = Literal["real", "demo_stock", "demo_derivatives"]


class KisError(RuntimeError):
    """KIS API or local configuration error."""


_PROFILE_ALIASES: dict[str, KisProfile] = {
    "real": "real",
    "live": "real",
    "prod": "real",
    "production": "real",
    "demo": "demo_stock",
    "mock": "demo_stock",
    "paper": "demo_stock",
    "demo_stock": "demo_stock",
    "demo_stocks": "demo_stock",
    "mock_stock": "demo_stock",
    "mock_stocks": "demo_stock",
    "paper_stock": "demo_stock",
    "paper_stocks": "demo_stock",
    "demo_derivative": "demo_derivatives",
    "demo_derivatives": "demo_derivatives",
    "demo_future": "demo_derivatives",
    "demo_futures": "demo_derivatives",
    "mock_derivative": "demo_derivatives",
    "mock_derivatives": "demo_derivatives",
    "mock_future": "demo_derivatives",
    "mock_futures": "demo_derivatives",
    "paper_derivative": "demo_derivatives",
    "paper_derivatives": "demo_derivatives",
    "paper_future": "demo_derivatives",
    "paper_futures": "demo_derivatives",
}

_PROFILE_ENV: dict[KisProfile, KisEnv] = {
    "real": "real",
    "demo_stock": "demo",
    "demo_derivatives": "demo",
}

_PROFILE_PREFIXES: dict[KisProfile, tuple[str, ...]] = {
    "real": ("KIS_REAL",),
    "demo_stock": ("KIS_DEMO_STOCK", "KIS_PAPER_STOCK", "KIS_MOCK_STOCK"),
    "demo_derivatives": (
        "KIS_DEMO_DERIVATIVES",
        "KIS_DEMO_FUTURES",
        "KIS_PAPER_DERIVATIVES",
        "KIS_PAPER_FUTURES",
        "KIS_MOCK_DERIVATIVES",
        "KIS_MOCK_FUTURES",
    ),
}


@dataclass(frozen=True)
class KisConfig:
    app_key: str
    app_secret: str
    env: KisEnv = "real"
    profile: KisProfile | None = None
    custtype: str = "P"
    token_cache_path: Path = Path.home() / ".kis" / "token_real.json"

    @property
    def base_url(self) -> str:
        if self.env == "demo":
            return "https://openapivts.koreainvestment.com:29443"
        return "https://openapi.koreainvestment.com:9443"

    @classmethod
    def from_env(cls) -> "KisConfig":
        _load_local_secret_files()
        profile = _normalize_profile(os.environ.get("KIS_PROFILE"))
        env = _select_env(profile)
        if env not in ("real", "demo"):
            raise KisError("KIS_ENV must be 'real' or 'demo'.")

        app_key, app_secret = _select_credentials(profile)
        missing = [
            name
            for name, value in (
                ("KIS_APP_KEY", app_key),
                ("KIS_APP_SECRET", app_secret),
            )
            if not value
        ]
        if missing:
            raise KisError(
                "Missing KIS credentials: "
                + ", ".join(missing)
                + ". Set "
                + _credential_hint(profile)
                + "."
            )

        cache = os.environ.get("KIS_TOKEN_CACHE")
        cache_name = profile or env
        token_cache_path = (
            Path(cache)
            if cache
            else Path.home() / ".kis" / f"token_{cache_name}.json"
        )
        return cls(
            app_key=app_key,
            app_secret=app_secret,
            env=env,  # type: ignore[arg-type]
            profile=profile,
            custtype=os.environ.get("KIS_CUSTTYPE", "P"),
            token_cache_path=token_cache_path,
        )


def _normalize_profile(raw: str | None) -> KisProfile | None:
    if raw is None or not raw.strip():
        return None
    key = raw.strip().lower().replace("-", "_").replace(" ", "_")
    try:
        return _PROFILE_ALIASES[key]
    except KeyError as exc:
        raise KisError(
            "KIS_PROFILE must be one of: real, demo_stock, demo_derivatives."
        ) from exc


def _select_env(profile: KisProfile | None) -> str:
    if profile is not None:
        return _PROFILE_ENV[profile]
    return os.environ.get("KIS_ENV", "real").lower()


def _select_credentials(profile: KisProfile | None) -> tuple[str, str]:
    if profile is not None:
        for prefix in _PROFILE_PREFIXES[profile]:
            app_key = os.environ.get(f"{prefix}_APP_KEY", "").strip()
            app_secret = os.environ.get(f"{prefix}_APP_SECRET", "").strip()
            if app_key or app_secret:
                return app_key, app_secret
        if profile != "real":
            return "", ""

    return (
        os.environ.get("KIS_APP_KEY", "").strip(),
        os.environ.get("KIS_APP_SECRET", "").strip(),
    )


def _credential_hint(profile: KisProfile | None) -> str:
    if profile is None:
        return "KIS_APP_KEY and KIS_APP_SECRET"
    options = [
        f"{prefix}_APP_KEY and {prefix}_APP_SECRET"
        for prefix in _PROFILE_PREFIXES[profile]
    ]
    if profile == "real":
        options.append("KIS_APP_KEY and KIS_APP_SECRET")
    return " or ".join(options)


def _load_local_secret_files() -> None:
    trading_dir = Path(__file__).resolve().parents[1]
    for path in (trading_dir / ".kis.env", trading_dir / ".kis.env.local"):
        _load_env_file(path)


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


class KisClient:
    def __init__(self, config: KisConfig | None = None) -> None:
        self.config = config or KisConfig.from_env()
        self.session = requests.Session()

    def issue_token(self, *, force: bool = False) -> str:
        cached = None if force else self._load_cached_token()
        if cached:
            return cached

        url = self.config.base_url + "/oauth2/tokenP"
        body = {
            "grant_type": "client_credentials",
            "appkey": self.config.app_key,
            "appsecret": self.config.app_secret,
        }
        try:
            response = self.session.post(
                url,
                headers={"content-type": "application/json; charset=utf-8"},
                json=body,
                timeout=10,
            )
        except requests.RequestException as exc:
            raise KisError(f"token request failed: {exc}") from exc

        data = self._decode_response(response)
        token = data.get("access_token")
        if not token:
            raise KisError(f"token response did not include access_token: {data}")

        expires_in = int(data.get("expires_in") or 86400)
        expires_at = int(time.time()) + max(expires_in - 300, 60)
        self._save_token({"access_token": token, "expires_at": expires_at})
        return str(token)

    def get(
        self,
        path: str,
        *,
        tr_id: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = self.config.base_url + path
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.issue_token()}",
            "appkey": self.config.app_key,
            "appsecret": self.config.app_secret,
            "tr_id": tr_id,
            "custtype": self.config.custtype,
        }
        try:
            response = self.session.get(url, headers=headers, params=params or {}, timeout=10)
        except requests.RequestException as exc:
            raise KisError(f"GET {path} failed: {exc}") from exc
        return self._decode_response(response)

    def _decode_response(self, response: requests.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise KisError(f"HTTP {response.status_code}: {response.text[:500]}") from exc

        if response.status_code != 200:
            raise KisError(f"HTTP {response.status_code}: {data}")

        rt_cd = data.get("rt_cd")
        if rt_cd is not None and str(rt_cd) != "0":
            msg_cd = data.get("msg_cd", "")
            msg = data.get("msg1", "")
            raise KisError(f"KIS API error {msg_cd}: {msg}")
        return data

    def _load_cached_token(self) -> str | None:
        path = self.config.token_cache_path
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if int(data.get("expires_at") or 0) <= int(time.time()):
            return None
        token = data.get("access_token")
        return str(token) if token else None

    def _save_token(self, data: dict[str, Any]) -> None:
        path = self.config.token_cache_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
