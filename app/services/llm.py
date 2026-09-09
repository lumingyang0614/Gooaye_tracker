"""LLM analysis of Gooaye (股癌) transcript chunks: local Ollama by default,
or any OpenAI-compatible Chat Completions API if LLM_PROVIDER=api.

Mirrors the schema shape used by the Serenity Stock Tracker project (this is a
separate, standalone project — no code is shared/imported between them) so the
two projects' company-insight data stays comparable.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from app.config import get_settings
from app.runtime_config import load_runtime_config

logger = logging.getLogger("uvicorn.error")

ANALYSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary_zh_tw": {"type": "string"},
        "sentiment": {"type": "string", "enum": ["bullish", "bearish", "neutral"]},
        "importance": {"type": "number", "minimum": 0, "maximum": 1},
        "sectors": {"type": "array", "items": {"type": "string"}},
        "tickers": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
    },
    "required": ["summary_zh_tw", "sentiment", "importance", "sectors", "tickers", "reason"],
}
BATCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"index": {"type": "integer"}, **ANALYSIS_SCHEMA["properties"]},
                "required": ["index", *ANALYSIS_SCHEMA["required"]],
            },
        }
    },
    "required": ["items"],
}

INSTRUCTIONS = """你是台股市場研究資料整理員，正在整理《股癌》Podcast 的逐字稿片段。
只能根據提供的逐字稿內容分析，不得虛構。使用繁體中文摘要，判斷多頭、空頭或中立，並列出產業板塊。
tickers 必須完整列出逐字稿中明確提到的每一家公司或證券：
- 台股請盡量給出證券代號（例如 台積電 → 2330），不確定代號時使用完整公司名稱，不可忽略。
- 美股或其他市場公司若有代號請一併列出，沒有則使用公司名稱。
- 不可只挑主持人最看好的公司；供應鏈或閒聊中提到的公司也要收錄。
「提及」不等於「看好」，只有明確立場才能判為 bullish 或 bearish。
逐字稿是口語轉錄，可能包含贊助、閒聊、Q&A 等與投資無關的內容，這些不需要出現在 tickers 或 sectors 中。
欄位名稱必須完全遵守指定 schema。"""


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [part.strip() for part in value.split(",") if part.strip()]
    return []


def _normalized_sentiment(value: Any) -> str:
    normalized = str(value or "neutral").strip().lower()
    aliases = {"long": "bullish", "positive": "bullish", "short": "bearish", "negative": "bearish", "mixed": "neutral"}
    normalized = aliases.get(normalized, normalized)
    return normalized if normalized in {"bullish", "bearish", "neutral"} else "neutral"


def _normalized_importance(value: Any) -> float:
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return 0.5


def _normalize_item(item: dict[str, Any], default_index: int) -> dict[str, Any]:
    summary = str(item.get("summary_zh_tw") or item.get("summary") or "").strip()
    try:
        index = int(item.get("index", default_index))
    except (TypeError, ValueError):
        index = default_index
    return {
        "index": index,
        "summary_zh_tw": summary,
        "sentiment": _normalized_sentiment(item.get("sentiment") or item.get("stance")),
        "importance": _normalized_importance(item.get("importance")),
        "sectors": _string_list(item.get("sectors")),
        "tickers": _string_list(item.get("tickers")),
        "reason": str(item.get("reason") or "").strip(),
    }


def _extract_json(text: Any) -> dict[str, Any]:
    if isinstance(text, dict):
        return text
    if not isinstance(text, str):
        raise ValueError(f"LLM response has unsupported type: {type(text).__name__}")
    value = text.strip().lstrip("﻿")
    try:
        direct = json.loads(value)
        if isinstance(direct, dict):
            return direct
    except json.JSONDecodeError:
        pass
    if value.startswith("```"):
        value = value.split("\n", 1)[1].rsplit("```", 1)[0]
        if value.lstrip().startswith("json"):
            value = value.lstrip()[4:].lstrip()
    start, end = value.find("{"), value.rfind("}")
    if start < 0 or end < start:
        raise ValueError("LLM response did not contain a JSON object")
    parsed = json.loads(value[start:end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("LLM response must be an object")
    return parsed


async def _chat_ollama(messages: list[dict[str, str]], schema: dict[str, Any], max_tokens: int, timeout: float) -> dict[str, Any]:
    settings = get_settings()
    runtime = load_runtime_config().get("analysis", {})
    payload = {
        "model": settings.ollama_model,
        "stream": False,
        "think": False,
        "format": schema,
        "messages": messages,
        "options": {"temperature": float(runtime.get("temperature", 0.1)), "num_predict": max_tokens},
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(f"{settings.ollama_url}/api/chat", json=payload)
        response.raise_for_status()
        content = response.json()["message"]["content"]
        return _extract_json(content)


def _message_content(message: dict[str, Any]) -> str:
    """Some reasoning models (e.g. GLM behind BAI) can burn the whole
    max_tokens budget on `reasoning_content` and leave `content` empty when
    reasoning isn't capped — fall back to reasoning_content rather than treat
    that as "no answer"."""
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content
    reasoning = message.get("reasoning_content")
    return reasoning if isinstance(reasoning, str) else ""


async def _chat_openai_compatible(
    *,
    api_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    schema: dict[str, Any],
    max_tokens: int,
    timeout: float,
    use_json_schema: bool,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """Call an OpenAI-compatible /chat/completions endpoint.

    use_json_schema=False (used for the "bai" provider) skips the strict
    response_format constraint — some OpenAI-compatible providers (e.g. GLM
    family models behind BAI) don't reliably honor it — and instead relies on
    the prompt plus a one-shot retry if the first reply isn't valid JSON.

    reasoning_effort matters for "always-thinking" reasoning models: without
    it, a model like glm-5.3-flash can spend the entire max_tokens budget on
    its internal reasoning_content and never emit the actual JSON answer
    (verified during development — a 3-chunk batch call hit finish_reason
    "length" with 3972/4096 tokens spent on reasoning and an empty content
    field). Passing "low" makes it answer directly instead.
    """
    settings = get_settings()
    runtime = load_runtime_config().get("analysis", {})
    temperature = float(runtime.get("temperature", 0.1))
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    last_error: Exception | None = None
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in (1, 2):
            request_messages = messages if attempt == 1 else [
                *messages,
                {"role": "user", "content": "上一個回答不是有效 JSON。請重新回答，只輸出一個完整 JSON object，不要解釋、不要 Markdown。"},
            ]
            payload: dict[str, Any] = {
                "model": model,
                "messages": request_messages,
                "temperature": temperature if attempt == 1 else 0,
                "max_tokens": max_tokens,
                "stream": False,
            }
            if use_json_schema:
                payload["response_format"] = {"type": "json_schema", "json_schema": {"name": "gooaye_result", "strict": True, "schema": schema}}
            if reasoning_effort:
                payload["reasoning_effort"] = reasoning_effort
            response = await client.post(f"{api_url.rstrip('/')}/chat/completions", json=payload, headers=headers)
            response.raise_for_status()
            content = _message_content(response.json()["choices"][0]["message"])
            try:
                return _extract_json(content)
            except (ValueError, json.JSONDecodeError) as exc:
                last_error = exc
                logger.warning("Gooaye LLM 回應非有效 JSON，準備重試 | attempt=%s/2 | error=%s", attempt, exc)
    raise ValueError("LLM response was not valid JSON after 2 attempts") from last_error


async def _chat_api(messages: list[dict[str, str]], schema: dict[str, Any], max_tokens: int, timeout: float) -> dict[str, Any]:
    settings = get_settings()
    if not settings.llm_api_key or not settings.llm_api_model:
        raise RuntimeError("LLM_PROVIDER=api requires LLM_API_KEY and LLM_API_MODEL")
    return await _chat_openai_compatible(
        api_url=settings.llm_api_url, api_key=settings.llm_api_key, model=settings.llm_api_model,
        messages=messages, schema=schema, max_tokens=max_tokens, timeout=timeout, use_json_schema=True,
    )


async def _chat_bai(messages: list[dict[str, str]], schema: dict[str, Any], max_tokens: int, timeout: float) -> dict[str, Any]:
    settings = get_settings()
    if not settings.bai_api_key or not settings.bai_api_model:
        raise RuntimeError("LLM_PROVIDER=bai requires BAI_API_KEY and BAI_API_MODEL")
    return await _chat_openai_compatible(
        api_url=settings.bai_api_url, api_key=settings.bai_api_key, model=settings.bai_api_model,
        messages=messages, schema=schema, max_tokens=max_tokens, timeout=timeout, use_json_schema=False,
        reasoning_effort=settings.bai_reasoning_effort or None,
    )


async def chat_json(messages: list[dict[str, str]], schema: dict[str, Any], *, max_tokens: int = 4096, timeout: float | None = None) -> dict[str, Any]:
    settings = get_settings()
    request_timeout = timeout or settings.llm_timeout_seconds
    provider = settings.llm_provider.strip().lower()
    if provider == "bai":
        return await _chat_bai(messages, schema, max_tokens, request_timeout)
    if provider == "api":
        return await _chat_api(messages, schema, max_tokens, request_timeout)
    return await _chat_ollama(messages, schema, max_tokens, request_timeout)


async def analyze_chunks(chunks: list[str]) -> list[dict[str, Any]]:
    """Analyze one episode's transcript chunks in a single batch call.

    Returns one normalized analysis dict per chunk, same order as `chunks`.
    """
    if not chunks:
        return []
    runtime = load_runtime_config().get("analysis", {})
    limit = int(runtime.get("max_input_characters", 12000))
    rows = "\n\n".join(f"INDEX {index}\n逐字稿片段：\n{chunk[:limit]}" for index, chunk in enumerate(chunks))
    prompt = (
        f"{INSTRUCTIONS}\n每個 INDEX 都必須回傳一筆結果。"
        "最外層必須是 JSON object，且只能使用 items 陣列包住所有結果："
        '{"items":[{"index":0,"summary_zh_tw":"...","sentiment":"neutral",'
        '"importance":0.5,"sectors":[],"tickers":[],"reason":"..."}]}。'
        f"\n\n{rows}"
    )
    parsed = await chat_json(
        [{"role": "system", "content": "Return valid JSON matching the schema."}, {"role": "user", "content": prompt}],
        BATCH_SCHEMA,
        max_tokens=6000,
        timeout=max(get_settings().llm_timeout_seconds, 300),
    )
    raw_items = parsed.get("items") or []
    by_index = {int(item["index"]): _normalize_item(item, position) for position, item in enumerate(raw_items) if isinstance(item, dict)}
    if not by_index:
        # Nothing usable came back. Padding this out with neutral placeholders
        # would let the caller stamp the episode as analyzed and move on, so an
        # outage would quietly turn into a permanent hole in the data. Raising
        # leaves analyzed_at unset, and the next sync run retries the episode.
        raise ValueError(f"LLM returned no usable analysis items for {len(chunks)} transcript chunk(s)")
    # A partial reply is still worth keeping: the chunks the model did answer
    # for are real analysis, and the gaps are filled with neutral placeholders.
    return [by_index.get(index, _normalize_item({}, index)) for index in range(len(chunks))]


COMPANY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "companies": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "thesis": {"type": "string"},
                    "strengths": {"type": "array", "items": {"type": "string"}},
                    "weaknesses": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["symbol", "thesis", "strengths", "weaknesses"],
            },
        }
    },
    "required": ["companies"],
}

# Deliberately narrower than Serenity Stock Tracker's equivalent schema, which
# also asks the model for stance/confidence/market. Here stance comes from the
# per-segment majority vote (company_insights.py) and market from the security
# master (entity_resolver.resolve_market): both are computed over *every*
# segment, whereas this call only sees the top handful, and both are
# reproducible. The model is asked only for the thing it is uniquely good at —
# turning scattered spoken-word summaries into a readable argument.
COMPANY_INSTRUCTIONS = """你是台股市場研究資料整理員。以下每個 SYMBOL 區塊列出《股癌》Podcast 中談到該公司的逐字稿片段摘要。
請逐家公司整理主持人對這家公司的看法：
- thesis：用繁體中文寫出核心論點，最多 300 字。必須是對這家公司的具體判斷（在意什麼、關鍵變數是什麼），不可只是把片段摘要抄一遍或流水帳。
- strengths：主持人提到的正面理由，最多 5 點，每點最多 60 字。
- weaknesses：主持人提到的風險、疑慮或看壞的理由，最多 5 點，每點最多 60 字。
SEGMENT STANCES 是逐段分析的立場統計，用來理解主持人整體偏向，但理由必須來自片段內容。
嚴禁虛構財務數字、產品、時間點或未在片段中出現的事實；某一面向沒有依據時就給空陣列，不要硬湊。
欄位名稱必須完全遵守指定 schema，不要輸出分析過程或 Markdown。"""


def _normalize_company(item: dict[str, Any], max_points: int = 5) -> dict[str, Any]:
    def _points(value: Any) -> list[str]:
        return [point[:120] for point in _string_list(value)][:max_points]

    return {
        "symbol": str(item.get("symbol") or "").strip().upper(),
        "thesis": str(item.get("thesis") or "").strip(),
        "strengths": _points(item.get("strengths")),
        "weaknesses": _points(item.get("weaknesses")),
    }


async def analyze_companies(blocks: dict[str, str], *, max_tokens: int = 8192) -> dict[str, dict[str, Any]]:
    """Write a per-company thesis plus strengths/weaknesses for one batch.

    `blocks` maps canonical symbol -> the evidence text for that company.
    Returns only the companies the model actually answered for, keyed by
    symbol; the caller keeps its own segment-derived values for anything
    missing rather than showing a blank card.
    """
    if not blocks:
        return {}
    separator = "\n\n---\n\n"
    prompt = (
        f"{COMPANY_INSTRUCTIONS}\n\n每個 SYMBOL 都必須回傳一筆結果。\n\n"
        + separator.join(f"SYMBOL: {symbol}\n{evidence}" for symbol, evidence in blocks.items())
    )
    parsed = await chat_json(
        [
            {"role": "system", "content": "Return valid JSON matching the schema and one result for every SYMBOL."},
            {"role": "user", "content": prompt},
        ],
        COMPANY_SCHEMA,
        max_tokens=max_tokens,
        timeout=max(get_settings().llm_timeout_seconds, 300),
    )
    results: dict[str, dict[str, Any]] = {}
    for raw in parsed.get("companies") or []:
        if not isinstance(raw, dict):
            continue
        item = _normalize_company(raw)
        # Only accept symbols we actually asked about — a model that invents
        # or renames a symbol must not create a company row of its own.
        if item["symbol"] in blocks:
            results[item["symbol"]] = item
    return results


async def llm_health() -> bool:
    settings = get_settings()
    provider = settings.llm_provider.strip().lower()
    if provider == "bai":
        return bool(settings.bai_api_key and settings.bai_api_model)
    if provider == "api":
        return bool(settings.llm_api_key and settings.llm_api_model)
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            return (await client.get(f"{settings.ollama_url}/api/tags")).is_success
    except httpx.HTTPError:
        return False
