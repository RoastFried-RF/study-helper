"""audio_converter.py 단위 테스트."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.converter.audio_converter import convert_to_mp3


def test_convert_to_mp3_default_path(tmp_path):
    """mp3_path 미지정 시 mp4와 같은 위치에 .mp3로 저장."""
    mp4 = tmp_path / "video.mp4"
    mp4.write_bytes(b"fake")
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        result = convert_to_mp3(mp4)
        assert result.suffix == ".mp3"
        assert result.stem == "video"


def test_convert_to_mp3_custom_path(tmp_path):
    """mp3_path 지정 시 해당 경로에 저장."""
    mp4 = tmp_path / "video.mp4"
    mp4.write_bytes(b"fake")
    custom = tmp_path / "output" / "custom.mp3"
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        result = convert_to_mp3(mp4, mp3_path=custom)
        assert result.name == "custom.mp3"


def test_convert_to_mp3_missing_file():
    """존재하지 않는 파일은 FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        convert_to_mp3(Path("/nonexistent/file.mp4"))


def test_convert_to_mp3_ffmpeg_failure(tmp_path):
    """ffmpeg 실패 시 RuntimeError."""
    mp4 = tmp_path / "video.mp4"
    mp4.write_bytes(b"fake")
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stderr="encoding error")
        with pytest.raises(RuntimeError, match="mp3 변환 실패"):
            convert_to_mp3(mp4)


# ── NEW-05: ffmpeg 실패 시 비-0 크기 부분 mp3 도 삭제 ────────────────────


def test_convert_failure_removes_nonzero_partial_mp3(tmp_path):
    """NEW-05: ffmpeg 실패 시 비어있지 않은(손상) 부분 mp3 도 삭제돼야 한다.

    수정 전: st_size == 0 인 부분 파일만 삭제 → 비-0 손상 mp3 가 잔존해
    다음 실행의 overwrite=False skip 가드가 깨진 파일을 정상으로 오인.
    subprocess.run 이 returncode=1 을 반환하기 전, ffmpeg 가 만들었을 법한
    비-0 크기 부분 mp3 를 미리 생성해 둔다.
    """
    mp4 = tmp_path / "video.mp4"
    mp4.write_bytes(b"fake")
    mp3 = mp4.with_suffix(".mp3")

    def _fake_run(*args, **kwargs):
        # ffmpeg 가 일부만 인코딩하고 실패한 상황 — 비-0 크기 부분 파일 잔존.
        mp3.write_bytes(b"partial broken mp3 data" * 10)
        return MagicMock(returncode=1, stderr="encoding error")

    with patch("subprocess.run", side_effect=_fake_run):
        with pytest.raises(RuntimeError, match="mp3 변환 실패"):
            convert_to_mp3(mp4)

    assert not mp3.exists(), "비-0 크기 손상 부분 mp3 가 삭제돼야 함 (NEW-05)"


def test_convert_failure_removes_zero_size_partial_mp3(tmp_path):
    """ffmpeg 실패 시 0-byte 부분 mp3 도 여전히 삭제돼야 한다 (회귀 방지)."""
    mp4 = tmp_path / "video.mp4"
    mp4.write_bytes(b"fake")
    mp3 = mp4.with_suffix(".mp3")

    def _fake_run(*args, **kwargs):
        mp3.write_bytes(b"")  # 0-byte 부분 파일
        return MagicMock(returncode=1, stderr="encoding error")

    with patch("subprocess.run", side_effect=_fake_run):
        with pytest.raises(RuntimeError, match="mp3 변환 실패"):
            convert_to_mp3(mp4)

    assert not mp3.exists(), "0-byte 부분 mp3 도 삭제돼야 함"
