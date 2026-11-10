"""llama.cpp OpenAI-compatible HTTP backend (to be implemented in Phase 3)."""

from __future__ import annotations

from diffwise.backends.base import LLMBackend
from diffwise.config import DEFAULT_LLAMACPP_URL, DEFAULT_MODEL, DEFAULT_TIMEOUT_SECONDS


class LlamaCppBackend(LLMBackend):
    """Client for a local llama.cpp server's OpenAI-compatible `/v1/chat/completions` API."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_LLAMACPP_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def health_check(self) -> None:
        """Verify the llama.cpp server is reachable (Phase 3)."""
        raise NotImplementedError("LlamaCppBackend.health_check is implemented in Phase 3.")

    def review(self, prompt: str, *, system_prompt: str | None = None) -> str:
        """Submit a review prompt to llama.cpp `/v1/chat/completions` (Phase 3)."""
        raise NotImplementedError("LlamaCppBackend.review is implemented in Phase 3.")

