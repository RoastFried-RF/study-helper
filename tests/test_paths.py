"""paths.py 단위 테스트 — BUG-7 marker-based fallback 회귀 방지.

paths.py 가 video_downloader (playwright 의존) 와 분리되어 host 환경에서도
단독 실행 가능. expected_paths / file_present 의 long_name 매칭 + course_id
마커 fallback 동작을 검증.
"""

from __future__ import annotations

from pathlib import Path

from src.downloader.paths import (
    _COURSE_ID_MARKER,
    _stamp_course_id_marker,
    expected_paths,
    file_present,
)
from src.scraper.models import Course, LectureItem, LectureType


def _make_course(course_id: str = "43708", long_name: str = "비전채플 (43708)") -> Course:
    return Course(
        id=course_id,
        long_name=long_name,
        href=f"/courses/{course_id}",
        term="2026-1",
    )


def _make_lec(
    *,
    title: str = "삭개오 이야기",
    week_label: str = "6주차(총 8주 중)",
    completion: str = "completed",
) -> LectureItem:
    return LectureItem(
        title=title,
        item_url="/courses/43708/modules/items/3316344",
        lecture_type=LectureType.MOVIE,
        week_label=week_label,
        completion=completion,
    )


def _touch(path: Path, size: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00" * size)


# ── 기본 동작 ─────────────────────────────────────────────────


def test_expected_paths_long_name_based(tmp_path: Path):
    """기본은 course.long_name sanitize 결과를 디렉토리로 사용."""
    course = _make_course(long_name="비전채플")
    lec = _make_lec(title="삭개오", week_label="6주차")

    mp4, mp3 = expected_paths(tmp_path, course, lec)
    assert mp4.parent.parent.name == "비전채플"
    assert mp4.parent.name == "6주차"
    assert mp4.name == "삭개오.mp4"
    assert mp3.name == "삭개오.mp3"


def test_expected_paths_week_label_with_suffix(tmp_path: Path):
    """`6주차(총 8주 중)` 같은 label 도 `6주차` 만 추출."""
    course = _make_course()
    lec = _make_lec(week_label="3주차(총 8주 중)")

    mp4, _ = expected_paths(tmp_path, course, lec)
    assert mp4.parent.name == "3주차"


def test_expected_paths_sanitizes_invalid_chars(tmp_path: Path):
    """경로에 사용 불가한 문자(`/`, `:`, `*` 등)는 제거."""
    course = _make_course(long_name="과목/이름:테스트")
    lec = _make_lec(title="강의*제목?")

    mp4, _ = expected_paths(tmp_path, course, lec)
    assert "/" not in mp4.parent.parent.name
    assert ":" not in mp4.parent.parent.name
    assert "*" not in mp4.name
    assert "?" not in mp4.name


# ── BUG-7 marker-based fallback ───────────────────────────────


def test_stamp_course_id_marker_idempotent(tmp_path: Path):
    """마커 stamp 는 중복 호출 안전 (이미 있으면 no-op)."""
    course_dir = tmp_path / "course-A"
    course_dir.mkdir()

    _stamp_course_id_marker(course_dir, "43708")
    marker = course_dir / _COURSE_ID_MARKER
    assert marker.exists()
    assert marker.read_text(encoding="utf-8") == "43708"

    # 2차 호출 — 기존 내용 보존 (덮어쓰기 안 함)
    _stamp_course_id_marker(course_dir, "DIFFERENT_ID")
    assert marker.read_text(encoding="utf-8") == "43708"


def test_expected_paths_stamps_marker_on_first_match(tmp_path: Path):
    """long_name 매칭 성공 시 marker 가 자동으로 stamp 된다 (idempotent)."""
    course = _make_course(course_id="43708", long_name="비전채플")
    lec = _make_lec(title="삭개오")

    # 디렉토리만 미리 생성 (파일 없이)
    primary_dir = tmp_path / "비전채플"
    primary_dir.mkdir()

    expected_paths(tmp_path, course, lec)

    marker = primary_dir / _COURSE_ID_MARKER
    assert marker.exists()
    assert marker.read_text(encoding="utf-8") == "43708"


def test_expected_paths_falls_back_to_marker_when_long_name_changes(tmp_path: Path):
    """LMS 가 long_name 을 변경해도 .course_id 마커 기반 fallback 으로 기존 디렉토리 매칭.

    BUG-7 회귀 방지 — 핵심 시나리오:
    1. 기존 long_name 으로 다운로드 (디렉토리 + 파일 생성, 마커 미존재)
    2. 다음 cycle: 같은 long_name 으로 file_present 호출 → 마커 자동 stamp
    3. LMS 가 long_name 을 다른 값으로 변경 (course.id 동일)
    4. 새 long_name 으로 expected_paths 호출 → fallback 으로 기존 디렉토리 발견

    stamp 는 expected_paths/file_present 가 호출되면서 디렉토리 존재 시 일어나므로,
    다운로드 → 다음 cycle 의 file_present 흐름이 자연스러운 stamp 시점이다.
    """
    course_v1 = _make_course(course_id="43708", long_name="비전채플 (43708)")
    course_v2 = _make_course(course_id="43708", long_name="비전채플 [재편성] (2150100001)")
    lec = _make_lec(title="삭개오")

    # 1. 첫 다운로드 (디렉토리 + 파일 생성). 첫 expected_paths 호출 시점엔 디렉토리
    #    미존재라 마커 stamp 안 됨.
    mp4_v1, _ = expected_paths(tmp_path, course_v1, lec)
    _touch(mp4_v1)

    # 2. 다음 cycle 의 file_present 호출 — 디렉토리가 이미 있으므로 마커 stamp 트리거.
    file_present(tmp_path, course_v1, lec, "both")

    course_dir_v1 = tmp_path / "비전채플 (43708)"
    assert (course_dir_v1 / _COURSE_ID_MARKER).read_text(encoding="utf-8") == "43708"

    # 3. LMS 가 long_name 변경 — fallback 동작 검증
    mp4_v2, _ = expected_paths(tmp_path, course_v2, lec)

    # 4. 같은 디렉토리/파일을 가리켜야 함
    assert mp4_v2 == mp4_v1
    assert mp4_v2.exists()


def test_expected_paths_no_fallback_when_id_differs(tmp_path: Path):
    """다른 course_id 의 마커 디렉토리는 fallback 매칭 안 됨."""
    course_other = _make_course(course_id="99999", long_name="다른과목")
    lec = _make_lec(title="강의")

    # 다른 course 의 디렉토리 + 마커 생성
    other_dir = tmp_path / "비전채플"
    other_dir.mkdir()
    (other_dir / _COURSE_ID_MARKER).write_text("43708", encoding="utf-8")

    # 99999 로 expected_paths 호출 — 다른 course_id 라 fallback 안 됨
    mp4, _ = expected_paths(tmp_path, course_other, lec)

    # 새 long_name 기반 경로를 반환 (다른과목 디렉토리)
    assert mp4.parent.parent.name == "다른과목"
    assert mp4.parent.parent != other_dir


def test_expected_paths_corrupted_marker_skipped(tmp_path: Path):
    """비정상 마커 (예: 권한 오류로 빈 파일) 는 fallback 매칭 안 됨."""
    course = _make_course(course_id="43708", long_name="비전채플")
    lec = _make_lec(title="삭개오")

    # 빈 마커 파일을 가진 다른 디렉토리
    other_dir = tmp_path / "다른이름"
    other_dir.mkdir()
    (other_dir / _COURSE_ID_MARKER).write_text("", encoding="utf-8")

    mp4, _ = expected_paths(tmp_path, course, lec)
    # 빈 마커는 fallback 매칭 안 됨 → primary path 반환
    assert mp4.parent.parent.name == "비전채플"


# ── file_present ──────────────────────────────────────────────


def test_file_present_both_rule(tmp_path: Path):
    """rule='both' 는 mp4 + mp3 둘 다 있어야 True."""
    course = _make_course()
    lec = _make_lec()

    assert file_present(tmp_path, course, lec, "both") is False

    mp4, mp3 = expected_paths(tmp_path, course, lec)
    _touch(mp4)
    assert file_present(tmp_path, course, lec, "both") is False  # mp3 없음

    _touch(mp3)
    assert file_present(tmp_path, course, lec, "both") is True


def test_file_present_works_with_marker_fallback(tmp_path: Path):
    """파일이 옛 디렉토리에 있고 LMS long_name 이 바뀌어도 file_present True."""
    course_v1 = _make_course(long_name="과목v1")
    course_v2 = _make_course(long_name="과목v2")  # 같은 id, 다른 이름
    lec = _make_lec()

    mp4, mp3 = expected_paths(tmp_path, course_v1, lec)
    _touch(mp4)
    _touch(mp3)

    # 운영 흐름: 다운로드 후 다음 cycle 의 file_present 호출 시점에 마커 stamp.
    # course_v1 로 한 번 호출 → stamp.
    file_present(tmp_path, course_v1, lec, "both")

    # course_v2 로 조회해도 fallback 으로 발견
    assert file_present(tmp_path, course_v2, lec, "both") is True


# ── NF-07: 빈 DOWNLOAD_RULE fallback 은 AND (both 와 동일) ───────────
#
# 버그 시나리오: 규칙 미설정(빈 DOWNLOAD_RULE) 시 fallback 이 `mp4 or mp3`
# (OR) 라서 mp4 만 있고 mp3 가 없어도 "완료"로 오판정 → 변환/STT 단계가
# 건너뛰어졌다. 수정 후에는 둘 다 존재할 때만 present(AND).


def test_file_present_empty_rule_requires_both(tmp_path: Path):
    """NF-07: 빈 rule 일 때 mp4 만 있고 mp3 없으면 False (OR fallback 회귀)."""
    course = _make_course()
    lec = _make_lec()

    mp4, mp3 = expected_paths(tmp_path, course, lec)
    _touch(mp4)  # mp4 만 생성, mp3 없음

    # 수정 전 OR fallback 이면 True 로 오판정 → 변환/STT 건너뜀
    assert file_present(tmp_path, course, lec, "") is False, (
        "빈 rule 에서 mp3 누락을 '완료'로 오판정하면 안 됨 (AND fallback)"
    )


def test_file_present_empty_rule_mp3_only_is_false(tmp_path: Path):
    """NF-07: 빈 rule 일 때 mp3 만 있고 mp4 없어도 False."""
    course = _make_course()
    lec = _make_lec()

    mp4, mp3 = expected_paths(tmp_path, course, lec)
    _touch(mp3)  # mp3 만 생성

    assert file_present(tmp_path, course, lec, "") is False


def test_file_present_empty_rule_both_present_is_true(tmp_path: Path):
    """NF-07: 빈 rule 도 mp4 + mp3 둘 다 있으면 True (정상 경로 유지)."""
    course = _make_course()
    lec = _make_lec()

    mp4, mp3 = expected_paths(tmp_path, course, lec)
    _touch(mp4)
    _touch(mp3)

    assert file_present(tmp_path, course, lec, "") is True


def test_file_present_unknown_rule_requires_both(tmp_path: Path):
    """NF-07: 알 수 없는 rule 값도 보수적으로 AND fallback 을 탄다."""
    course = _make_course()
    lec = _make_lec()

    mp4, mp3 = expected_paths(tmp_path, course, lec)
    _touch(mp4)

    assert file_present(tmp_path, course, lec, "garbage-rule") is False
