"""calendarize_lms_schedule 순수 함수 회귀 테스트.

네트워크/Google API/자격증명 없이 순수 분석 함수(HTML 정규화, 한국어 날짜 추출,
pending/공지 분석, 후보 생성, 중복 제거, fingerprint)만 검증한다. 실제 Google
Calendar write(--apply) 경로는 본 테스트 범위 밖이며 mock/fake/stub 를 쓰지 않는다.

now 기준 시각은 2026-06-04 09:00 KST(과제 요청의 "현재 후보" 맥락).
"""

import io
import sys
from datetime import datetime

from scripts.calendarize_lms_schedule import (
    SOURCE_MARKER_PREFIX,
    _load_items,
    analyze_announcement,
    analyze_pending,
    build_candidates,
    extract_pdf_text,
    html_to_text,
    make_fingerprint,
    parse_event_mentions,
)

from src.config import KST

NOW = datetime(2026, 6, 4, 9, 0, tzinfo=KST)


def _first_date(event: dict) -> str:
    """이벤트 시작 날짜('YYYY-MM-DD')를 반환한다(timed/all_day 공통)."""
    start = event["start"]
    return start["date"] if "date" in start else start["dateTime"][:10]


# ── HTML → plain text 정규화 ────────────────────────────────────────


def test_html_to_text_strips_tags_and_unescapes():
    html = "<p>기말 발표 &amp; 자료 제출</p>"
    assert html_to_text(html) == "기말 발표 & 자료 제출"


def test_html_to_text_br_and_block_to_spaces():
    html = "6월 13일<br>오전 10시 30분<p>업로드 마감</p>"
    out = html_to_text(html)
    # 줄바꿈/블록 경계는 공백으로 합쳐지고 토큰은 모두 보존된다.
    assert "6월 13일" in out
    assert "오전 10시 30분" in out
    assert "업로드 마감" in out
    assert "<" not in out and ">" not in out


def test_html_to_text_drops_script_style_content():
    html = "본문<script>alert('x')</script><style>.a{}</style> 끝"
    out = html_to_text(html)
    assert "alert" not in out
    assert ".a{" not in out
    assert "본문" in out and "끝" in out


def test_html_to_text_empty():
    assert html_to_text("") == ""
    assert html_to_text(None) == ""


# ── 한국어 날짜/시간 추출 ────────────────────────────────────────────


def test_parse_korean_date_with_ampm_time():
    mentions = parse_event_mentions("자료 업로드 마감은 6월 13일 오전 10시 30분입니다.", NOW)
    assert len(mentions) == 1
    m = mentions[0]
    assert m["date"] == "2026-06-13"
    assert m["time"] == "10:30"
    assert m["kind"] == "deadline"  # '마감' 키워드 → deadline
    assert "마감" in m["keywords"]


def test_parse_korean_date_only_no_time():
    mentions = parse_event_mentions("기말 발표 참관은 6월 20일에 진행합니다.", NOW)
    assert len(mentions) == 1
    assert mentions[0]["date"] == "2026-06-20"
    assert mentions[0]["time"] is None


def test_parse_iso_date_with_colon_time():
    mentions = parse_event_mentions("제출 기한 2026-06-08 19:30 까지", NOW)
    assert len(mentions) == 1
    assert mentions[0]["date"] == "2026-06-08"
    assert mentions[0]["time"] == "19:30"


def test_parse_year_rollover_dec_to_jan():
    now = datetime(2026, 12, 31, 12, 0, tzinfo=KST)
    mentions = parse_event_mentions("1월 5일 시험", now)
    assert len(mentions) == 1
    assert mentions[0]["date"] == "2027-01-05"


def test_parse_no_date_returns_empty():
    assert parse_event_mentions("일정은 추후 공지하겠습니다.", NOW) == []


def test_parse_pm_hour_conversion():
    mentions = parse_event_mentions("6월 8일 오후 7시 30분 마감", NOW)
    assert mentions[0]["time"] == "19:30"


