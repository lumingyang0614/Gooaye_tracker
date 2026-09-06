"""Fetch the whatmkreallysaid.com bulk transcript pack for 股癌 (Gooaye).

whatmkreallysaid.com is a non-official, AI-transcribed + human-corrected archive
of the show. Its own front-end loads one public, unauthenticated, brotli
compressed JSON file (verified via manual HTTP requests while designing this
project: the response carries `Access-Control-Allow-Origin: *` and is served as
a static asset, i.e. it is meant for exactly this kind of bulk client-side
consumption) rather than one page per episode, which makes bulk sync far
cheaper than scraping ~700 individual HTML pages.

The pack lags the live RSS feed by roughly the newest 1-3 episodes (the site
updates its archive a few days after each episode airs); see feed.py and
sync.py for how that gap is filled with a local Whisper fallback.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Any

import httpx

logger = logging.getLogger("uvicorn.error")

# Taiwan does not observe daylight saving time, so a fixed UTC+8 offset is
# exact and avoids depending on the IANA tzdata package on Windows.
TAIPEI = timezone(timedelta(hours=8))


async def fetch_pack_manifest(manifest_url: str, client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.get(manifest_url)
    response.raise_for_status()
    return response.json()


async def fetch_transcript_pack(
    pack_url: str,
    episode_url_template: str,
    client: httpx.AsyncClient,
) -> list[dict[str, Any]]:
    """Download and parse the bulk transcript pack.

    Returns normalized episode dicts:
    {episode_number, title, published_at, description, transcript_text, source_url}
    """
    response = await client.get(pack_url)
    response.raise_for_status()
    raw = _decode_pack(response)
    if not isinstance(raw, list):
        logger.warning("Gooaye transcript pack 格式異常 | type=%s", type(raw).__name__)
        return []
    episodes = [_normalize_record(record, episode_url_template) for record in raw]
    return [episode for episode in episodes if episode is not None]


def _decode_pack(response: httpx.Response) -> Any:
    """Parse the pack response, decompressing brotli manually if httpx did not."""
    try:
        return response.json()
    except ValueError:
        pass
    import brotli
    return json.loads(brotli.decompress(response.content))


def _normalize_record(record: dict[str, Any], episode_url_template: str) -> dict[str, Any] | None:
    try:
        episode_number = int(record["n"])
    except (KeyError, TypeError, ValueError):
        return None
    published_at = _parse_date(str(record.get("d") or ""))
    if published_at is None:
        return None
    return {
        "episode_number": episode_number,
        "title": str(record.get("t") or f"EP{episode_number}").strip(),
        "published_at": published_at,
        "description": str(record.get("desc") or "").strip(),
        "transcript_text": str(record.get("tx") or "").strip(),
        "source_url": episode_url_template.format(n=episode_number),
    }


def _parse_date(value: str) -> datetime | None:
    try:
        naive = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None
    return naive.replace(tzinfo=TAIPEI).astimezone(timezone.utc)
