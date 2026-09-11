"""export_lms_schedule.normalize_schedule 회귀 테스트.

네트워크/자격증명 없이 순수 정규화 + 날짜 파싱만 검증한다. CourseScraper 의
실제 LMS 접속은 별도(수동/통합) 경로로 분리한다 — 본 테스트는 dataclass 를
직접 조립해 입력으로 쓰며 mock/fake/stub 를 사용하지 않는다.
"""

from datetime import datetime

from scripts.export_lms_schedule import (
    _MAX_ATTACHMENT_BYTES,
    _parse_announcements_json,
    _select_pdf_attachments,
    _strip_canvas_prefix,
    normalize_announcements,
    normalize_schedule,
)

from src.config import KST
from src.scraper.models import (
    Course,
    CourseDetail,
    LectureItem,
    LectureType,
    Week,
)


def _course() -> Course:
    return Course(id="100", long_name="자료구조", href="/courses/100", term="2026-1")


def _detail(course: Course, lectures: list[LectureItem]) -> CourseDetail:
    week = Week(title="3주차", week_number=3, lectures=lectures)
    return CourseDetail(course=course, course_name="자료구조", professors="교수", weeks=[week])


def _date_str(dt: datetime) -> str:
    """datetime → LMS 날짜 문자열('N월 N일 오후 HH:MM')."""
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


# ── 타입 분류 + pending 판정 ────────────────────────────────────────


def test_pending_assignment_normalized_with_date():
    now = datetime(2026, 6, 4, 9, 0, tzinfo=KST)
    deadline = datetime(2026, 6, 5, 23, 59, tzinfo=KST)
    course = _course()
    lec = LectureItem(
        title="과제 1",
        item_url="/courses/100/assignments/5",
        lecture_type=LectureType.ASSIGNMENT,
        completion="incomplete",
        end_date=_date_str(deadline),
    )
    items = normalize_schedule([course], [_detail(course, [lec])], now=now)

    assert len(items) == 1
    it = items[0]
    assert it["source"] == "lms"
    assert it["course"] == "자료구조"
    assert it["type"] == "assignment"
    assert it["title"] == "과제 1"
    assert it["date"] == "2026-06-05"
    assert it["time"] == "23:59"
    assert it["status"] == "pending"
    assert it["url"] == "https://canvas.ssu.ac.kr/courses/100/assignments/5"
    assert it["raw_due"] == _date_str(deadline)


def test_completed_item_excluded():
    now = datetime(2026, 6, 4, 9, 0, tzinfo=KST)
    course = _course()
    lec = LectureItem(
        title="과제 완료",
        item_url="/courses/100/assignments/6",
        lecture_type=LectureType.ASSIGNMENT,
        completion="completed",
        end_date="6월 5일 오후 11:59",
    )
    assert normalize_schedule([course], [_detail(course, [lec])], now=now) == []


def test_attendance_done_item_excluded():
    """출석 인정(attendance/late/excused) 항목은 제외."""
    now = datetime(2026, 6, 4, 9, 0, tzinfo=KST)
    course = _course()
    lec = LectureItem(
        title="출석 인정 퀴즈",
        item_url="/courses/100/quizzes/3",
        lecture_type=LectureType.QUIZ,
        completion="incomplete",
        attendance="late",
        end_date="6월 5일 오후 11:59",
    )
    assert normalize_schedule([course], [_detail(course, [lec])], now=now) == []


def test_upcoming_item_excluded():
    now = datetime(2026, 6, 4, 9, 0, tzinfo=KST)
    course = _course()
    lec = LectureItem(
        title="아직 안 열린 토론",
        item_url="/courses/100/discussion_topics/2",
        lecture_type=LectureType.DISCUSSION,
        completion="incomplete",
        is_upcoming=True,
        end_date="6월 9일 오후 11:59",
    )
    assert normalize_schedule([course], [_detail(course, [lec])], now=now) == []


def test_unwatched_video_included_with_no_date():
    """미시청 동영상은 마감일이 없어도 date/time None 으로 포함."""
    now = datetime(2026, 6, 4, 9, 0, tzinfo=KST)
    course = _course()
    lec = LectureItem(
        title="3주차 강의",
        item_url="/courses/100/external_tools/71#movie",
        lecture_type=LectureType.MOVIE,
        completion="incomplete",
    )
    items = normalize_schedule([course], [_detail(course, [lec])], now=now)
    assert len(items) == 1
    assert items[0]["type"] == "video"
    assert items[0]["date"] is None
    assert items[0]["time"] is None
    assert items[0]["raw_due"] is None


