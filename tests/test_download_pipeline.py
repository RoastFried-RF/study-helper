"""download_pipeline.py 회귀 테스트 — SVC-F6.

`run_pipeline` 의 `success` 의미 통일 검증:
STT/요약/알림 단계 중 하나라도 실패하면 `stage_errors` 가 채워지고
`success=False` 가 되어야 한다 (`success == (not real_errors)` 불변식).
무음(`TRANSCRIPT_EMPTY`) 센티넬은 실패가 아니므로 `success=True` 유지.

외부 의존(ffmpeg/whisper/gemini/telegram)은 각 stage 함수를 mock 해 차단한다.
"""

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from src.service.download_pipeline import run_pipeline


def _run(coro):
    """동기 테스트에서 async run_pipeline 실행."""
    return asyncio.run(coro)


def _make_mp4(tmp_path: Path) -> Path:
    mp4 = tmp_path / "lecture.mp4"
    mp4.write_bytes(b"fake mp4")
    return mp4


def test_convert_only_success(tmp_path):
    """변환만 요청하고 성공하면 success=True, stage_errors 비어 있음."""
    mp4 = _make_mp4(tmp_path)
    mp3 = tmp_path / "lecture.mp3"
    mp3.write_bytes(b"fake mp3")

    with patch(
        "src.converter.audio_converter.convert_to_mp3", return_value=mp3
    ):
        result = _run(
            run_pipeline(
                mp4, "과목", "1주차", "강의", both=True, stt_enabled=False, ai_enabled=False
            )
        )

    assert result.success is True
    assert result.stage_errors == {}
    assert result.mp3_path == mp3


def test_convert_failure_sets_success_false(tmp_path):
    """CONVERT 단계 실패 시 success=False, stage_errors['convert'] 채워짐."""
    mp4 = _make_mp4(tmp_path)

    with patch(
        "src.converter.audio_converter.convert_to_mp3",
        side_effect=RuntimeError("mp3 변환 실패: encoding error"),
    ):
        result = _run(
            run_pipeline(mp4, "과목", "1주차", "강의", both=True)
        )

    assert result.success is False
    assert "convert" in result.stage_errors
    assert result.stage_errors["convert"] == "RuntimeError"
    assert result.error == "CONVERT_FAILED"


def test_convert_success_stt_failure_sets_success_false(tmp_path):
    """CONVERT 성공 + STT 실패 시 stage_errors 채워지고 success=False (SVC-F6 핵심).

    수정 전: CONVERT 만 success=False 로 두어 STT 실패는 success=True 로 반영 안 됨.
    """
    mp4 = _make_mp4(tmp_path)
    mp3 = tmp_path / "lecture.mp3"
    mp3.write_bytes(b"fake mp3")

    with patch(
        "src.converter.audio_converter.convert_to_mp3", return_value=mp3
    ), patch(
        "src.stt.transcriber.transcribe",
        side_effect=RuntimeError("disk full"),
    ), patch("src.stt.transcriber.safe_unload"):
        result = _run(
            run_pipeline(
                mp4,
                "과목",
                "1주차",
                "강의",
                both=True,
                stt_enabled=True,
            )
        )

    assert result.success is False, "STT 실패가 success 에 반영돼야 함 (SVC-F6)"
    assert "transcribe" in result.stage_errors
    assert result.stage_errors["transcribe"] == "RuntimeError"
    assert result.error == "TRANSCRIBE_FAILED"
    # 파일은 확보됨 — 호출자는 success + 파일 경로로 구분 가능 (R2-11).
    assert result.mp3_path == mp3


def test_summarize_failure_sets_success_false(tmp_path):
    """STT 성공 + 요약 실패 시 success=False, stage_errors['summarize'] 채워짐."""
    mp4 = _make_mp4(tmp_path)
    mp3 = tmp_path / "lecture.mp3"
    mp3.write_bytes(b"fake mp3")
    txt = tmp_path / "lecture.txt"
    txt.write_text("강의 본문 텍스트입니다 충분히 긴 내용.", encoding="utf-8")

    with patch(
        "src.converter.audio_converter.convert_to_mp3", return_value=mp3
    ), patch(
        "src.stt.transcriber.transcribe", return_value=txt
    ), patch("src.stt.transcriber.safe_unload"), patch(
        "src.summarizer.summarizer.summarize",
        side_effect=RuntimeError("gemini API error"),
    ):
        result = _run(
            run_pipeline(
                mp4,
                "과목",
                "1주차",
                "강의",
                both=True,
                stt_enabled=True,
                ai_enabled=True,
                ai_api_key="fake-key",
                ai_model="gemini-2.5-flash",
            )
        )

    assert result.success is False
    assert "summarize" in result.stage_errors
    assert result.stage_errors["summarize"] == "RuntimeError"
    assert result.error == "SUMMARIZE_FAILED"