# ── 슬래시(M/D) 날짜 + 영문 am/pm 추출 (실제 공지 포맷) ─────────────────


def test_parse_slash_date_with_ampm_time():
    """`6/13(금) 10:30am` → 2026-06-13 10:30 (괄호 요일 무시)."""
    mentions = parse_event_mentions("발표자료 업로드: 6/13(금) 10:30am 게시판", NOW)
    assert len(mentions) == 1
    m = mentions[0]
    assert m["date"] == "2026-06-13"
    assert m["time"] == "10:30"
    assert m["kind"] == "deadline"  # '업로드' 키워드 → deadline
    assert "업로드" in m["keywords"]


def test_parse_slash_date_only_allday():
    """`6/20(토)` → 2026-06-20 (시간 없음 → all-day)."""
    mentions = parse_event_mentions("6/20(토) 발표", NOW)
    assert len(mentions) == 1
    assert mentions[0]["date"] == "2026-06-20"
    assert mentions[0]["time"] is None


def test_parse_slash_date_weekday_ignored_keeps_keyword():
    """`6/13(토) 발표` → 2026-06-13, 괄호 요일 무시 + 키워드(발표) 보존."""
    mentions = parse_event_mentions("6/13(토) 발표", NOW)
    assert len(mentions) == 1
    assert mentions[0]["date"] == "2026-06-13"
    assert mentions[0]["time"] is None
    assert "발표" in mentions[0]["keywords"]


def test_parse_slash_pm_hour_conversion():
    """영문 pm 은 24시간제로 변환된다(`2:30pm` → 14:30)."""
    mentions = parse_event_mentions("6/13(금) 자료 제출 2:30pm 마감", NOW)
    assert mentions[0]["date"] == "2026-06-13"
    assert mentions[0]["time"] == "14:30"


def test_parse_slash_does_not_break_iso_slash_date():
    """ISO 슬래시 날짜(2026/06/13)는 한 건으로만 잡힌다(M/D 중복 매치 없음)."""
    mentions = parse_event_mentions("제출 기한 2026/06/13 18:00", NOW)
    assert len(mentions) == 1
    assert mentions[0]["date"] == "2026-06-13"
    assert mentions[0]["time"] == "18:00"


def test_parse_slash_ignores_ratios_scores_and_counts():
    """`1/2`, `9/10`, `10/20명` 같은 비날짜 슬래시는 일정으로 만들지 않는다."""
    text = "출석 1/2 이상, 퀴즈 9/10점, 발표 10/20명 참여"
    assert parse_event_mentions(text, NOW) == []


# ── pending 항목 분석 ──────────────────────────────────────────────


def _pending(course, title, date, time, url):
    return {
        "source": "lms",
        "course": course,
        "type": "assignment",
        "title": title,
        "date": date,
        "time": time,
        "status": "pending",
        "url": url,
        "raw_due": None,
    }


def test_pending_future_timed_creates():
    item = _pending(
        "데이터사이언스 (2150693001)",
        "실습 8. 심화문제 9.1, 9.2",
        "2026-06-08",
        "19:30",
        "https://canvas.ssu.ac.kr/courses/45617/modules/items/3350342",
    )
    res = analyze_pending(item, NOW)
    assert res["action"] == "create"
    ev = res["event"]
    assert ev["when_type"] == "timed"
    assert ev["start"]["dateTime"] == "2026-06-08T19:30:00+09:00"
    assert ev["start"]["timeZone"] == "Asia/Seoul"
    assert ev["origin"] == "pending"
    assert "데이터사이언스" in ev["summary"]
    # 코드 접미사 (2150693001) 는 summary 에서 제거된다.
    assert "2150693001" not in ev["summary"]


def test_pending_future_dateonly_allday():
    item = _pending("핀테크 (2150010701)", "과제 제출", "2026-06-10", None, "https://x/1")
    res = analyze_pending(item, NOW)
    assert res["action"] == "create"
    ev = res["event"]
    assert ev["when_type"] == "all_day"
    assert ev["start"]["date"] == "2026-06-10"
    # all-day 종료일은 익일(배타적)
    assert ev["end"]["date"] == "2026-06-11"


