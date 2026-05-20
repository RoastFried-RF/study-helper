"""atomic_write 모듈 테스트 — 원자성 + cross-process lock.

Windows 에서는 flock 대신 msvcrt.locking 사용. best-effort 라 락 테스트는
POSIX 에서만 직렬화를 강제 검증한다.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import sys
import threading
from pathlib import Path

import pytest

from src.util.atomic_write import atomic_write_text, file_lock, locked_transaction


def test_atomic_write_creates_file(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    atomic_write_text(target, "hello")
    assert target.read_text(encoding="utf-8") == "hello"


def test_atomic_write_overwrites_existing(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    target.write_text("old")
    atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "new"


def test_atomic_write_removes_stale_tmp(tmp_path: Path) -> None:
    """이전 크래시로 남은 `.tmp` 가 있어도 새 쓰기가 성공한다."""
    target = tmp_path / "out.txt"
    stale_tmp = tmp_path / "out.txt.tmp"
    stale_tmp.write_text("stale")
    atomic_write_text(target, "fresh")
    assert target.read_text(encoding="utf-8") == "fresh"
    assert not stale_tmp.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX chmod 전용")
def test_atomic_write_sets_permissions(tmp_path: Path) -> None:
    target = tmp_path / "secret.txt"
    atomic_write_text(target, "sensitive", mode=0o600)
    st = target.stat()
    # 권한 비트 중 소유자 rw 만 허용
    assert (st.st_mode & 0o777) == 0o600


@pytest.mark.skipif(sys.platform == "win32", reason="Windows msvcrt.locking 은 프로세스 수준만 — 스레드 serialize 불가")
def test_thread_serialization_with_lock(tmp_path: Path) -> None:
    """atomic_write_text 는 단일 writer 가정이므로 멀티 스레드에서는
    file_lock 으로 serialize 해야 한다. 결합 시 결과가 10개 value 중 하나로 수렴.

    주의: 스레드 serialize 가 목적이면 threading.Lock 을 써야 한다.
    file_lock 은 cross-process 용 — POSIX flock 은 스레드에서도 동작하지만
    Windows msvcrt.locking 은 프로세스 수준만 잠근다.
    """
    target = tmp_path / "out.txt"
    values = [f"value-{i:04d}" * 20 for i in range(10)]

    def writer(v: str) -> None:
        with file_lock(target):
            atomic_write_text(target, v)

    threads = [threading.Thread(target=writer, args=(v,)) for v in values]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    result = target.read_text(encoding="utf-8")
    assert result in values, f"partial write 감지: {result[:60]!r}"


def _worker_with_lock(lock_path_str: str, out_path_str: str, worker_id: int) -> None:
    """multiprocessing worker — 락 획득 후 파일에 자기 ID 를 append."""
    lock_path = Path(lock_path_str)
    out_path = Path(out_path_str)
    with file_lock(lock_path):
        # 락 안에서 짧은 작업 시뮬레이트 — 직렬화가 없으면 interleave 가 관찰됨
        existing = out_path.read_text(encoding="utf-8") if out_path.exists() else ""
        out_path.write_text(existing + f"start-{worker_id}\n", encoding="utf-8")
        # 약간의 지연으로 경쟁 상태를 드러냄
        import time as _time

        _time.sleep(0.05)
        existing2 = out_path.read_text(encoding="utf-8")
        out_path.write_text(existing2 + f"end-{worker_id}\n", encoding="utf-8")


@pytest.mark.skipif(sys.platform == "win32", reason="Windows msvcrt.locking 은 best-effort 라 직렬화 미보장")
def test_cross_process_lock_serializes_posix(tmp_path: Path) -> None:
    """POSIX 에서는 flock 으로 cross-process 직렬화를 보장해야 한다."""
    lock_path = tmp_path / "shared.dat"
    out_path = tmp_path / "order.log"
    out_path.write_text("", encoding="utf-8")

    ctx = mp.get_context("fork") if sys.platform != "win32" else mp.get_context()
    workers = [ctx.Process(target=_worker_with_lock, args=(str(lock_path), str(out_path), i)) for i in range(4)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=10)
        assert w.exitcode == 0

    lines = out_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 8, f"예상 8줄, 실제: {lines}"
    # 각 worker 의 start/end 가 인접해야 직렬화된 것 (interleave 되면 실패)
    for i in range(0, 8, 2):
        start_id = lines[i].split("-")[1]
        end_id = lines[i + 1].split("-")[1]
        assert start_id == end_id, f"interleave 감지: {lines}"


def test_lock_releases_on_exception(tmp_path: Path) -> None:
    """with 블록 내에서 예외가 나도 락은 해제되어 다음 획득이 가능해야 한다."""
    lock_path = tmp_path / "shared.dat"

    with pytest.raises(RuntimeError):
        with file_lock(lock_path):
            raise RuntimeError("boom")

    # 재획득 가능해야 함
    with file_lock(lock_path):
        pass


def test_lock_file_created_in_parent_dir(tmp_path: Path) -> None:
    """parent 디렉토리가 없으면 생성해야 한다."""
    target = tmp_path / "deep" / "nested" / "path.json"
    with file_lock(target):
        assert target.parent.exists()


# ── WS-0: locked_transaction ──────────────────────────────────


def test_locked_transaction_load_mutate_save(tmp_path: Path) -> None:
    """load → mutate(yield) → save 가 단일 lock 안에서 원자적으로 수행된다."""
    path = tmp_path / "data.json"
    path.write_text(json.dumps({"x": 1}), encoding="utf-8")

    with locked_transaction(
        path,
        load_fn=lambda: json.loads(path.read_text(encoding="utf-8")),
        save_fn=lambda d: path.write_text(json.dumps(d), encoding="utf-8"),
    ) as data:
        data["y"] = 2

    assert json.loads(path.read_text(encoding="utf-8")) == {"x": 1, "y": 2}


def test_locked_transaction_skips_save_on_exception(tmp_path: Path) -> None:
    """with 본문에서 예외 발생 시 save_fn 을 호출하지 않는다 (corrupt 방지)."""
    path = tmp_path / "data.json"
    path.write_text(json.dumps({"x": 1}), encoding="utf-8")

    with pytest.raises(RuntimeError):
        with locked_transaction(
            path,
            load_fn=lambda: json.loads(path.read_text(encoding="utf-8")),
            save_fn=lambda d: path.write_text(json.dumps(d), encoding="utf-8"),
        ) as data:
            data["y"] = 2
            raise RuntimeError("boom")

    # 예외 → save 건너뜀 → 원본 보존
    assert json.loads(path.read_text(encoding="utf-8")) == {"x": 1}


def test_locked_transaction_releases_lock_after_use(tmp_path: Path) -> None:
    """locked_transaction 종료 후 file_lock 이 해제되어 재획득 가능해야 한다."""
    path = tmp_path / "data.json"
    with locked_transaction(path, load_fn=lambda: {}, save_fn=lambda d: None):
        pass
    # 재획득 가능
    with file_lock(path):
        pass