def test_notify_failure_sets_success_false(tmp_path):
    """알림 단계 실패(ok=False) 시 success=False, stage_errors['notify'] 채워짐."""
    mp4 = _make_mp4(tmp_path)
    mp3 = tmp_path / "lecture.mp3"
    mp3.write_bytes(b"fake mp3")
    txt = tmp_path / "lecture.txt"
    txt.write_text("강의 본문 텍스트입니다 충분히 긴 내용.", encoding="utf-8")
    summary = tmp_path / "lecture_summarized.txt"
    summary.write_text("요약 결과", encoding="utf-8")

    with patch(
        "src.converter.audio_converter.convert_to_mp3", return_value=mp3
    ), patch(
        "src.stt.transcriber.transcribe", return_value=txt
    ), patch("src.stt.transcriber.safe_unload"), patch(
        "src.summarizer.summarizer.summarize", return_value=summary
    ), patch(
        "src.notifier.telegram_notifier.notify_summary_complete",
        return_value=False,
    ):
        result = _run(
            run_pipeline(
                mp4,
                "과목",
                "1주차",
                "강의",
                both=True,
                stt_enabled=True,
                ai_enabled=True,
                ai_api_key="fake-key",
                ai_model="gemini-2.5-flash",
                tg_token="fake-token",
                tg_chat_id="123",
            )
        )

    assert result.success is False
    assert result.stage_errors.get("notify") == "NOTIFY_FAILED"
    assert result.error == "NOTIFY_FAILED"


def test_transcript_empty_keeps_success_true(tmp_path):
    """무음(TRANSCRIPT_EMPTY) 은 실패가 아니므로 success=True 유지."""
    mp4 = _make_mp4(tmp_path)
    mp3 = tmp_path / "lecture.mp3"
    mp3.write_bytes(b"fake mp3")
    txt = tmp_path / "lecture.txt"
    # 무음 — is_transcript_usable 가 False 를 반환할 만큼 짧은 내용.
    txt.write_text("  \n", encoding="utf-8")

    with patch(
        "src.converter.audio_converter.convert_to_mp3", return_value=mp3
    ), patch(
        "src.stt.transcriber.transcribe", return_value=txt
    ), patch("src.stt.transcriber.safe_unload"):
        result = _run(
            run_pipeline(
                mp4,
                "과목",
                "1주차",
                "강의",
                both=True,
                stt_enabled=True,
                ai_enabled=True,
                ai_api_key="fake-key",
                ai_model="gemini-2.5-flash",
            )
        )

    assert result.success is True, "TRANSCRIPT_EMPTY 는 실패 아님 — success 유지"
    assert result.stage_errors.get("summarize") == "TRANSCRIPT_EMPTY"
    assert result.error == "", "TRANSCRIPT_EMPTY 는 대표 에러로 승격되면 안 됨"


def test_full_success_invariant(tmp_path):
    """모든 단계 성공 시 success=True 이고 stage_errors 비어 있음 (불변식)."""
    mp4 = _make_mp4(tmp_path)
    mp3 = tmp_path / "lecture.mp3"
    mp3.write_bytes(b"fake mp3")
    txt = tmp_path / "lecture.txt"
    txt.write_text("강의 본문 텍스트입니다 충분히 긴 내용.", encoding="utf-8")
    summary = tmp_path / "lecture_summarized.txt"
    summary.write_text("요약 결과", encoding="utf-8")

    with patch(
        "src.converter.audio_converter.convert_to_mp3", return_value=mp3
    ), patch(
        "src.stt.transcriber.transcribe", return_value=txt
    ), patch("src.stt.transcriber.safe_unload"), patch(
        "src.summarizer.summarizer.summarize", return_value=summary
    ), patch(
        "src.notifier.telegram_notifier.notify_summary_complete",
        return_value=True,
    ):
        result = _run(
            run_pipeline(
                mp4,
                "과목",
                "1주차",
                "강의",
                both=True,
                stt_enabled=True,
                ai_enabled=True,
                ai_api_key="fake-key",
                ai_model="gemini-2.5-flash",
                tg_token="fake-token",
                tg_chat_id="123",
            )
        )

    assert result.success is True
    assert result.stage_errors == {}
    assert result.success == (not result.stage_errors)
