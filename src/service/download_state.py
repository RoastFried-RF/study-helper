"""다운로드 상태 공용 유틸 — `auto` 모드와 `recover` 파이프라인이 공유.

파일시스템 (`expected_paths` / `file_present`) 과 `ProgressStore` 사이의 drift 를
일관된 방법으로 감지·조정한다. 기존에 `ui/auto.py` 와 `service/recover_pipeline.py` 가
각각 구현하던 로직을 단일 소스로 통합.

핵심 개념:
  - 파일시스템 = "실제 있는가"
  - ProgressStore = "무엇을 시도했고 어떤 reason 으로 실패했는가"
  두 소스가 불일치하면 사용자 경험이 이상해진다. 본 모듈의 함수들은 불일치를
  감지하고 한 방향(FS → store)으로만 정정한다. 반대 방향은 실제 다운로드
  재실행으로만 이루어진다.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.downloader.paths import expected_paths, file_present, media_present
from src.downloader.result import REASON_UNSUPPORTED

if TYPE_CHECKING:
    from src.scraper.models import Course, CourseDetail, LectureItem
    from src.service.progress_store import ProgressStore


# ── 누락 항목 ────────────────────────────────────────────────────


@dataclass
class MissingItem:
    """파일시스템 기준 누락 + (선택) progress_store drift 정보."""

    course: Course
    lec: LectureItem
    kind: str  # "mp4" / "mp3" / "mp4+mp3"
    # progress_store drift 감지용 — store.get(url).reason 이 있는 경우 채워진다.
    store_reason: str | None = None


# ── 누락 조회 (Query — 부수효과 없음) ─────────────────────────


def list_missing_items(
    courses: Iterable[Course],
    details: Iterable[CourseDetail | None],
    download_dir: str,
    rule: str,
    *,
    store: ProgressStore | None = None,
    include_fs_present_but_store_failed: bool = False,
) -> list[MissingItem]:
    """LMS completion 기준 파일 누락 항목을 수집한다.

    Args:
        courses / details: 동일 순서의 과목 목록과 상세 (zip 파이프라인).
        download_dir: 기준 다운로드 루트 (Config.get_download_dir()).
        rule: "video" / "audio" / "both".
        store: 제공 시 `MissingItem.store_reason` 을 채움.
        include_fs_present_but_store_failed:
            True 이면 "파일은 있지만 store 에 실패 reason 이 남아있는" drift 항목도
            누락으로 취급. CLI `--force-drift` 같은 옵션에 쓰면 "다시 다운로드해서
            정상화" 가 가능. 기본 False 는 기존 파일 존재 시 skip.

    Returns:
        MissingItem 리스트 (입력 과목 순서 보존).
    """
    missing: list[MissingItem] = []
    for course, detail in zip(courses, details, strict=False):
        if detail is None:
            continue
        for lec in detail.all_video_lectures:
            if lec.completion != "completed":
                continue
            if not lec.is_downloadable:
                continue

            mp4, mp3 = expected_paths(download_dir, course, lec)
            # 스텁 마스킹 방어 — 수 KB 짜리 실패 잔여 파일을 "있음"으로 오판하면
            # completed 강의가 재다운로드에서 누락된다. media_present 가 크기 하한 적용.
            has_video = media_present(mp4)
            has_audio = media_present(mp3)

            # 파일시스템 누락 판정
            if rule == "video":
                fs_missing = not has_video
                kind = "mp4"
            elif rule == "audio":
                fs_missing = not has_audio
                kind = "mp3"
            else:  # both (또는 미설정 fallback)
                fs_missing = not (has_video and has_audio)
                parts: list[str] = []
                if not has_video:
                    parts.append("mp4")
                if not has_audio:
                    parts.append("mp3")
                kind = "+".join(parts) if parts else "mp4+mp3"

            store_reason: str | None = None
            if store is not None:
                entry = store.get(lec.full_url)
                if entry and entry.downloaded is False and entry.reason:
                    store_reason = entry.reason

            # FS drift: 파일은 있지만 store 는 실패 상태 → 옵션 따라 포함
            store_drift = (not fs_missing) and store_reason is not None

            if fs_missing or (include_fs_present_but_store_failed and store_drift):
                missing.append(
                    MissingItem(course=course, lec=lec, kind=kind, store_reason=store_reason)
                )
    return missing


# ── 상태 동기화 (Command) ─────────────────────────────────────


def reconcile_store_with_filesystem(
    courses: Iterable[Course],
    details: Iterable[CourseDetail | None],
    store: ProgressStore,
    download_dir: str,
    rule: str,
) -> tuple[int, int]:
    """파일시스템 관찰 결과를 store 에 반영한다 (Command).

    두 유형의 drift 를 정정:
      1. 구조적 다운로드 불가(learningx 등) 인데 store 에 downloadable=False 미기록
         → `mark_unsupported`
      2. 파일은 존재하는데 store 는 downloaded=False 또는 reason 이 남아있음
         → `mark_download_confirmed_from_filesystem`

    BUG-4: 이전에는 `lec.completion == "completed"` 인 강의만 reconcile 했다.
    그러나 LMS 가 일시적으로 incomplete 로 표시 중인 강의도 디스크에 파일이
    실재한다면 store 의 `downloaded` 상태는 fs 사실을 따라야 한다 — 그렇지 않으면
    매 사이클 동일 파일을 재다운로드 시도하는 catastrophic loop 가 된다.
    `played` 상태는 별도 (auto 루프의 mark_incomplete) 가 LMS 신호로 관리하므로
    여기서는 손대지 않는다.

    Returns:
        (unsupported_marked, downloaded_confirmed) 건수
    """
    unsupported = 0
    confirmed = 0
    for course, detail in zip(courses, details, strict=False):
        if detail is None:
            continue
        for lec in detail.all_video_lectures:
            if not lec.is_downloadable:
                # learningx 같은 구조적 미지원은 LMS completion 과 무관하게 마킹
                entry = store.get(lec.full_url)
                if entry is None or entry.downloadable is not False:
                    store.mark_unsupported(lec.full_url, reason=REASON_UNSUPPORTED)
                    unsupported += 1
                continue
            if file_present(download_dir, course, lec, rule):
                entry = store.get(lec.full_url)
                # 파일은 있는데 store 가 "미완료/실패" 상태면 정정한다.
                # LMS completion 과 무관하게 — 디스크가 SoT.
                if entry is None or entry.downloaded is not True or entry.reason is not None:
                    store.mark_download_confirmed_from_filesystem(lec.full_url)
                    confirmed += 1
    return unsupported, confirmed
