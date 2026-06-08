"""
마감 임박 알림 모듈.

비디오가 아닌 강의 항목(퀴즈, 과제 등)의 마감이 임박할 때
텔레그램으로 알림을 전송한다.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
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
    # R2-09: 한 강의가 여러 threshold 를 동시 통과할 때 가장 임박한 1건만
    # 발송하고, 함께 통과했으나 발송하지 않은 다른 threshold 의 dedup 키를
    # 여기 담아 호출자가 notified 에 기록(suppress)하도록 한다.
    suppress_keys: list[str] = field(default_factory=list)


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
    """과목 ID + 강의 URL 해시 기반의 안정적인 dedup 키를 생성한다.

    COD-N01: 기존에는 `lecture.title` 을 키 재료로 썼으나 LMS 에서 강의
    제목이 수정되면 동일 강의가 다른 키로 인식돼 마감 알림이 중복 발송됐다.
    `full_url`(item_url 기반)은 강의 항목 고유 식별자라 제목 변경에 불변이다.
    """
    stable_id = hashlib.sha256(f"{course.id}:{lecture.full_url}".encode()).hexdigest()[:16]
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


def _write_notified(notified: set[str]) -> None:
    """deadline_notified.json 을 원자적으로 기록한다 (lock 없음).

    L6: `locked_transaction` 의 save_fn 으로 쓰이므로 자체적으로 file_lock 을
    잡지 않는다 — 중첩 flock 은 self-deadlock 이기 때문(WS-0 제약).
    호출자가 `locked_transaction`(또는 file_lock)으로 직렬화를 보장한다.
    """
    from src.util.atomic_write import atomic_write_text

    atomic_write_text(_DEADLINE_FILE, json.dumps(sorted(notified)))


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

                # R2-09: 한 강의가 24h·12h 양쪽 threshold 를 동시 통과할 때
                # (예: 마감 12h 이내 구간에서 처음 관측) 가장 임박한(가장 작은)
                # threshold 1건만 발송하고, 함께 통과한 나머지 threshold 의
                # dedup 키는 suppress_keys 로 넘겨 호출자가 notified 에 기록한다.
                # → 같은 강의에 알림 2건 동시 발송 방지.
                passing = [
                    threshold
                    for threshold in _THRESHOLDS
                    if remaining_hours <= threshold
                    and _make_dedup_key(course, lec, threshold) not in notified
                ]
                if not passing:
                    continue
                chosen = min(passing)
                items.append(
                    DeadlineItem(
                        course=course,
                        lecture=lec,
                        type_label=type_label,
                        remaining_hours=remaining_hours,
                        threshold=chosen,
                        dedup_key=_make_dedup_key(course, lec, chosen),
                        suppress_keys=[
                            _make_dedup_key(course, lec, threshold)
                            for threshold in passing
                            if threshold != chosen
                        ],
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
    from src.util.atomic_write import locked_transaction

    notified = _load_notified()

    # ARCH-015: find_approaching_deadlines 에 valid_keys 집합을 주입해 강의 순회를
    # 1회로 통합. stale 키 정리는 발송 후 수행.
    valid_keys: set[str] = set()
    items = find_approaching_deadlines(courses, details, notified=notified, collect_keys=valid_keys)
    stale_keys = notified - valid_keys

    # 발송은 file_lock **밖**에서 — telegram 네트워크 I/O 동안 파일락을 잡지 않는다.
    # L4: sent_keys 는 dedup + suppress 키를 모두 모으므로 그 길이는 "실제 발송 건수"
    # 와 다르다. 반환값(전송된 알림 수)은 별도 카운터로 정확히 센다.
    sent_keys: set[str] = set()
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
            sent_count += 1
            sent_keys.add(item.dedup_key)
            # R2-09: 함께 통과했으나 발송하지 않은 threshold 키도 notified 에
            # 기록해 다음 체크에서 잔여 알림이 재발송되지 않도록 suppress 한다.
            sent_keys.update(item.suppress_keys)

    # L6: stale 제거 + 발송 성공 키 추가를 단일 file_lock 안에서 수행한다.
    # locked_transaction 이 디스크 최신본을 다시 읽어 merge — 자동 모드와 수동
    # deadline check 가 동시 실행돼도 서로의 변경을 덮어쓰지 않는다.
    if stale_keys or sent_keys:
        def _apply(disk: set[str]) -> None:
            disk -= stale_keys
            disk |= sent_keys

        try:
            with locked_transaction(
                _DEADLINE_FILE, load_fn=_load_notified, save_fn=_write_notified,
            ) as disk:
                _apply(disk)
        except Exception as e:
            _log.warning("deadline_notified.json 저장 실패: %s", e)

    return sent_count
