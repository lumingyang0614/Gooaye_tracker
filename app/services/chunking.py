"""Pure text-splitting helpers for long-form Gooaye (股癌) podcast transcripts.

Episode transcripts run ~20k characters on average, well past the LLM analysis
pipeline's max-input-character budget (see app/services/llm.py). Rather than
blindly truncating (which would silently drop the listener Q&A half of every
episode), we first try to split on the natural "market commentary" / "Q&A"
boundary that the show's own transcripts use, then fall back to fixed-size
windows for anything still too long.

No I/O here on purpose so this stays trivially unit-testable.
"""
from __future__ import annotations

# Markers observed in both whatmkreallysaid.com and vocus.cc transcripts that
# separate the market-commentary half of an episode from the listener Q&A half.
QA_MARKERS = ("Q&A", "Q & A", "Q和A")


def _find_qa_boundary(text: str, min_offset: int) -> int:
    """Return the index of the first Q&A marker after min_offset, or -1."""
    best = -1
    for marker in QA_MARKERS:
        index = text.find(marker)
        if index >= min_offset and (best == -1 or index < best):
            best = index
    return best


def _fixed_windows(text: str, max_characters: int, overlap: int) -> list[str]:
    if len(text) <= max_characters:
        return [text] if text.strip() else []
    step = max(1, max_characters - overlap)
    windows = []
    start = 0
    while start < len(text):
        window = text[start:start + max_characters]
        if window.strip():
            windows.append(window)
        if start + max_characters >= len(text):
            break
        start += step
    return windows


def split_transcript(
    text: str,
    max_characters: int = 11000,
    overlap: int = 200,
) -> list[str]:
    """Split one episode's transcript into LLM-analysis-sized chunks.

    Prefers splitting on the market-commentary/Q&A boundary (keeps each half
    coherent for the analysis prompt); each resulting half is further cut into
    fixed-size overlapping windows only if it still exceeds max_characters.
    """
    normalized = text.strip()
    if not normalized:
        return []

    # Require the marker to appear reasonably deep into the transcript so an
    # incidental early mention of "Q&A" doesn't cut the episode in half.
    boundary = _find_qa_boundary(normalized, min_offset=max(200, len(normalized) // 8))
    halves = [normalized[:boundary], normalized[boundary:]] if boundary != -1 else [normalized]

    chunks: list[str] = []
    for half in halves:
        chunks.extend(_fixed_windows(half, max_characters, overlap))
    return chunks
