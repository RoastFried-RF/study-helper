"""누락 다운로드 복구 파이프라인 — UI/CLI 공용.

`src/ui/recover.py`(TUI 메뉴)와 `scripts/recover_missing.py`(CLI)에서 동일하게 사용한다.
두 진입점은 입력(scraper/courses/details)과 출력(console/stdout) 어댑터만 담당하고,
수집·실행·집계는 이 모듈이 단일 소스로 제공한다.

수집 로직은 `src.service.download_state.list_missing_items()` 에 위임해
`ui/auto.py` 와 동일한 코드 경로를 공유한다.

수집 대상:
- `lec.completion == "completed"` (LMS 기준 출석 완료)
- `lec.is_downloadable` True (learningx 등 구조적 불가는 제외)
- 파일시스템에서 현재 DOWNLOAD_RULE 기준 파일 누락
- (옵션) ProgressStore 에 downloaded=False + reason 이 남아있는 drift 항목
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from src.config import Config
from src.downloader.result import DownloadResult
from src.logger import get_logger
from src.service.download_state import MissingItem, list_missing_items

if TYPE_CHECKING:
    from src.scraper.course_scraper import CourseScraper
    from src.scraper.models import Course, CourseDetail
    from src.service.progress_store import ProgressStore

_log = get_logger("recover_pipeline")


# MissingItem 은 download_state 로 이전했지만 기존 import 호환용으로 re-export
__all__ = [
    "MissingItem",
    "RecoveryReport",
    "collect_missing",
    "run_recovery",
]


@dataclass
class RecoveryReport:
    total: int
    success: int
    failed_by_reason: Counter[str] = field(default_factory=Counter)


ProgressCallback = Callable[[int, int, MissingItem, DownloadResult | Exception | None], None]


def collect_missing(
    courses: list[Course],
    details: list[CourseDetail | None],
    *,
    store: ProgressStore | None = None,
    include_store_drift: bool = False,
) -> list[MissingItem]:
    """LMS completion 기준 누락된 다운로드 항목을 전수 수집한다.

    Args:
        store: 제공 시 MissingItem.store_reason 에 진행 상태의 실패 사유를 함께 담는다.
        include_store_drift: True 면 "파일은 있지만 store 는 실패 상태" drift 항목도
            포함해 재다운로드로 정상화 가능. 기본 False 는 파일 존재 시 건너뜀.
    """
    download_dir = Config.get_download_dir()
    rule = Config.DOWNLOAD_RULE or "both"
    return list_missing_items(
        courses,
        details,
        download_dir=download_dir,
        rule=rule,
        store=store,
        include_fs_present_but_store_failed=include_store_drift,
    )


async def run_recovery(
    scraper: CourseScraper,
    missing: list[MissingItem],
    *,
    on_progress: ProgressCallback | None = None,
    store: ProgressStore | None = None,
) -> RecoveryReport:
    """미싱 항목을 순차적으로 다운로드 재시도한다.

    Args:
        scraper: CourseScraper 인스턴스 (scraper.page 필요)
        missing: collect_missing 결과 리스트
        on_progress: (index, total, item, result) 콜백 — UI에서 per-item 진행 표시용.
                     `result`가 None이면 진입 전, DownloadResult면 완료, Exception이면 예외.
                     콜백 내부 예외는 복구 루프에 영향을 주지 않도록 격리된다(SEC-104).
        store: 제공 시 각 항목 성공/실패에 따라 `mark_download_success` /
            `mark_download_failed` / `mark_unsupported` 를 호출. 호출자가 주기적으로
            `store.save()` 하거나 함수 종료 후 저장 책임. 누락 시 progress 동기화가
            이루어지지 않는다(과거 동작 호환).

    Returns:
        RecoveryReport: 성공 카운트와 실패 사유 분포
    """
    from src.downloader.result import REASON_UNSUPPORTED
    from src.ui.download import run_download

    rule = Config.DOWNLOAD_RULE or "both"
    audio_only = rule == "audio"
    both = rule == "both"

    total = len(missing)
    success = 0
    reasons: Counter[str] = Counter()

    def _notify(index: int, item: MissingItem, payload: DownloadResult | Exception | None) -> None:
        if on_progress is None:
            return
        try:
            on_progress(index, total, item, payload)
        except Exception as cb_exc:  # SEC-104: 콜백 예외 격리
            _log.warning("on_progress 콜백 예외 무시: %s", cb_exc)

    for i, item in enumerate(missing, 1):
        label = f"[{item.course.long_name}] {item.lec.title}"
        _notify(i, item, None)
        _log.info("복구 중 (%d/%d): %s", i, total, label)

        try:
            result = await run_download(scraper.page, item.lec, item.course, audio_only=audio_only, both=both)
        except Exception as e:
            _log.error("복구 예외: %s — %s", label, e, exc_info=True)
            reasons[f"exception:{type(e).__name__}"] += 1
            if store is not None:
                store.mark_download_failed(item.lec.full_url, reason=f"exception:{type(e).__name__}")
            _notify(i, item, e)
            continue

        if result.ok:
            success += 1
            _log.info("복구 성공: %s", label)
            if store is not None:
                store.mark_download_success(item.lec.full_url)
        else:
            reason = result.reason or "unknown"
            reasons[reason] += 1
            _log.warning("복구 실패: %s — reason=%s", label, reason)
            if store is not None:
                # unsupported 는 downloadable=False 로 굳히고, 나머지는 downloadable=True 유지
                if reason == REASON_UNSUPPORTED:
                    store.mark_unsupported(item.lec.full_url, reason=reason)
                else:
                    store.mark_download_failed(item.lec.full_url, reason=reason)
        _notify(i, item, result)

    _log.info("복구 종료: 성공 %d/%d, 실패=%s", success, total, dict(reasons))
    return RecoveryReport(total=total, success=success, failed_by_reason=reasons)
