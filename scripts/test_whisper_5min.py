"""Run a bounded five-minute Whisper integration test.

Usage:
    python -m scripts.test_whisper_5min --episode 694

All artifacts are written below ``test_output/whisper_5min/EP<n>``. The
downloaded source audio and clipped audio are removed by default, leaving the
transcript, metadata, result JSON, and log for inspection.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from app.config import get_settings
from app.runtime_config import load_runtime_config
from app.services.feed import fetch_feed_episodes
from app.services.whisper_transcribe import _run_transcription_worker, download_audio


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = ROOT / "test_output" / "whisper_5min"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Transcribe only the first five minutes of one Gooaye episode.")
    parser.add_argument("--episode", type=int, required=True, help="Episode number, for example 694")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--keep-audio", action="store_true", help="Keep source and clipped audio for debugging")
    return parser


def _require_ffmpeg() -> str:
    executable = shutil.which("ffmpeg")
    if not executable:
        candidates = sorted(ROOT.glob("ffmpeg*/**/bin/ffmpeg.exe"))
        if candidates:
            executable = str(candidates[0])
    if not executable:
        raise RuntimeError("ffmpeg was not found on PATH or under the project directory")
    return executable


def _clip_first_five_minutes(ffmpeg: str, source: Path, destination: Path) -> None:
    command = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(source),
        "-t", "300", "-vn", "-c", "copy", str(destination),
    ]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed with code {result.returncode}: {result.stderr.strip()[-2000:]}")


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    if args.episode < 1:
        raise ValueError("--episode must be >= 1")
    output_dir = (args.output_root / f"EP{args.episode}").resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / "test.log"
    source_audio = output_dir / "source.mp3"
    clipped_audio = output_dir / "first_5_minutes.mp3"
    transcript_path = output_dir / "transcript.txt"
    result_path = output_dir / "result.json"
    result: dict[str, Any] = {
        "episode": args.episode,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "output_dir": str(output_dir),
        "status": "failed",
        "audio_cleaned": False,
    }

    def log(message: str) -> None:
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{datetime.now(timezone.utc).isoformat()}] {message}\n")
        print(message, flush=True)

    try:
        ffmpeg = _require_ffmpeg()
        settings = get_settings()
        feed_url = str(load_runtime_config()["sources"]["feed"]["rss_url"])
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            log("Fetching RSS feed")
            episodes = await fetch_feed_episodes(feed_url, client)
            episode = next((item for item in episodes if item["episode_number"] == args.episode), None)
            if not episode or not episode.get("audio_url"):
                raise RuntimeError(f"EP{args.episode} was not found in RSS or has no audio URL")
            (output_dir / "episode.json").write_text(
                json.dumps(episode, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
            )
            log(f"Downloading audio into {source_audio}")
            await download_audio(episode["audio_url"], client, source_audio)

        log("Clipping first 300 seconds")
        _clip_first_five_minutes(ffmpeg, source_audio, clipped_audio)
        result["source_bytes"] = source_audio.stat().st_size
        result["clip_bytes"] = clipped_audio.stat().st_size
        log("Running isolated Whisper worker")
        ffmpeg_bin = str(Path(ffmpeg).resolve().parent)
        os.environ["PATH"] = ffmpeg_bin + os.pathsep + os.environ.get("PATH", "")
        started = time.monotonic()
        transcript = await asyncio.to_thread(
            _run_transcription_worker, clipped_audio, settings.whisper_model, settings.whisper_device
        )
        result["duration_seconds"] = round(time.monotonic() - started, 2)
        result["transcript_characters"] = len(transcript)
        transcript_path.write_text(transcript + "\n", encoding="utf-8")
        result["status"] = "passed"
        log(f"Whisper passed; transcript characters={len(transcript)}")
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        log(result["error"])
    finally:
        if not args.keep_audio:
            for path in (source_audio, clipped_audio):
                path.unlink(missing_ok=True)
            result["audio_cleaned"] = not source_audio.exists() and not clipped_audio.exists()
            log(f"Temporary audio cleaned={result['audio_cleaned']}")
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> int:
    args = _parser().parse_args()
    result = asyncio.run(_run(args))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
