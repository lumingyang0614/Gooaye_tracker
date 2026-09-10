"""Command-line entry points for Gooaye maintenance tasks.

Console output is pinned to UTF-8 here because every module in this package is
run as `python -m scripts.<name>` from a Windows console, where Python's text
layer defaults to the ANSI codepage — cp950 (Big5) on a Traditional Chinese
system. Script output routinely carries transcript-derived text: a sync summary
prints `affected_symbols`, which contains any company name the model produced
but the alias list could not resolve to a ticker. Whisper transcribing Mandarin
regularly emits Simplified characters that Big5 has no mapping for, so without
this a sync that completed all of its work would still die on its final print()
— the same failure that once killed the Whisper worker on its last line.

errors="replace" rather than a hard failure: a mangled character in a progress
line is never worth losing a completed run over.
"""
from __future__ import annotations

import sys


def _force_utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        # Not every stream is a reconfigurable TextIOWrapper — pytest replaces
        # them during capture, and output can be redirected to something else
        # entirely. Anything without reconfigure() is left alone.
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


_force_utf8_console()