def test_pending_no_date_skipped():
    item = _pending("데이터사이언스 (2150693001)", "강의자료.pdf", None, None, "https://x/2")
    res = analyze_pending(item, NOW)
    assert res["action"] == "skip"
    assert res["reason"] == "no_date"


def test_pending_past_skipped():
    item = _pending("핀테크 (2150010701)", "지난 과제", "2026-05-26", "22:00", "https://x/3")
    res = analyze_pending(item, NOW)
    assert res["action"] == "skip"
    assert res["reason"] == "past"


# ── 공지 분석 ──────────────────────────────────────────────────────


def _announcement(course, title, message, url):
    return {
        "source": "lms",
        "course": course,
        "type": "announcement",
        "title": title,
        "message": message,
        "date": "2026-05-30",
        "time": "16:55",
        "status": "info",
        "url": url,
        "raw_due": "2026-05-30T07:55:19Z",
    }


def test_announcement_timed_deadline_creates():
    ann = _announcement(
        "디지털스토리텔링 (2150012601)",
        "기말 발표 안내",
        "<p>발표 자료 업로드 마감은 6월 13일 오전 10시 30분 입니다.</p>",
        "https://canvas.ssu.ac.kr/courses/43279/discussion_topics/218697",
    )
    res = analyze_announcement(ann, NOW)
    assert res["action"] == "create"
    events = res["events"]
    assert len(events) == 1
    ev = events[0]
    assert ev["when_type"] == "timed"
    assert ev["start"]["dateTime"] == "2026-06-13T10:30:00+09:00"
    assert ev["origin"] == "announcement"
    assert "디지털스토리텔링" in ev["summary"]


def test_announcement_dateonly_allday():
    ann = _announcement(
        "디지털스토리텔링 (2150012601)",
        "기말 발표 참관",
        "<p>기말 발표 참관은 6월 20일 진행됩니다.</p>",
        "https://canvas.ssu.ac.kr/courses/43279/discussion_topics/218662",
    )
    res = analyze_announcement(ann, NOW)
    assert res["action"] == "create"
    ev = res["events"][0]
    assert ev["when_type"] == "all_day"
    assert ev["start"]["date"] == "2026-06-20"
    assert ev["end"]["date"] == "2026-06-21"


def test_announcement_vague_manual_review():
    ann = _announcement(
        "창의융합인재되기3code전략 (사전녹화) (2150051201)",
        "기말고사 안내",
        "<p>기말고사 일정은 추후 공지하겠습니다.</p>",
        "https://canvas.ssu.ac.kr/courses/43408/discussion_topics/218483",
    )
    res = analyze_announcement(ann, NOW)
    assert res["action"] == "manual_review"
    assert res["reason"] == "no_parseable_date"


def test_announcement_too_many_dates_manual_review():
    """날짜가 과하게 많은 공지는 과생성 방지를 위해 자동 생성하지 않는다."""
    ann = _announcement(
        "디지털스토리텔링 (2150012601)",
        "전체 학사 일정 안내",
        "<p>6/5(금), 6/6(토), 6/7(일), 6/8(월), 6/9(화) 모두 확인하세요.</p>",
        "https://canvas.ssu.ac.kr/courses/43279/discussion_topics/1",
    )
    res = analyze_announcement(ann, NOW)
    assert res["action"] == "manual_review"
    assert res["reason"] == "too_many_dates"


