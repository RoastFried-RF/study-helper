"""deadline_checker.py 회귀 테스트 — R2-09, COD-N01.

R2-09:  한 강의가 24h·12h threshold 를 동시 통과할 때 알림은 가장 임박한
        1건만 발송되고, 함께 통과한 나머지 threshold 키는 suppress 돼야 한다.
COD-N01: dedup 키가 강의 제목이 아닌 full_url 기반이라 제목 변경에 불변이어야
        한다.

텔레그램 네트워크 I/O 는 notify_deadline_warning 을 mock 해 차단한다.
"""

from datetime import datetime, timedelta
from unittest.mock import patch

from src.config import KST
from src.scraper.models import (
    Course,
    CourseDetail,
    LectureItem,
    LectureType,
    Week,
)
from src.notifier.deadline_checker import (
    _make_dedup_key,
    find_approaching_deadlines,
)


def _course() -> Course:
    return Course(id="100", long_name="자료구조", href="/courses/100", term="2026-1")


def _assignment(
    *,
    title: str = "과제 1",
    item_url: str = "/courses/100/assignments/5",
    end_date: str,
) -> LectureItem:
    """마감 임박 후보가 되는 비-비디오 강의 항목."""
    return LectureItem(
        title=title,
        item_url=item_url,
        lecture_type=LectureType.ASSIGNMENT,
        week_label="3주차",
        end_date=end_date,
        completion="incomplete",
    )


def _detail(course: Course, lectures: list[LectureItem]) -> CourseDetail:
    week = Week(title="3주차", week_number=3, lectures=lectures)
    return CourseDetail(course=course, course_name="자료구조", professors="교수", weeks=[week])


def _date_str(dt: datetime) -> str:
    """datetime 을 LMS 날짜 문자열('N월 N일 오후 HH:MM') 로 변환."""
    hour = dt.hour
    if hour == 0:
        ampm, h12 = "오전", 12
    elif hour < 12:
        ampm, h12 = "오전", hour
    elif hour == 12:
        ampm, h12 = "오후", 12
    else:
        ampm, h12 = "오후", hour - 12
    return f"{dt.month}월 {dt.day}일 {ampm} {h12}:{dt.minute:02d}"


# ── R2-09: 동시 통과 시 1건만 발송, 나머지 suppress ─────────────────────


def test_deadline_within_12h_emits_single_item():
    """마감 10h 남아 24h·12h 둘 다 통과 → DeadlineItem 1건, threshold=12."""
    now = datetime(2026, 5, 20, 12, 0, tzinfo=KST)
    deadline = now + timedelta(hours=10)
    course = _course()
    detail = _detail(course, [_assignment(end_date=_date_str(deadline))])

    items = find_approaching_deadlines([course], [detail], notified=set(), now=now)

    assert len(items) == 1, "동시 통과해도 알림 항목은 1건"
    item = items[0]
    assert item.threshold == 12, "가장 임박한(작은) threshold 가 선택돼야 함"


def test_deadline_within_12h_suppresses_24h_key():
    """함께 통과한 24h threshold 키는 suppress_keys 에 담겨야 한다."""
    now = datetime(2026, 5, 20, 12, 0, tzinfo=KST)
    deadline = now + timedelta(hours=10)
    course = _course()
    lec = _assignment(end_date=_date_str(deadline))
    detail = _detail(course, [lec])

    items = find_approaching_deadlines([course], [detail], notified=set(), now=now)
    item = items[0]

    key_24 = _make_dedup_key(course, lec, 24)
    key_12 = _make_dedup_key(course, lec, 12)
    assert item.dedup_key == key_12
    assert item.suppress_keys == [key_24], (
        "발송하지 않은 24h 키는 suppress_keys 로 넘겨져야 함 (R2-09)"
    )


def test_check_and_notify_sends_one_and_records_suppressed_keys():
    """check_and_notify_deadlines: 동시 통과 시 알림 1건 발송 + 24h·12h 키 모두
    notified 에 기록(suppress) 되는지 검증.
    """
    # check_and_notify_deadlines 는 now 를 주입받지 못하고 내부에서 현재 시각을
    # 쓰므로, deadline 을 실제 현재 시각 기준 상대값으로 만든다 (날짜 의존 flaky 방지).
    now = datetime.now(KST).replace(minute=0, second=0, microsecond=0)
    deadline = now + timedelta(hours=10)
    course = _course()
    lec = _assignment(end_date=_date_str(deadline))
    detail = _detail(course, [lec])

    import src.notifier.deadline_checker as mod

    written: dict[str, set[str]] = {}

    def _fake_locked_transaction(path, load_fn, save_fn):
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            disk: set[str] = set()
            yield disk
            written["notified"] = set(disk)

        return _cm()

    with patch(
        "src.notifier.telegram_notifier.notify_deadline_warning", return_value=True
    ) as mock_notify, patch.object(
        mod, "_load_notified", return_value=set()
    ), patch(
        "src.util.atomic_write.locked_transaction", side_effect=_fake_locked_transaction
    ):
        sent = mod.check_and_notify_deadlines(
            [course], [detail], token="t", chat_id="c"
        )

    # 알림은 1건만 발송 (12h).
    assert mock_notify.call_count == 1, "동시 통과해도 텔레그램 발송은 1건"
    # L4: 반환값은 docstring 계약대로 "전송된 알림 수"(실제 텔레그램 발송 건수) = 1.
    # suppress 키는 notified 에 기록될 뿐 발송 건수에 포함되지 않는다(과거 len(sent_keys)
    # 가 dedup+suppress 를 더해 2 를 반환하던 버그를 수정).
    assert sent == 1
    key_24 = _make_dedup_key(course, lec, 24)
    key_12 = _make_dedup_key(course, lec, 12)
    assert written["notified"] == {key_12, key_24}, (
        "발송한 12h 키 + suppress 한 24h 키 모두 notified 에 기록돼야 함"
    )


