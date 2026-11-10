"""Ollama local LLM backend (to be implemented in Phase 3)."""

from __future__ import annotations

from diffwise.backends.base import LLMBackend
from diffwise.config import DEFAULT_MODEL, DEFAULT_OLLAMA_URL, DEFAULT_TIMEOUT_SECONDS


class OllamaBackend(LLMBackend):
    """Client for the local Ollama `/api/chat` HTTP API."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_OLLAMA_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def health_check(self) -> None:
        """Verify the Ollama server is reachable (Phase 3)."""
        raise NotImplementedError("OllamaBackend.health_check is implemented in Phase 3.")

    def review(self, prompt: str, *, system_prompt: str | None = None) -> str:
        """Submit a review prompt to Ollama `/api/chat` (Phase 3)."""
        raise NotImplementedError("OllamaBackend.review is implemented in Phase 3.")