def test_announcement_structured_table_many_rows_creates_events():
    """명시적인 HTML 표 일정은 4개를 넘어도 과생성으로 보지 않고 row별 생성한다."""
    ann = _announcement(
        "운영관리 (2150000001)",
        "기말 일정표",
        """
        <table>
          <tr><th>항목</th><th>일시</th></tr>
          <tr><td>퀴즈</td><td>6/5 09:00</td></tr>
          <tr><td>토론 제출</td><td>6/6 10:00</td></tr>
          <tr><td>자료 업로드</td><td>6/7 11:00</td></tr>
          <tr><td>발표</td><td>6/8 12:00</td></tr>
          <tr><td>시험</td><td>6/9 13:00</td></tr>
        </table>
        """,
        "https://canvas.ssu.ac.kr/courses/1/discussion_topics/2",
    )
    res = analyze_announcement(ann, NOW)

    assert res["action"] == "create"
    assert len(res["events"]) == 5
    assert [ev["start"]["dateTime"][:16] for ev in res["events"]] == [
        "2026-06-05T09:00",
        "2026-06-06T10:00",
        "2026-06-07T11:00",
        "2026-06-08T12:00",
        "2026-06-09T13:00",
    ]


def test_extract_pdf_text_fallback_reads_uncompressed_pdf_strings():
    """첨부 PDF는 외부 PDF 의존성이 없어도 단순 uncompressed text를 최소 추출한다."""
    pdf_bytes = b"%PDF-1.4\n1 0 obj<<>>stream\nBT (Final exam 6/14 09:00) Tj ET\nendstream\n%%EOF"

    assert "Final exam 6/14 09:00" in extract_pdf_text(pdf_bytes)


def test_announcement_attachment_pdf_text_creates_event_when_message_has_no_date():
    """본문에 날짜가 없어도 PDF 첨부 추출 텍스트에 날짜가 있으면 캘린더화한다."""
    ann = _announcement(
        "창의융합인재되기3code전략 (사전녹화) (2150051201)",
        "기말고사 안내 PDF 첨부",
        "<p>세부 일정은 첨부파일을 확인하세요.</p>",
        "https://canvas.ssu.ac.kr/courses/43408/discussion_topics/218483",
    )
    ann["attachment_texts"] = ["기말고사 일정: 6월 14일 오전 9시 시험"]

    res = analyze_announcement(ann, NOW)

    assert res["action"] == "create"
    ev = res["events"][0]
    assert ev["start"]["dateTime"] == "2026-06-14T09:00:00+09:00"
    assert "시험" in ev["summary"]


# ── 지문 / 출처 표시 ────────────────────────────────────


def test_fingerprint_stable_and_marker():
    fp1 = make_fingerprint("2026-06-08", "[데이터사이언스] 실습 8 마감", "https://x/1")
    fp2 = make_fingerprint("2026-06-08", "[데이터사이언스] 실습 8 마감", "https://x/1")
    fp3 = make_fingerprint("2026-06-08", "[데이터사이언스] 다른 제목", "https://x/1")
    assert fp1 == fp2
    assert fp1 != fp3
    assert len(fp1) >= 8


def test_event_description_contains_url_and_marker():
    item = _pending("데이터사이언스 (2150693001)", "실습 8", "2026-06-08", "19:30", "https://x/9")
    ev = analyze_pending(item, NOW)["event"]
    assert "https://x/9" in ev["description"]
    assert SOURCE_MARKER_PREFIX in ev["description"]
    assert ev["fingerprint"] in ev["description"]


# ── build_candidates: 통합 + 중복 제거 ──────────────────────────────


def test_build_candidates_dedup_own_marker():
    """이미 우리가 만든 이벤트(같은 source marker)는 다시 생성하지 않는다."""
    item = _pending("데이터사이언스 (2150693001)", "실습 8", "2026-06-08", "19:30", "https://x/9")
    ev = analyze_pending(item, NOW)["event"]
    existing = [
        {
            "date": "2026-06-08",
            "summary": "아무 제목",
            "description": f"{SOURCE_MARKER_PREFIX}{ev['fingerprint']}",
        }
    ]
    out = build_candidates([item], NOW, existing_events=existing)
    assert out["stats"]["create"] == 0
    assert len(out["skipped"]) == 1
    assert out["skipped"][0]["reason"] == "duplicate_marker"


