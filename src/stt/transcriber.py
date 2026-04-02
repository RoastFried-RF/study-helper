"""
Whisper STT 변환기.

mp3/mp4 파일을 faster-whisper에 직접 전달해 텍스트로 변환한다.
wav 중간 파일은 생성하지 않는다.
"""

import gc
import threading
from pathlib import Path

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

    # 세그먼트를 스트리밍으로 파일에 직접 기록하여 전체 텍스트 메모리 적재 방지
    txt_path = audio_path.with_suffix(".txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        for segment in segments:
            f.write(segment.text)
    return txt_path
