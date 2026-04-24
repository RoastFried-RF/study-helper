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
                "ts": str                  # 마지막 업데이트 ISO-8601
            },
            ...
        }
    }

v1 → v2 자동 마이그레이션은 load 시점에 수행된다.
저장은 항상 v2 포맷. 원자적 교체(.tmp → rename)로 쓰기 중 크래시를 방어한다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from src.config import KST

_log = logging.getLogger(__name__)


@dataclass
class ProgressEntry:
    played: bool = False
    downloaded: bool | None = None
    downloadable: bool | None = None
    reason: str | None = None
    ts: str = ""


@dataclass
class ProgressStore:
    """url → ProgressEntry 매핑을 메모리에 보관하고 파일과 동기화한다.

    캐시 일관성은 호출자가 보장한다 (단일 자동 모드 루프 내 단일 인스턴스 사용).
    """

    path: Path
    entries: dict[str, ProgressEntry] = field(default_factory=dict)

    # ── 로드 ─────────────────────────────────────────────────
    def load(self) -> None:
        if not self.path.exists():
            self.entries = {}
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            self.entries = {}
            return

        # v1: 리스트 → 모든 URL을 "재생 완료, 다운로드/가능 여부 미확인"으로 마이그레이션
        if isinstance(raw, list):
            self.entries = {
                url: ProgressEntry(played=True, downloaded=None, downloadable=None)
                for url in raw
                if isinstance(url, str)
            }
            return

        # v2
        if isinstance(raw, dict) and raw.get("version") == 2:
            entries_raw = raw.get("entries", {})
            if isinstance(entries_raw, dict):
                self.entries = {
                    url: ProgressEntry(
                        played=bool(data.get("played", False)),
                        downloaded=data.get("downloaded"),
                        downloadable=data.get("downloadable"),
                        reason=data.get("reason"),
                        ts=str(data.get("ts", "")),
                    )
                    for url, data in entries_raw.items()
                    if isinstance(url, str) and isinstance(data, dict)
                }
                return

        # 알 수 없는 포맷 → 안전하게 비움
        self.entries = {}

    # ── 저장 ─────────────────────────────────────────────────
    def save(self) -> None:
        """원자적으로 auto_progress.json을 교체한다.

        ARCH-011: atomic_write_text 공용 모듈로 수렴 (O_EXCL + O_NOFOLLOW + 0o600 + fsync).
        LOG-009: file_lock 으로 cross-process 직렬화. 자동 모드 + recover 스크립트
        동시 실행 시 lost update 를 방지한다. POSIX(flock) 는 직렬화 보장,
        Windows(msvcrt.locking) 는 best-effort advisory.
        """
        from src.util.atomic_write import atomic_write_text, file_lock

        payload = {
            "version": 2,
            "entries": {url: asdict(entry) for url, entry in self.entries.items()},
        }
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)

        with file_lock(self.path):
            atomic_write_text(self.path, serialized, mode=0o600)

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
        e = self.entries.setdefault(url, ProgressEntry())
        e.played = True
        e.ts = self._now()

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

    def mark_unsupported(self, url: str, reason: str | None = None) -> None:
        e = self.entries.setdefault(url, ProgressEntry())
        e.played = True  # 재생 자체는 어쨌든 완료됐거나 별도 판정 대상
        e.downloadable = False
        e.downloaded = False
        e.reason = reason
        e.ts = self._now()

    def mark_download_success(self, url: str) -> None:
        e = self.entries.setdefault(url, ProgressEntry())
        e.downloaded = True
        e.downloadable = True
        e.reason = None
        e.ts = self._now()

    def mark_download_failed(self, url: str, reason: str) -> None:
        e = self.entries.setdefault(url, ProgressEntry())
        # downloadable은 유지 — 네트워크 실패 등은 재시도 여지가 있으므로 True로 간주
        if e.downloadable is None:
            e.downloadable = True
        e.downloaded = False
        e.reason = reason
        e.ts = self._now()

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

    def remove(self, url: str) -> bool:
        return self.entries.pop(url, None) is not None

    def retain_only(self, allowed_urls: set[str]) -> int:
        """LMS에서 사라진 항목을 제거한다. 반환값은 제거된 개수."""
        orphan = self.known_urls() - allowed_urls
        for url in orphan:
            del self.entries[url]
        return len(orphan)
