"""llama.cpp OpenAI-compatible HTTP backend."""

from __future__ import annotations

import json
from typing import Any

import httpx

from diffwise.backends.base import BackendError, LLMBackend
from diffwise.config import DEFAULT_LLAMACPP_URL, DEFAULT_MODEL, DEFAULT_TIMEOUT_SECONDS


class LlamaCppBackend(LLMBackend):
    """Client for a local llama.cpp server's OpenAI-compatible `/v1/chat/completions` API."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_LLAMACPP_URL,
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
        """Verify the llama.cpp server is running and not still loading a model."""
        check_timeout = min(self.timeout, 10.0)
        client, should_close = self._get_client(timeout=check_timeout)
        try:
            response = client.get(f"{self.base_url}/health", timeout=check_timeout)
            if response.status_code == 404:
                response = client.get(f"{self.base_url}/v1/models", timeout=check_timeout)
        except httpx.ConnectError as exc:
            raise BackendError(
                f"llama.cpp server is not running at {self.base_url}. "
                "Start `llama-server` and verify the server URL."
            ) from exc
        except httpx.TimeoutException as exc:
            raise BackendError(
                f"Timed out checking llama.cpp health at {self.base_url} "
                f"after {check_timeout:.1f}s."
            ) from exc
        except httpx.HTTPError as exc:
            raise BackendError(
                f"Failed to connect to llama.cpp at {self.base_url}: {exc}"
            ) from exc
        finally:
            if should_close:
                client.close()

        if response.status_code == 503:
            raise BackendError(
                f"llama.cpp server at {self.base_url} is still loading the model (HTTP 503)."
            )
        if response.status_code >= 400:
            raise BackendError(
                f"llama.cpp health check failed with HTTP {response.status_code} "
                f"at {self.base_url}."
            )

    def review(self, prompt: str, *, system_prompt: str | None = None) -> str:
        """Submit a review prompt to llama.cpp `/v1/chat/completions`."""
        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "temperature": 0.1,
        }

        client, should_close = self._get_client(timeout=self.timeout)
        parts: list[str] = []
        try:
            with client.stream(
                "POST",
                f"{self.base_url}/v1/chat/completions",
                json=payload,
                timeout=self.timeout,
            ) as response:
                if response.status_code >= 400:
                    err_body = response.read().decode("utf-8", errors="replace").strip()
                    detail = f": {err_body}" if err_body else ""
                    raise BackendError(
                        "llama.cpp /v1/chat/completions failed with "
                        f"HTTP {response.status_code}{detail}"
                    )

                content_type = response.headers.get("content-type", "").lower()
                if "application/json" in content_type and "text/event-stream" not in content_type:
                    raw_body = response.read().decode("utf-8", errors="replace")
                    return _extract_openai_completion_text(raw_body)

                for raw_line in response.iter_lines():
                    line = raw_line.strip()
                    if not line or line.startswith(":"):
                        continue
                    if line.startswith("data:"):
                        data_str = line[5:].strip()
                    else:
                        # Support fallback non-SSE JSON body even when header omits content-type
                        if line.startswith("{"):
                            return _extract_openai_completion_text(line)
                        continue
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk: Any = json.loads(data_str)
                    except json.JSONDecodeError as exc:
                        raise BackendError(
                            f"Malformed SSE JSON chunk from llama.cpp: {data_str[:200]}"
                        ) from exc
                    if not isinstance(chunk, dict):
                        continue
                    if isinstance(chunk.get("error"), dict):
                        msg = chunk["error"].get("message", chunk["error"])
                        raise BackendError(f"llama.cpp error: {msg}")
                    if isinstance(chunk.get("error"), str):
                        raise BackendError(f"llama.cpp error: {chunk['error']}")
                    delta_text = _extract_chunk_delta(chunk)
                    if delta_text:
                        parts.append(delta_text)
        except httpx.ConnectError as exc:
            raise BackendError(
                f"Lost connection to llama.cpp server at {self.base_url}."
            ) from exc
        except httpx.TimeoutException as exc:
            raise BackendError(
                f"llama.cpp request timed out after {self.timeout:.1f}s."
            ) from exc
        except httpx.HTTPError as exc:
            raise BackendError(f"HTTP error communicating with llama.cpp: {exc}") from exc
        finally:
            if should_close:
                client.close()

        return "".join(parts)


def _extract_chunk_delta(chunk: dict[str, Any]) -> str:
    choices = chunk.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    delta = first.get("delta")
    if isinstance(delta, dict):
        delta_content = delta.get("content")
        if isinstance(delta_content, str):
            return delta_content
    message = first.get("message")
    if isinstance(message, dict):
        message_content = message.get("content")
        if isinstance(message_content, str):
            return message_content
    text = first.get("text")
    return text if isinstance(text, str) else ""


def _extract_openai_completion_text(raw_body: str) -> str:
    try:
        data: Any = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise BackendError(f"Invalid JSON response from llama.cpp: {raw_body[:200]}") from exc
    if not isinstance(data, dict):
        raise BackendError("Unexpected JSON payload from llama.cpp.")
    if "error" in data:
        raise BackendError(f"llama.cpp error: {data['error']}")
    return _extract_chunk_delta(data)
