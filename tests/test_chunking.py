from app.services.chunking import split_transcript


def test_splits_on_qa_marker_when_present():
    market_half = "市場分析內容。" * 50
    qa_half = "聽眾提問內容。" * 50
    text = f"{market_half}\n\nQ&A\n\n{qa_half}"
    chunks = split_transcript(text, max_characters=100_000)
    assert len(chunks) == 2
    assert "Q&A" not in chunks[0]
    assert chunks[1].startswith("Q&A")


def test_falls_back_to_fixed_windows_without_marker():
    text = "股癌內容。" * 3000  # no Q&A marker, comfortably over max_characters
    chunks = split_transcript(text, max_characters=5000, overlap=100)
    assert len(chunks) > 1
    assert all(len(chunk) <= 5000 for chunk in chunks)
    # no characters lost: rejoining recovers at least the original length
    assert sum(len(chunk) for chunk in chunks) >= len(text)


def test_ignores_qa_marker_too_close_to_start():
    text = "Q&A" + ("內容" * 5000)
    chunks = split_transcript(text, max_characters=100_000)
    assert len(chunks) == 1


def test_empty_transcript_returns_no_chunks():
    assert split_transcript("") == []
    assert split_transcript("   ") == []