def test_already_notified_threshold_not_resent():
    """이미 발송된 threshold 키는 재발송되지 않는다."""
    now = datetime(2026, 5, 20, 12, 0, tzinfo=KST)
    deadline = now + timedelta(hours=10)
    course = _course()
    lec = _assignment(end_date=_date_str(deadline))
    detail = _detail(course, [lec])

    # 12h 키는 이미 발송됨 → 12h 는 suppress, 24h 만 남음.
    key_12 = _make_dedup_key(course, lec, 12)
    items = find_approaching_deadlines(
        [course], [detail], notified={key_12}, now=now
    )

    assert len(items) == 1
    # passing 에서 12h 가 빠지면 chosen 은 24h.
    assert items[0].threshold == 24


def test_no_item_when_all_thresholds_notified():
    """모든 threshold 키가 이미 발송됐으면 알림 항목 없음."""
    now = datetime(2026, 5, 20, 12, 0, tzinfo=KST)
    deadline = now + timedelta(hours=10)
    course = _course()
    lec = _assignment(end_date=_date_str(deadline))
    detail = _detail(course, [lec])

    key_12 = _make_dedup_key(course, lec, 12)
    key_24 = _make_dedup_key(course, lec, 24)
    items = find_approaching_deadlines(
        [course], [detail], notified={key_12, key_24}, now=now
    )
    assert items == []


def test_deadline_within_24h_only_one_threshold():
    """마감 20h 남으면 24h 만 통과 → threshold=24, suppress_keys 비어 있음."""
    now = datetime(2026, 5, 20, 12, 0, tzinfo=KST)
    deadline = now + timedelta(hours=20)
    course = _course()
    lec = _assignment(end_date=_date_str(deadline))
    detail = _detail(course, [lec])

    items = find_approaching_deadlines([course], [detail], notified=set(), now=now)
    assert len(items) == 1
    assert items[0].threshold == 24
    assert items[0].suppress_keys == []


# ── COD-N01: dedup 키가 강의 제목 변경에 불변 ───────────────────────────


def test_dedup_key_stable_across_title_change():
    """COD-N01: 강의 제목이 바뀌어도 같은 full_url 이면 dedup 키가 동일해야 한다.

    수정 전: 키 재료에 lecture.title 이 포함돼 제목 수정 시 다른 키로 인식 →
    이미 발송한 마감 알림이 중복 발송됐다.
    """
    course = _course()
    lec_before = _assignment(title="과제 1", item_url="/courses/100/assignments/5", end_date="x")
    lec_after = _assignment(
        title="과제 1 (수정됨)", item_url="/courses/100/assignments/5", end_date="x"
    )

    for threshold in (24, 12):
        key_before = _make_dedup_key(course, lec_before, threshold)
        key_after = _make_dedup_key(course, lec_after, threshold)
        assert key_before == key_after, (
            f"제목 변경 시 dedup 키가 달라짐 (threshold={threshold}, COD-N01)"
        )


def test_dedup_key_differs_for_different_url():
    """full_url 이 다르면 dedup 키도 달라야 한다 (다른 강의 구분)."""
    course = _course()
    lec_a = _assignment(item_url="/courses/100/assignments/5", end_date="x")
    lec_b = _assignment(item_url="/courses/100/assignments/6", end_date="x")

    assert _make_dedup_key(course, lec_a, 24) != _make_dedup_key(course, lec_b, 24)


def test_dedup_key_differs_for_different_threshold():
    """같은 강의라도 threshold 가 다르면 dedup 키가 달라야 한다."""
    course = _course()
    lec = _assignment(end_date="x")
    assert _make_dedup_key(course, lec, 24) != _make_dedup_key(course, lec, 12)


def test_dedup_key_differs_for_different_course():
    """course.id 가 다르면 dedup 키도 달라야 한다."""
    lec = _assignment(end_date="x")
    course_a = Course(id="100", long_name="A", href="/c/100", term="2026-1")
    course_b = Course(id="200", long_name="B", href="/c/200", term="2026-1")
    assert _make_dedup_key(course_a, lec, 24) != _make_dedup_key(course_b, lec, 24)
