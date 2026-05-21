"""transcriber.py 단위 테스트 — 경로 생성 로직만 검증 (Whisper 모델 로드 없음)."""

import sys
import threading
import time
from unittest.mock import MagicMock

import pytest


def _setup_mock_faster_whisper():
    """faster_whisper 모듈을 mock으로 등록한다."""
    mock_module = MagicMock()
    sys.modules["faster_whisper"] = mock_module
    return mock_module


def test_transcribe_output_path(tmp_path):
    """출력 파일이 .txt 확장자로 생성되어야 한다."""
    _setup_mock_faster_whisper()
    audio = tmp_path / "lecture.mp3"
    audio.write_bytes(b"fake audio")

    mock_model = MagicMock()
    mock_segment = MagicMock()
    mock_segment.text = "테스트 텍스트"
    mock_model.transcribe.return_value = ([mock_segment], None)

    import src.stt.transcriber as mod

    mod._model_cache.clear()
    mod._model_cache["base"] = mock_model

    result = mod.transcribe(audio, model_size="base")
    assert result.suffix == ".txt"
    assert result.stem == "lecture"
    assert result.read_text(encoding="utf-8") == "테스트 텍스트"

    mod._model_cache.clear()


def test_transcribe_with_language(tmp_path):
    """language 파라미터가 전달되어야 한다."""
    _setup_mock_faster_whisper()
    audio = tmp_path / "lecture.mp4"
    audio.write_bytes(b"fake")

    mock_model = MagicMock()
    mock_model.transcribe.return_value = ([], MagicMock())

    import src.stt.transcriber as mod

    mod._model_cache.clear()
    mod._model_cache["base"] = mock_model

    mod.transcribe(audio, model_size="base", language="ko")
    mock_model.transcribe.assert_called_once_with(str(audio), language="ko")

    mod._model_cache.clear()


def test_transcribe_without_language(tmp_path):
    """language가 빈 문자열이면 kwargs에 포함되지 않아야 한다."""
    _setup_mock_faster_whisper()
    audio = tmp_path / "lecture.mp4"
    audio.write_bytes(b"fake")

    mock_model = MagicMock()
    mock_model.transcribe.return_value = ([], MagicMock())

    import src.stt.transcriber as mod

    mod._model_cache.clear()
    mod._model_cache["base"] = mock_model

    mod.transcribe(audio, model_size="base", language="")
    mock_model.transcribe.assert_called_once_with(str(audio))

    mod._model_cache.clear()


# ── NEW-06: 세그먼트 루프 중 예외 시 부분 txt 잔존 금지 ──────────────────


def test_transcribe_partial_txt_removed_on_segment_exception(tmp_path):
    """NEW-06: STT 세그먼트 루프 도중 예외가 나면 부분 txt 가 남으면 안 된다.

    수정 전: 세그먼트 디코드 예외 시 부분 .txt 가 잔존해 다음 실행의
    is_transcript_usable 가드를 통과 → 불완전 요약 발생.
    수정 후: .txt.partial 임시 파일에 기록하고 정상 완료 시에만 rename,
    예외 시 임시 파일 제거 + 예외 전파. 최종 .txt 는 생성되지 않아야 함.
    """
    _setup_mock_faster_whisper()
    audio = tmp_path / "lecture.mp3"
    audio.write_bytes(b"fake audio")

    def _exploding_segments():
        # 첫 세그먼트는 정상, 두 번째에서 디코드 예외 발생.
        yield MagicMock(text="앞부분 텍스트")
        raise RuntimeError("decode error")

    mock_model = MagicMock()
    mock_model.transcribe.return_value = (_exploding_segments(), None)

    import src.stt.transcriber as mod

    mod._model_cache.clear()
    mod._model_cache["base"] = mock_model

    txt_path = audio.with_suffix(".txt")
    partial_path = txt_path.with_suffix(".txt.partial")

    with pytest.raises(RuntimeError, match="decode error"):
        mod.transcribe(audio, model_size="base")

    assert not txt_path.exists(), "예외 시 최종 .txt 가 생성되면 안 됨 (NEW-06)"
    assert not partial_path.exists(), "예외 시 .txt.partial 임시 파일도 제거돼야 함"

    mod._model_cache.clear()


