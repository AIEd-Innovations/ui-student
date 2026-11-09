"""Vercel AI Gateway adapter for the standalone Participant."""

from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

GATEWAY_URL = "https://ai-gateway.vercel.sh/v1/chat/completions"
DEFAULT_MODEL = "google/gemini-2.5-flash"


class GatewayLLM:
    def __init__(self, *, api_key: str, model: str = DEFAULT_MODEL, opener=None) -> None:
        if not api_key:
            raise ValueError("AI Gateway credentials are missing")
        self._api_key = api_key
        self._model = model
        self._opener = opener or urlopen

    @classmethod
    def from_env(cls) -> "GatewayLLM":
        key = os.getenv("AI_GATEWAY_API_KEY") or os.getenv("VERCEL_OIDC_TOKEN")
        if not key:
            raise ValueError("Set AI_GATEWAY_API_KEY for local use")
        return cls(api_key=key, model=os.getenv("AI_GATEWAY_MODEL", DEFAULT_MODEL))

    def complete(
        self,
        messages: list[dict],
        *,
        temperature: float = 0.3,
        max_tokens: int = 200,
        json_mode: bool = False,
        response_schema: dict | None = None,
    ) -> str:
        body: dict = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if response_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": response_schema,
            }
        elif json_mode:
            body["response_format"] = {"type": "json_object"}

        payload = json.dumps(body).encode("utf-8")
        request = Request(
            GATEWAY_URL,
            data=payload,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with self._opener(request, timeout=30) as response:
                result = json.load(response)
        except HTTPError as exc:
            try:
                error = json.load(exc).get("error", {})
                detail = error.get("message", "") if isinstance(error, dict) else str(error)
            except (ValueError, OSError):
                detail = ""
            msg = f"AI Gateway returned HTTP {exc.code}"
            if detail:
                msg += f": {detail[:300]}"
            raise RuntimeError(msg) from exc
        except URLError as exc:
            raise RuntimeError("Could not reach AI Gateway") from exc

        try:
            content = result["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("AI Gateway returned an unexpected response") from exc
        if not isinstance(content, str) or not content.strip():
            finish = result.get("choices", [{}])[0].get("finish_reason", "unknown")
            raise RuntimeError(
                f"AI Gateway returned an empty response (finish_reason: {finish})"
            )
        return content.strip()
