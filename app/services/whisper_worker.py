"""Disposable subprocess entry point for faster-whisper.

This module intentionally has no network or database access.  It prints only
the transcript to stdout so the parent sync process can distinguish a clean
result from a native CTranslate2/Intel runtime crash.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.services.whisper_transcribe import _transcribe_file


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--device", required=True)
    args = parser.parse_args()
    text = _transcribe_file(Path(args.audio), args.model, args.device)
    # Write encoded bytes instead of print(): the parent reads this pipe as
    # UTF-8, but a child process's text stdout defaults to the console's ANSI
    # codepage — cp950 (Big5) on a Traditional Chinese Windows. Whisper
    # transcribing Mandarin regularly emits Simplified characters such as
    # 尔 (U+5C14) that Big5 cannot represent, so print() raised
    # UnicodeEncodeError and killed the worker *after* the entire episode had
    # already been downloaded and transcribed, throwing all of that work away.
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