def test_completed_video_excluded():
    now = datetime(2026, 6, 4, 9, 0, tzinfo=KST)
    course = _course()
    lec = LectureItem(
        title="완료 강의",
        item_url="/courses/100/external_tools/71#movie2",
        lecture_type=LectureType.MOVIE,
        completion="completed",
    )
    assert normalize_schedule([course], [_detail(course, [lec])], now=now) == []


def test_wiki_and_zoom_excluded():
    """위키/Zoom/기타 타입은 일정에서 제외(과제/퀴즈/토론/파일/동영상만)."""
    now = datetime(2026, 6, 4, 9, 0, tzinfo=KST)
    course = _course()
    lectures = [
        LectureItem(title="위키", item_url="/w/1", lecture_type=LectureType.WIKI_PAGE),
        LectureItem(title="줌", item_url="/z/1", lecture_type=LectureType.ZOOM),
        LectureItem(title="기타", item_url="/o/1", lecture_type=LectureType.OTHER),
    ]
    assert normalize_schedule([course], [_detail(course, lectures)], now=now) == []


def test_file_type_included():
    now = datetime(2026, 6, 4, 9, 0, tzinfo=KST)
    course = _course()
    lec = LectureItem(
        title="강의자료.pdf",
        item_url="/courses/100/files/9",
        lecture_type=LectureType.FILE,
        completion="incomplete",
    )
    items = normalize_schedule([course], [_detail(course, [lec])], now=now)
    assert len(items) == 1
    assert items[0]["type"] == "file"


def test_none_detail_skipped():
    """fetch 실패(None) 과목은 건너뛰고 나머지는 정상 수집."""
    now = datetime(2026, 6, 4, 9, 0, tzinfo=KST)
    course = _course()
    lec = LectureItem(
        title="과제",
        item_url="/courses/100/assignments/1",
        lecture_type=LectureType.ASSIGNMENT,
        completion="incomplete",
    )
    items = normalize_schedule([course, course], [None, _detail(course, [lec])], now=now)
    assert len(items) == 1


# ── 날짜 파싱: 연도 전환기 보정이 반영되는지 ───────────────────────────


def test_year_rollover_dec_to_jan():
    """12월 말 기준 1월 마감은 다음 해로 파싱된다(_parse_lms_date 재사용)."""
    now = datetime(2026, 12, 31, 12, 0, tzinfo=KST)
    course = _course()
    lec = LectureItem(
        title="1월 과제",
        item_url="/courses/100/assignments/12",
        lecture_type=LectureType.ASSIGNMENT,
        completion="incomplete",
        end_date="1월 2일 오후 11:59",
    )
    items = normalize_schedule([course], [_detail(course, [lec])], now=now)
    assert len(items) == 1
    assert items[0]["date"] == "2027-01-02"


# ── 비-비디오 pending 항목 포함 경로(퀴즈/토론) ─────────────────────────


def test_pending_quiz_included():
    """완료/upcoming 이 아닌 퀴즈는 pending 으로 포함된다."""
    now = datetime(2026, 6, 4, 9, 0, tzinfo=KST)
    deadline = datetime(2026, 6, 5, 23, 59, tzinfo=KST)
    course = _course()
    lec = LectureItem(
        title="퀴즈 1",
        item_url="/courses/100/quizzes/7",
        lecture_type=LectureType.QUIZ,
        completion="incomplete",
        end_date=_date_str(deadline),
    )
    items = normalize_schedule([course], [_detail(course, [lec])], now=now)
    assert len(items) == 1
    assert items[0]["type"] == "quiz"
    assert items[0]["status"] == "pending"
    assert items[0]["date"] == "2026-06-05"
    assert items[0]["time"] == "23:59"


