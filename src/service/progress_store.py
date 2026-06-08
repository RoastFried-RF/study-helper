"""자동 모드 진행 상태 저장소.

auto_progress.json 스키마:

v1 (legacy):
    ["url1", "url2", ...]                               # 처리 완료된 강의 URL 리스트

v2 (current):
    {
        "version": 2,
        "entries": {
            "<url>": {
                "played": bool,            # 재생(출석) 성공 여부
                "downloaded": bool | null, # 파일 다운로드 완료 여부 (null=미확인)
                "downloadable": bool | null, # 구조적 다운로드 가능 여부 (learningx→false)
                "reason": str | null,      # 실패 사유 (Phase 1 reason 상수)
                "ts": str,                 # 마지막 업데이트 ISO-8601
                "play_fail_count": int     # 누적 재생 실패 횟수 (BUG-5 격리 임계 측정)
            },
            ...
        }
    }

v1 → v2 자동 마이그레이션은 load 시점에 수행된다.
저장은 항상 v2 포맷. 원자적 교체(.tmp → rename)로 쓰기 중 크래시를 방어한다.

영속화 모델 (M2/M6):
    in-memory `entries` 가 SoT. `flush()` 가 유일한 저장 경로 — `locked_transaction`
    안에서 **디스크 최신본을 다시 읽어** 이번 프로세스가 건드린 entry(`_touched`)·
    삭제한 entry(`_removed`) 만 merge 한 뒤 원자적으로 쓴다. 따라서 자동 모드와
    유지보수 스크립트(recover/reconcile)가 동시 실행돼도 서로의 변경을 덮어쓰지
    않는다(lost update 방지). `maybe_flush()` 는 `_dirty` 누적이 임계를 넘을 때만
    `flush()` — 강의마다 전체 직렬화하던 O(N²) 비용을 줄인다.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from src.config import KST, RetryPolicy
from src.downloader.result import REASON_PLAY_QUARANTINED
from src.logger import get_logger

_log = get_logger("service.progress_store")

# M6: flush 배치 임계 — _dirty 가 이 값 이상 누적되면 maybe_flush() 가 flush().
# PROGRESS_SAVE_INTERVAL=1 이면 강의마다 저장(기존 per-item 동작).
def _resolve_save_interval() -> int:
    """PROGRESS_SAVE_INTERVAL 환경변수를 안전하게 정수로 해석한다.

    L1: 비숫자 값(예: "abc")이면 int() 가 ValueError 를 던져 모듈 import 자체가
    실패, auto 모드 기동이 막힌다. 잘못된 값은 경고 후 기본값 5 로 폴백한다.
    """
    raw = os.environ.get("PROGRESS_SAVE_INTERVAL", "5") or "5"
    try:
        return max(1, int(raw))
    except ValueError:
        _log.warning("PROGRESS_SAVE_INTERVAL 값이 정수가 아님 (%r) — 기본값 5 사용", raw)
        return 5


_SAVE_INTERVAL = _resolve_save_interval()


@dataclass
class ProgressEntry:
    played: bool = False
    downloaded: bool | None = None
    downloadable: bool | None = None
    reason: str | None = None
    ts: str = ""
    # BUG-5: 누적 재생 실패 카운터. 임계 초과 시 영구 격리 (mark_play_failed 참조).
    # 기존 v2 데이터에 필드가 없어도 0 으로 안전하게 로드된다.
    play_fail_count: int = 0


# BUG-5: 누적 재생 실패 임계는 ARCH-010 (재시도 정책 단일 관리) 에 따라
# Config.RetryPolicy.PLAY_FAIL_QUARANTINE 로 이관. 본 모듈은 default 인자에서만
# 참조한다.
PLAY_FAIL_QUARANTINE_THRESHOLD = RetryPolicy.PLAY_FAIL_QUARANTINE


def _parse_entries(raw: Any) -> dict[str, ProgressEntry]:
    """auto_progress.json 원본(JSON 파싱 결과)을 entries dict 로 변환한다.

    v1 리스트 / v2 dict 모두 처리. 알 수 없는 포맷은 빈 dict.
    """
    # v1: 리스트 → 모든 URL을 "재생 완료, 다운로드/가능 여부 미확인"으로 마이그레이션
    if isinstance(raw, list):
        return {
            url: ProgressEntry(played=True, downloaded=None, downloadable=None)
            for url in raw
            if isinstance(url, str)
        }

    # v2
    if isinstance(raw, dict) and raw.get("version") == 2:
        entries_raw = raw.get("entries", {})
        if isinstance(entries_raw, dict):
            def _to_int(v: Any) -> int:
                try:
                    return int(v) if v is not None else 0
                except (TypeError, ValueError):
                    return 0

            return {
                url: ProgressEntry(
                    played=bool(data.get("played", False)),
                    downloaded=data.get("downloaded"),
                    downloadable=data.get("downloadable"),
                    reason=data.get("reason"),
                    ts=str(data.get("ts", "")),
                    play_fail_count=_to_int(data.get("play_fail_count", 0)),
                )
                for url, data in entries_raw.items()
                if isinstance(url, str) and isinstance(data, dict)
            }

    # 알 수 없는 포맷 → 안전하게 비움
    return {}


def _read_entries_file(path: Path) -> dict[str, ProgressEntry]:
    """디스크에서 entries 를 읽어 반환한다 (lock 없음). 부재/파손 시 빈 dict."""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return _parse_entries(raw)


def _serialize_entries(entries: dict[str, ProgressEntry]) -> str:
    """entries 를 v2 JSON 문자열로 직렬화한다."""
    payload = {
        "version": 2,
        "entries": {url: asdict(entry) for url, entry in entries.items()},
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


@dataclass
class ProgressStore:
    """url → ProgressEntry 매핑을 메모리에 보관하고 파일과 동기화한다.

    in-memory `entries` 는 **작업 상태**다. 변경은 mark_* / retain_only / remove 로만
    하며 `flush()` (또는 `maybe_flush()`) 로 디스크에 cross-process 안전하게 반영한다.
    단, SoT 는 영구히 in-memory 가 아니라 **flush 경계에서 디스크와 재수렴**한다 —
    `flush()` 는 디스크 최신본에 이번 프로세스의 delta 를 merge 한 결과로 `entries` 를
    교체하므로, flush 직후 `entries` 는 타 프로세스 변경까지 반영한 최신 상태가 된다.
    """

    path: Path
    entries: dict[str, ProgressEntry] = field(default_factory=dict)
    # delta 추적 (M2/M6) — flush 시 merge 대상. init/repr 제외.
    _touched: set[str] = field(default_factory=set, init=False, repr=False)
    _removed: set[str] = field(default_factory=set, init=False, repr=False)
    _dirty: int = field(default=0, init=False, repr=False)

    # ── delta 추적 헬퍼 ──────────────────────────────────────
    def _touch(self, url: str) -> None:
        """mark_* mutator 가 호출 — 변경된 entry 를 flush merge 대상에 등록."""
        self._touched.add(url)
        self._removed.discard(url)
        self._dirty += 1

    def _mark_removed(self, url: str) -> None:
        """retain_only/remove 가 호출 — 삭제 의도를 flush merge 대상에 등록."""
        self._removed.add(url)
        self._touched.discard(url)
        self._dirty += 1

    # ── 로드 ─────────────────────────────────────────────────
    def load(self) -> None:
        """디스크에서 entries 를 읽어 in-memory 상태를 초기화한다.

        load 직후 delta(_touched/_removed/_dirty)는 비어있다 — 디스크와 동기 상태.
        """
        self.entries = _read_entries_file(self.path)
        self._touched.clear()
        self._removed.clear()
        self._dirty = 0

    # ── 저장 ─────────────────────────────────────────────────
    def flush(self) -> None:
        """in-memory delta 를 디스크에 cross-process 안전하게 반영한다.

        ARCH-011 / LOG-009 / M2: `locked_transaction` 으로 단일 file_lock 안에서
        디스크 최신본을 다시 읽어, 이번 프로세스가 건드린 entry(`_touched`)·삭제한
        entry(`_removed`)만 merge 한 뒤 atomic_write 한다. 건드리지 않은 URL 은
        디스크본을 유지 — 동시 실행 중인 recover/reconcile 의 변경을 보존한다.

        flush 종료 후 `self.entries` 는 merge 결과로 교체된다 — SoT 가 flush 경계에서
        디스크와 재수렴함을 뜻한다 (class docstring 참조). 이후 조회는 타 프로세스
        변경까지 반영한 상태를 본다.

        POSIX flock 은 직렬화 보장. Windows 는 best-effort advisory(직렬화 미보장).
        """
        if self._dirty == 0:
            return

        from src.util.atomic_write import locked_transaction

        with locked_transaction(
            self.path,
            load_fn=lambda: _read_entries_file(self.path),
            save_fn=lambda merged: _write_entries_file(self.path, merged),
        ) as disk:
            for url in self._touched:
                entry = self.entries.get(url)
                if entry is not None:
                    disk[url] = entry
            for url in self._removed:
                disk.pop(url, None)
            # in-memory 를 merge 결과로 동기화 — 이후 조회가 타 프로세스 변경도 반영.
            self.entries = dict(disk)

        self._touched.clear()
        self._removed.clear()
        self._dirty = 0

    def maybe_flush(self) -> None:
        """누적 변경(`_dirty`)이 `_SAVE_INTERVAL` 이상이면 flush (M6 배치 저장)."""
        if self._dirty >= _SAVE_INTERVAL:
            self.flush()

    def save(self) -> None:
        """flush() 의 하위호환 alias — 단일 저장 경로(flush)로 수렴.

        호출 시점의 in-memory delta 를 즉시 디스크에 반영한다.
        """
        self.flush()

    # ── 조회 ─────────────────────────────────────────────────
    def get(self, url: str) -> ProgressEntry | None:
        return self.entries.get(url)

    def is_fully_done(self, url: str) -> bool:
        """재생 완료 + (다운로드 완료 OR 다운로드 불가)이면 True."""
        e = self.entries.get(url)
        if not e or not e.played:
            return False
        if e.downloadable is False:
            return True
        return e.downloaded is True

    def needs_download_retry(self, url: str) -> bool:
        """재생은 완료됐지만 다운로드가 아직 성공하지 못했고, 구조적으로 가능한 경우."""
        e = self.entries.get(url)
        if not e or not e.played:
            return False
        if e.downloadable is False:
            return False
        return e.downloaded is not True

    def known_urls(self) -> set[str]:
        return set(self.entries.keys())

    # ── 변경 ─────────────────────────────────────────────────
    def _now(self) -> str:
        return datetime.now(KST).isoformat(timespec="seconds")

    def mark_played(self, url: str) -> None:
        """재생(출석) 성공 기록.

        PROBLEM-A 수정: 정상 재생 성공 시 누적 실패 카운터를 0 으로 reset.
        이전에는 카운터가 단조 증가만 해서 LMS 일시 토글 + 일시 driver crash 가
        반복되면 정상 강의도 false-positive 격리될 위험이 있었다 ("일시적 실패"와
        "지속적 실패" 구분 불가). 재생 성공이라는 명확한 신호에서 reset.
        """
        e = self.entries.setdefault(url, ProgressEntry())
        e.played = True
        e.play_fail_count = 0
        e.ts = self._now()
        self._touch(url)

    def mark_incomplete(self, url: str) -> None:
        """LMS가 해당 항목을 다시 미완료로 바꾼 경우 store의 played 상태를 해제한다.

        downloaded도 None(미확인)으로 되돌려 다음 사이클에 풀 파이프라인으로 재진입하게 한다.
        ARCH-013: entry None은 "예상 밖 호출"이므로 진단용 warning을 남긴다.
        """
        e = self.entries.get(url)
        if e is None:
            _log.warning("mark_incomplete: 추적되지 않는 URL — url=%s", url)
            return
        e.played = False
        e.downloaded = None
        e.ts = self._now()
        self._touch(url)

    def mark_unsupported(self, url: str, reason: str | None = None) -> None:
        e = self.entries.setdefault(url, ProgressEntry())
        e.played = True  # 재생 자체는 어쨌든 완료됐거나 별도 판정 대상
        e.downloadable = False
        e.downloaded = False
        e.reason = reason
        e.ts = self._now()
        self._touch(url)

    def mark_play_failed(
        self, url: str, threshold: int = PLAY_FAIL_QUARANTINE_THRESHOLD,
    ) -> bool:
        """재생 시도 실패를 기록하고 누적 임계 초과 시 격리한다 (BUG-5).

        호출 흐름:
            - `_process_lecture` 가 재생 3회 재시도 모두 실패 (PlayResult(played=False,
              reason=REASON_PLAY_FAILED)) 한 강의에 대해 호출
            - 누적 카운터 증가 → 임계 도달 시 mark_unsupported 로 격리
            - 격리되면 True 반환 → 호출자가 텔레그램 알림 1 회 발송 가능

        threshold: 격리 임계 (기본은 RetryPolicy.PLAY_FAIL_QUARANTINE). 일시적
            driver crash 등으로 인한 false-positive 격리를 막기 위해 보수적으로 잡는다.

        Returns: **transition-edge bool** — 매 호출이 아닌, "격리 transition 이 일어난
            this 호출에서만" True. 같은 강의에 임계 도달 후 재호출되어도 두 번째부터는
            False 반환 (이미 downloadable=False) — 호출자의 텔레그램 알림 중복 방지.

            True  — 이번 호출로 격리됨 (호출자 알림 트리거)
            False — 아직 임계 미달 OR 이미 격리됨 (이중 트리거 방지)

        CQS 주의: 카운터 mutation (Command) 과 transition-edge query (Query) 가
            합쳐진 형태. atomic transition 보장을 위해 의도적으로 합침 — 분리하면
            동시 호출 race window 가 생긴다 (asyncio 단일 task 라 실제 race 는 없지만
            계약 측면에서 atomic 가 안전).
        """
        e = self.entries.setdefault(url, ProgressEntry())
        e.play_fail_count += 1
        e.ts = self._now()
        self._touch(url)

        if e.play_fail_count >= threshold and e.downloadable is not False:
            # 격리 — 더 이상 재생 큐에 넣지 않음. mark_unsupported 와 동등한 effect
            # (played=True, downloadable=False) 로 is_fully_done=True 가 되어
            # 자동 모드 루프에서 자연스럽게 빠진다.
            e.played = True
            e.downloadable = False
            e.downloaded = False
            e.reason = REASON_PLAY_QUARANTINED
            return True
        return False

    def mark_download_success(self, url: str) -> None:
        e = self.entries.setdefault(url, ProgressEntry())
        e.downloaded = True
        e.downloadable = True
        e.reason = None
        e.ts = self._now()
        self._touch(url)

    def mark_download_failed(self, url: str, reason: str) -> None:
        e = self.entries.setdefault(url, ProgressEntry())
        # downloadable은 유지 — 네트워크 실패 등은 재시도 여지가 있으므로 True로 간주
        if e.downloadable is None:
            e.downloadable = True
        e.downloaded = False
        e.reason = reason
        e.ts = self._now()
        self._touch(url)

    def mark_download_confirmed_from_filesystem(self, url: str) -> None:
        """파일시스템 점검 결과 이미 파일이 존재할 때 사용.

        파일이 실제로 존재한다는 게 확정 증거이므로 이전에 기록돼 있던 실패
        reason (예: suspicious_stub) 은 더 이상 유효하지 않아 함께 리셋한다.
        """
        e = self.entries.setdefault(url, ProgressEntry())
        e.downloaded = True
        e.downloadable = True
        e.reason = None
        if not e.ts:
            e.ts = self._now()
        self._touch(url)

    def remove(self, url: str) -> bool:
        removed = self.entries.pop(url, None) is not None
        if removed:
            self._mark_removed(url)
        return removed

    def retain_only(self, allowed_urls: set[str]) -> int:
        """LMS에서 사라진 항목을 제거한다. 반환값은 제거된 개수.

        BUG-2 안전망: 빈 set 으로 호출되면 모든 entry 가 제거되는 catastrophic
        삭제가 발생하므로 0 을 반환하고 skip. 호출자가 fetch 부분 실패 가드를
        거치는 것이 1차 방어선이고, 본 가드는 호출자 회귀에 대한 2차 방어.
        """
        if not allowed_urls:
            return 0
        orphan = self.known_urls() - allowed_urls
        for url in orphan:
            del self.entries[url]
            self._mark_removed(url)
        return len(orphan)


def _write_entries_file(path: Path, entries: dict[str, ProgressEntry]) -> None:
    """entries 를 v2 JSON 으로 원자적으로 기록한다 (lock 없음).

    `locked_transaction` 의 save_fn 으로 쓰이므로 자체 file_lock 을 잡지 않는다
    (중첩 flock = self-deadlock, WS-0 제약).
    """
    from src.util.atomic_write import atomic_write_text

    atomic_write_text(path, _serialize_entries(entries), mode=0o600)
