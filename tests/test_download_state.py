"""download_state 공용 유틸 단위 테스트."""

from __future__ import annotations

from pathlib import Path

import pytest

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


def _touch(path: Path, size: int = 100) -> None:
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

    # 실제 파일 생성 (make_filepath 구조: 과목/N주차/강의명.mp4)
    lecture_root = tmp_path / course.long_name / "6주차"
    _touch(lecture_root / "6주차(총 8주 중) 비전채플 삭개오 이야기.mp4")
    _touch(lecture_root / "6주차(총 8주 중) 비전채플 삭개오 이야기.mp3")

    # 실제 sanitize_filename 이 title 을 그대로 두므로 파일명 일치 확인을 위해
    # make_filepath 로 기대 경로 계산 후 touch.
    from src.downloader.video_downloader import make_filepath

    rel = make_filepath(course.long_name, lec.week_label, lec.title)
    full = (tmp_path / rel).resolve()
    _touch(full)
    _touch(full.with_suffix(".mp3"))

    missing = list_missing_items(
        [course], [detail], download_dir=str(tmp_path), rule="both",
    )
    assert missing == []


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
    from src.downloader.video_downloader import make_filepath

    rel = make_filepath(course.long_name, lec.week_label, lec.title)
    full = (tmp_path / rel).resolve()
    _touch(full)
    _touch(full.with_suffix(".mp3"))

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

    from src.downloader.video_downloader import make_filepath

    rel = make_filepath(course.long_name, lec.week_label, lec.title)
    full = (tmp_path / rel).resolve()
    _touch(full)
    _touch(full.with_suffix(".mp3"))

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


def test_reconcile_no_op_when_store_already_correct(tmp_path: Path):
    """이미 downloaded=True 상태면 reconcile 은 중복 호출하지 않는다."""
    course = _make_course()
    lec = _make_lec("3316344")
    detail = _make_detail([lec])

    from src.downloader.video_downloader import make_filepath

    rel = make_filepath(course.long_name, lec.week_label, lec.title)
    full = (tmp_path / rel).resolve()
    _touch(full)
    _touch(full.with_suffix(".mp3"))

    store = ProgressStore(path=tmp_path / "progress.json")
    store.mark_played(lec.full_url)
    store.mark_download_success(lec.full_url)

    _, confirmed = reconcile_store_with_filesystem(
        [course], [detail], store,
        download_dir=str(tmp_path), rule="both",
    )
    assert confirmed == 0


# ── Config Windows /data 드라이브 루트 트랩 ───────────────────


def test_config_windows_drive_root_trap_resolves_not_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    """Windows + unix 절대경로 DOWNLOAD_DIR + Docker 아님 → Path.resolve() 결과 유지.

    기존 구현은 `~/Downloads` 로 fallback 했으나, 사용자가 이미 해당 경로에
    파일을 쌓아둔 경우(예: D:/data/downloads) 정합성이 깨지므로 fallback 대신
    resolve 만 수행하고 경고를 남기는 것이 올바른 동작.
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
        # 경고 플래그는 켜져야 하지만, 경로는 fallback 이 아닌 Path.resolve() 결과
        assert config_module.Config._drive_root_trap_warned is True
        # Path("/data/downloads").resolve() 는 현재 드라이브 루트 기반 절대경로
        # (e.g. 'D:\\data\\downloads' 또는 'C:\\data\\downloads' — OS별 차이)
        # raw 그대로가 아닌 OS 절대경로여야 하고, "data" 와 "downloads" 를 포함해야 함
        assert "data" in result.lower() and "downloads" in result.lower()
        # fallback 된 경우라면 Downloads 폴더 등 다른 경로가 나오는데 그 차단 확인:
        # Path("/data/downloads").resolve() 는 Linux 에서도 "/data/downloads" 그대로.
        # Windows 에서만 드라이브 루트 접두어 추가된 형태.
        assert result != "/data/downloads" or "\\" in result or ":" in result
    finally:
        config_module.Config.DOWNLOAD_DIR = original_dir
        config_module.Config._drive_root_trap_warned = original_warned
        monkeypatch.setattr(config_module.sys, "platform", original_platform)
