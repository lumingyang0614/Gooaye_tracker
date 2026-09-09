from app.models import Episode
from app.services.sync import _apply_transcript, _canonicalized_segment_rows
from app.services import whisper_transcribe


def _episode(**kwargs) -> Episode:
    defaults = dict(episode_number=1, transcript_text="", transcript_source="unknown")
    defaults.update(kwargs)
    return Episode(**defaults)


def test_segment_rows_keep_each_chunks_own_sentiment_and_tickers():
    # One episode, two segments with opposite stances on different stocks —
    # this is exactly the case the episode-level majority-vote merge used to
    # lose: a chunk's own sentiment/tickers must survive independently.
    analyses = [
        {
            "summary_zh_tw": "台積電法說優於預期，續看多。",
            "sentiment": "bullish",
            "importance": 0.8,
            "sectors": ["半導體"],
            "tickers": ["台積電"],
            "reason": "資本支出上修",
        },
        {
            "summary_zh_tw": "這檔冷門股基本面轉差，看空。",
            "sentiment": "bearish",
            "importance": 0.6,
            "sectors": [],
            "tickers": ["冷門股"],
            "reason": "營收連續下滑",
        },
    ]

    rows = _canonicalized_segment_rows(analyses)

    assert [row["chunk_index"] for row in rows] == [0, 1]
    assert rows[0]["sentiment"] == "bullish"
    assert rows[0]["tickers"] == ["2330.TW"]  # resolved via config/settings.yaml seed alias
    assert rows[1]["sentiment"] == "bearish"
    assert rows[1]["tickers"] == ["冷門股"]  # unknown alias passes through uppercased


def test_written_transcript_is_stored_when_no_prior_whisper():
    episode = _episode()
    changed = _apply_transcript(episode, {"transcript_text": "正式逐字稿"}, None, None)
    assert changed is True
    assert episode.transcript_text == "正式逐字稿"
    assert episode.transcript_source == "whatmkreallysaid"
    assert episode.comparison_score is None


def test_written_transcript_replaces_whisper_and_records_comparison():
    episode = _episode(
        transcript_text="Whisper 版本文字",
        transcript_source="whisper",
        whisper_transcript_text="Whisper 版本文字",
    )
    changed = _apply_transcript(episode, {"transcript_text": "Whisper 版本文字"}, None, None)
    assert changed is False  # text is identical, but comparison should still be recorded
    assert episode.transcript_source == "whatmkreallysaid"
    assert episode.comparison_score == 1.0
    assert "相似度" in episode.comparison_notes


def test_whisper_used_only_when_no_written_transcript_available():
    episode = _episode()
    changed = _apply_transcript(episode, None, None, "本機轉譯文字")
    assert changed is True
    assert episode.transcript_text == "本機轉譯文字"
    assert episode.transcript_source == "whisper"
    assert episode.whisper_transcript_text == "本機轉譯文字"


def test_nothing_new_leaves_episode_unchanged():
    episode = _episode(transcript_text="舊逐字稿", transcript_source="whatmkreallysaid")
    changed = _apply_transcript(episode, None, None, None)
    assert changed is False
    assert episode.transcript_text == "舊逐字稿"


def test_whisper_worker_returns_transcript(monkeypatch, tmp_path):
    captured = {}

    class Result:
        returncode = 0
        stdout = "worker transcript"
        stderr = ""

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return Result()

    monkeypatch.setattr(whisper_transcribe.subprocess, "run", fake_run)
    result = whisper_transcribe._run_transcription_worker(tmp_path / "episode.mp3", "small", "cpu")

    assert result == "worker transcript"
    assert captured["command"][1:4] == ["-m", "app.services.whisper_worker", "--audio"]
    assert captured["kwargs"]["capture_output"] is True
    assert captured["kwargs"]["encoding"] == "utf-8"


def test_whisper_worker_failure_is_reported(monkeypatch, tmp_path):
    class Result:
        returncode = 200
        stdout = ""
        stderr = "forrtl: error (200): program aborting due to window-CLOSE event"

    monkeypatch.setattr(whisper_transcribe.subprocess, "run", lambda *args, **kwargs: Result())

    try:
        whisper_transcribe._run_transcription_worker(tmp_path / "episode.mp3", "small", "cpu")
    except RuntimeError as exc:
        assert "Whisper worker exited with code 200" in str(exc)
        assert "window-CLOSE" in str(exc)
    else:
        raise AssertionError("worker failure should raise RuntimeError")
