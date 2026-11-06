"""Small, provider-independent base for a locally runnable agent."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Protocol


class LLMClient(Protocol):
    def complete(self, messages: list[dict], *, temperature: float = 0.0,
                 max_tokens: int = 400, json_mode: bool = True,
                 response_schema: dict | None = None) -> str:
        """Return the text of one chat completion."""


class BaseAgent(ABC):
    def __init__(self, *, llm: LLMClient) -> None:
        self._llm = llm
        self.status = "standby"

    def get_status(self) -> str:
        return self.status

    def update_status(self, status: str) -> dict:
        self.status = status
        return {"status": "updated", "new_status": status}

    def generate_response(self, messages: list[dict]) -> str:
        """Make the API call through the injected provider."""
        return self._llm.complete(messages, temperature=0.0,
                                  max_tokens=400, json_mode=True)

    @abstractmethod
    def process_request(self, request: dict) -> dict:
        """Return a response and the agent's status for one request."""
