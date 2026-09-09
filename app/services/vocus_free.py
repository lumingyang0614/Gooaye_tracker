"""Best-effort read of vocus.cc's FREE 股癌 transcript articles only.

vocus.cc hosts a paid transcript salon. This module deliberately does not
attempt to log in, hold a session cookie, or bypass the paywall in any way —
it only reads what an anonymous visitor's browser already receives.

Each article page is a Next.js app that embeds the full article record (title,
publish date, HTML body, and an `isPay` flag) in a `<script id="__NEXT_DATA__">`
JSON blob, verified by manually fetching a real article page while designing
this project. An article is used only when `isPay` is explicitly false; a true,
null or absent flag means the article is skipped entirely — its `content` field
is not something this code inspects or attempts to use in that case.

Used only as an optional cross-check against the whatmkreallysaid.com pack
(see sync.py); disabled by default via config/settings.yaml sources.vocus.enabled.
"""
from __future__ import annotations

import html
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger("uvicorn.error")

_EPISODE_NUMBER_RE = re.compile(r"EP\s*(\d+)", re.IGNORECASE)
_ITEM_LIST_RE = re.compile(r'"@type":"ItemList".*?"itemListElement":(\[.*?\])\}</script>', re.DOTALL)
_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


async def list_free_articles(room_url: str, client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Return {episode_number, title, published_at, vocus_url, transcript_text}
    for every FREE article linked from the room page's first listing page.

    Pagination beyond the room page's initial item list is intentionally not
    followed — this is a secondary cross-check source, not a full mirror.
    """
    response = await client.get(room_url)
    response.raise_for_status()
    article_urls = _extract_article_urls(response.text)
    results: list[dict[str, Any]] = []
    for url in article_urls:
        try:
            article_response = await client.get(url)
            article_response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("vocus.cc 文章下載失敗 | url=%s | error=%s: %s", url, type(exc).__name__, exc)
            continue
        parsed = _parse_article(article_response.text, url)
        if parsed is not None:
            results.append(parsed)
    logger.info("vocus.cc 免費集數擷取完成 | candidates=%s | free=%s", len(article_urls), len(results))
    return results


def _extract_article_urls(page_html: str) -> list[str]:
    match = _ITEM_LIST_RE.search(page_html)
    if not match:
        return []
    try:
        items = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []
    return [str(item["url"]) for item in items if isinstance(item, dict) and item.get("url")]


def _parse_article(page_html: str, url: str) -> dict[str, Any] | None:
    match = _NEXT_DATA_RE.search(page_html)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
        article = data["props"]["pageProps"]["parsedArticle"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
    # Only an explicit isPay=False means "this article is free to read". A
    # missing or null flag means the page did not tell us, and an unconfirmed
    # article must be treated as paid — the safe default for a module whose
    # whole premise is never touching paywalled content. Guessing from the
    # presence of a `content` field would be exactly the wrong call, since a
    # truncated preview also carries one.
    if article.get("isPay") is not False:
        return None  # paid or unconfirmed — not read, not stored, not used.

    title = str(article.get("title") or "").strip()
    episode_match = _EPISODE_NUMBER_RE.search(title)
    if not episode_match:
        return None
    created_at = _parse_iso(str(article.get("createdAt") or ""))
    if created_at is None:
        return None
    return {
        "episode_number": int(episode_match.group(1)),
        "title": title,
        "published_at": created_at,
        "vocus_url": url,
        "transcript_text": _strip_html(str(article.get("content") or "")),
    }


def _strip_html(content: str) -> str:
    text = _TAG_RE.sub("\n", content)
    return html.unescape(text).strip()


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None
