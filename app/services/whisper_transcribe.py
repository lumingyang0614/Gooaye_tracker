"""Local speech-to-text fallback for episodes not yet in the transcript pack.

Downloads one episode's mp3 from the SoundOn RSS enclosure URL to a temp file,
transcribes it locally with faster-whisper (no audio or text ever leaves the
machine — same "local inference only" principle this project already applies
to LLM analysis), then deletes the audio file. The raw audio is never stored
or served by this project.

faster-whisper needs the `ffmpeg` executable on PATH to decode mp3/m4a; see
README.md for installation notes.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx

logger = logging.getLogger("uvicorn.error")

_model_cache: dict[tuple[str, str], "object"] = {}


def _get_model(model_size: str, device: str):
    key = (model_size, device)
    if key not in _model_cache:
        from faster_whisper import WhisperModel
        compute_type = "int8" if device == "cpu" else "float16"
        _model_cache[key] = WhisperModel(model_size, device=device, compute_type=compute_type)
    return _model_cache[key]


async def download_audio(audio_url: str, client: httpx.AsyncClient, destination: Path) -> None:
    async with client.stream("GET", audio_url) as response:
        response.raise_for_status()
        with destination.open("wb") as handle:
            async for chunk in response.aiter_bytes():
                handle.write(chunk)


def _transcribe_file(path: Path, model_size: str, device: str) -> str:
    model = _get_model(model_size, device)
    segments, _info = model.transcribe(str(path), language="zh", vad_filter=True)
    return "".join(segment.text for segment in segments).strip()


def _run_transcription_worker(path: Path, model_size: str, device: str) -> str:
    """Run CTranslate2 in a disposable process.

    On Windows, Intel's native runtime can abort the whole process with
    ``forrtl: error (200)`` when a console/window-close event is delivered.
    Keeping faster-whisper in a child process prevents that native abort from
    taking down the parent sync job.  A fresh console also avoids inheriting
    the Task Scheduler host's console-close lifecycle.
    """
    command = [
        sys.executable,
        "-m",
        "app.services.whisper_worker",
        "--audio",
        str(path),
        "--model",
        model_size,
        "--device",
        device,
    ]
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP
    # The worker writes the transcript as raw UTF-8 bytes, but anything else
    # reaching stdout/stderr — a library warning, a traceback carrying Chinese
    # text — still goes through Python's text layer, which defaults to the
    # console ANSI codepage (cp950 here) and cannot encode Simplified Chinese.
    # Pinning the child's IO encoding keeps those paths from failing too, and
    # keeps stderr decodable when the worker really does crash.
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-2000:]
        raise RuntimeError(
            f"Whisper worker exited with code {result.returncode}"
            + (f": {detail}" if detail else "")
        )
    return result.stdout.strip()


async def transcribe_episode_audio(
    audio_url: str,
    model_size: str,
    device: str,
    client: httpx.AsyncClient,
) -> str:
    """Download, transcribe, and clean up one episode's audio. Returns plain text."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        audio_path = Path(tmp_dir) / "episode.mp3"
        logger.info("Gooaye Whisper 下載音檔開始 | url=%s", audio_url)
        await download_audio(audio_url, client, audio_path)
        logger.info("Gooaye Whisper 轉譯開始 | model=%s | device=%s | bytes=%s", model_size, device, audio_path.stat().st_size)
        text = await asyncio.to_thread(_run_transcription_worker, audio_path, model_size, device)
        logger.info("Gooaye Whisper 轉譯完成 | characters=%s", len(text))
        return text
