"""download_state 공용 유틸 단위 테스트."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.downloader.paths import _MIN_VALID_MEDIA_BYTES, expected_paths
from src.scraper.models import Course, CourseDetail, LectureItem, LectureType, Week
from src.service.download_state import (
    list_missing_items,
    reconcile_store_with_filesystem,
)
from src.service.progress_store import ProgressStore


def _make_course(course_id: str = "43708", name: str = "비전채플") -> Course:
    long_name = f"{name} ({course_id})"
    return Course(
        id=course_id,
        long_name=long_name,
        href=f"/courses/{course_id}",
        term="2026-1",
    )


def _make_lec(
    item_id: str = "3316344",
    *,
    title: str = "삭개오 이야기",
    week_label: str = "6주차(총 8주 중)",
    completion: str = "completed",
    lecture_type: LectureType = LectureType.MOVIE,
    item_url: str | None = None,
) -> LectureItem:
    url = item_url if item_url is not None else f"/courses/43708/modules/items/{item_id}"
    return LectureItem(
        title=title,
        item_url=url,
        lecture_type=lecture_type,
        completion=completion,
        week_label=week_label,
    )


def _make_detail(lecs: list[LectureItem], course: Course | None = None) -> CourseDetail:
    course = course or _make_course()
    week_title = lecs[0].week_label if lecs else "1주차"
    week = Week(title=week_title, week_number=6, lectures=list(lecs))
    return CourseDetail(course=course, course_name=course.long_name, professors="", weeks=[week])


def _touch(path: Path, size: int = _MIN_VALID_MEDIA_BYTES) -> None:
    """기본 크기는 스텁 하한 이상 — 유효 미디어로 인식되게 한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00" * size)


# ── list_missing_items ─────────────────────────────────────────


def test_list_missing_files_absent(tmp_path: Path):
    course = _make_course()
    lec = _make_lec("3316344")
    detail = _make_detail([lec])

    missing = list_missing_items(
        [course], [detail], download_dir=str(tmp_path), rule="both",
    )
    assert len(missing) == 1
    assert missing[0].lec.item_url == lec.item_url
    assert "mp4" in missing[0].kind


def test_list_missing_files_present(tmp_path: Path):
    """파일이 존재하면 missing 에서 제외."""
    course = _make_course()
    lec = _make_lec("3316344")
    detail = _make_detail([lec])

    # expected_paths SoT 로 정확한 경로에 파일 생성
    mp4, mp3 = expected_paths(tmp_path, course, lec)
    _touch(mp4)
    _touch(mp3)

    missing = list_missing_items(
        [course], [detail], download_dir=str(tmp_path), rule="both",
    )
    assert missing == []


def test_list_missing_treats_stub_as_missing(tmp_path: Path):
    """스텁(수 KB) 잔여 파일은 '있음'이 아니라 누락으로 — recover 가 재다운로드하도록.

    회귀 방지: 실패 다운로드가 남긴 2,684B mp4 / 4,746B mp3 가 .exists() 로는
    '존재'라 completed 강의가 재다운로드에서 영구 누락되던 버그.
    """
    course = _make_course()
    lec = _make_lec("3316344")
    detail = _make_detail([lec])

    mp4, mp3 = expected_paths(tmp_path, course, lec)
    _touch(mp4, size=2684)  # 스텁
    _touch(mp3, size=4746)  # 스텁

    missing = list_missing_items(
        [course], [detail], download_dir=str(tmp_path), rule="both",
    )
    assert len(missing) == 1
    assert missing[0].lec.item_url == lec.item_url


def test_list_missing_excludes_incomplete(tmp_path: Path):
    """LMS 출석 미완료 강의는 missing 수집 대상이 아니다."""
    course = _make_course()
    lec = _make_lec("3316344", completion="incomplete")
    detail = _make_detail([lec])

    missing = list_missing_items(
        [course], [detail], download_dir=str(tmp_path), rule="both",
    )
    assert missing == []


def test_list_missing_excludes_non_downloadable(tmp_path: Path):
    """learningx 등 구조적 다운로드 불가 lecture 는 제외.

    LectureItem.is_downloadable 은 full_url 에 'learningx' 문자열 포함 여부로 판정.
    """
    course = _make_course()
    lec = _make_lec(
        item_url="/learningx/courses/43708/modules/items/3316344",
    )
    detail = _make_detail([lec])
    # 전제 조건 확인: is_downloadable False 여야 의미 있는 테스트
    assert not lec.is_downloadable

    missing = list_missing_items(
        [course], [detail], download_dir=str(tmp_path), rule="both",
    )
    assert missing == []


