

from __future__ import annotations

import json
from typing import Any

import requests

from news_crawl.config import StrategyConfig
from news_crawl.utils import extract_json


def look_ahead_guard(as_of: str) -> str:
    return (
        "‼️ LOOK-AHEAD BIAS 금지 (반드시 준수):\n"
        f"- 기준 시점은 {as_of} 이다. 오직 이 시점 '이전'에 공개된 정보만 사용하라.\n"
        f"- {as_of} 이후에 발생한 가격, 뉴스, 실적, 사후 결과는 절대 알 수 없는 것으로 간주하고 사용하지 마라.\n"
        "- 제공된 데이터는 모두 기준 시점까지의 과거/현재 정보다. 여기에 없는 미래 데이터를 추정·가정하지 마라.\n"
        "- 모든 판단은 '그 시점의 투자자가 실제로 알 수 있었던 정보'만으로 내려라.\n"
        "(Strictly avoid look-ahead bias: use ONLY information available before the reference time.)\n"
    )


class LLMRouter:
    def __init__(self, config: StrategyConfig, *, timeout: float = 30.0) -> None:
        self.config = config
        self.timeout = timeout
        self.session = requests.Session()

    # --- 가용성 ---
    @property
    def has_qwen(self) -> bool:
        return bool(self.config.qwen_api_key)

    @property
    def has_gemini(self) -> bool:
        return bool(self.config.gemini_api_key)

    @property
    def has_gpt(self) -> bool:
        return bool(self.config.openai_api_key)

    # --- 개별 백엔드 ---
    def qwen(self, system: str, user: str) -> str | None:
        if not self.has_qwen:
            return None
        return self._openai_compatible(
            base_url=self.config.qwen_base_url,
            api_key=self.config.qwen_api_key,
            model=self.config.qwen_model,
            system=system,
            user=user,
        )

    def gpt(self, system: str, user: str) -> str | None:
        if not self.has_gpt:
            return None
        return self._openai_compatible(
            base_url=self.config.openai_base_url,
            api_key=self.config.openai_api_key,
            model=self.config.openai_model,
            system=system,
            user=user,
        )

    def gemini(self, system: str, user: str) -> str | None:
        if not self.has_gemini:
            return None
        url = (
            f"{self.config.gemini_base_url.rstrip('/')}/models/"
            f"{self.config.gemini_model}:generateContent"
        )
        body = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
        }
        try:
            resp = self.session.post(
                url,
                params={"key": self.config.gemini_api_key},
                headers={"Content-Type": "application/json"},
                json=body,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
            text = "".join(part.get("text", "") for part in parts)
            return text or None
        except (requests.RequestException, ValueError, KeyError, IndexError):
            return None

    def _openai_compatible(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        system: str,
        user: str,
    ) -> str | None:
        url = f"{base_url.rstrip('/')}/chat/completions"
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
        }
        try:
            resp = self.session.post(
                url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            return (data.get("choices") or [{}])[0].get("message", {}).get("content") or None
        except (requests.RequestException, ValueError, KeyError, IndexError):
            return None

    # --- 편의: JSON 파싱 ---
    def call_json(self, backend: str, system: str, user: str) -> Any | None:
        caller = {"qwen": self.qwen, "gemini": self.gemini, "gpt": self.gpt}.get(backend)
        if caller is None:
            return None
        text = caller(system, user)
        if not text:
            return None
        try:
            return extract_json(text)
        except ValueError:
            return None

    def screen_json(self, system: str, user: str) -> tuple[Any | None, str]:
        for backend in ("gemini", "gpt"):
            result = self.call_json(backend, system, user)
            if result is not None:
                return result, backend
        return None, "heuristic"
