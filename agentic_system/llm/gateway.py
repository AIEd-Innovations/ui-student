"""Vercel AI Gateway LLM client.

Talks to https://ai-gateway.vercel.sh using the OpenAI-compatible chat completions endpoint. 
Supports any model the Gateway proxies (default: google/gemini-2.5-flash).

Credentials - checked in order: 
    AI-GATEWAY_API_KEY  explicit API key (local dev and Vercel Preview)
    VERCEL-OIDC_TOKEN   injected automatically inside Vercel serverless functions; no extra env config needed there.
"""

from __future__ import annotations

import json
import os
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

GATEWAY_URL = "https://ai-gateway.vercel.sh/v1/chat/completions"
DEFAULT_MODEL = "google/gemini-2.5-flash"

class GatewayLLM: 
    """LLMClient backed by the Vercel AI Gateway."""

    def __init__(
            self,
            *, 
            api_key: str,
            model: str = DEFAULT_MODEL,
            _opener=None,
    ) -> None: 
        if not api_key: 
            raise ValueError(
                "AI Gateway credentials missing - set AI_GATEWAY_API_KEY"
            )
        self._api_key = api_key
        self._model = model
        self._opener = _opener or urlopen
    
    @classmethod
    def from_env(cls) -> "GatewayLLM":
        key = os.getenv("AI_GATEWAY_API_KEY") or os.getenv("VERCEL_OIDC_TOKEN", "")
        if not key: 
            raise ValueError(
                "Set AI_GATEWAY_API_KEY for local use "
                "(VERCEL_OIDC_TOKEN is injected automatically inside Vercel)"
            )
        return cls(api_key=key, model=os.getenv("AI_GATEWAY_MODEL", DEFAULT_MODEL))
    
    def complete(
        self, 
        messages: list[dict], 
        *, 
        temperature: float = 0.4, 
        max_tokens: Optional[int] = None,
        json_mode: bool = False,
    ) -> str: 
        body: dict = {
            "model": self._model,
            "messages": messages, 
            "temperature": temperature,
            "stream": False,
        }
        if max_tokens is not None: 
            body["max_tokens"] = max_tokens
        if json_mode: 
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
            raise RuntimeError("Could not reach AI Gateway")
        
        try:
            content = result["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("AI Gateway returned an unexpected response") from exc
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("AI Gateway returned an empty response")
        return content.strip()
        
