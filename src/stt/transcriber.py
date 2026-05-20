"""
Whisper STT 변환기.

mp3/mp4 파일을 faster-whisper에 직접 전달해 텍스트로 변환한다.
wav 중간 파일은 생성하지 않는다.
"""

import gc
import os
import threading
from pathlib import Path

from src.logger import get_logger

_log = get_logger("stt")

# 모델 싱글톤 캐시: 동일 크기 모델은 재사용, 다른 크기 요청 시 기존 해제
_model_cache: dict = {}
_model_lock = threading.Lock()

# NEW-08: 전사(model.transcribe + segment 소비) 전용 직렬화 락.
# `_model_lock`(로드/해제 전용, 짧은 임계영역)과 분리한 별도 락이다.
# 같은 캐시 WhisperModel 인스턴스를 여러 스레드(/transcribe 동시 요청)가
# 병렬 호출하면 CTranslate2 내부 상태 race 가 발생할 수 있어 전사 자체를
# 직렬화한다. 모델 로드와 전사가 서로 다른 락이라 데드락은 없다.
_transcribe_lock = threading.Lock()

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

# M4: CTranslate2 cpu_threads 기본 상한. docker-compose 에 CPU 제한이 없어
# 미지정 시 STT 가 호스트 전 코어를 포화시킨다.
_DEFAULT_CPU_THREADS = 2


def _resolve_cpu_threads() -> int:
    """WHISPER_CPU_THREADS env 로 cpu_threads 상한 결정 (기본 2).

    비정수/0 이하 같은 잘못된 값은 기본값으로 fallback.
    """
    raw = os.environ.get("WHISPER_CPU_THREADS", "").strip()
    try:
        n = int(raw)
    except ValueError:
        return _DEFAULT_CPU_THREADS
    return n if n > 0 else _DEFAULT_CPU_THREADS


