"""읽기 전용 LMS 일정 export 스크립트.

Canvas LMS(canvas.ssu.ac.kr) 에서 처리해야 할 항목(미시청 동영상 / 과제 / 퀴즈 /
토론 / 파일)을 수집해 **JSON 한 덩어리를 stdout 으로** 출력한다. Hermes 의 개인
일정 digest 가 이 출력을 사용자 TODO 와 병합한다.

설계 원칙:
- **read-only**: 로그인 후 과목/강의 DOM 을 스크래핑하기만 한다. 재생/다운로드/
  텔레그램 전송 등 부수효과 없음.
- **날짜 파싱/상태 판단은 기존 SSOT 재사용**: `deadline_checker._parse_lms_date`
  (LMS '3월 19일 오후 11:59' 포맷 + 연도 전환기 보정) 와 `CourseScraper` 의
  완료/출석/upcoming 판정을 그대로 사용한다.
- **stdout 오염 금지**: JSON 외 어떤 것도 stdout 으로 내보내지 않는다. 진행 로그는
  파일 로거(study_helper.log) 와 stderr 로만 나간다.

종료 코드:
    0  정상 — items 수집 성공(빈 배열 포함). `{"ok": true, ...}`
    2  자격증명 미설정 — LMS_USER_ID/PASSWORD 없음. `{"ok": false, "error": ...}`
    3  수집 실패 — 로그인/스크래핑 예외. 마지막까지 JSON 은 출력한다.

출력 스키마 (stdout):
    {
      "ok": true,
      "generated_at": "2026-06-04T17:00:00+09:00",
      "source": "lms",
      "items": [
        {"source": "lms", "course": "자료구조", "type": "assignment",
         "title": "과제 1", "date": "2026-06-05", "time": "23:59",
         "status": "pending", "url": "https://...", "raw_due": "6월 5일 오후 11:59"},
        ...
      ],
      "errors": ["..."],
      "announcements_supported": true,
      "stats": {"courses": 5, "pending": 12, "announcements": 3}
    }

공지사항(announcements): 로그인된 Playwright 세션 쿠키로 Canvas REST
`/api/v1/courses/{id}/discussion_topics?only_announcements=true` 를 read-only 로
호출해 최근(기본 7일) 공지만 수집한다. 응답 본문은 anti-JSON-hijacking 접두사
`while(1);` 가 붙어 오므로 안전하게 제거 후 파싱한다. 공지는 정보성 항목으로
`type: "announcement"`, `status: "info"` 로 정규화되며, 한 과목 수집 실패는 전체
export 를 중단하지 않고 `errors` 에 간결한 사유만 추가한다. 수집 경로가 동작하면
`announcements_supported: true`.

사용법:
    python -m scripts.export_lms_schedule            # JSON 한 줄
    python -m scripts.export_lms_schedule --pretty   # 들여쓰기 JSON
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

# 모듈 레벨에서는 playwright 를 끌어오지 않는다(테스트가 무거운 의존성 없이
# normalize_schedule 을 import 할 수 있도록). CourseScraper 는 collect() 안에서 import.
from src.config import KST, Config
from src.logger import get_logger

# 날짜 파싱 SSOT 재사용 — 별도 파서를 재구현하지 않는다(연도 전환기 보정 포함).
from src.notifier.deadline_checker import _parse_lms_date
from src.scraper.models import LectureType

if TYPE_CHECKING:
    from src.scraper.models import Course, CourseDetail

_log = get_logger("export_lms_schedule")

# 완료로 간주하는 출석 상태(결석/absent 제외) — deadline_checker 와 동일 기준.
_DONE_ATTENDANCE = {"attendance", "late", "excused"}

# 비-비디오 항목 중 일정에 포함할 타입 → export type 라벨.
# 과제/퀴즈/토론/파일만 포함(위키/Zoom/기타는 제외).
_NON_VIDEO_TYPES: dict[LectureType, str] = {
    LectureType.ASSIGNMENT: "assignment",
    LectureType.QUIZ: "quiz",
    LectureType.DISCUSSION: "discussion",
    LectureType.FILE: "file",
}

# Canvas LMS 베이스 URL — html_url 부재 시 공지 URL 직접 구성용.
_BASE_URL = "https://canvas.ssu.ac.kr"

# Canvas anti-JSON-hijacking 접두사 — 응답 본문 앞에 붙어 온다.
_CANVAS_PREFIX = "while(1);"

# 공지 수집 파라미터.
_ANNOUNCEMENTS_PER_PAGE = 5  # 과목당 최신 N건만 요청(오래된 공지 홍수 방지).
_ANNOUNCEMENT_WINDOW_DAYS = 7  # now 기준 최근 N일 공지만 포함.

# 첨부 PDF 추출 상한 — content-length/본문 크기로 과다 다운로드를 막는다.
_MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024  # 10MB

# 공지 1건당 다운로드할 PDF 첨부 상한 — 첨부 폭주 공지에서 다운로드 시간·메모리
# 피크가 무한정 커지지 않게 한다(마감 관련 첨부는 통상 앞쪽에 온다).
_MAX_PDF_ATTACHMENTS_PER_ANNOUNCEMENT = 5


def _strip_canvas_prefix(text: str) -> str:
    """Canvas anti-JSON-hijacking 접두사(`while(1);`)를 안전하게 제거한다 (순수 함수).

    접두사 앞에 공백이 섞여 와도 처리하며, 접두사가 없으면 원본을 그대로 반환한다.
    """
    stripped = text.lstrip()
    if stripped.startswith(_CANVAS_PREFIX):
        return stripped[len(_CANVAS_PREFIX) :]
    return stripped


def _parse_announcements_json(text: str) -> list[dict]:
    """공지 응답 본문을 접두사 제거 후 JSON 파싱해 dict 리스트로 반환한다 (순수 함수).

    Canvas 는 공지 목록을 JSON 배열로 반환한다. 배열이 아니거나 dict 가 아닌
    원소는 제외한다.
    """
    obj = json.loads(_strip_canvas_prefix(text))
    if not isinstance(obj, list):
        return []
    return [a for a in obj if isinstance(a, dict)]


def _parse_canvas_timestamp(raw: str | None) -> datetime | None:
    """Canvas ISO8601 UTC 타임스탬프('...Z')를 KST aware datetime 으로 변환한다.

    파싱 실패/빈값이면 None. tz 정보가 없으면 UTC 로 간주한다.
    """
    if not raw:
        return None
    s = raw.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(KST)


def _normalize_attachments(raw: object) -> list[dict]:
    """Canvas 공지 첨부 메타데이터를 calendarize 소비용 형태로 정규화한다 (순수 함수).

    각 항목: {id, name, url, content_type, size}. name 은 display_name → filename →
    name 순으로 결정한다. dict 가 아닌 원소는 제외한다.
    """
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for att in raw:
        if not isinstance(att, dict):
            continue
        out.append(
            {
                "id": att.get("id"),
                "name": att.get("display_name") or att.get("filename") or att.get("name"),
                "url": att.get("url"),
                "content_type": att.get("content-type") or att.get("content_type"),
                "size": att.get("size"),
            }
        )
    return out


def _announcement_url(course: Course, ann: dict) -> str:
    """공지 URL 을 결정한다 — html_url 우선, 없으면 id 로 안전하게 구성."""
    html_url = ann.get("html_url")
    if isinstance(html_url, str) and html_url.strip():
        return html_url.strip()
    topic_id = ann.get("id")
    if topic_id is not None:
        return f"{_BASE_URL}/courses/{course.id}/discussion_topics/{topic_id}"
    return course.full_url


def normalize_announcements(
    course: Course,
    raw_announcements: list[dict],
    now: datetime | None = None,
    window_days: int = _ANNOUNCEMENT_WINDOW_DAYS,
) -> list[dict]:
    """과목 공지 목록을 정규화한다 (순수 함수, 네트워크 의존성 없음).

    now 기준 최근 `window_days` 일 이내 공지만 포함한다(오래된 공지 홍수 방지).
    날짜를 파싱할 수 없는 공지는 제외한다.

    각 항목 필드: source=`lms`, course, type=`announcement`, title, date(KST),
    time(KST), status=`info`, url, raw_due=<posted_at 또는 created_at 원본 문자열>,
    message=<본문 HTML 원본 또는 "">.

    message 는 Canvas 공지의 HTML 본문을 그대로 통과시킨다(plain text 정규화/날짜
    추출은 소비측 calendarize_lms_schedule.html_to_text 가 담당). 본문은 정보성
    메타데이터이며 쿠키/토큰/.env 값을 포함하지 않는다.
    """
    if now is None:
        now = datetime.now(KST)
    cutoff = now - timedelta(days=window_days)

    items: list[dict] = []
    for ann in raw_announcements:
        if not isinstance(ann, dict):
            continue
        raw_ts = ann.get("posted_at") or ann.get("created_at")
        posted = _parse_canvas_timestamp(raw_ts)
        if posted is None or posted < cutoff:
            continue
        title = (ann.get("title") or "").strip() or "(제목 없음)"
        attachment_texts = [t for t in (ann.get("attachment_texts") or []) if isinstance(t, str) and t.strip()]
        items.append(
            {
                "source": "lms",
                "course": course.long_name,
                "type": "announcement",
                "title": title,
                "date": posted.strftime("%Y-%m-%d"),
                "time": posted.strftime("%H:%M"),
                "status": "info",
                "url": _announcement_url(course, ann),
                "raw_due": raw_ts,
                "message": ann.get("message") or "",
                "attachments": _normalize_attachments(ann.get("attachments")),
                "attachment_texts": attachment_texts,
            }
        )
    return items


def _is_done(lec) -> bool:
    """이미 완료(처리 불필요)된 항목인지 판단한다.

    완료 판정 기준은 deadline_checker / auto.py 와 동일:
    - completion == "completed"
    - attendance ∈ {attendance, late, excused}
    """
    if lec.completion == "completed":
        return True
    return lec.attendance in _DONE_ATTENDANCE


def _item_type(lec) -> str | None:
    """LectureItem 을 export type 라벨로 분류한다.

    - 비디오 계열(MOVIE/READYSTREAM/...) → "video"
    - 과제/퀴즈/토론/파일 → 해당 라벨
    - 그 외(위키/Zoom/기타) → None (일정에서 제외)
    """
    if lec.is_video:
        return "video"
    return _NON_VIDEO_TYPES.get(lec.lecture_type)


def normalize_schedule(
    courses: list[Course],
    details: list[CourseDetail | None],
    now: datetime | None = None,
) -> list[dict]:
    """과목/강의 상세에서 처리해야 할(pending) 항목을 정규화한다 (순수 함수).

    네트워크 의존성이 없어 단위 테스트 가능하다. 각 항목은 Hermes digest 가 그대로
    소비할 수 있는 dict 로 변환된다.

    Args:
        courses: 과목 목록
        details: 과목별 강의 상세(courses 와 동일 순서, 실패 과목은 None)
        now:     현재 시각(테스트 주입용). None 이면 datetime.now(KST).

    Returns:
        pending 항목 dict 리스트. 필드: source/course/type/title/date/time/
        status/url/raw_due.
    """
    if now is None:
        now = datetime.now(KST)

    items: list[dict] = []
    for course, detail in zip(courses, details, strict=False):
        if detail is None:
            continue
        for week in detail.weeks:
            for lec in week.lectures:
                item_type = _item_type(lec)
                if item_type is None:
                    continue

                # 비디오: 시청 미완료(needs_watch)만. needs_watch 가 완료/upcoming 을
                # 이미 배제한다.
                if item_type == "video":
                    if not lec.needs_watch:
                        continue
                else:
                    # 비-비디오: 완료/upcoming 제외.
                    if lec.is_upcoming:
                        continue
                    if _is_done(lec):
                        continue

                raw_due = lec.end_date or None
                date_str: str | None = None
                time_str: str | None = None
                deadline = _parse_lms_date(raw_due, now=now) if raw_due else None
                if deadline is not None:
                    date_str = deadline.strftime("%Y-%m-%d")
                    time_str = deadline.strftime("%H:%M")

                items.append(
                    {
                        "source": "lms",
                        "course": course.long_name,
                        "type": item_type,
                        "title": lec.title,
                        "date": date_str,
                        "time": time_str,
                        "status": "pending",
                        "url": lec.full_url,
                        "raw_due": raw_due,
                    }
                )
    return items


async def _fetch_announcements_raw(page, course: Course, per_page: int) -> str:
    """로그인된 세션 쿠키로 과목 공지 목록을 read-only 로 요청해 본문 텍스트를 반환한다.

    `page.request` 는 BrowserContext 의 쿠키를 공유하므로 추가 로그인 없이 호출된다.
    HTTP 비정상 응답은 RuntimeError 로 올려 호출부가 과목 단위로 잡게 한다(상태 코드만,
    URL/토큰 미노출).
    """
    url = f"{_BASE_URL}/api/v1/courses/{course.id}/discussion_topics?only_announcements=true&per_page={per_page}"
    resp = await page.request.get(url)
    if not resp.ok:
        raise RuntimeError(f"HTTP {resp.status}")
    return await resp.text()


def _looks_like_pdf(att: dict) -> bool:
    """첨부가 PDF 로 보이는지 판정한다(content-type / 파일명 / URL 기준)."""
    if not isinstance(att, dict):
        return False
    ct = (att.get("content-type") or att.get("content_type") or "").lower()
    if "pdf" in ct:
        return True
    name = (att.get("display_name") or att.get("filename") or att.get("name") or "").lower()
    if name.endswith(".pdf"):
        return True
    url = (att.get("url") or "").lower()
    return ".pdf" in url


def _select_pdf_attachments(attachments: object, limit: int = _MAX_PDF_ATTACHMENTS_PER_ANNOUNCEMENT) -> list[dict]:
    """다운로드 대상 PDF 첨부를 고른다 (순수 함수).

    - PDF 로 보이는 첨부 + 유효한 url 이 있는 것만
    - 메타데이터 `size` 가 상한(_MAX_ATTACHMENT_BYTES)을 넘으면 다운로드 없이 제외
    - 공지 1건당 최대 `limit` 건 (등장 순서 보존)
    """
    if not isinstance(attachments, list):
        return []
    out: list[dict] = []
    for att in attachments:
        if len(out) >= limit:
            break
        if not _looks_like_pdf(att):
            continue
        url = att.get("url")
        if not isinstance(url, str) or not url.strip():
            continue
        size = att.get("size")
        # bool 은 int 의 서브클래스라 명시 제외. float 로 오는 size 도 상한 판정에 포함.
        if isinstance(size, (int, float)) and not isinstance(size, bool) and size > _MAX_ATTACHMENT_BYTES:
            continue
        out.append(att)
    return out


async def _augment_attachment_texts(page, announcements: list[dict]) -> None:
    """공지 첨부 PDF 를 인증 세션으로 read-only 다운로드해 추출 텍스트를 주입한다.

    각 raw 공지 dict 의 `attachment_texts` 에 비어있지 않은 추출 텍스트를 추가한다.
    한 첨부 실패는 해당 공지/과목에 국한되며 간결한 타입명만 로깅한다(URL 미노출).
    대상 선별(`_select_pdf_attachments`)은 PDF + 유효 url + size 상한 + 공지당
    개수 상한을 적용하고, 다운로드 후에도 content-length/본문 크기로 재확인한다.
    """
    from scripts.calendarize_lms_schedule import extract_pdf_text

    for ann in announcements:
        if not isinstance(ann, dict):
            continue
        texts: list[str] = list(ann.get("attachment_texts") or [])
        for att in _select_pdf_attachments(ann.get("attachments")):
            url = att["url"]
            try:
                resp = await page.request.get(url)
                if not resp.ok:
                    raise RuntimeError(f"HTTP {resp.status}")
                content_length = resp.headers.get("content-length")
                if (
                    content_length is not None
                    and content_length.isdigit()
                    and int(content_length) > _MAX_ATTACHMENT_BYTES
                ):
                    raise RuntimeError("attachment_too_large")
                body = await resp.body()
                if len(body) > _MAX_ATTACHMENT_BYTES:
                    raise RuntimeError("attachment_too_large")
                text = extract_pdf_text(body)
            except Exception as e:  # 첨부 실패가 전체 export 를 깨면 안 됨 (URL 미노출)
                _log.warning("공지 첨부 추출 실패: %s", type(e).__name__)
                continue
            if text and text.strip():
                texts.append(text.strip())
        if texts:
            ann["attachment_texts"] = texts


async def _collect_announcements(
    page, courses: list[Course], now: datetime | None = None
) -> tuple[list[dict], list[str]]:
    """과목별 공지를 순차 수집·정규화한다. 한 과목 실패는 전체를 중단하지 않는다.

    Returns:
        (announcements, errors) — errors 는 과목 단위 실패 사유(타입명만, URL 미노출).
    """
    announcements: list[dict] = []
    errors: list[str] = []
    for course in courses:
        try:
            raw = await _fetch_announcements_raw(page, course, _ANNOUNCEMENTS_PER_PAGE)
            parsed = _parse_announcements_json(raw)
            await _augment_attachment_texts(page, parsed)
        except Exception as e:  # 한 과목 실패가 전체 export 를 깨면 안 됨
            # SEC: 원본 메시지에는 세션 URL 이 섞일 수 있어 타입명만 노출.
            _log.warning("공지 수집 실패 (%s): %s", course.long_name, e, exc_info=True)
            errors.append(f"공지 수집 실패({course.long_name}): {type(e).__name__}")
            continue
        announcements.extend(normalize_announcements(course, parsed, now=now))
    return announcements, errors


async def _collect() -> tuple[list[Course], list[CourseDetail | None], list[dict], list[str]]:
    """LMS 에 로그인해 과목/강의 상세 + 공지를 read-only 로 수집한다.

    CourseScraper 를 그대로 재사용한다(별도 스크래퍼 재구현 금지). 진행 로그는
    log_callback 으로 stderr 에만 출력해 stdout(JSON) 오염을 막는다.

    Returns:
        (courses, details, announcements, announcement_errors)
    """
    from src.scraper.course_scraper import CourseScraper

    def _stderr_log(msg: str) -> None:
        print(msg, file=sys.stderr, flush=True)

    scraper = CourseScraper(
        username=Config.LMS_USER_ID,
        password=Config.LMS_PASSWORD,
        headless=True,
        log_callback=_stderr_log,
    )
    await scraper.start()
    try:
        courses = await scraper.fetch_courses()
        details = await scraper.fetch_all_details(courses)
        announcements, ann_errors = await _collect_announcements(scraper.page, courses)
        return courses, details, announcements, ann_errors
    finally:
        await scraper.close()


def _emit(payload: dict, *, pretty: bool) -> None:
    """payload 를 UTF-8 JSON 으로 stdout 에 출력한다(한국어 보존)."""
    text = json.dumps(payload, ensure_ascii=False, indent=2 if pretty else None)
    print(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="읽기 전용 LMS 일정 export (JSON stdout)")
    parser.add_argument("--pretty", action="store_true", help="들여쓰기 JSON 출력")
    args = parser.parse_args(argv)

    # Windows cp949 stdout 에서도 한국어가 깨지지 않도록 UTF-8 로 재설정.
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]  # 표준 출력의 메서드 존재 확인 후 호출
        except Exception:
            pass
    elif isinstance(getattr(sys.stdout, "buffer", None), io.BufferedWriter):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

    generated_at = datetime.now(KST).isoformat()

    if not Config.has_credentials():
        _emit(
            {
                "ok": False,
                "generated_at": generated_at,
                "source": "lms",
                "items": [],
                "errors": ["LMS 자격증명 미설정 (LMS_USER_ID / LMS_PASSWORD)"],
                "announcements_supported": False,
                "stats": {"courses": 0, "pending": 0},
            },
            pretty=args.pretty,
        )
        return 2

    import asyncio

    try:
        courses, details, announcements, ann_errors = asyncio.run(_collect())
    except Exception as e:
        # SEC: 원본 예외 메시지에는 세션 토큰 포함 URL 이 섞일 수 있어 타입명만 노출.
        # 상세 traceback 은 study_helper.log 에만 남긴다.
        exc_type = type(e).__name__
        _log.error("LMS 수집 실패: %s", e, exc_info=True)
        _emit(
            {
                "ok": False,
                "generated_at": generated_at,
                "source": "lms",
                "items": [],
                "errors": [f"LMS 수집 실패: {exc_type}"],
                "announcements_supported": False,
                "stats": {"courses": 0, "pending": 0},
            },
            pretty=args.pretty,
        )
        return 3

    items = normalize_schedule(courses, details)
    pending_count = len(items)
    # 공지(정보성)는 pending 항목 뒤에 합쳐 단일 items 리스트로 내보낸다.
    # digest 가 type=="announcement" 로 분리해 별도 섹션에 렌더한다.
    items.extend(announcements)

    failed_courses = sum(1 for d in details if d is None)
    errors: list[str] = list(ann_errors)
    if failed_courses:
        errors.append(f"{failed_courses}개 과목 강의 상세 로딩 실패(부분 수집)")

    _emit(
        {
            "ok": True,
            "generated_at": generated_at,
            "source": "lms",
            "items": items,
            "errors": errors,
            "announcements_supported": True,
            "stats": {
                "courses": len(courses),
                "pending": pending_count,
                "announcements": len(announcements),
            },
        },
        pretty=args.pretty,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
