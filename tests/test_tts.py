from app.service import split_tts_chunks


def test_split_tts_chunks_preserves_content_and_bounds_long_segments() -> None:
    text = "第一句完成。第二句内容比较长，" + ("请继续处理。" * 12)
    chunks = split_tts_chunks(text, max_chars=20)

    assert chunks
    assert "".join(chunks) == text
    assert all(len(chunk) <= 20 for chunk in chunks)


def test_split_tts_chunks_ignores_whitespace_only_input() -> None:
    assert split_tts_chunks(" \n\t ") == []
