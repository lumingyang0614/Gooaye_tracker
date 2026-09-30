"""透過本機已登入的 Claude Code / Codex CLI（headless 模式）呼叫模型並取得結構化 JSON。

不需要 API 金鑰：直接沿用 `claude` 與 `codex` 登入的訂閱帳號。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("uvicorn.error")

# 找不到 PATH 上的 CLI 時，改找 VS Code 擴充套件內附的執行檔（取最新版本）。
_EXTENSION_BINARIES = {
    "claude": ("anthropic.claude-code-*", ("resources/native-binary/claude.exe", "resources/native-binary/claude")),
    "codex": ("openai.chatgpt-*", ("bin/windows-x86_64/codex.exe", "bin/linux-x86_64/codex",
                                   "bin/macos-aarch64/codex", "bin/macos-x86_64/codex")),
}
# 避免被當成巢狀 Claude Code session。
_STRIPPED_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT")
# 登入狀態檢查會啟動子程序，快取結果避免每次 /api/health 都執行。
_LOGIN_CACHE_SECONDS = 60
_login_cache: dict[str, tuple[float, bool]] = {}


def resolve_cli(name: str, configured: str = "") -> str | None:
    candidate = (configured or "").strip() or name
    if Path(candidate).is_file():
        return candidate
    found = shutil.which(candidate)
    if found:
        return found
    pattern, relatives = _EXTENSION_BINARIES[name]
    for root in (Path.home() / ".vscode" / "extensions", Path.home() / ".cursor" / "extensions"):
        if not root.is_dir():
            continue
        for extension in sorted(root.glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True):
            for relative in relatives:
                binary = extension / relative
                if binary.is_file():
                    return str(binary)
    return None


def _split_messages(messages: list[dict[str, str]]) -> tuple[str, str]:
    system = "\n\n".join(m["content"] for m in messages if m.get("role") == "system" and m.get("content"))
    turns = [m for m in messages if m.get("role") != "system" and m.get("content")]
    if len(turns) == 1:
        return system, turns[0]["content"]
    return system, "\n\n".join(f"[{m.get('role', 'user')}]\n{m['content']}" for m in turns)


def _strict_schema(schema: Any) -> Any:
    """Codex（OpenAI strict structured output）要求每個 object 都明確 additionalProperties=false。"""
    if isinstance(schema, list):
        return [_strict_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    strict = {key: _strict_schema(value) for key, value in schema.items()}
    if strict.get("type") == "object":
        strict.setdefault("additionalProperties", False)
    return strict


def _run(command: list[str], prompt: str, timeout: float, cwd: str) -> subprocess.CompletedProcess[bytes]:
    env = {key: value for key, value in os.environ.items() if key not in _STRIPPED_ENV}
    return subprocess.run(
        command, input=prompt.encode("utf-8"), capture_output=True,
        timeout=timeout, cwd=cwd, env=env, check=False,
    )


def _check_logged_in(name: str, binary: str) -> bool:
    command = [binary, "auth", "status"] if name == "claude" else [binary, "login", "status"]
    try:
        completed = _run(command, "", 30, tempfile.gettempdir())
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("CLI 登入狀態檢查失敗 | cli=%s | error=%s: %s", name, type(exc).__name__, exc)
        return False
    if completed.returncode != 0:
        return False
    if name == "claude":
        try:
            return bool(json.loads(completed.stdout.decode("utf-8", errors="replace")).get("loggedIn"))
        except (json.JSONDecodeError, AttributeError):
            return False
    return True


async def cli_logged_in(name: str, binary: str) -> bool:
    cached = _login_cache.get(binary)
    if cached and time.monotonic() - cached[0] < _LOGIN_CACHE_SECONDS:
        return cached[1]
    logged_in = await asyncio.to_thread(_check_logged_in, name, binary)
    _login_cache[binary] = (time.monotonic(), logged_in)
    return logged_in


def _stderr_tail(completed: subprocess.CompletedProcess[bytes]) -> str:
    text = (completed.stderr or completed.stdout or b"").decode("utf-8", errors="replace").strip()
    return " ".join(text[-500:].splitlines())


async def chat_json_claude_code(
    *, binary: str, model: str, messages: list[dict[str, str]], schema: dict[str, Any], timeout: float,
) -> dict[str, Any]:
    system, prompt = _split_messages(messages)
    command = [
        binary, "-p", "--output-format", "json", "--json-schema", json.dumps(schema, ensure_ascii=False),
        "--tools", "", "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence",
    ]
    if model:
        command += ["--model", model]
    if system:
        command += ["--system-prompt", system]
    started = time.monotonic()
    logger.info("LLM CLI 請求開始 | provider=claude_code | model=%s | prompt_chars=%s | timeout=%ss",
                model or "default", len(prompt), timeout)
    with tempfile.TemporaryDirectory(prefix="gooaye-claude-") as workdir:
        completed = await asyncio.to_thread(_run, command, prompt, timeout, workdir)
    elapsed = time.monotonic() - started
    stdout = completed.stdout.decode("utf-8", errors="replace").strip()
    try:
        body = json.loads(stdout)
    except json.JSONDecodeError as exc:
        logger.error("LLM CLI 輸出無法解析 | provider=claude_code | exit=%s | elapsed=%.2fs | stderr=%r",
                     completed.returncode, elapsed, _stderr_tail(completed))
        raise RuntimeError(f"Claude Code CLI failed (exit {completed.returncode}): {_stderr_tail(completed)}") from exc
    if body.get("is_error") or completed.returncode != 0:
        raise RuntimeError(f"Claude Code CLI error ({body.get('subtype')}): {str(body.get('result'))[:500]}")
    result = body.get("structured_output")
    if not isinstance(result, dict):
        from app.services.llm import _extract_json
        result = _extract_json(body.get("result"))
    logger.info("LLM CLI 回應成功 | provider=claude_code | model=%s | elapsed=%.2fs | cost_usd=%s | keys=%s",
                model or "default", elapsed, body.get("total_cost_usd"), sorted(result.keys()))
    return result


async def chat_json_codex(
    *, binary: str, model: str, reasoning_effort: str, messages: list[dict[str, str]],
    schema: dict[str, Any], timeout: float,
) -> dict[str, Any]:
    system, prompt = _split_messages(messages)
    if system:
        prompt = f"{system}\n\n{prompt}"
    started = time.monotonic()
    logger.info("LLM CLI 請求開始 | provider=codex | model=%s | prompt_chars=%s | timeout=%ss",
                model or "default", len(prompt), timeout)
    with tempfile.TemporaryDirectory(prefix="gooaye-codex-") as workdir:
        schema_path = Path(workdir) / "schema.json"
        output_path = Path(workdir) / "output.json"
        schema_path.write_text(json.dumps(_strict_schema(schema), ensure_ascii=False), encoding="utf-8")
        command = [
            binary, "exec", "--skip-git-repo-check", "--ephemeral", "--sandbox", "read-only",
            "--color", "never", "--output-schema", str(schema_path), "--output-last-message", str(output_path),
        ]
        if model:
            command += ["--model", model]
        if reasoning_effort:
            command += ["-c", f'model_reasoning_effort="{reasoning_effort}"']
        command.append("-")
        completed = await asyncio.to_thread(_run, command, prompt, timeout, workdir)
        output = output_path.read_text(encoding="utf-8").strip() if output_path.is_file() else ""
    elapsed = time.monotonic() - started
    if completed.returncode != 0 or not output:
        logger.error("LLM CLI 失敗 | provider=codex | exit=%s | elapsed=%.2fs | stderr=%r",
                     completed.returncode, elapsed, _stderr_tail(completed))
        raise RuntimeError(f"Codex CLI failed (exit {completed.returncode}): {_stderr_tail(completed)}")
    from app.services.llm import _extract_json
    result = _extract_json(output)
    logger.info("LLM CLI 回應成功 | provider=codex | model=%s | elapsed=%.2fs | keys=%s",
                model or "default", elapsed, sorted(result.keys()))
    return result