def _read_int(path: Path) -> int | None:
    """cgroup 파일에서 정수를 읽는다. `max`/비정수/부재 시 None."""
    try:
        raw = path.read_text().strip()
    except OSError:
        return None
    if raw == "max":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _cgroup_available_mb() -> int | None:
    """컨테이너 cgroup 한도 기준 가용 메모리 (MB). cgroup 미설정/무제한 시 None.

    M3: `_available_memory_mb` 가 호스트 RAM 만 보면 Docker `mem_limit` 안에서 OOM
    가드가 무력화된다. cgroup v2(`memory.max`/`memory.current`) · v1
    (`memory.limit_in_bytes`/`memory.usage_in_bytes`) 의 **한도 - 현재사용량** 을
    가용량으로 계산한다 (한도 자체가 아님 — STT 로드 시점에 이미 점유 중인
    Python/Chromium 메모리를 차감해야 정확).
    """
    # cgroup v2
    limit = _read_int(Path("/sys/fs/cgroup/memory.max"))
    used = _read_int(Path("/sys/fs/cgroup/memory.current"))
    if limit is None:
        # cgroup v1
        limit = _read_int(Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"))
        used = _read_int(Path("/sys/fs/cgroup/memory/memory.usage_in_bytes"))
    if limit is None or limit >= 2**62:  # 무제한 sentinel
        return None
    free = max(0, limit - (used or 0))
    return free // (1024 * 1024)


def _available_memory_mb() -> int | None:
    """STT 모델 로드 가능 여부 판정용 가용 RAM (MB). 측정 불가 시 None.

    컨테이너 안에서는 cgroup 한도와 호스트 가용량의 **min** 을 채택해 가장
    보수적으로 다운그레이드를 판정한다 (cgroup-aware — M3).
    """
    cgroup_mb = _cgroup_available_mb()

    host_mb: int | None = None
    # psutil 이 없는 환경(Docker minimal, 외부 Python) 도 대응.
    try:
        import psutil  # type: ignore[import-not-found]

        host_mb = int(psutil.virtual_memory().available / (1024 * 1024))
    except ImportError:
        # POSIX fallback — /proc/meminfo 의 MemAvailable 파싱.
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        host_mb = int(line.split()[1]) // 1024
                        break
        except (OSError, ValueError):
            host_mb = None

    candidates = [v for v in (cgroup_mb, host_mb) if v is not None]
    return min(candidates) if candidates else None


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
    """외부에서 모델을 명시적으로 해제할 때 사용한다.

    NEW-08: in-flight 전사가 사용 중인 WhisperModel 을 해제하지 않도록
    `_transcribe_lock` 을 먼저 잡아 전사 완료를 기다린 뒤 해제한다.
    락 순서는 항상 `_transcribe_lock` → `_model_lock` 으로 일관 유지해
    데드락을 방지한다 (transcribe 도 동일 순서로 획득).
    """
    with _transcribe_lock, _model_lock:
        _release_model()


def safe_unload() -> None:
    """unload_model 을 안전하게 호출한다 (ARCH-014).

    STT 의존성(faster-whisper) 이 로드 안 된 경우/이미 해제된 경우에도
    예외 없이 no-op. 호출 사이트 6곳에서 반복하던 try/import/bare except
    패턴을 축약한다.
    """
    try:
        unload_model()
    except Exception:
        pass


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
            # M4: cpu_threads 미지정 시 CTranslate2 가 전 코어를 점유한다. docker-compose 에
            # CPU 제한이 없어 STT 가 호스트 CPU 를 포화시키므로 WHISPER_CPU_THREADS(기본 2)
            # 로 상한을 둔다. num_workers=1 — 단일 전사라 병렬 worker 불필요.
            _cpu_threads = _resolve_cpu_threads()
            _model_cache[effective_size] = WhisperModel(
                effective_size,
                device="cpu",
                compute_type="int8",
                cpu_threads=_cpu_threads,
                num_workers=1,
            )
        model = _model_cache[effective_size]

    transcribe_kwargs = {}
    if language:
        transcribe_kwargs["language"] = language

    # NEW-08: faster-whisper segment generator 는 lazy — 실제 디코딩은 아래
    # 루프 소비 시점에 일어난다. 같은 캐시 WhisperModel 의 병렬 전사 race 와
    # safe_unload() 와의 상호배제를 위해 전사 호출+소비 전체를 전용 락으로
    # 직렬화한다. `_model_lock`(로드 전용)과 분리한 별도 락이라 모델 로드와
    # 전사가 서로 막지 않고, 데드락 없이 전사끼리만 직렬화된다.
    txt_path = audio_path.with_suffix(".txt")
    # NEW-06: 세그먼트 루프 도중 디코드 예외가 나면 부분 txt 가 남아 다음
    # 실행의 is_transcript_usable 가드를 통과해 불완전 요약이 생긴다.
    # 임시 경로(.txt.partial)에 기록하고 루프 정상 완료 후에만 최종 경로로
    # rename 한다. 예외 시 임시 파일을 제거하고 전파한다.
    tmp_path = txt_path.with_suffix(".txt.partial")
    segment_count = 0
    total_chars = 0
    with _transcribe_lock:
        segments, _ = model.transcribe(str(audio_path), **transcribe_kwargs)
        try:
            # 세그먼트를 스트리밍으로 파일에 직접 기록하여 전체 텍스트 메모리 적재 방지.
            # B4: 세그먼트 개수를 집계해 무음/빈 결과를 가시화하고 후속 파이프라인이
            # 빈 파일로 요약 시도하지 않도록 판단 근거를 로깅한다.
            with open(tmp_path, "w", encoding="utf-8") as f:
                for segment in segments:
                    text = segment.text
                    f.write(text)
                    segment_count += 1
                    total_chars += len(text)
        except Exception:
            # 부분 txt 잔존 방지 — 임시 파일 제거 후 예외 전파.
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
            raise
    # 루프 정상 완료 — 임시 파일을 최종 경로로 원자적 교체.
    tmp_path.replace(txt_path)

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