def test_list_missing_records_store_drift_reason(tmp_path: Path):
    """store.reason 이 있으면 MissingItem.store_reason 에 복사된다."""
    course = _make_course()
    lec = _make_lec("3316344")
    detail = _make_detail([lec])

    store = ProgressStore(path=tmp_path / "progress.json")
    store.mark_played(lec.full_url)
    store.mark_download_failed(lec.full_url, reason="suspicious_stub")

    missing = list_missing_items(
        [course], [detail], download_dir=str(tmp_path), rule="both",
        store=store,
    )
    assert len(missing) == 1
    assert missing[0].store_reason == "suspicious_stub"


def test_list_missing_force_drift_includes_fs_present(tmp_path: Path):
    """파일은 있지만 store 에 실패 reason 이 남은 drift 항목을 재분류."""
    course = _make_course()
    lec = _make_lec("3316344")
    detail = _make_detail([lec])

    # 파일 생성 → 기본 모드에선 missing 아님
    mp4, mp3 = expected_paths(tmp_path, course, lec)
    _touch(mp4)
    _touch(mp3)

    # store 에는 실패 기록
    store = ProgressStore(path=tmp_path / "progress.json")
    store.mark_played(lec.full_url)
    store.mark_download_failed(lec.full_url, reason="suspicious_stub")

    # 기본 (drift 제외)
    baseline = list_missing_items(
        [course], [detail], download_dir=str(tmp_path), rule="both",
        store=store,
    )
    assert baseline == []

    # --force-drift 시 포함
    with_drift = list_missing_items(
        [course], [detail], download_dir=str(tmp_path), rule="both",
        store=store,
        include_fs_present_but_store_failed=True,
    )
    assert len(with_drift) == 1
    assert with_drift[0].store_reason == "suspicious_stub"


# ── reconcile_store_with_filesystem ───────────────────────────


def test_reconcile_marks_unsupported_for_learningx(tmp_path: Path):
    course = _make_course()
    lec = _make_lec(item_url="/learningx/courses/43708/modules/items/3316344")
    assert not lec.is_downloadable
    detail = _make_detail([lec])
    store = ProgressStore(path=tmp_path / "progress.json")

    unsupported, confirmed = reconcile_store_with_filesystem(
        [course], [detail], store,
        download_dir=str(tmp_path), rule="both",
    )
    assert unsupported == 1
    assert confirmed == 0
    entry = store.get(lec.full_url)
    assert entry.downloadable is False


def test_reconcile_confirms_when_file_exists_but_store_failed(tmp_path: Path):
    """BUG-FIX (비전채플 6주차 시나리오): 파일은 있지만 store 에 실패 기록이 남아있던
    drift 를 reconcile 이 정상화한다."""
    course = _make_course()
    lec = _make_lec("3316344")
    detail = _make_detail([lec])

    mp4, mp3 = expected_paths(tmp_path, course, lec)
    _touch(mp4)
    _touch(mp3)

    store = ProgressStore(path=tmp_path / "progress.json")
    store.mark_played(lec.full_url)
    store.mark_download_failed(lec.full_url, reason="suspicious_stub")

    unsupported, confirmed = reconcile_store_with_filesystem(
        [course], [detail], store,
        download_dir=str(tmp_path), rule="both",
    )
    assert unsupported == 0
    assert confirmed == 1

    entry = store.get(lec.full_url)
    assert entry.downloaded is True
    assert entry.downloadable is True
    assert entry.reason is None


