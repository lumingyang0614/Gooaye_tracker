"""LLM analysis of Gooaye (股癌) transcript chunks: local Ollama by default,
or any OpenAI-compatible Chat Completions API if LLM_PROVIDER=api.

Mirrors the schema shape used by the Serenity Stock Tracker project (this is a
separate, standalone project — no code is shared/imported between them) so the
two projects' company-insight data stays comparable.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from app.config import get_settings
from app.runtime_config import load_runtime_config

logger = logging.getLogger("uvicorn.error")

MENTION_SCHEMA = {
    "type": "object",
    "properties": {
        "symbol": {"type": "string"},
        "sentiment": {"type": "string", "enum": ["bullish", "bearish", "neutral"]},
        "reason": {"type": "string"},
    },
    "required": ["symbol", "sentiment", "reason"],
}
ANALYSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary_zh_tw": {"type": "string"},
        "sentiment": {"type": "string", "enum": ["bullish", "bearish", "neutral"]},
        "importance": {"type": "number", "minimum": 0, "maximum": 1},
        "sectors": {"type": "array", "items": {"type": "string"}},
        "tickers": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
        "mentions": {"type": "array", "items": MENTION_SCHEMA},
    },
    "required": ["summary_zh_tw", "sentiment", "importance", "sectors", "tickers", "reason", "mentions"],
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
mentions 必須對本 INDEX 的每個 ticker 分別輸出 symbol、sentiment、reason。symbol 與 tickers 完全一致。
每家公司獨立判斷立場；只是舉例、沒有明確判斷就填 neutral，不可套用整段 sentiment。
不得把其他 INDEX 提到的公司移到本 INDEX。平台或未上市團隊可保留名稱，不可杜撰股票代號。
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
        "mentions": item.get("mentions") if isinstance(item.get("mentions"), list) else [],
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
        parsed = _extract_json(content)
        _validate_schema(parsed, schema)
        return parsed


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


# Statuses worth retrying: 429 is the rate limit that made a whole company
# narrative pass return nothing (17 companies attempted, 2 completed, the
# other 8 batches all rejected back-to-back), and 5xx are transient provider
# faults. Anything else — a bad key, a bad model name, a malformed request —
# will fail identically on a retry, so it is raised immediately.
_RETRY_STATUSES = {429, 500, 502, 503, 504}


def _retry_delay(response: httpx.Response, attempt: int, base_seconds: float, cap_seconds: float) -> float:
    """How long to wait before retrying, preferring the provider's own answer.

    A `Retry-After` header (seconds, or an HTTP date) is the provider telling
    us exactly when it will accept traffic again; guessing a shorter backoff
    just burns another request against the same limit. Without one, back off
    exponentially with jitter so several concurrent callers don't retry in
    lockstep and re-trigger the limit together.
    """
    header = response.headers.get("Retry-After", "").strip()
    if header:
        try:
            return max(0.0, min(float(header), cap_seconds))
        except ValueError:
            with contextlib.suppress(Exception):
                retry_at = parsedate_to_datetime(header)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                delay = (retry_at - datetime.now(timezone.utc)).total_seconds()
                return max(0.0, min(delay, cap_seconds))
    return min(base_seconds * (2 ** (attempt - 1)), cap_seconds) * (0.5 + random.random() / 2)


async def paced(items, pause_seconds: float):
    """Yield `items`, waiting `pause_seconds` between them.

    The complement to _post_with_retries: that one recovers from a rate limit
    already hit, this one keeps a loop from provoking it. Both of this
    project's bulk model loops — episodes in sync.py, company batches in
    company_insights.py — were firing requests back to back, which is how a
    single run collected a 429 on every remaining item after the first few.

    Never sleeps before the first item (nothing has been sent yet) or after
    the last (nothing follows it), so N items cost N-1 pauses.
    """
    for index, item in enumerate(items):
        if index and pause_seconds > 0:
            await asyncio.sleep(pause_seconds)
        yield item


async def _post_with_retries(
    client: httpx.AsyncClient,
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    *,
    max_retries: int,
    base_backoff: float,
    max_backoff: float,
) -> httpx.Response:
    """POST, waiting out rate limits instead of surfacing them as failures.

    A 429 used to propagate straight out of the caller, which for the company
    narrative pass meant every remaining batch failed in turn and the run
    finished with the mechanical fallback text for nearly every company. The
    request is cheap to repeat and the provider tells us when to; retrying is
    what the caller would otherwise have to do by hand.

    The final attempt's response is returned as-is so `raise_for_status()`
    still reports the real status when the limit genuinely outlasts us.
    """
    response = None
    for attempt in range(1, max_retries + 2):
        response = await client.post(url, json=payload, headers=headers)
        if response.status_code not in _RETRY_STATUSES or attempt > max_retries:
            return response
        delay = _retry_delay(response, attempt, base_backoff, max_backoff)
        logger.warning(
            "Gooaye LLM 供應商暫時拒絕請求，等待後重試 | status=%s | attempt=%s/%s | sleep=%.1fs",
            response.status_code, attempt, max_retries, delay,
        )
        await asyncio.sleep(delay)
    return response


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
    max_retries = max(0, int(runtime.get("rate_limit_max_retries", 4)))
    base_backoff = max(0.0, float(runtime.get("rate_limit_backoff_seconds", 5.0)))
    max_backoff = max(base_backoff, float(runtime.get("rate_limit_max_backoff_seconds", 60.0)))
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    last_error: Exception | None = None
    # B.ai does not receive response_format; the schema must be in the prompt.
    messages = [*messages, {"role": "system", "content":
        "Output one JSON object matching this JSON schema exactly:\n" + json.dumps(schema, ensure_ascii=False)}]
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
            response = await _post_with_retries(
                client, f"{api_url.rstrip('/')}/chat/completions", payload, headers,
                max_retries=max_retries, base_backoff=base_backoff, max_backoff=max_backoff,
            )
            response.raise_for_status()
            content = _message_content(response.json()["choices"][0]["message"])
            try:
                parsed = _extract_json(content)
                _validate_schema(parsed, schema)
                return parsed
            except (ValueError, json.JSONDecodeError) as exc:
                last_error = exc
                logger.warning("Gooaye LLM 回應非有效 JSON，準備重試 | attempt=%s/2 | error=%s", attempt, exc)
    raise ValueError("LLM response was not valid JSON after 2 attempts") from last_error


def _validate_schema(value: Any, schema: dict[str, Any]) -> None:
    """Validate the small schema subset used here, including nested fields."""
    kind = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float)}
    if kind in types and (not isinstance(value, types[kind]) or kind in {"integer", "number"} and isinstance(value, bool)):
        raise ValueError(f"Expected {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError("Invalid enum value")
    if kind == "object":
        if any(key not in value for key in schema.get("required", [])):
            raise ValueError("Missing required JSON fields")
        for key, child in schema.get("properties", {}).items():
            if key in value:
                _validate_schema(value[key], child)
    if kind == "array":
        for item in value:
            _validate_schema(item, schema.get("items", {}))


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
        '"importance":0.5,"sectors":[],"tickers":[],"reason":"...","mentions":[]}]}。'
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
- 同一家公司的跨集內容必須融合成一份公司級判斷；片段同時提及其他公司時，只取與目標公司直接相關的理由，不可把其他公司的業務、利多或風險移植過來。不同時期看法有變化時交代變化，不要逐集貼上摘要。
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
        '最外層格式必須是 {"companies":[{"symbol":"代號","thesis":"論點","strengths":[],"weaknesses":[]}]}。\n'
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
    _validate_schema(parsed, COMPANY_SCHEMA)
    for raw in parsed.get("companies") or []:
        if not isinstance(raw, dict):
            continue
        item = _normalize_company(raw)
        # Only accept symbols we actually asked about — a model that invents
        # or renames a symbol must not create a company row of its own.
        if item["symbol"] in blocks:
            results[item["symbol"]] = item
    if not results:
        raise ValueError("LLM returned no requested companies")
    return results


MERGED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "companies": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "stance": {"type": "string", "enum": ["bullish", "bearish", "neutral", "mixed"]},
                    "thesis": {"type": "string"},
                    "strengths": {"type": "array", "items": {"type": "string"}},
                    "weaknesses": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["symbol", "stance", "thesis", "strengths", "weaknesses"],
            },
        }
    },
    "required": ["companies"],
}

# The point of this pass is integration, not juxtaposition: the caller already
# has both sides' text and could have pasted them together itself. What it
# cannot do mechanically is decide what the two views jointly imply — where
# they reinforce each other, where they genuinely conflict, and which variable
# actually matters. Hence the explicit ban on per-source bullet lists.
MERGED_INSTRUCTIONS = """你是股票研究資料整理員。每個 SYMBOL 區塊會給你同一家公司的兩份既有觀點，
標為「觀點A」與「觀點B」。這兩份的來源身分與你的工作無關，也不會告訴你。