def test_build_candidates_dedup_personal_event_same_date_similar_summary():
    """같은 날짜 + 유사 summary 의 기존 개인 일정이 있으면 생성하지 않는다."""
    ann = _announcement(
        "4차산업혁명시대의기술혁신과AI (2150138501)",
        "14주차 퀴즈 공지",
        "<p>퀴즈 응시는 6월 6일 진행됩니다.</p>",
        "https://canvas.ssu.ac.kr/courses/44038/discussion_topics/218958",
    )
    existing = [{"date": "2026-06-06", "summary": "퀴즈 응시", "description": ""}]
    out = build_candidates([ann], NOW, existing_events=existing)
    assert out["stats"]["create"] == 0
    assert any(s["reason"] == "duplicate_existing" for s in out["skipped"])


def test_build_candidates_same_date_different_summary_not_duplicate():
    """같은 날짜라도 summary 가 다르면 중복이 아니다(자료 업로드 vs 기말 발표)."""
    ann = _announcement(
        "디지털스토리텔링 (2150012601)",
        "기말 발표 안내",
        "<p>발표 자료 업로드 마감은 6월 13일 오전 10시 30분 입니다.</p>",
        "https://canvas.ssu.ac.kr/courses/43279/discussion_topics/218697",
    )
    existing = [{"date": "2026-06-13", "summary": "기말 발표", "description": ""}]
    out = build_candidates([ann], NOW, existing_events=existing)
    assert out["stats"]["create"] == 1


def test_build_candidates_mixed_scenario():
    """현재 후보 반영 규칙(요청 6번)의 분류가 일관되게 나오는지 통합 검증."""
    items = [
        # 생성: pending 실습 8 (미래 timed)
        _pending(
            "데이터사이언스 (2150693001)",
            "실습 8. 심화문제 9.1, 9.2",
            "2026-06-08",
            "19:30",
            "https://canvas.ssu.ac.kr/courses/45617/modules/items/3350342",
        ),
        # skip(past): 지난 과제
        _pending("핀테크 (2150010701)", "지난 과제", "2026-05-26", "22:00", "https://x/p1"),
        # skip(no_date): 파일
        _pending("데이터사이언스 (2150693001)", "강의자료", None, None, "https://x/p2"),
        # 생성: 공지 자료 업로드 6/13 10:30 (개인 일정 '기말 발표' 와 다른 summary)
        _announcement(
            "디지털스토리텔링 (2150012601)",
            "기말 발표 안내",
            "<p>발표 자료 업로드 마감은 6월 13일 오전 10시 30분 입니다.</p>",
            "https://canvas.ssu.ac.kr/courses/43279/discussion_topics/218697",
        ),
        # 생성: 공지 발표 참관 6/20 종일
        _announcement(
            "디지털스토리텔링 (2150012601)",
            "기말 발표 참관",
            "<p>기말 발표 참관은 6월 20일 진행됩니다.</p>",
            "https://canvas.ssu.ac.kr/courses/43279/discussion_topics/218662",
        ),
        # manual_review: 창의융합 기말고사 날짜 불명확
        _announcement(
            "창의융합인재되기3code전략 (사전녹화) (2150051201)",
            "기말고사 안내",
            "<p>기말고사 일정은 추후 공지하겠습니다.</p>",
            "https://canvas.ssu.ac.kr/courses/43408/discussion_topics/218483",
        ),
    ]
    # 개인 일정: 6/6 4차산업 퀴즈(여기선 입력에 없음), 6/13 디스텔 기말 발표(자료 업로드와 다른 summary)
    existing = [{"date": "2026-06-13", "summary": "기말 발표", "description": ""}]
    out = build_candidates(items, NOW, existing_events=existing)

    assert out["stats"]["create"] == 3  # 실습8 + 자료업로드 + 발표참관
    assert out["stats"]["manual_review"] == 1  # 기말고사
    # past + no_date = 최소 2건 skip
    skip_reasons = [s["reason"] for s in out["skipped"]]
    assert "past" in skip_reasons
    assert "no_date" in skip_reasons


