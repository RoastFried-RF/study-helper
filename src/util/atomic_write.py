"""공용 atomic write / cross-process lock 모듈.

프로젝트 내 여러 모듈(`config._save_env`, `progress_store.save`,
`deadline_checker._save_notified`)이 각자 다른 atomic write 패턴을
구현하고 있었다. 가장 엄격한 progress_store.save 구현을 기준으로 통합한다.

설계 원칙:
- `O_CREAT | O_EXCL | O_WRONLY | O_TRUNC` + `O_NOFOLLOW`(POSIX) 로
  레이스/심볼릭 링크 공격을 차단한다.
- 생성 시점에 `0o600` 권한을 부여 (POSIX). Windows 는 noop.
- `fsync` 후 `replace` 로 교체하여 쓰기 중 크래시에도 원본 보존.
- `flock`(POSIX) / `msvcrt.locking`(Windows) 로 cross-process 직렬화.
  Windows 는 advisory — 획득 실패 시 warning 로그 후 진행(best-effort).
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)


def atomic_write_text(path: Path, text: str, *, mode: int = 0o600, encoding: str = "utf-8") -> None:
    """경로에 텍스트를 원자적으로 쓴다.

    이미 tmp 파일이 남아있으면 정리한 뒤 `O_EXCL` 로 재생성한다.
    성공 시 `path` 는 `mode` 권한(POSIX)으로 남는다.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")

    # stale tmp 정리 — 이전 크래시 이후 남아있을 수 있음
    with contextlib.suppress(FileNotFoundError, OSError):
        tmp.unlink()

    flags = os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    fd = os.open(str(tmp), flags, mode)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
            f.flush()
            with contextlib.suppress(OSError):
                os.fsync(f.fileno())
    except Exception:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise

    tmp.replace(path)
    # POSIX 에서는 O_CREAT 시점에 이미 mode 설정. Windows chmod 는 noop.
    with contextlib.suppress(OSError):
        path.chmod(mode)


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Cross-process 배타적 파일 락을 획득한다.

    POSIX: `fcntl.flock(LOCK_EX)` — 블로킹, 직렬화 보장.
    Windows: `msvcrt.locking(LK_NBLCK)` — non-blocking advisory.
      획득 실패 시 warning 로그 후 락 없이 진행(best-effort).

    락 파일 자체는 `{path}.lock` 으로 생성한다.
    동일 프로세스 내 재진입은 보장하지 않으므로 호출자가 조정해야 한다.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    # SEC: lock 파일 symlink-following 차단 — atomic_write_text 와 동일하게 O_NOFOLLOW.
    _lock_flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        _lock_flags |= os.O_NOFOLLOW
    fd = os.open(str(lock_path), _lock_flags, 0o600)
    acquired = False
    try:
        if sys.platform == "win32":
            import msvcrt

            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                acquired = True
            except OSError as e:
                _log.warning(
                    "file_lock: Windows advisory lock 획득 실패 — best-effort 모드로 진행: %s", e
                )
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
            acquired = True
        yield
    finally:
        try:
            if acquired:
                if sys.platform == "win32":
                    import msvcrt

                    with contextlib.suppress(OSError):
                        # LK_UNLCK 는 locking() 호출과 동일 오프셋을 요구. 파일 시작이라 OK.
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    with contextlib.suppress(OSError):
                        fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            with contextlib.suppress(OSError):
                os.close(fd)


@contextlib.contextmanager
def locked_transaction(
    path: Path,
    *,
    load_fn: Callable[[], Any],
    save_fn: Callable[[Any], None],
) -> Iterator[Any]:
    """단일 `file_lock` 안에서 load → yield(mutate) → save 를 원자적으로 수행한다.

    `.env` / `auto_progress.json` / `deadline_notified.json` 처럼 "디스크에서 읽어
    수정 후 다시 쓰는" 상태 파일의 lost-update (load 는 락 밖, write 만 락 안이라
    두 프로세스가 서로의 변경을 덮어쓰던 문제) 를 막는다.

    사용:
        with locked_transaction(path, load_fn=_load, save_fn=_save) as data:
            data[key] = value          # mutate

    제약 (반드시 준수):
    - **재진입 금지**: POSIX `flock(LOCK_EX)` 는 동일 프로세스가 같은 path 에 중첩
      호출하면 self-deadlock 한다. `load_fn` / `save_fn` / with 본문 안에서 같은
      path 의 `locked_transaction` / `file_lock` 을 다시 호출하지 말 것.
    - **Windows 한계**: Windows `file_lock` 은 non-blocking advisory(`LK_NBLCK`) 이므로
      cross-process 직렬화를 보장하지 않는다. 강보장이 필요한 환경은 Docker/Linux
      (flock) 를 사용한다.

    with 본문에서 예외가 발생하면 `save_fn` 을 호출하지 않는다 (corrupt 데이터
    저장 방지). 정상 완료 시에만 저장한다.
    """
    with file_lock(path):
        data = load_fn()
        ok = False
        try:
            yield data
            ok = True
        finally:
            if ok:
                save_fn(data)
