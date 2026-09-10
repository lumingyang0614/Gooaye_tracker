"""The worker's transcript must survive the pipe on a Big5 Windows console.

Regression cover: the worker used print(), whose text layer defaults to the
console ANSI codepage (cp950 on a Traditional Chinese Windows). Whisper
transcribing Mandarin emits Simplified characters that Big5 cannot encode, so
the worker died with UnicodeEncodeError on its very last line — after the whole
episode had been downloaded and transcribed.
"""
import io
from pathlib import Path

import pytest

from app.services import whisper_transcribe, whisper_worker

# 尔 is the character that actually broke a real sync run; the rest cover the
# Traditional/mixed cases that already worked and must keep working.
SIMPLIFIED_TEXT = "这是尔的测试逐字稿"
MIXED_TEXT = "台積電 CoWoS 產能，尔后再看"


class _FakeStdout:
    """Stands in for sys.stdout, exposing only the binary buffer the worker uses."""

    def __init__(self) -> None:
        self.buffer = io.BytesIO()


@pytest.mark.parametrize("text", [SIMPLIFIED_TEXT, MIXED_TEXT, "plain ascii", ""])
def test_worker_writes_utf8_bytes_for_any_transcript(monkeypatch, text):
    monkeypatch.setattr(whisper_worker, "_transcribe_file", lambda path, model, device: text)
    monkeypatch.setattr(
        "sys.argv",
        ["whisper_worker", "--audio", "episode.mp3", "--model", "small", "--device", "cpu"],
    )
    stdout = _FakeStdout()
    monkeypatch.setattr("sys.stdout", stdout)

    whisper_worker.main()

    assert stdout.buffer.getvalue().decode("utf-8") == text


def test_simplified_characters_are_unencodable_in_cp950():
    # Guards the premise: if this ever stops raising, the bug it protects
    # against was environmental and this test file can go.
    with pytest.raises(UnicodeEncodeError):
        SIMPLIFIED_TEXT.encode("cp950")


def test_worker_adds_no_trailing_newline(monkeypatch):
    # The parent .strip()s anyway, but print() used end="" for a reason: the
    # transcript is passed through to the LLM and stored verbatim.
    monkeypatch.setattr(whisper_worker, "_transcribe_file", lambda path, model, device: "text")
    monkeypatch.setattr(
        "sys.argv",
        ["whisper_worker", "--audio", "episode.mp3", "--model", "small", "--device", "cpu"],
    )
    stdout = _FakeStdout()
    monkeypatch.setattr("sys.stdout", stdout)

    whisper_worker.main()

    assert stdout.buffer.getvalue() == b"text"


def test_child_process_io_encoding_is_pinned_to_utf8(monkeypatch):
    captured = {}

    class Result:
        returncode = 0
        stdout = SIMPLIFIED_TEXT
        stderr = ""

    def fake_run(command, **kwargs):
        captured.update(kwargs)
        return Result()

    monkeypatch.setattr(whisper_transcribe.subprocess, "run", fake_run)
    result = whisper_transcribe._run_transcription_worker(Path("episode.mp3"), "small", "cpu")

    assert result == SIMPLIFIED_TEXT
    assert captured["env"]["PYTHONIOENCODING"] == "utf-8"
    # The parent still decodes as UTF-8; both halves have to agree.
    assert captured["encoding"] == "utf-8"


def test_child_env_keeps_the_rest_of_the_environment(monkeypatch):
    captured = {}

    class Result:
        returncode = 0
        stdout = "text"
        stderr = ""

    monkeypatch.setenv("GOOAYE_TEST_MARKER", "kept")
    monkeypatch.setattr(whisper_transcribe.subprocess, "run",
                        lambda command, **kwargs: (captured.update(kwargs), Result())[1])

    whisper_transcribe._run_transcription_worker(Path("episode.mp3"), "small", "cpu")

    # Replacing os.environ wholesale would strip PATH and break the ffmpeg
    # lookup the worker depends on.
    assert captured["env"]["GOOAYE_TEST_MARKER"] == "kept"
    assert "PATH" in {key.upper() for key in captured["env"]}
