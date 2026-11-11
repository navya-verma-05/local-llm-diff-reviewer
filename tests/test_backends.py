"""Tests for Ollama and llama.cpp LLM backends using httpx.MockTransport."""

from __future__ import annotations

import json

import httpx
import pytest

from diffwise.backends import BackendError, LlamaCppBackend, OllamaBackend, create_backend
from diffwise.config import DiffwiseConfig


def test_ollama_health_check_success_and_missing_model() -> None:
    def ok_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags"
        return httpx.Response(
            200,
            json={"models": [{"name": "qwen2.5-coder:7b"}, {"name": "llama3:latest"}]},
        )

    client = httpx.Client(transport=httpx.MockTransport(ok_handler))
    OllamaBackend(model="qwen2.5-coder:7b", client=client).health_check()
    OllamaBackend(model="llama3", client=client).health_check()

    with pytest.raises(BackendError, match="ollama pull codellama:7b"):
        OllamaBackend(model="codellama:7b", client=client).health_check()


def test_ollama_health_check_connection_and_timeout_errors() -> None:
    def connect_fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    def timeout_fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(BackendError, match="Ollama server is not running"):
        OllamaBackend(client=httpx.Client(transport=httpx.MockTransport(connect_fail))).health_check()

    with pytest.raises(BackendError, match="Timed out checking Ollama health"):
        OllamaBackend(client=httpx.Client(transport=httpx.MockTransport(timeout_fail))).health_check()


def test_ollama_review_streaming_and_errors() -> None:
    captured_payload: dict[str, object] = {}

    def stream_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        captured_payload.update(json.loads(request.content.decode("utf-8")))
        ndjson = "\n".join(
            [
                json.dumps({"message": {"content": "["}, "done": False}),
                json.dumps({"message": {"content": '{"title": "x"}'}, "done": False}),
                json.dumps({"message": {"content": "]"}, "done": True}),
            ]
        )
        return httpx.Response(200, content=ndjson.encode("utf-8"))

    backend = OllamaBackend(
        model="qwen2.5-coder:7b",
        client=httpx.Client(transport=httpx.MockTransport(stream_handler)),
    )
    text = backend.review("diff body", system_prompt="sys prompt")
    assert text == '[{"title": "x"}]'
    assert captured_payload["model"] == "qwen2.5-coder:7b"
    assert captured_payload["stream"] is True

    def error_stream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "model not found"})

    err_backend = OllamaBackend(client=httpx.Client(transport=httpx.MockTransport(error_stream)))
    with pytest.raises(BackendError, match="model not found"):
        err_backend.review("diff")

    def http_500(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal failure")

    err500 = OllamaBackend(client=httpx.Client(transport=httpx.MockTransport(http_500)))
    with pytest.raises(BackendError, match="HTTP 500"):
        err500.review("diff")


def test_llamacpp_health_check_and_fallback() -> None:
    def health_ok(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404)

    LlamaCppBackend(client=httpx.Client(transport=httpx.MockTransport(health_ok))).health_check()

    def models_fallback(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(404)
        return httpx.Response(200, json={"data": [{"id": "qwen"}]})

    LlamaCppBackend(
        client=httpx.Client(transport=httpx.MockTransport(models_fallback))
    ).health_check()

    def loading_503(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"message": "Loading model"}})

    with pytest.raises(BackendError, match="HTTP 503"):
        LlamaCppBackend(
            client=httpx.Client(transport=httpx.MockTransport(loading_503))
        ).health_check()

    def connect_fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(BackendError, match=r"llama\.cpp server is not running"):
        LlamaCppBackend(
            client=httpx.Client(transport=httpx.MockTransport(connect_fail))
        ).health_check()


def test_llamacpp_review_sse_and_non_streaming_json() -> None:
    def sse_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        sse = "\n".join(
            [
                'data: {"choices": [{"delta": {"content": "[]"}}]}',
                "data: [DONE]",
            ]
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse.encode("utf-8"),
        )

    backend = LlamaCppBackend(client=httpx.Client(transport=httpx.MockTransport(sse_handler)))
    assert backend.review("diff", system_prompt="sys") == "[]"

    def json_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"choices": [{"message": {"content": '[{"title": "a"}]'}}]},
        )

    json_backend = LlamaCppBackend(client=httpx.Client(transport=httpx.MockTransport(json_handler)))
    assert json_backend.review("diff") == '[{"title": "a"}]'


def test_create_backend_factory() -> None:
    ollama_cfg = DiffwiseConfig(backend="ollama", model="qwen2.5-coder:7b")
    assert isinstance(create_backend(ollama_cfg), OllamaBackend)

    llama_cfg = DiffwiseConfig(backend="llamacpp", model="local-model")
    assert isinstance(create_backend(llama_cfg), LlamaCppBackend)