def test_build_candidates_actual_slash_scenario():
    """실제 dry-run 누락 버그 회귀: 슬래시 날짜 공지에서 후보 3개가 나와야 한다.

    a) pending 데이터사이언스 실습 8 (2026-06-08 19:30 timed)
    b) 디지털스토리텔링 발표자료 업로드 (6/13 10:30am timed)
    c) 디지털스토리텔링 발표 참관 (6/20 all-day)

    - 6/6 퀴즈 공지는 기존 일정(6/6)과 중복 → skip 유지.
    - 6/13 기존 `디스텔 기말발` 이벤트가 있어도 `발표 자료 업로드`(6/13)는 skip 안 됨.
    """
    items = [
        _pending(
            "데이터사이언스 (2150693001)",
            "실습 8. 심화문제 9.1, 9.2",
            "2026-06-08",
            "19:30",
            "https://canvas.ssu.ac.kr/courses/45617/modules/items/3350342",
        ),
        # 디지털스토리텔링: 발표자료 업로드 6/13 10:30am + 발표 참관 6/20 (실제 슬래시 포맷)
        _announcement(
            "디지털스토리텔링 (2150012601)",
            "기말 발표 안내 - 확인 필수!!",
            (
                "<ul><li><strong>발표자료 업로드: 6/13(금) 10:30am 게시판 "
                "(발표 일자 무관하게 전원 필수)</strong></li>"
                "<li><strong>6/20(토) 발표 참관</strong></li></ul>"
            ),
            "https://canvas.ssu.ac.kr/courses/43279/discussion_topics/218697",
        ),
        # 4차산업 6/6 퀴즈 공지 — 기존 일정과 중복되어 skip
        _announcement(
            "4차산업혁명시대의기술혁신과AI (사전녹화) (2150138501)",
            "[14주차 퀴즈 공지 및 가이드라인]",
            "<p>6월 6일에 Zoom 으로 진행되는 14주차 퀴즈에 관련하여 안내드립니다.</p>",
            "https://canvas.ssu.ac.kr/courses/44038/discussion_topics/218958",
        ),
    ]
    # 개인 일정: 6/6 퀴즈(중복 트랩), 6/13 디스텔 기말발(발표자료 업로드 오탐 트랩)
    existing = [
        {"date": "2026-06-06", "summary": "4차산업 퀴즈", "description": ""},
        {"date": "2026-06-13", "summary": "디스텔 기말발", "description": ""},
    ]
    out = build_candidates(items, NOW, existing_events=existing)

    assert out["stats"]["create"] == 3
    dates = sorted(_first_date(ev) for ev in out["create"])
    assert dates == ["2026-06-08", "2026-06-13", "2026-06-20"]

    # 6/13 발표자료 업로드는 10:30 timed 로 생성되고, 기존 '디스텔 기말발'에 먹히지 않는다.
    upload = next(ev for ev in out["create"] if _first_date(ev) == "2026-06-13")
    assert upload["when_type"] == "timed"
    assert upload["start"]["dateTime"] == "2026-06-13T10:30:00+09:00"
    assert "업로드" in upload["summary"]

    # 6/20 발표 참관은 all-day.
    parade = next(ev for ev in out["create"] if _first_date(ev) == "2026-06-20")
    assert parade["when_type"] == "all_day"
    assert "발표" in parade["summary"]

    # 6/6 퀴즈는 기존 일정과 중복으로 skip.
    assert any(s.get("reason") == "duplicate_existing" for s in out["skipped"])