def test_pending_discussion_included():
    """완료/upcoming 이 아닌 토론은 pending 으로 포함된다."""
    now = datetime(2026, 6, 4, 9, 0, tzinfo=KST)
    deadline = datetime(2026, 6, 6, 23, 59, tzinfo=KST)
    course = _course()
    lec = LectureItem(
        title="토론 1",
        item_url="/courses/100/discussion_topics/8",
        lecture_type=LectureType.DISCUSSION,
        completion="incomplete",
        end_date=_date_str(deadline),
    )
    items = normalize_schedule([course], [_detail(course, [lec])], now=now)
    assert len(items) == 1
    assert items[0]["type"] == "discussion"
    assert items[0]["status"] == "pending"


# ── Canvas 공지 파싱/정규화 ────────────────────────────────────────────


def test_strip_canvas_prefix_and_parse():
    """`while(1);` 접두사를 제거하고 JSON 배열로 파싱한다."""
    body = 'while(1);[{"id":218958,"title":"[14주차 퀴즈 공지 및 가이드라인]","posted_at":"2026-06-01T04:00:48Z"}]'
    parsed = _parse_announcements_json(body)
    assert len(parsed) == 1
    assert parsed[0]["id"] == 218958
    assert parsed[0]["title"] == "[14주차 퀴즈 공지 및 가이드라인]"


def test_strip_canvas_prefix_without_prefix():
    """접두사가 없으면 그대로 파싱한다."""
    assert _parse_announcements_json('[{"id":1}]') == [{"id": 1}]


def test_strip_canvas_prefix_with_leading_whitespace():
    """접두사 앞 공백이 섞여 와도 안전하게 제거한다."""
    assert _strip_canvas_prefix("  while(1);[]") == "[]"


def test_recent_announcement_normalized_with_kst():
    """최근 공지는 KST 날짜/시간으로 정규화된다(UTC→KST +9h)."""
    now = datetime(2026, 6, 4, 9, 0, tzinfo=KST)
    course = _course()
    raw = [
        {
            "id": 218958,
            "title": "[14주차 퀴즈 공지 및 가이드라인]",
            "posted_at": "2026-06-01T04:00:48Z",
            "html_url": "https://canvas.ssu.ac.kr/courses/100/discussion_topics/218958",
        }
    ]
    items = normalize_announcements(course, raw, now=now)
    assert len(items) == 1
    it = items[0]
    assert it["source"] == "lms"
    assert it["course"] == "자료구조"
    assert it["type"] == "announcement"
    assert it["title"] == "[14주차 퀴즈 공지 및 가이드라인]"
    # 2026-06-01T04:00:48Z(UTC)에 9시간을 더하면 2026-06-01 13:00 KST
    assert it["date"] == "2026-06-01"
    assert it["time"] == "13:00"
    assert it["status"] == "info"
    assert it["url"] == "https://canvas.ssu.ac.kr/courses/100/discussion_topics/218958"
    assert it["raw_due"] == "2026-06-01T04:00:48Z"


def test_old_announcement_outside_window_excluded():
    """7일 window 밖(오래된) 공지는 제외된다."""
    now = datetime(2026, 6, 4, 9, 0, tzinfo=KST)
    course = _course()
    raw = [{"id": 1, "title": "오래된 공지", "posted_at": "2026-05-01T00:00:00Z"}]
    assert normalize_announcements(course, raw, now=now) == []


def test_announcement_url_fallback_and_created_at():
    """html_url 부재 시 id 로 URL 구성, posted_at 부재 시 created_at 을 raw_due 로."""
    now = datetime(2026, 6, 4, 9, 0, tzinfo=KST)
    course = _course()
    raw = [{"id": 555, "title": "공지", "created_at": "2026-06-03T00:00:00Z"}]
    items = normalize_announcements(course, raw, now=now)
    assert len(items) == 1
    assert items[0]["url"] == "https://canvas.ssu.ac.kr/courses/100/discussion_topics/555"
    assert items[0]["raw_due"] == "2026-06-03T00:00:00Z"


def test_announcement_without_date_excluded():
    """날짜를 파싱할 수 없는 공지는 제외(홍수 방지)."""
    now = datetime(2026, 6, 4, 9, 0, tzinfo=KST)
    course = _course()
    raw = [{"id": 9, "title": "날짜 없음"}]
    assert normalize_announcements(course, raw, now=now) == []


