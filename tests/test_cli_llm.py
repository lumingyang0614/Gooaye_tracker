import asyncio
import json
import subprocess
from pathlib import Path

from app.config import get_settings
from app.services import cli_llm
from app.services.llm import BATCH_SCHEMA, chat_json, llm_health

SCHEMA = {"type": "object", "properties": {"value": {"type": "string"}},
          "required": ["value"], "additionalProperties": False}
MESSAGES = [{"role": "system", "content": "Return JSON."}, {"role": "user", "content": "翻譯 test"}]


def _use_provider(monkeypatch, provider):
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_provider", provider)
    monkeypatch.setattr(settings, "claude_code_cli_path", "")
    monkeypatch.setattr(settings, "claude_code_model", "sonnet")
    monkeypatch.setattr(settings, "codex_cli_path", "")
    monkeypatch.setattr(settings, "codex_model", "gpt-test")
    monkeypatch.setattr(settings, "codex_reasoning_effort", "low")
    monkeypatch.setattr("app.services.llm.resolve_cli", lambda name, configured="": f"/bin/{name}")


def test_claude_code_provider_runs_headless_with_json_schema(monkeypatch):
    _use_provider(monkeypatch, "claude_code")
    captured = {}

    def fake_run(command, input, **kwargs):
        captured["command"], captured["input"] = command, input.decode("utf-8")
        body = {"type": "result", "is_error": False, "result": "", "structured_output": {"value": "好"}}
        return subprocess.CompletedProcess(command, 0, json.dumps(body).encode("utf-8"), b"")

    monkeypatch.setattr(cli_llm.subprocess, "run", fake_run)
    assert asyncio.run(chat_json(MESSAGES, SCHEMA)) == {"value": "好"}
    command = captured["command"]
    assert command[:2] == ["/bin/claude", "-p"]
    assert json.loads(command[command.index("--json-schema") + 1]) == SCHEMA
    assert command[command.index("--tools") + 1] == ""
    assert command[command.index("--model") + 1] == "sonnet"
    assert command[command.index("--system-prompt") + 1] == "Return JSON."
    assert captured["input"] == "翻譯 test"


def test_claude_code_error_result_raises(monkeypatch):
    _use_provider(monkeypatch, "claude_code")
    body = {"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "not logged in"}
    monkeypatch.setattr(cli_llm.subprocess, "run", lambda command, input, **kw: subprocess.CompletedProcess(
        command, 1, json.dumps(body).encode(), b""))
    try:
        asyncio.run(chat_json(MESSAGES, SCHEMA))
    except RuntimeError as exc:
        assert "not logged in" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_codex_provider_uses_output_schema_and_last_message(monkeypatch):
    _use_provider(monkeypatch, "codex")
    captured = {}

    def fake_run(command, input, **kwargs):
        captured["command"], captured["input"] = command, input.decode("utf-8")
        captured["schema"] = json.loads(Path(command[command.index("--output-schema") + 1]).read_text("utf-8"))
        Path(command[command.index("--output-last-message") + 1]).write_text('{"value":"好"}', encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(cli_llm.subprocess, "run", fake_run)
    assert asyncio.run(chat_json(MESSAGES, SCHEMA)) == {"value": "好"}
    command = captured["command"]
    assert command[:2] == ["/bin/codex", "exec"]
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[command.index("--model") + 1] == "gpt-test"
    assert 'model_reasoning_effort="low"' in command
    assert command[-1] == "-"
    assert captured["schema"] == SCHEMA
    assert captured["input"] == "Return JSON.\n\n翻譯 test"


def test_codex_missing_required_field_raises(monkeypatch):
    _use_provider(monkeypatch, "codex")

    def fake_run(command, input, **kwargs):
        Path(command[command.index("--output-last-message") + 1]).write_text('{"other":"x"}', encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(cli_llm.subprocess, "run", fake_run)
    try:
        asyncio.run(chat_json(MESSAGES, SCHEMA))
    except ValueError as exc:
        assert "Missing required JSON fields" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_codex_schema_is_made_strict_without_mutating_original():
    strict = cli_llm._strict_schema(BATCH_SCHEMA)
    assert strict["additionalProperties"] is False
    assert strict["properties"]["items"]["items"]["additionalProperties"] is False
    assert "additionalProperties" not in BATCH_SCHEMA


def test_resolve_cli_prefers_configured_file():
    assert cli_llm.resolve_cli("claude", __file__) == __file__


def test_health_reports_codex_not_logged_in(monkeypatch):
    _use_provider(monkeypatch, "codex")
    cli_llm._login_cache.clear()
    monkeypatch.setattr(cli_llm.subprocess, "run", lambda command, input, **kw: subprocess.CompletedProcess(
        command, 1, b"", b"Not logged in"))
    assert asyncio.run(llm_health()) is False
    assert cli_llm._login_cache["/bin/codex"][1] is False


def test_health_reads_claude_logged_in_flag(monkeypatch):
    _use_provider(monkeypatch, "claude_code")
    cli_llm._login_cache.clear()
    calls = []

    def fake_run(command, input, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, b'{"loggedIn": true}', b"")

    monkeypatch.setattr(cli_llm.subprocess, "run", fake_run)
    assert asyncio.run(llm_health()) is True
    assert asyncio.run(llm_health()) is True
    assert calls == [["/bin/claude", "auth", "status"]]
