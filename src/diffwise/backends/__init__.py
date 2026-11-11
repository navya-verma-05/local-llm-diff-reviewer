"""LLM backend implementations and factory for diffwise."""

from __future__ import annotations

from diffwise.backends.base import BackendError, LLMBackend
from diffwise.backends.llamacpp import LlamaCppBackend
from diffwise.backends.ollama import OllamaBackend
from diffwise.config import DiffwiseConfig

__all__ = [
    "BackendError",
    "LLMBackend",
    "LlamaCppBackend",
    "OllamaBackend",
    "create_backend",
]


def create_backend(config: DiffwiseConfig) -> LLMBackend:
    """Instantiate the configured `LLMBackend` from a `DiffwiseConfig`."""
    if config.backend == "ollama":
        return OllamaBackend(
            model=config.model,
            base_url=config.ollama_url,
            timeout=config.timeout_seconds,
        )
    if config.backend == "llamacpp":
        return LlamaCppBackend(
            model=config.model,
            base_url=config.llamacpp_url,
            timeout=config.timeout_seconds,
        )
    raise BackendError(f"Unsupported backend: {config.backend}")