def test_announcement_message_html_passed_through():
    """공지 본문(message HTML)은 정규화 없이 그대로 통과한다(소비측 분석용)."""
    now = datetime(2026, 6, 4, 9, 0, tzinfo=KST)
    course = _course()
    raw = [
        {
            "id": 1,
            "title": "공지",
            "posted_at": "2026-06-03T00:00:00Z",
            "message": "<p>발표 마감 6월 13일</p>",
        }
    ]
    items = normalize_announcements(course, raw, now=now)
    assert len(items) == 1
    assert items[0]["message"] == "<p>발표 마감 6월 13일</p>"


def test_announcement_message_defaults_empty():
    """message 부재 시 빈 문자열로 정규화된다(소비측 None 방어)."""
    now = datetime(2026, 6, 4, 9, 0, tzinfo=KST)
    course = _course()
    raw = [{"id": 2, "title": "공지", "posted_at": "2026-06-03T00:00:00Z"}]
    items = normalize_announcements(course, raw, now=now)
    assert items[0]["message"] == ""


def test_announcement_attachments_normalized_for_calendarization():
    """Canvas 공지 첨부 메타데이터와 추출 텍스트는 calendarize 입력으로 보존된다."""
    now = datetime(2026, 6, 4, 9, 0, tzinfo=KST)
    course = _course()
    raw = [
        {
            "id": 3,
            "title": "PDF 첨부 공지",
            "posted_at": "2026-06-03T00:00:00Z",
            "attachments": [
                {
                    "id": 99,
                    "display_name": "기말고사 안내.pdf",
                    "url": "https://canvas.ssu.ac.kr/files/99/download",
                    "content-type": "application/pdf",
                    "size": 1234,
                }
            ],
            "attachment_texts": ["기말고사 일정: 6월 14일 오전 9시"],
        }
    ]
    items = normalize_announcements(course, raw, now=now)

    assert items[0]["attachments"] == [
        {
            "id": 99,
            "name": "기말고사 안내.pdf",
            "url": "https://canvas.ssu.ac.kr/files/99/download",
            "content_type": "application/pdf",
            "size": 1234,
        }
    ]
    assert items[0]["attachment_texts"] == ["기말고사 일정: 6월 14일 오전 9시"]


def _pdf_att(i: int, **overrides) -> dict:
    base = {
        "id": i,
        "display_name": f"자료{i}.pdf",
        "url": f"https://canvas.ssu.ac.kr/files/{i}/download",
        "content-type": "application/pdf",
        "size": 1000,
    }
    base.update(overrides)
    return base


def test_select_pdf_attachments_filters_non_pdf_and_invalid_url():
    """PDF 아닌 첨부·url 없는 첨부·dict 아닌 원소는 다운로드 대상에서 제외된다."""
    atts = [
        "문자열원소",
        _pdf_att(1, **{"content-type": "image/png", "display_name": "그림.png", "url": "https://x/1"}),
        _pdf_att(2, url=None),
        _pdf_att(3, url="   "),
        _pdf_att(4),
    ]
    sel = _select_pdf_attachments(atts)
    assert [a["id"] for a in sel] == [4]


def test_select_pdf_attachments_caps_per_announcement():
    """공지 1건당 상한(기본 5)을 넘는 PDF 첨부는 앞쪽부터만 선별된다."""
    atts = [_pdf_att(i) for i in range(1, 9)]
    sel = _select_pdf_attachments(atts)
    assert [a["id"] for a in sel] == [1, 2, 3, 4, 5]


def test_select_pdf_attachments_oversize_metadata_excluded_without_download():
    """메타데이터 size 가 상한을 넘으면(정수/실수 모두) 다운로드 없이 제외된다."""
    atts = [
        _pdf_att(1, size=_MAX_ATTACHMENT_BYTES + 1),
        _pdf_att(2, size=float(_MAX_ATTACHMENT_BYTES) * 1.5),
        _pdf_att(3),
    ]
    sel = _select_pdf_attachments(atts)
    assert [a["id"] for a in sel] == [3]


def test_select_pdf_attachments_non_list_input():
    """attachments 가 리스트가 아니면 빈 목록을 반환한다."""
    assert _select_pdf_attachments(None) == []
    assert _select_pdf_attachments({"url": "https://x/a.pdf"}) == []
