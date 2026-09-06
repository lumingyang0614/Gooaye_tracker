"""Disposable subprocess entry point for faster-whisper.

This module intentionally has no network or database access.  It prints only
the transcript to stdout so the parent sync process can distinguish a clean
result from a native CTranslate2/Intel runtime crash.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from app.services.whisper_transcribe import _transcribe_file


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--device", required=True)
    args = parser.parse_args()
    text = _transcribe_file(Path(args.audio), args.model, args.device)
    print(text, end="")


if __name__ == "__main__":
    main()
