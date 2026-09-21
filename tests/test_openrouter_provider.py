import asyncio
import json

import httpx
import pytest

from app.config import get_settings
from app.services import llm


def test_openrouter_structured_output(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_provider", "openrouter")
    monkeypatch.setattr(settings, "openrouter_api_key", "test-key")
    captured = []
    client = httpx.AsyncClient

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"value":"ok"}'}}]})

    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(handler)))
    schema = {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"], "additionalProperties": False}
    assert asyncio.run(llm.chat_json([{"role": "user", "content": "test"}], schema)) == {"value": "ok"}
    request = captured[0]
    payload = json.loads(request.content)
    assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer test-key"
    assert payload["model"] == "openrouter/free"
    assert payload["response_format"]["json_schema"]["schema"] == schema
    assert "reasoning_effort" not in payload


def test_missing_key_fails_before_request(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_provider", "openrouter")
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        asyncio.run(llm.chat_json([], {}))
