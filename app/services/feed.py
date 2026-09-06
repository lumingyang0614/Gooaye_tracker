"""Parse the SoundOn RSS feed for 股癌 (Gooaye) — the show's actual audio source.

Apple Podcasts / Spotify / the official website all point back to this same
public RSS feed (verified via manual fetch: <channel><title> is "Gooaye 股癌",
owner 謝孟恭, matching the show); it is the source of truth for "which episodes
actually exist" and for each episode's direct mp3 enclosure URL, which the
whatmkreallysaid.com transcript pack lags behind by the newest 1-3 episodes.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any
from xml.etree import ElementTree

import httpx

logger = logging.getLogger("uvicorn.error")

_EPISODE_NUMBER_RE = re.compile(r"EP\s*(\d+)", re.IGNORECASE)
_ENCLOSURE_TAG = "enclosure"


async def fetch_feed_episodes(feed_url: str, client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Return every episode currently published in the RSS feed.

    Each item: {episode_number, title, published_at, audio_url}. Items whose
    title does not contain a recognizable "EP<number>" are skipped since we
    have no way to reconcile them with the transcript pack.
    """
    response = await client.get(feed_url)
    response.raise_for_status()
    root = ElementTree.fromstring(response.content)
    items = root.findall("./channel/item")
    episodes = [parsed for item in items if (parsed := _parse_item(item)) is not None]
    logger.info("Gooaye RSS 解析完成 | items=%s | matched_episodes=%s", len(items), len(episodes))
    return episodes


def _parse_item(item: ElementTree.Element) -> dict[str, Any] | None:
    title = (item.findtext("title") or "").strip()
    match = _EPISODE_NUMBER_RE.search(title)
    if not match:
        return None
    episode_number = int(match.group(1))
    published_at = _parse_pub_date(item.findtext("pubDate") or "")
    if published_at is None:
        return None
    enclosure = item.find(_ENCLOSURE_TAG)
    audio_url = enclosure.get("url") if enclosure is not None else None
    return {
        "episode_number": episode_number,
        "title": title,
        "published_at": published_at,
        "audio_url": audio_url,
    }


def _parse_pub_date(value: str) -> datetime | None:
    if not value.strip():
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
