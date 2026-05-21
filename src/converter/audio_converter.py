"""
mp4 → mp3 변환.

ffmpeg를 subprocess로 호출하여 영상에서 오디오를 추출한다.
"""

import os
import subprocess
from pathlib import Path

from src.logger import get_logger

_log = get_logger("converter")


def _safe_subprocess_env() -> dict[str, str]:
    """SEC-010: subprocess 에 전달할 whitelist 환경 변수.

    기본 전체 env 를 상속하면 LMS_PASSWORD / GOOGLE_API_KEY / TELEGRAM_BOT_TOKEN
    등 민감 정보가 ffmpeg child 에 그대로 전달된다. ffmpeg 자체는 악성이 아니지만
    segfault 시 core dump 에 노출되거나 libavformat 이 HTTP 프록시/auth env 를
    참조할 수 있다. PATH / TMPDIR 만 whitelisting.
    """
    env = {"PATH": os.environ.get("PATH", "")}
    for key in ("TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "WINDIR"):
        val = os.environ.get(key)
        if val:
            env[key] = val
    return env


def convert_to_mp3(mp4_path: Path, mp3_path: Path | None = None, overwrite: bool = False) -> Path:
    """
    mp4 파일을 mp3로 변환한다.

    Args:
        mp4_path:  원본 mp4 파일 경로
        mp3_path:  저장할 mp3 경로. None이면 mp4와 같은 위치에 확장자만 변경.
        overwrite: True 면 기존 mp3 를 덮어쓴다. False(default) 에서 기존 파일이
                   이미 있으면 변환 skip 하고 해당 경로를 그대로 반환.

    Returns:
        저장된 mp3 파일의 Path

    Raises:
        FileNotFoundError: mp4 파일이 없거나 ffmpeg가 설치되지 않은 경우
        RuntimeError: ffmpeg 변환 실패 시
    """
    if not mp4_path.exists():
        raise FileNotFoundError(f"파일을 찾을 수 없습니다: {mp4_path}")

    if mp3_path is None:
        mp3_path = mp4_path.with_suffix(".mp3")

    # Overwrite 가드 — 사용자가 이전에 편집/정리한 mp3 를 말없이 덮어쓰지 않도록
    # 명시적으로 overwrite=True 를 받은 경우에만 진행.
    if mp3_path.exists() and not overwrite:
        _log.info("mp3 이미 존재 — 변환 skip (overwrite=False): %s", mp3_path.name)
        return mp3_path.resolve()

    mp3_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg",
        "-y" if overwrite else "-n",  # -n: 출력 파일 존재 시 실패 (race 방어)
        "-i",
        str(mp4_path),  # 입력 파일
        "-vn",  # 비디오 스트림 제외
        "-acodec",
        "libmp3lame",
        "-q:a",
        "2",  # VBR 품질 (0=최고, 9=최저), 2 ≈ 192kbps
        str(mp3_path),
    ]

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_safe_subprocess_env(),  # SEC-010
        )
    except FileNotFoundError:
        raise FileNotFoundError("ffmpeg가 설치되어 있지 않습니다. ffmpeg를 먼저 설치해주세요.") from None

    if result.returncode != 0:
        # NEW-05: 실패 시 이번 실행이 생성한 부분 mp3 를 size 무관하게 정리한다.
        # 기존 `st_size == 0` 조건은 비-0 손상 mp3 를 남겨, 다음 실행의
        # overwrite=False skip 가드가 깨진 파일을 정상 파일로 오인하게 했다.
        # 단, ffmpeg `-n` 플래그 덕에 기존 정상 mp3 가 있으면 ffmpeg 자체가
        # 그 파일을 건드리지 않으므로(생성 거부) 사용자 파일 삭제 위험은 없다.
        try:
            if mp3_path.exists():
                mp3_path.unlink()
        except OSError:
            pass
        # stderr 에 포함될 수 있는 경로/시스템 정보를 최소화 (마지막 줄만 기록).
        stderr_tail = result.stderr.strip().splitlines()[-1] if result.stderr else ""
        raise RuntimeError(f"mp3 변환 실패: {stderr_tail}")

    return mp3_path.resolve()