請把它們**融合成一份觀點**，不是並列兩份：
- thesis：用繁體中文寫出整合後的核心論點，最多 300 字。要寫出這家公司目前的關鍵變數與判斷依據。
  兩邊講到同一件事時合併成一句，不要重複；兩邊有分歧時，說明分歧在哪、兩種解讀各自的理由是什麼，
  寫成「一種解讀是…另一種解讀是…」這種就事論事的寫法，**不要寫是誰說的**。
  **嚴禁**分段並列，也嚴禁在輸出中出現「觀點A」「觀點B」「股癌」「Podcast」「逐字稿」「社群」
  「貼文」「分析師」或任何指涉來源、載體、平台的字眼。讀者要看到的是對這家公司的一份判斷，
  而不是一份關於「誰說了什麼」的整理。
- stance：整合後的整體立場，只能是 bullish／bearish／neutral／mixed。
  兩邊方向一致就用該方向；方向相反且都有實質理由時用 mixed；證據薄弱或中性時用 neutral。
- strengths：整合後的正面理由，最多 5 點，每點最多 60 字。兩邊重複的合併成一點。
- weaknesses：整合後的風險與疑慮，最多 5 點，每點最多 60 字。兩邊重複的合併成一點。

輸入的兩份觀點都已經是彙總結果，直接當成兩份既有判斷來整合即可，不需要再自行加權任何來源。
每份觀點都附有「彙整更新於」。那是該來源最後一次重算彙總的時間，**不是原始發言時間**，只能當成新鮮度的上限。
兩份時間相隔較久時，把較舊的那份當成背景脈絡，不要寫成兩者同時成立。就算兩份時間相近，也不得只因為時間接近
就宣稱彼此印證——要印證必須是兩邊講的內容本身互相支持，並在論點裡寫出是哪一點互相支持。
嚴禁虛構財務數字、產品、時間點或未在輸入中出現的事實；某一面向沒有依據就給空陣列，不要硬湊。
欄位名稱必須完全遵守指定 schema，不要輸出分析過程或 Markdown。"""


def _normalize_merged(item: dict[str, Any], max_points: int = 5) -> dict[str, Any]:
    stance = str(item.get("stance") or "").strip().lower()
    return {
        "symbol": str(item.get("symbol") or "").strip().upper(),
        "stance": stance if stance in {"bullish", "bearish", "neutral", "mixed"} else "neutral",
        "thesis": str(item.get("thesis") or "").strip(),
        "strengths": [point[:120] for point in _string_list(item.get("strengths"))][:max_points],
        "weaknesses": [point[:120] for point in _string_list(item.get("weaknesses"))][:max_points],
    }


async def synthesize_merged_views(blocks: dict[str, str], *, max_tokens: int = 8192) -> dict[str, dict[str, Any]]:
    """Fuse each company's two source views into one, for a batch of companies.

    Same contract as analyze_companies: `blocks` maps canonical symbol -> the
    evidence text, and only the companies the model actually answered for come
    back. A company left out keeps whatever the caller already had rather than
    being shown as blank.
    """
    if not blocks:
        return {}
    separator = "\n\n---\n\n"
    prompt = (
        f"{MERGED_INSTRUCTIONS}\n\n每個 SYMBOL 都必須回傳一筆結果。\n\n"
        '最外層格式必須是 {"companies":[{"symbol":"代號","stance":"bullish","thesis":"論點","strengths":[],"weaknesses":[]}]}。\n'
        + separator.join(f"SYMBOL: {symbol}\n{evidence}" for symbol, evidence in blocks.items())
    )
    parsed = await chat_json(
        [
            {"role": "system", "content": "Return valid JSON matching the schema and one result for every SYMBOL."},
            {"role": "user", "content": prompt},
        ],
        MERGED_SCHEMA,
        max_tokens=max_tokens,
        timeout=max(get_settings().llm_timeout_seconds, 300),
    )
    _validate_schema(parsed, MERGED_SCHEMA)
    results: dict[str, dict[str, Any]] = {}
    for raw in parsed.get("companies") or []:
        if not isinstance(raw, dict):
            continue
        item = _normalize_merged(raw)
        if item["symbol"] in blocks:
            results[item["symbol"]] = item
    if not results:
        raise ValueError("LLM returned no requested companies")
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
