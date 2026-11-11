"""Abstract base interface for local LLM backends."""

from __future__ import annotations

from abc import ABC, abstractmethod


class BackendError(RuntimeError):
    """Raised when an LLM backend is unreachable, unhealthy, or fails to respond."""


class LLMBackend(ABC):
    """Contract for local LLM inference backends."""

    model: str
    base_url: str
    timeout: float

    @abstractmethod
    def health_check(self) -> None:
        """Verify that the backend server is running and ready to serve requests."""

    @abstractmethod
    def review(self, prompt: str, *, system_prompt: str | None = None) -> str:
        """Send a review prompt to the model and return the raw text response."""
