# -*- coding: utf-8 -*-
"""Small KIS Open API REST client.

Secrets are read from environment variables or local-only env files:

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


class KisError(RuntimeError):
    """KIS API or local configuration error."""


@dataclass(frozen=True)
class KisConfig:
    app_key: str
    app_secret: str
    env: KisEnv = "real"
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
        env = os.environ.get("KIS_ENV", "real").lower()
        if env not in ("real", "demo"):
            raise KisError("KIS_ENV must be 'real' or 'demo'.")

        app_key = os.environ.get("KIS_APP_KEY", "").strip()
        app_secret = os.environ.get("KIS_APP_SECRET", "").strip()
        missing = [
            name
            for name, value in (
                ("KIS_APP_KEY", app_key),
                ("KIS_APP_SECRET", app_secret),
            )
            if not value
        ]
        if missing:
            raise KisError("Missing environment variables: " + ", ".join(missing))

        cache = os.environ.get("KIS_TOKEN_CACHE")
        token_cache_path = (
            Path(cache)
            if cache
            else Path.home() / ".kis" / f"token_{env}.json"
        )
        return cls(
            app_key=app_key,
            app_secret=app_secret,
            env=env,  # type: ignore[arg-type]
            custtype=os.environ.get("KIS_CUSTTYPE", "P"),
            token_cache_path=token_cache_path,
        )


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
