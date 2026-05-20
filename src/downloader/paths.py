"""강의 다운로드 경로 계산 — 서비스/UI 레이어가 공통으로 사용하는 순수 함수 모음.

`LectureItem`에 메서드로 추가하면 scraper → downloader 역방향 의존이 되어
`src/scraper/models.py`가 이 파일을 import해야 한다. 그래서 모델 대신 downloader 레이어에
두고, 호출자(service/ui)가 lecture + 과목 + 경로를 넘기도록 한다.

경로 구조 `과목명/N주차/강의명.mp4`는 `make_filepath`가 단일 소스 오브 트루스.

BUG-7 (course id fallback): LMS 가 학기 도중 `course.long_name`을 가공된 cohort
코드 등으로 변경하면 디렉토리 매칭이 mismatch 가 되어 "재다운로드" 무한 루프나
"이미 받은 파일을 못 찾는" drift 가 발생할 수 있다. 마이그레이션 없이 안전하게
방어하기 위해, 디렉토리 안에 `.course_id` 마커 파일을 두고 long_name 매칭이
실패하면 마커 기반으로 fallback. 마커 stamp 는 long_name 매칭이 성공한 디렉토리에
opportunistic 으로 추가되어 다음 cycle 부터 fallback 가능.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.scraper.models import Course, LectureItem


_COURSE_ID_MARKER = ".course_id"

# M9: course_dir 해결 결과 메모이즈. expected_paths/file_present 가 강의별로
# _find_course_dir 를 호출해 같은 과목을 반복 재해결(최악: download root 전체
# iterdir + 마커 read)하던 비용을 1회로 줄인다.
_course_dir_cache: dict[tuple[str, str, str], Path] = {}
_course_dir_lock = threading.Lock()


def clear_course_dir_cache() -> None:
    """course_dir 해결 캐시를 비운다.

    자동 모드는 **매 사이클 시작 시** 호출해, 학기 중 디렉토리가 새로 생성/변경
    되어도 다음 사이클에 반영되게 한다. 단발성 스크립트(recover/reconcile)는
    프로세스 수명이 1 pass 라 호출 불필요.
    """
    with _course_dir_lock:
        _course_dir_cache.clear()


def _sanitize_segment(name: str) -> str:
    """경로 segment 한 단계의 이름 새니타이즈.

    `video_downloader._sanitize_filename` 과 같은 규칙을 사용해야 디렉토리/파일 이름이
    일관된다. video_downloader 가 playwright 의존이라 host 단위 테스트에서 import
    불가능하므로 동일 규칙을 paths.py 에도 두되, 두 곳이 drift 하지 않도록
    동일 정규식을 사용한다 (둘 중 하나만 변경하면 디렉토리 매칭이 깨지므로 함께 수정).
    """
    sanitized = re.sub(r'[<>:"/\\|?*]', "", name)
    sanitized = re.sub(r"\.{2,}", "", sanitized)
    sanitized = sanitized.strip(" .")
    sanitized = re.sub(r"\s+", " ", sanitized)
    return sanitized or "lecture"


def _stamp_course_id_marker(course_dir: Path, course_id: str) -> None:
    """디렉토리에 `.course_id` 마커 파일 생성 (idempotent, 추가만).

    이미 마커가 있으면 no-op. 권한 등으로 쓰기 실패하면 silently 무시 — fallback
    효과만 약화될 뿐 다운로드 본 흐름은 영향 없음.
    """
    marker = course_dir / _COURSE_ID_MARKER
    if marker.exists():
        return
    try:
        marker.write_text(course_id, encoding="utf-8")
    except OSError:
        pass


def _find_course_dir(
    download_dir: Path,
    course_long_name: str,
    course_id: str,
) -> Path:
    """course 의 디렉토리 경로 결정 (M9: 해결 결과 캐시).

    같은 `(download_dir, long_name, course_id)` 조합은 사이클 내에서 결과가
    안정적이므로 메모이즈한다. 캐시는 `clear_course_dir_cache()` (자동 모드 매
    사이클) 로 무효화한다.

    **마커 stamp 부수효과는 캐시 적중과 무관하게 매 호출 수행한다** — BUG-7 의
    `.course_id` 마커 stamping 이 캐시로 인해 누락되면 longName 변경 fallback 이
    깨지기 때문 (idempotent 라 재호출 비용 없음).
    """
    key = (str(download_dir), course_long_name, str(course_id))
    with _course_dir_lock:
        result = _course_dir_cache.get(key)
    if result is None:
        result = _find_course_dir_uncached(download_dir, course_long_name, course_id)
        with _course_dir_lock:
            _course_dir_cache[key] = result
    # BUG-7: 디렉토리가 실재하면 마커를 stamp (캐시 적중 시에도 — idempotent).
    if result.exists() and result.is_dir():
        _stamp_course_id_marker(result, str(course_id))
    return result


def _find_course_dir_uncached(
    download_dir: Path,
    course_long_name: str,
    course_id: str,
) -> Path:
    """course 의 디렉토리 경로 결정 (실제 FS 탐색 — 마커 stamp 는 호출자가 수행).

    1차: `_sanitize_segment(course_long_name)` 기반. 존재하면 반환.
    2차: `.course_id` 마커가 동일 course_id 인 디렉토리 검색 (longName 변경 fallback).
    3차: 1차 경로 반환 (호출자가 다운로드 시점에 mkdir).
    """
    primary_name = _sanitize_segment(course_long_name)
    primary = download_dir / primary_name

    if primary.exists() and primary.is_dir():
        return primary

    # 2차: 마커 기반 fallback (longName 변경 후 디렉토리 매칭)
    if download_dir.is_dir():
        for d in download_dir.iterdir():
            if not d.is_dir():
                continue
            marker = d / _COURSE_ID_MARKER
            if not marker.exists():
                continue
            try:
                if marker.read_text(encoding="utf-8").strip() == str(course_id):
                    return d
            except OSError:
                continue

    # 3차: 새로 생성될 경로 (호출자가 mkdir)
    return primary


def _week_segment(week_label: str) -> str:
    """`6주차(총 8주 중)` → `6주차` 같은 규칙. 숫자주차 없으면 sanitize 결과 또는 '기타'."""
    week_match = re.match(r"(\d+주차)", week_label or "")
    if week_match:
        return week_match.group(1)
    sanitized = _sanitize_segment(week_label or "")
    return sanitized or "기타"


def expected_paths(
    download_dir: str | Path,
    course: Course,
    lec: LectureItem,
) -> tuple[Path, Path]:
    """`(mp4, mp3)` 절대 경로 튜플.

    course.long_name 기반 디렉토리가 우선이지만, LMS longName 변경 시
    `.course_id` 마커 기반 fallback 으로 기존 디렉토리를 발견한다 (BUG-7).
    """
    course_dir = _find_course_dir(Path(download_dir), course.long_name, str(course.id))
    week_dir = _week_segment(lec.week_label)
    title = _sanitize_segment(lec.title)
    mp4 = (course_dir / week_dir / f"{title}.mp4").resolve()
    mp3 = mp4.with_suffix(".mp3")
    return mp4, mp3


def file_present(
    download_dir: str | Path,
    course: Course,
    lec: LectureItem,
    rule: str,
) -> bool:
    """DOWNLOAD_RULE에 따라 기대되는 파일이 모두 존재하는지 확인한다."""
    mp4, mp3 = expected_paths(download_dir, course, lec)
    if rule == "video":
        return mp4.exists()
    if rule == "audio":
        return mp3.exists()
    if rule == "both":
        return mp4.exists() and mp3.exists()
    # NF-07: 규칙 미설정(빈 DOWNLOAD_RULE) — `both` 와 동일하게 보수적으로
    # 판정한다. OR 로 두면 mp3 누락을 "완료"로 오판정해 변환/STT 단계를
    # 건너뛰는 위험이 있어, 둘 다 존재할 때만 present 로 본다.
    return mp4.exists() and mp3.exists()
