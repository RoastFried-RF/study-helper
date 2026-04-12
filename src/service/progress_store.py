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
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from src.config import KST


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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 2,
            "entries": {url: asdict(entry) for url, entry in self.entries.items()},
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass  # Windows에서는 chmod 미지원

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
        """파일시스템 점검 결과 이미 파일이 존재할 때 사용."""
        e = self.entries.setdefault(url, ProgressEntry())
        e.downloaded = True
        e.downloadable = True
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
