"""
Whisper STT 변환기.

mp3/mp4 파일을 faster-whisper에 직접 전달해 텍스트로 변환한다.
wav 중간 파일은 생성하지 않는다.
"""

import gc
import threading
from pathlib import Path

from src.logger import get_logger

_log = get_logger("stt")

# 모델 싱글톤 캐시: 동일 크기 모델은 재사용, 다른 크기 요청 시 기존 해제
_model_cache: dict = {}
_model_lock = threading.Lock()

# 각 Whisper 모델 로드 시 필요한 대략적 RAM (MB). int8 quantization 기준.
# faster-whisper 공식 가이드 수치에 여유(+50%)를 더한 실전 최저선.
# 현재 가용 메모리가 이 값 미만이면 더 작은 모델로 자동 다운그레이드.
_MODEL_RAM_BUDGET_MB = {
    "tiny": 500,
    "base": 1000,
    "small": 2500,
    "medium": 5500,
    "large": 10500,
}

# 다운그레이드 우선순위 (큰 것 → 작은 것)
_MODEL_FALLBACK_ORDER = ("large", "medium", "small", "base", "tiny")


def _available_memory_mb() -> int | None:
    """현재 프로세스가 쓸 수 있는 대략적 RAM (MB). 측정 불가 시 None."""
    # psutil 이 없는 환경(Docker minimal, 외부 Python) 도 대응 — 실패 시 건너뜀.
    try:
        import psutil  # type: ignore[import-not-found]

        return int(psutil.virtual_memory().available / (1024 * 1024))
    except ImportError:
        pass

    # POSIX fallback — /proc/meminfo 의 MemAvailable 파싱.
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    kb = int(line.split()[1])
                    return kb // 1024
    except (OSError, ValueError):
        pass
    return None


def _resolve_model_size(requested: str) -> str:
    """요청 모델이 가용 메모리에 비해 과하면 자동으로 더 작은 모델로 다운그레이드.

    large 요청 + 4GB 가용 상황에서 OOM kill 로 프로세스가 조용히 죽던 문제 방지.
    메모리 측정이 불가능하면 요청 모델을 그대로 반환 (Docker 등 정확한 측정이
    어려운 환경에서는 기존 동작 유지).
    """
    available = _available_memory_mb()
    if available is None:
        return requested
    needed = _MODEL_RAM_BUDGET_MB.get(requested)
    if needed is None or available >= needed:
        return requested
    # requested 로부터 더 작은 쪽으로 순차 downgrade.
    try:
        idx = _MODEL_FALLBACK_ORDER.index(requested)
    except ValueError:
        return requested
    for fallback in _MODEL_FALLBACK_ORDER[idx + 1 :]:
        if available >= _MODEL_RAM_BUDGET_MB.get(fallback, 10_000):
            _log.warning(
                "Whisper %s 모델은 RAM %dMB 필요하나 가용 %dMB — %s 로 다운그레이드",
                requested, needed, available, fallback,
            )
            return fallback
    # 가장 작은 모델도 부족 — 그래도 tiny 로 시도 (실패는 호출자가 처리)
    _log.warning(
        "Whisper 모든 모델이 RAM 부족 (가용 %dMB) — tiny 로 시도", available,
    )
    return "tiny"


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

    # OOM preflight — RAM 부족 시 자동 다운그레이드
    effective_size = _resolve_model_size(model_size)

    with _model_lock:
        if effective_size not in _model_cache:
            _release_model()
            _model_cache[effective_size] = WhisperModel(effective_size, device="cpu", compute_type="int8")
        model = _model_cache[effective_size]

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