def test_build_candidates_actual_full_announcement_dedups_repeated_presentation_dates():
    """실제 공지 조합에서는 중복 발표 날짜를 합쳐 현재 승인 후보 3개만 생성한다."""
    items = [
        _pending(
            "데이터사이언스 (2150693001)",
            "실습 8. 심화문제 9.1, 9.2",
            "2026-06-08",
            "19:30",
            "https://canvas.ssu.ac.kr/courses/45617/modules/items/3350342",
        ),
        _announcement(
            "4차산업혁명시대의기술혁신과AI (사전녹화) (2150138501)",
            "[14주차 퀴즈 공지 및 가이드라인]",
            "<p>6월 6일에 Zoom 으로 진행되는 14주차 퀴즈에 관련하여 안내드립니다.</p>",
            "https://canvas.ssu.ac.kr/courses/44038/discussion_topics/218958",
        ),
        _announcement(
            "디지털스토리텔링 (2150012601)",
            "기말 발표 안내 - 확인 필수!!",
            (
                "<p>발표자료 업로드: 6/13(금) 10:30am 게시판 "
                "(발표 일자 무관하게 전원 필수)</p>"
                "<p>본인 발표일 아닐 시에도 출석 필요</p>"
                "<p>6/13(토) 발표: 김태겸 포함</p>"
                "<p>6/20(토) 발표</p>"
            ),
            "https://canvas.ssu.ac.kr/courses/43279/discussion_topics/218697",
        ),
        _announcement(
            "디지털스토리텔링 (2150012601)",
            "기말 발표 순서",
            "<p>*6/13 -> 13번까지</p><p>*6/20 -> 14번부터 29번까지</p>",
            "https://canvas.ssu.ac.kr/courses/43279/discussion_topics/218662",
        ),
        _announcement(
            "창의융합인재되기3code전략 (사전녹화) (2150051201)",
            "성적 평가에 관한 정리",
            "<p>기말고사는 오프라인 시험을 실시합니다. 날짜는 추후 공지합니다.</p>",
            "https://canvas.ssu.ac.kr/courses/43408/discussion_topics/218483",
        ),
    ]
    existing = [
        {"date": "2026-06-06", "summary": "4차산업 퀴즈", "description": ""},
        {"date": "2026-06-13", "summary": "디스텔 기말발", "description": ""},
    ]
    out = build_candidates(items, NOW, existing_events=existing)

    assert out["stats"]["create"] == 3
    dates = sorted(_first_date(ev) for ev in out["create"])
    assert dates == ["2026-06-08", "2026-06-13", "2026-06-20"]
    assert sum(1 for ev in out["create"] if _first_date(ev) == "2026-06-20") == 1
    assert out["stats"]["manual_review"] == 1
    duplicate_dates = {s.get("date") for s in out["skipped"] if s.get("reason") == "duplicate_existing"}
    assert {"2026-06-06", "2026-06-13", "2026-06-20"} <= duplicate_dates


def test_build_candidates_dry_run_default():
    """build_candidates 는 후보만 만들고 절대 write 하지 않는다(applied=False)."""
    item = _pending("핀테크 (2150010701)", "과제", "2026-06-10", None, "https://x/1")
    out = build_candidates([item], NOW)
    assert out["applied"] is False
    assert out["calendar"]  # 기본 캘린더 이름이 채워져 있음


def test_load_items_reads_utf8_stdin_buffer(monkeypatch):
    """Windows cp949 locale에서도 pipe 입력은 raw buffer를 UTF-8로 읽는다."""

    class _FakeStdin:
        buffer = io.BytesIO('{"items":[{"title":"한글 일정"}]}'.encode())

    monkeypatch.setattr(sys, "stdin", _FakeStdin())
    assert _load_items(None) == [{"title": "한글 일정"}]


def test_extract_pdf_text_fallback_no_literals_returns_empty():
    """괄호 리터럴이 전혀 없는 입력은 빈 문자열을 반환한다(find 점프 회귀 가드)."""
    assert extract_pdf_text(b"%PDF-1.4 binary junk without parens \x00\xff\x01") == ""


def test_extract_pdf_text_fallback_skips_binary_gaps():
    """리터럴 사이에 긴 무괄호 바이너리 구간이 있어도 추출 결과는 동일하다."""
    data = b"%PDF-1.4 " + b"\x00" * 5000 + b"(Hello) " + b"\xff" * 5000 + b"(World 6/14)"
    text = extract_pdf_text(data)
    assert "Hello" in text
    assert "World 6/14" in text