def test_reconcile_confirms_when_lms_marks_incomplete(tmp_path: Path):
    """BUG-4 회귀 방지: LMS 가 일시적으로 incomplete 표시이지만 fs 에 파일이
    실재한다면 reconcile 이 store.downloaded 를 정정한다.

    이전에는 `if lec.completion != "completed": continue` 가드 때문에 LMS
    incomplete 강의는 건너뛰었고, 결과적으로 store 의 downloaded=False 가
    매 사이클 그대로 남아 동일 파일을 무한 재다운로드 시도하는 catastrophic
    loop 가 발생했다 (디스크 96 mp4 vs store 83 downloaded 의 13건 drift).
    """
    course = _make_course()
    # LMS 가 일시적으로 incomplete 표시
    lec = _make_lec("3316344", completion="incomplete")
    detail = _make_detail([lec])

    mp4, mp3 = expected_paths(tmp_path, course, lec)
    _touch(mp4)
    _touch(mp3)

    store = ProgressStore(path=tmp_path / "progress.json")
    store.mark_played(lec.full_url)
    store.mark_download_failed(lec.full_url, reason="network")

    unsupported, confirmed = reconcile_store_with_filesystem(
        [course], [detail], store,
        download_dir=str(tmp_path), rule="both",
    )
    assert unsupported == 0
    assert confirmed == 1

    entry = store.get(lec.full_url)
    assert entry.downloaded is True
    assert entry.reason is None


def test_reconcile_no_op_when_store_already_correct(tmp_path: Path):
    """이미 downloaded=True 상태면 reconcile 은 중복 호출하지 않는다."""
    course = _make_course()
    lec = _make_lec("3316344")
    detail = _make_detail([lec])

    mp4, mp3 = expected_paths(tmp_path, course, lec)
    _touch(mp4)
    _touch(mp3)

    store = ProgressStore(path=tmp_path / "progress.json")
    store.mark_played(lec.full_url)
    store.mark_download_success(lec.full_url)

    _, confirmed = reconcile_store_with_filesystem(
        [course], [detail], store,
        download_dir=str(tmp_path), rule="both",
    )
    assert confirmed == 0


# ── Config Windows /data 드라이브 루트 트랩 ───────────────────


def test_config_windows_remaps_data_to_project_root(
    monkeypatch: pytest.MonkeyPatch,
):
    """Windows + /data/downloads + non-Docker → 프로젝트 루트/data/downloads 매핑.

    드라이브 루트 `D:\\data\\downloads` 트랩 방지 + `get_data_path()` 와 동일한
    '프로젝트 내부' 원칙.
    """
    from src import config as config_module

    original_dir = config_module.Config.DOWNLOAD_DIR
    original_warned = config_module.Config._drive_root_trap_warned
    original_platform = getattr(config_module.sys, "platform", "")

    try:
        config_module.Config.DOWNLOAD_DIR = "/data/downloads"
        config_module.Config._drive_root_trap_warned = False
        monkeypatch.setattr(config_module.sys, "platform", "win32")
        monkeypatch.setattr(
            config_module, "_is_docker_with_data_volume", lambda: False,
        )

        result = config_module.Config.get_download_dir()
        assert config_module.Config._drive_root_trap_warned is True

        # 프로젝트 루트 기반이어야 함
        project_root = Path(config_module.__file__).resolve().parent.parent
        expected = str((project_root / "data" / "downloads").resolve())
        assert result == expected
    finally:
        config_module.Config.DOWNLOAD_DIR = original_dir
        config_module.Config._drive_root_trap_warned = original_warned
        monkeypatch.setattr(config_module.sys, "platform", original_platform)


def test_config_windows_remaps_generic_unix_path(monkeypatch: pytest.MonkeyPatch):
    """/data 접두어 외 경로도 프로젝트 루트/data/<rest> 로 안전하게 매핑."""
    from src import config as config_module

    original_dir = config_module.Config.DOWNLOAD_DIR
    original_warned = config_module.Config._drive_root_trap_warned
    original_platform = getattr(config_module.sys, "platform", "")

    try:
        config_module.Config.DOWNLOAD_DIR = "/foo/bar"
        config_module.Config._drive_root_trap_warned = False
        monkeypatch.setattr(config_module.sys, "platform", "win32")
        monkeypatch.setattr(
            config_module, "_is_docker_with_data_volume", lambda: False,
        )

        result = config_module.Config.get_download_dir()
        project_root = Path(config_module.__file__).resolve().parent.parent
        expected = str((project_root / "data" / "foo" / "bar").resolve())
        assert result == expected
    finally:
        config_module.Config.DOWNLOAD_DIR = original_dir
        config_module.Config._drive_root_trap_warned = original_warned
        monkeypatch.setattr(config_module.sys, "platform", original_platform)
