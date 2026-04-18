"""
Whisper STT 변환기.

mp3/mp4 파일을 faster-whisper에 직접 전달해 텍스트로 변환한다.
wav 중간 파일은 생성하지 않는다.
"""

import gc
import logging
import threading
from pathlib import Path

_log = logging.getLogger(__name__)

# 모델 싱글톤 캐시: 동일 크기 모델은 재사용, 다른 크기 요청 시 기존 해제
_model_cache: dict = {}
_model_lock = threading.Lock()


def _release_model() -> None:
    """캐시된 모델을 명시적으로 해제하고 GC를 강제 실행한다."""
    for key in list(_model_cache):
        del _model_cache[key]
    gc.collect()


def unload_model() -> None:
    """외부에서 모델을 명시적으로 해제할 때 사용한다."""
    with _model_lock:
        _release_model()


def transcribe(audio_path: Path, model_size: str = "base", language: str = "") -> Path:
    """
    faster-whisper로 음성 파일을 텍스트로 변환한다.

    Args:
        audio_path: mp3 또는 mp4 파일 경로
        model_size: Whisper 모델 크기 (tiny/base/small/medium/large)
        language: 언어 코드 (예: "ko"). 빈 문자열이면 자동 감지.

    Returns:
        생성된 .txt 파일 경로
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise RuntimeError(
            "faster-whisper 패키지가 설치되어 있지 않습니다.\n설치: pip install faster-whisper"
        ) from None

    with _model_lock:
        if model_size not in _model_cache:
            _release_model()
            _model_cache[model_size] = WhisperModel(model_size, device="cpu", compute_type="int8")
        model = _model_cache[model_size]

    transcribe_kwargs = {}
    if language:
        transcribe_kwargs["language"] = language
    segments, _ = model.transcribe(str(audio_path), **transcribe_kwargs)

    # 세그먼트를 스트리밍으로 파일에 직접 기록하여 전체 텍스트 메모리 적재 방지.
    # B4: 세그먼트 개수를 집계해 무음/빈 결과를 가시화하고 후속 파이프라인이
    # 빈 파일로 요약 시도하지 않도록 판단 근거를 로깅한다.
    txt_path = audio_path.with_suffix(".txt")
    segment_count = 0
    total_chars = 0
    with open(txt_path, "w", encoding="utf-8") as f:
        for segment in segments:
            text = segment.text
            f.write(text)
            segment_count += 1
            total_chars += len(text)

    if segment_count == 0 or total_chars == 0:
        _log.warning(
            "STT 결과 비어 있음 — 무음/저음량 가능 (segments=%d, chars=%d): %s",
            segment_count, total_chars, audio_path.name,
        )
    else:
        _log.info("STT 완료 — segments=%d chars=%d: %s", segment_count, total_chars, audio_path.name)
    return txt_path


def is_transcript_usable(txt_path: Path, min_chars: int = 10) -> bool:
    """STT 결과가 요약 단계로 넘길 만큼 내용이 있는지 판정한다.

    공백/개행만 있거나 `min_chars` 미만이면 False. summarize 호출 전에
    빈 결과를 감지해 쓸데없는 API 비용/실패 알림을 방지한다.
    """
    try:
        text = txt_path.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return len(text) >= min_chars
