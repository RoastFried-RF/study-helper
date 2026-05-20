"""summarizer.py 단위 테스트."""

from unittest.mock import patch

import pytest


def test_summarize_empty_text(tmp_path):
    """빈 텍스트 파일은 ValueError를 발생시켜야 한다."""
    txt = tmp_path / "empty.txt"
    txt.write_text("", encoding="utf-8")
    from src.summarizer.summarizer import summarize

    with pytest.raises(ValueError, match="비어 있습니다"):
        summarize(txt, agent="gemini", api_key="key", model="model")


def test_summarize_output_path(tmp_path):
    """출력 파일명이 _summarized.txt로 끝나야 한다."""
    txt = tmp_path / "lecture.txt"
    txt.write_text("강의 내용입니다.", encoding="utf-8")
    with patch("src.summarizer.summarizer._summarize_gemini", return_value="요약 결과"):
        from src.summarizer.summarizer import summarize

        result = summarize(txt, agent="gemini", api_key="key", model="model")
        assert result.name == "lecture_summarized.txt"
        assert result.read_text(encoding="utf-8") == "요약 결과"


def test_summarize_unsupported_agent(tmp_path):
    """지원하지 않는 에이전트는 ValueError."""
    txt = tmp_path / "test.txt"
    txt.write_text("내용", encoding="utf-8")
    from src.summarizer.summarizer import summarize

    with pytest.raises(ValueError, match="지원하지 않는"):
        summarize(txt, agent="claude", api_key="key", model="model")


def test_gemini_model_ids():
    """모델 ID 목록이 비어있지 않아야 한다."""
    from src.summarizer.summarizer import GEMINI_DEFAULT_MODEL, GEMINI_MODEL_IDS

    assert len(GEMINI_MODEL_IDS) > 0
    assert GEMINI_DEFAULT_MODEL in GEMINI_MODEL_IDS


def test_summarize_openai_path(tmp_path):
    """OpenAI 에이전트 경로도 동작해야 한다."""
    txt = tmp_path / "lecture.txt"
    txt.write_text("강의 내용입니다.", encoding="utf-8")
    with patch("src.summarizer.summarizer._summarize_openai", return_value="OpenAI 요약"):
        from src.summarizer.summarizer import summarize

        result = summarize(txt, agent="openai", api_key="key", model="gpt-4")
        assert result.name == "lecture_summarized.txt"
        assert result.read_text(encoding="utf-8") == "OpenAI 요약"


# ── NEW-01: 청크 크기가 prefix 포함 후에도 _MAX_CHUNK_CHARS 이내 ──────────


def test_chunk_text_size_includes_user_prompt_header():
    """NEW-01: _chunk_text 청크는 _USER_PROMPT_HEADER prefix 를 붙여도
    _MAX_CHUNK_CHARS 상한을 넘지 않아야 한다.

    수정 전: 기본 max_chars 가 _MAX_CHUNK_CHARS 그대로라 prefix 포함 시 초과.
    """
    from src.summarizer.summarizer import (
        _MAX_CHUNK_CHARS,
        _USER_PROMPT_HEADER,
        _chunk_text,
    )

    # 상한을 충분히 초과하는 길이라 반드시 분할됨.
    long_text = "가" * (_MAX_CHUNK_CHARS * 3)
    chunks = _chunk_text(long_text)

    assert len(chunks) > 1, "충분히 긴 입력은 여러 청크로 분할돼야 함"
    for chunk in chunks:
        prefixed_len = len(_USER_PROMPT_HEADER) + len(chunk)
        assert prefixed_len <= _MAX_CHUNK_CHARS, (
            f"prefix 포함 청크 길이 {prefixed_len} 가 상한 {_MAX_CHUNK_CHARS} 초과"
        )


def test_chunk_text_short_input_single_chunk():
    """짧은 입력은 분할 없이 단일 청크."""
    from src.summarizer.summarizer import _chunk_text

    text = "짧은 강의 내용."
    assert _chunk_text(text) == [text]


def test_chunk_text_overlap_guard():
    """overlap >= max_chars 인 잘못된 설정에서도 무한 루프가 없어야 한다."""
    from src.summarizer.summarizer import _chunk_text

    text = "가" * 100
    chunks = _chunk_text(text, max_chars=20, overlap=50)
    assert len(chunks) > 1
    # step 이 1 이상으로 보정돼 진행 — 청크 수가 텍스트 길이를 넘지 않음.
    assert len(chunks) <= len(text)


# ── NEW-02: depth 상한 절단 시 결과물에 절단 마커 ────────────────────────


def test_summarize_chunked_truncation_marker(tmp_path):
    """NEW-02: 통합 입력이 절단되면 결과물 말미에 절단 마커가 붙어야 한다.

    부분 요약이 계속 길어 depth 상한까지 재귀 후에도 merged 가 상한을
    초과하면 절단되며, 사용자가 알 수 있도록 마커를 남긴다.
    """
    from src.summarizer.summarizer import (
        _MAX_CHUNK_CHARS,
        _TRUNCATION_MARKER,
        _summarize_chunked,
    )

    # 각 _call_summary 호출이 입력보다 살짝 짧지만 여전히 상한 초과 길이를
    # 반환하도록 만들어, depth 상한에 도달해도 merged 가 절단되게 한다.
    big_reply = "요" * (_MAX_CHUNK_CHARS * 2)

    with patch(
        "src.summarizer.summarizer._call_summary", return_value=big_reply
    ):
        result = _summarize_chunked(
            "gemini",
            "key",
            "model",
            "system",
            "강" * (_MAX_CHUNK_CHARS * 4),
            depth=0,
        )

    assert result.endswith(_TRUNCATION_MARKER), (
        "절단 발생 시 결과물 말미에 절단 마커가 있어야 함"
    )


def test_summarize_chunked_no_marker_when_not_truncated(tmp_path):
    """절단이 없으면 절단 마커가 붙지 않아야 한다."""
    from src.summarizer.summarizer import (
        _MAX_CHUNK_CHARS,
        _TRUNCATION_MARKER,
        _summarize_chunked,
    )

    # 짧은 요약 응답 — merged 가 상한 이내라 절단 없음.
    with patch(
        "src.summarizer.summarizer._call_summary", return_value="짧은 요약"
    ):
        result = _summarize_chunked(
            "gemini",
            "key",
            "model",
            "system",
            "강" * (_MAX_CHUNK_CHARS * 2),
            depth=0,
        )

    assert _TRUNCATION_MARKER not in result
    assert result == "짧은 요약"


def test_summarize_chunked_short_input_single_call(tmp_path):
    """상한 이내 텍스트는 청크 분할 없이 단일 _call_summary 호출."""
    from unittest.mock import MagicMock

    from src.summarizer.summarizer import _summarize_chunked

    mock_call = MagicMock(return_value="요약")
    with patch("src.summarizer.summarizer._call_summary", mock_call):
        result = _summarize_chunked(
            "gemini", "key", "model", "system", "짧은 텍스트", depth=0
        )

    assert result == "요약"
    assert mock_call.call_count == 1