def test_transcribe_atomic_rename_on_success(tmp_path):
    """정상 완료 시 .txt.partial 은 최종 .txt 로 rename 되고 잔존하지 않는다."""
    _setup_mock_faster_whisper()
    audio = tmp_path / "lecture.mp3"
    audio.write_bytes(b"fake audio")

    mock_model = MagicMock()
    seg1 = MagicMock()
    seg1.text = "앞부분 "
    seg2 = MagicMock()
    seg2.text = "뒷부분"
    mock_model.transcribe.return_value = ([seg1, seg2], None)

    import src.stt.transcriber as mod

    mod._model_cache.clear()
    mod._model_cache["base"] = mock_model

    result = mod.transcribe(audio, model_size="base")
    partial_path = result.with_suffix(".txt.partial")

    assert result.exists()
    assert result.read_text(encoding="utf-8") == "앞부분 뒷부분"
    assert not partial_path.exists(), "정상 완료 후 .txt.partial 잔존 금지"

    mod._model_cache.clear()


# ── NEW-08: 전사 직렬화 락 — 동시 호출이 직렬화 ──────────────────────────


def test_transcribe_lock_serializes_concurrent_calls(tmp_path):
    """NEW-08: 전사 직렬화 락(_transcribe_lock)이 model.transcribe + 세그먼트
    소비를 직렬화하는지 검증한다.

    두 스레드가 동시에 transcribe 를 호출할 때, model.transcribe 핸들러가
    겹쳐 실행되면 같은 캐시 WhisperModel race 가 발생한다. 락이 정상 동작하면
    한 번에 한 호출만 임계영역에 들어가므로 동시 진입 카운터가 1 을 넘지
    않아야 한다.
    """
    _setup_mock_faster_whisper()

    import src.stt.transcriber as mod

    mod._model_cache.clear()

    state = {"active": 0, "max_active": 0}
    state_lock = threading.Lock()

    def _slow_transcribe(*args, **kwargs):
        with state_lock:
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
        # 임계영역 안에서 시간을 보내 락이 없으면 겹치도록 유도.
        time.sleep(0.05)
        with state_lock:
            state["active"] -= 1
        return ([], None)

    mock_model = MagicMock()
    mock_model.transcribe.side_effect = _slow_transcribe
    mod._model_cache["base"] = mock_model

    def _worker(name: str):
        audio = tmp_path / f"{name}.mp3"
        audio.write_bytes(b"fake")
        mod.transcribe(audio, model_size="base")

    threads = [
        threading.Thread(target=_worker, args=(f"lec{i}",)) for i in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert state["max_active"] == 1, (
        f"전사가 직렬화되지 않음 — 동시 진입 {state['max_active']} (NEW-08)"
    )

    mod._model_cache.clear()


def test_transcribe_lock_and_unload_lock_ordering():
    """전사 락과 모델 락이 서로 다른 락이며 unload 가 전사 락을 사용한다.

    데드락 방지를 위해 락 순서는 항상 _transcribe_lock → _model_lock 이어야
    하고, 두 락은 별개 객체여야 한다 (전사와 모델 로드가 서로 막지 않음).
    """
    import src.stt.transcriber as mod

    assert mod._transcribe_lock is not mod._model_lock, (
        "전사 락과 모델 락은 별개 객체여야 함 (NEW-08)"
    )
    # unload_model 이 전사 락을 잡아 in-flight 전사 완료를 기다리는지 — 락이
    # 이미 점유 중이면 unload 가 진입하지 못함을 확인.
    acquired = mod._transcribe_lock.acquire(blocking=False)
    assert acquired, "테스트 시작 시 전사 락은 해제 상태여야 함"
    try:
        done = threading.Event()

        def _try_unload():
            mod.unload_model()
            done.set()

        t = threading.Thread(target=_try_unload, daemon=True)
        t.start()
        # 전사 락을 잡고 있는 동안 unload 는 완료되면 안 됨 (대기).
        assert not done.wait(timeout=0.2), (
            "전사 락 점유 중에도 unload 가 진행됨 — 직렬화 실패"
        )
    finally:
        mod._transcribe_lock.release()
    # 락 해제 후에는 unload 가 완료돼야 함.
    assert done.wait(timeout=1.0), "전사 락 해제 후 unload 가 완료돼야 함"
