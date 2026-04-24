"""
마감 임박 알림 모듈.

비디오가 아닌 강의 항목(퀴즈, 과제 등)의 마감이 임박할 때
텔레그램으로 알림을 전송한다.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime

from src.config import KST, get_data_path
from src.logger import get_logger
from src.scraper.models import VIDEO_LECTURE_TYPES, Course, CourseDetail, LectureItem, LectureType

_log = get_logger("deadline_checker")

_DEADLINE_FILE = get_data_path("deadline_notified.json")

# 알림 기준 시간 (시간 단위)
_THRESHOLDS = [24, 12]

_TYPE_LABELS = {
    LectureType.QUIZ: "퀴즈",
    LectureType.ASSIGNMENT: "과제",
    LectureType.DISCUSSION: "토론",
    LectureType.WIKI_PAGE: "위키",
    LectureType.FILE: "파일",
    LectureType.ZOOM: "Zoom",
    LectureType.OTHER: "기타",
}


@dataclass
class DeadlineItem:
    """마감 임박 항목."""

    course: Course
    lecture: LectureItem
    type_label: str
    remaining_hours: float
    threshold: int
    dedup_key: str


def _parse_lms_date(date_str: str, now: datetime | None = None) -> datetime | None:
    """LMS 날짜 문자열을 파싱한다. (예: '3월 19일 오후 11:59')

    연도 전환기(12월→1월, 1월→12월) 보정:
    - 현재 11~12월인데 파싱 월이 1~2월이면 다음 해
    - 현재 1~2월인데 파싱 월이 11~12월이면 전년도
    """
    if not date_str:
        return None
    match = re.match(r"(\d+)월\s*(\d+)일(?:\s*(오전|오후)\s*(\d+):(\d+))?", date_str.strip())
    if not match:
        return None
    month = int(match.group(1))
    day = int(match.group(2))
    ampm = match.group(3)
    hour = int(match.group(4)) if match.group(4) else 23
    minute = int(match.group(5)) if match.group(5) else 59

    if ampm == "오후" and hour != 12:
        hour += 12
    elif ampm == "오전" and hour == 12:
        hour = 0

    if now is None:
        now = datetime.now(KST)

    # LOG-005: 연도 전환기 보정 — `월/일` 만 있는 문자열에 대해 now 와의 거리가
    # 가장 가까운 연도를 선택한다. 기존의 `month<=2 / month>=11` 규칙은 12/31 ↔
    # 1/1 경계에서 오판(이미 지난 날짜로 판정) 하던 버그가 있었다.
    candidates: list[datetime] = []
    for offset in (-1, 0, 1):
        try:
            candidates.append(datetime(now.year + offset, month, day, hour, minute, tzinfo=KST))
        except ValueError:
            continue
    if not candidates:
        return None
    return min(candidates, key=lambda d: abs((d - now).total_seconds()))


def _make_dedup_key(course: Course, lecture: LectureItem, threshold: int) -> str:
    """과목 ID + 강의 제목 해시 기반의 안정적인 dedup 키를 생성한다."""
    stable_id = hashlib.sha256(f"{course.id}:{lecture.title}".encode()).hexdigest()[:16]
    return f"{stable_id}:{threshold}"


def _load_notified() -> set[str]:
    try:
        if _DEADLINE_FILE.exists():
            return set(json.loads(_DEADLINE_FILE.read_text(encoding="utf-8")))
    except json.JSONDecodeError:
        _log.warning("deadline_notified.json 파싱 실패 — 초기화합니다.")
    except Exception:
        pass
    return set()


def _save_notified(notified: set[str]) -> None:
    """deadline_notified.json 을 원자적으로 저장한다 (LOG-003).

    자동 모드 + 수동 deadline check 동시 실행 시 lost update 를 방지하기 위해
    atomic_write_text + file_lock 공용 모듈을 사용한다.
    """
    from src.util.atomic_write import atomic_write_text, file_lock

    try:
        with file_lock(_DEADLINE_FILE):
            atomic_write_text(_DEADLINE_FILE, json.dumps(sorted(notified)))
    except Exception as e:
        _log.warning("deadline_notified.json 저장 실패: %s", e)


def find_approaching_deadlines(
    courses: list[Course],
    details: list[CourseDetail | None],
    notified: set[str] | None = None,
    now: datetime | None = None,
    collect_keys: set[str] | None = None,
) -> list[DeadlineItem]:
    """마감 임박 항목을 검색한다 (순수 로직, 알림 전송 없음).

    Args:
        courses:      과목 목록
        details:      과목별 강의 상세 (courses와 동일 순서)
        notified:     이미 알림 전송된 키 집합 (None이면 빈 set)
        now:          현재 시각 (테스트 시 주입 가능)
        collect_keys: ARCH-015 — non-None 전달 시 모든 강의x threshold dedup 키를
                      이 집합에 추가. stale 키 정리에 사용. 비디오/완료 강의도 포함.

    Returns:
        마감 임박 DeadlineItem 목록
    """
    if now is None:
        now = datetime.now(KST)
    if notified is None:
        notified = set()

    items: list[DeadlineItem] = []

    for course, detail in zip(courses, details, strict=False):
        if detail is None:
            continue
        for week in detail.weeks:
            for lec in week.lectures:
                # 전체 강의x threshold dedup 키를 valid_keys 집약 — 루프 1회로 해결
                if collect_keys is not None:
                    for threshold in _THRESHOLDS:
                        collect_keys.add(_make_dedup_key(course, lec, threshold))

                if lec.lecture_type in VIDEO_LECTURE_TYPES:
                    continue
                # 완료 판별: completion 또는 attendance 둘 중 하나라도 완료면 건너뜀
                if lec.completion == "completed":
                    continue
                if lec.attendance in ("attendance", "late", "excused"):
                    continue
                if lec.is_upcoming:
                    continue
                if not lec.end_date:
                    continue

                deadline = _parse_lms_date(lec.end_date, now=now)
                if deadline is None:
                    continue

                remaining_hours = (deadline - now).total_seconds() / 3600
                if remaining_hours <= 0:
                    continue

                type_label = _TYPE_LABELS.get(lec.lecture_type, lec.lecture_type.value)

                for threshold in _THRESHOLDS:
                    key = _make_dedup_key(course, lec, threshold)
                    if key in notified:
                        continue
                    if remaining_hours <= threshold:
                        items.append(
                            DeadlineItem(
                                course=course,
                                lecture=lec,
                                type_label=type_label,
                                remaining_hours=remaining_hours,
                                threshold=threshold,
                                dedup_key=key,
                            )
                        )

    return items


def check_and_notify_deadlines(
    courses: list[Course],
    details: list[CourseDetail | None],
    token: str = "",
    chat_id: str = "",
) -> int:
    """마감 임박 항목을 확인하고 텔레그램으로 알림을 전송한다.

    Args:
        courses:  과목 목록
        details:  과목별 강의 상세
        token:    텔레그램 봇 토큰 (빈 문자열이면 전송 건너뜀)
        chat_id:  텔레그램 Chat ID

    Returns:
        전송된 알림 수
    """
    if not token or not chat_id:
        return 0

    from src.notifier.telegram_notifier import notify_deadline_warning

    notified = _load_notified()

    # ARCH-015: find_approaching_deadlines 에 valid_keys 집합을 주입해 강의 순회를
    # 1회로 통합. stale 키 정리는 루프 종료 후 수행.
    valid_keys: set[str] = set()
    items = find_approaching_deadlines(courses, details, notified=notified, collect_keys=valid_keys)
    stale_keys = notified - valid_keys
    if stale_keys:
        notified -= stale_keys
        _save_notified(notified)

    if not items:
        return 0

    sent_count = 0
    for item in items:
        ok = notify_deadline_warning(
            bot_token=token,
            chat_id=chat_id,
            course_name=item.course.long_name,
            week_label=item.lecture.week_label,
            lecture_title=item.lecture.title,
            type_label=item.type_label,
            end_date=item.lecture.end_date or "",
            remaining_hours=item.remaining_hours,
        )
        if ok:
            notified.add(item.dedup_key)
            sent_count += 1

    if sent_count > 0:
        _save_notified(notified)

    return sent_count
