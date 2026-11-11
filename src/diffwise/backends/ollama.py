"""Ollama local LLM backend."""

from __future__ import annotations

import json
from typing import Any

import httpx

from diffwise.backends.base import BackendError, LLMBackend
from diffwise.config import DEFAULT_MODEL, DEFAULT_OLLAMA_URL, DEFAULT_TIMEOUT_SECONDS


class OllamaBackend(LLMBackend):
    """Client for the local Ollama `/api/chat` HTTP API."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_OLLAMA_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.Client | None = None,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client = client

    def _get_client(self, timeout: float | None = None) -> tuple[httpx.Client, bool]:
        if self._client is not None:
            return self._client, False
        return httpx.Client(timeout=timeout if timeout is not None else self.timeout), True

    def health_check(self) -> None:
        """Verify the Ollama server is running and the requested model is installed."""
        check_timeout = min(self.timeout, 10.0)
        client, should_close = self._get_client(timeout=check_timeout)
        try:
            response = client.get(f"{self.base_url}/api/tags", timeout=check_timeout)
        except httpx.ConnectError as exc:
            raise BackendError(
                f"Ollama server is not running at {self.base_url}. "
                "Start it with `ollama serve` (or launch the Ollama app) and try again."
            ) from exc
        except httpx.TimeoutException as exc:
            raise BackendError(
                f"Timed out checking Ollama health at {self.base_url} after {check_timeout:.1f}s."
            ) from exc
        except httpx.HTTPError as exc:
            raise BackendError(f"Failed to connect to Ollama at {self.base_url}: {exc}") from exc
        finally:
            if should_close:
                client.close()

        if response.status_code >= 400:
            raise BackendError(
                f"Ollama health check failed with HTTP {response.status_code} at {self.base_url}."
            )

        try:
            payload: Any = response.json()
        except ValueError:
            return

        if isinstance(payload, dict) and isinstance(payload.get("models"), list):
            available: set[str] = set()
            for entry in payload["models"]:
                if isinstance(entry, dict):
                    for key in ("name", "model"):
                        val = entry.get(key)
                        if isinstance(val, str) and val:
                            available.add(val)
            candidates = {self.model}
            if ":" not in self.model:
                candidates.add(f"{self.model}:latest")
            if not (available & candidates):
                raise BackendError(
                    f"Model '{self.model}' is not available in Ollama at {self.base_url}. "
                    f"Pull it first with `ollama pull {self.model}`."
                )

    def review(self, prompt: str, *, system_prompt: str | None = None) -> str:
        """Submit a review prompt to Ollama `/api/chat` using streaming NDJSON."""
        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "options": {"temperature": 0.1},
        }

        client, should_close = self._get_client(timeout=self.timeout)
        parts: list[str] = []
        try:
            with client.stream(
                "POST",
                f"{self.base_url}/api/chat",
                json=payload,
                timeout=self.timeout,
            ) as response:
                if response.status_code >= 400:
                    err_body = response.read().decode("utf-8", errors="replace").strip()
                    detail = f": {err_body}" if err_body else ""
                    raise BackendError(
                        f"Ollama /api/chat failed with HTTP {response.status_code}{detail}"
                    )

                for raw_line in response.iter_lines():
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        chunk: Any = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise BackendError(
                            f"Received malformed JSON chunk from Ollama stream: {line[:200]}"
                        ) from exc
                    if not isinstance(chunk, dict):
                        continue
                    if isinstance(chunk.get("error"), str):
                        raise BackendError(f"Ollama error: {chunk['error']}")
                    msg = chunk.get("message")
                    if isinstance(msg, dict):
                        content = msg.get("content")
                        if isinstance(content, str):
                            parts.append(content)
                    if chunk.get("done") is True:
                        break
        except httpx.ConnectError as exc:
            raise BackendError(
                f"Lost connection to Ollama at {self.base_url}. Ensure `ollama serve` is running."
            ) from exc
        except httpx.TimeoutException as exc:
            raise BackendError(
                f"Ollama request timed out after {self.timeout:.1f}s using model '{self.model}'."
            ) from exc
        except httpx.HTTPError as exc:
            raise BackendError(f"HTTP error communicating with Ollama: {exc}") from exc
        finally:
            if should_close:
                client.close()

        return "".join(parts)
