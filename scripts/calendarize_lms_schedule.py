"""LMS export 결과를 Google Calendar `업무` 캘린더로 안전하게 캘린더화한다.

`scripts/export_lms_schedule.py` (또는 Hermes 의 lms_schedule_cache.json) 가 만든
JSON items 를 입력으로 받아, **확실한 날짜/시간이 있는 pending 항목과 공지 본문**만
calendar event 후보로 만든다. Google Calendar 쓰기는 **기본 dry-run** 이며, 실제
생성은 `--apply` 가 있을 때만 수행한다(이 옵션은 Hermes 부모 에이전트가 별도 검증
후 호출한다).

설계 원칙:
- **stdout 은 JSON 한 덩어리만**: 진행/경고 로그는 stderr 로만 보낸다.
- **순수 분석 + 부수효과 분리**: 날짜 추출/후보 생성/중복 판정은 네트워크 없는 순수
  함수(테스트 대상). Google REST 호출은 `--apply` 경로에서만 lazy import.
- **민감값 미노출**: OAuth 토큰/client secret/.env/쿠키는 stdout/stderr/이벤트
  본문 어디에도 출력하지 않는다.
- **중복 방지**: (a) 우리가 만든 이벤트는 description 의 source marker
  `LMS-CALENDARIZE:<fingerprint>` 로 식별, (b) 기존 개인 일정은 같은 날짜 +
  유사 summary 로 식별. 둘 중 하나라도 매치되면 생성하지 않는다.

분류:
- create        : 확실한 날짜(+시간) → timed / all_day event 후보
- manual_review : 공지에 날짜가 있으나 파싱 불가/불명확 → 사람이 검토(생성 안 함)
- skipped       : past / no_date / duplicate_marker / duplicate_existing

사용법:
    # dry-run (기본) — export 결과를 파이프로 받아 후보 JSON 출력
    python -m scripts.export_lms_schedule | python -m scripts.calendarize_lms_schedule

    # 캐시 파일 + 기존 일정 파일을 입력으로
    python -m scripts.calendarize_lms_schedule \
        --input lms_schedule_cache.json --existing-events existing.json

    # 실제 생성(Hermes 부모만 호출) — 본 작업에서는 실행하지 않는다
    python -m scripts.calendarize_lms_schedule --apply --token google_token.json
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
from datetime import datetime, timedelta
from html.parser import HTMLParser
from typing import ClassVar

from src.config import KST
from src.logger import get_logger

_log = get_logger("calendarize_lms_schedule")

# 생성한 이벤트 description 에 남기는 출처 마커(중복 방지 식별자).
SOURCE_MARKER_PREFIX = "LMS-CALENDARIZE:"

# 기본 대상 캘린더 이름.
DEFAULT_CALENDAR = "업무"

# timed 이벤트 기본 지속시간(마감/공지 시점 표시용 블록).
_TIMED_DURATION_MIN = 30

# 공지 본문에서 추출할 일정 키워드(등장 순서대로 label 구성에 사용).
_EVENT_KEYWORDS = [
    "마감",
    "제출",
    "업로드",
    "발표",
    "참관",
    "퀴즈",
    "출석",
    "시험",
    "고사",
    "과제",
    "공지",
    "시작",
    "종료",
    "자료",
]
# 마감 성격(데드라인) 키워드 — kind 판정.
_DEADLINE_KEYWORDS = {"마감", "제출", "업로드", "종료"}

# 날짜 패턴: ISO(YYYY-MM-DD / YYYY.MM.DD) 와 한국어(N월 N일).
_ISO_DATE_RE = re.compile(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})")
_KR_DATE_RE = re.compile(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일")
# 슬래시 짧은 날짜(M/D, 연도 없음). 앞뒤가 숫자/슬래시/구분자면 제외해
# ISO 슬래시 날짜(2026/06/13) 의 일부나 분수/시간(10:30)과 충돌하지 않게 한다.
_SLASH_DATE_RE = re.compile(r"(?<![\d/.\-])(\d{1,2})/(\d{1,2})(?![\d/])")
_NON_DATE_SLASH_AFTER_RE = re.compile(r"^\s*(?:이상|이하|초과|미만|점|명|개|%)")
# 시간 패턴.
# 영문 오전/오후(`10:30am` / `2:30pm`) — 한국어보다 먼저 검사한다.
_AMPM_EN_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*([AaPp])[Mm]")
_AMPM_TIME_RE = re.compile(r"(오전|오후)\s*(\d{1,2})\s*(?:시|:)\s*(\d{1,2})?\s*분?")
_COLON_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")
_KR_TIME_RE = re.compile(r"(\d{1,2})\s*시\s*(\d{1,2})?\s*분?")

# course 표시명 끝의 과목코드 접미사 ' (2150693001)' 제거용.
_COURSE_CODE_RE = re.compile(r"\s*\(\d+\)\s*$")

# 과생성 방지: 공지 하나에서 너무 많은 날짜가 나오면 syllabus/일정표 성격으로 보고
# 자동 생성하지 않고 사람이 검토한다.
_MAX_ANNOUNCEMENT_MENTIONS = 4


# ── HTML → plain text 정규화 ────────────────────────────────────────


class _TextExtractor(HTMLParser):
    """HTML 본문에서 표시 텍스트만 추출한다(script/style 내용 제외).

    block/br 경계는 공백으로 환원해 토큰 인접을 막는다. 엔티티는
    convert_charrefs(기본 True)가 자동 unescape 한다.
    """

    _BLOCK_TAGS: ClassVar[set[str]] = {
        "br",
        "p",
        "div",
        "li",
        "tr",
        "td",
        "th",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "ul",
        "ol",
        "table",
        "section",
        "article",
    }
    _SKIP_TAGS: ClassVar[set[str]] = {"script", "style", "head", "noscript"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        if tag in self._BLOCK_TAGS:
            self._parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        if tag in self._BLOCK_TAGS:
            self._parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def html_to_text(html: str | None) -> str:
    """공지 HTML 본문을 안전하게 plain text 로 정규화한다 (순수 함수).

    - script/style 내용 제거
    - 태그 제거 + 엔티티 unescape
    - 연속 공백/줄바꿈을 단일 공백으로 합침

    None/빈 입력은 빈 문자열을 반환한다.
    """
    if not html:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # 깨진 HTML 이라도 분석 파이프라인을 죽이지 않는다
        _log.warning("HTML 파싱 부분 실패 — 추출된 텍스트만 사용", exc_info=True)
    return " ".join(parser.text().split())


class _TableRowExtractor(HTMLParser):
    """HTML 표(`<table>`) 안의 각 행(`<tr>`) 텍스트를 행 단위로 수집한다.

    표는 명시적 일정표로 보고 행마다 한 일정으로 해석한다(과생성 가드 우회 근거).
    표 밖 텍스트는 무시한다.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[str] = []
        self._table_depth = 0
        self._in_row = False
        self._cur: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "table":
            self._table_depth += 1
        elif tag == "tr" and self._table_depth > 0:
            self._in_row = True
            self._cur = []
        elif tag in ("td", "th") and self._in_row:
            self._cur.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "table" and self._table_depth > 0:
            self._table_depth -= 1
        elif tag == "tr" and self._in_row:
            self._in_row = False
            self.rows.append(" ".join("".join(self._cur).split()))

    def handle_data(self, data: str) -> None:
        if self._in_row:
            self._cur.append(data)


def _table_row_mentions(html: str | None, now: datetime) -> list[dict]:
    """HTML 표가 있으면 행 단위로 일정 mention 을 추출한다(없으면 빈 리스트).

    명시적 표는 일정표로 간주하므로 행 수가 과생성 임계값을 넘어도 row별로 생성한다.
    """
    if not html or "<table" not in html.lower():
        return []
    parser = _TableRowExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # 깨진 HTML 이라도 파이프라인을 죽이지 않는다
        _log.warning("표 파싱 부분 실패 — 추출된 행만 사용", exc_info=True)
    mentions: list[dict] = []
    for row in parser.rows:
        mentions.extend(parse_event_mentions(row, now))
    return mentions


# ── PDF 첨부 텍스트 추출 ────────────────────────────────────────────


def extract_pdf_text(data: bytes) -> str:
    """PDF 바이트에서 텍스트를 추출한다.

    PyMuPDF(fitz) 가 설치돼 있으면 우선 사용하고, 없으면 단순 uncompressed PDF 의
    리터럴 문자열만 보수적으로 추출하는 stdlib fallback 을 쓴다(의존성 추가 없음).
    """
    if not data:
        return ""
    fitz = None
    try:
        import pymupdf as fitz  # PyMuPDF (신규 import), 선택 사항
    except Exception:
        try:
            import fitz  # PyMuPDF (기존 import), 선택 사항
        except Exception:
            fitz = None
    if fitz is not None:
        try:
            with fitz.open(stream=data, filetype="pdf") as doc:
                parts = [page.get_text() for page in doc]
            text = " ".join(part for part in parts if part)
            if text.strip():
                return " ".join(text.split())
        except Exception:  # 손상 PDF 라도 fallback 으로 최소 추출 시도
            _log.warning("PyMuPDF 추출 실패 — stdlib fallback 사용", exc_info=True)
    return _extract_pdf_text_fallback(data)


# PDF 리터럴 문자열 escape 매핑(`\n` 등).
_PDF_ESCAPES = {
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "b": "\b",
    "f": "\f",
    "(": "(",
    ")": ")",
    "\\": "\\",
}


def _read_pdf_literal(text: str, i: int) -> tuple[str, int]:
    """`(` 직후 위치 i 에서 PDF 리터럴 문자열을 읽어 (값, 다음 위치) 를 반환한다."""
    depth = 0
    buf: list[str] = []
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\":
            i += 1
            if i >= n:
                break
            esc = text[i]
            if esc in _PDF_ESCAPES:
                buf.append(_PDF_ESCAPES[esc])
                i += 1
            elif esc in "01234567":
                octal = esc
                i += 1
                for _ in range(2):
                    if i < n and text[i] in "01234567":
                        octal += text[i]
                        i += 1
                    else:
                        break
                buf.append(chr(int(octal, 8) & 0xFF))
            elif esc == "\r":
                i += 1
                if i < n and text[i] == "\n":  # CRLF 줄 연결
                    i += 1
            elif esc == "\n":  # 줄 연결(escape + newline)
                i += 1
            else:
                buf.append(esc)
                i += 1
        elif ch == "(":
            depth += 1
            buf.append(ch)
            i += 1
        elif ch == ")":
            if depth == 0:
                i += 1
                break
            depth -= 1
            buf.append(ch)
            i += 1
        else:
            buf.append(ch)
            i += 1
    return "".join(buf), i


def _extract_pdf_text_fallback(data: bytes) -> str:
    """외부 의존성 없이 단순 uncompressed PDF 의 리터럴 문자열만 추출한다 (보수적).

    PDF content stream 의 `( ... )` 리터럴(텍스트 표시용)만 모아 공백 정규화한다.
    압축(FlateDecode) 스트림은 다루지 않는다 — 정밀 추출은 PyMuPDF 경로가 담당.
    """
    text = data.decode("latin-1", errors="replace")
    strings: list[str] = []
    i, n = 0, len(text)
    # 리터럴 시작 `(` 탐색은 str.find(C 레벨)로 점프한다 — 10MB급 PDF 를
    # 문자 단위 파이썬 루프로 훑으면 무괄호 바이너리 구간에서 수 초를 태운다.
    while i < n:
        i = text.find("(", i)
        if i == -1:
            break
        value, i = _read_pdf_literal(text, i + 1)
        if value:
            strings.append(value)
    return " ".join(" ".join(strings).split())


# ── 한국어/ISO 날짜·시간 추출 ────────────────────────────────────────


def _resolve_year(month: int, day: int, now: datetime) -> int:
    """연도 없는 'N월 N일' 의 연도를 보정한다(연도 전환기 대응).

    오늘로부터 하루 이전보다 과거면 다음 해로 본다(deadline_checker 의 롤오버 취지).
    """
    candidate = datetime(now.year, month, day, tzinfo=now.tzinfo).date()
    if candidate < (now.date() - timedelta(days=1)):
        return now.year + 1
    return now.year


def _parse_time(window: str) -> str | None:
    """텍스트 조각에서 시각을 'HH:MM' 으로 추출한다(없으면 None)."""
    m = _AMPM_EN_TIME_RE.search(window)
    if m:
        hh, mm, ap = int(m.group(1)), int(m.group(2)), m.group(3).lower()
        if ap == "p" and hh < 12:
            hh += 12
        elif ap == "a" and hh == 12:
            hh = 0
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return f"{hh:02d}:{mm:02d}"
    m = _AMPM_TIME_RE.search(window)
    if m:
        ampm, hh, mm = m.group(1), int(m.group(2)), int(m.group(3) or 0)
        if ampm == "오후" and hh < 12:
            hh += 12
        elif ampm == "오전" and hh == 12:
            hh = 0
        return f"{hh:02d}:{mm:02d}"
    m = _COLON_TIME_RE.search(window)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return f"{hh:02d}:{mm:02d}"
    m = _KR_TIME_RE.search(window)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2) or 0)
        if 0 <= hh <= 23:
            return f"{hh:02d}:{mm:02d}"
    return None


def _keywords_in(window: str) -> list[str]:
    """텍스트 조각에서 일정 키워드를 등장 순서대로 추출한다(중복 제거)."""
    found: list[tuple[int, str]] = []
    for kw in _EVENT_KEYWORDS:
        idx = window.find(kw)
        if idx >= 0:
            found.append((idx, kw))
    found.sort(key=lambda x: x[0])
    out: list[str] = []
    for _, kw in found:
        if kw not in out:
            out.append(kw)
    return out


def _date_matches(text: str) -> list[tuple[int, str]]:
    """텍스트의 모든 날짜를 (위치, 'YYYY-MM-DD-or-월일마커') 로 수집한다.

    ISO 는 연도 확정이라 'YYYY-MM-DD' 로, 한국어는 연도 미정이라 'KR:M:D' 로
    표시한 뒤 호출부에서 연도 보정한다.
    """
    spans: list[tuple[int, str]] = []
    for m in _ISO_DATE_RE.finditer(text):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            spans.append((m.start(), f"{y:04d}-{mo:02d}-{d:02d}"))
    for m in _KR_DATE_RE.finditer(text):
        mo, d = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            spans.append((m.start(), f"KR:{mo}:{d}"))
    for m in _SLASH_DATE_RE.finditer(text):
        mo, d = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            after = text[m.end() : m.end() + 8]
            if _NON_DATE_SLASH_AFTER_RE.match(after):
                continue
            # 연도 미정 → 한국어 날짜와 동일하게 KR 마커로 두고 호출부에서 보정.
            spans.append((m.start(), f"KR:{mo}:{d}"))
    spans.sort(key=lambda x: x[0])
    return spans


def parse_event_mentions(text: str, now: datetime) -> list[dict]:
    """plain text 에서 일정 후보(날짜/시간/키워드)를 추출한다 (순수 함수).

    각 항목: {"date": "YYYY-MM-DD", "time": "HH:MM"|None,
              "kind": "deadline"|"event", "keywords": [...]}.
    날짜가 없으면 빈 리스트. 동일 (date, time, keywords) 는 1건으로 합친다.
    """
    if not text:
        return []
    mentions: list[dict] = []
    seen: set[tuple] = set()
    for pos, raw in _date_matches(text):
        if raw.startswith("KR:"):
            _, mo_s, d_s = raw.split(":")
            mo, d = int(mo_s), int(d_s)
            year = _resolve_year(mo, d, now)
            date_str = f"{year:04d}-{mo:02d}-{d:02d}"
        else:
            date_str = raw
        # 날짜 주변 윈도우에서 시간/키워드를 수집(앞뒤 문맥 포함).
        window = text[max(0, pos - 20) : pos + 60]
        time_str = _parse_time(window)
        keywords = _keywords_in(window)
        kind = "deadline" if any(k in _DEADLINE_KEYWORDS for k in keywords) else "event"
        key = (date_str, time_str, tuple(keywords))
        if key in seen:
            continue
        seen.add(key)
        mentions.append({"date": date_str, "time": time_str, "kind": kind, "keywords": keywords})
    return mentions


# ── fingerprint / 이벤트 빌드 ───────────────────────────────────────


def make_fingerprint(date: str, summary: str, url: str) -> str:
    """(날짜, summary, url) 로 안정적 fingerprint 를 만든다(중복 방지/마커용)."""
    digest = hashlib.sha1(f"{date}|{summary}|{url}".encode()).hexdigest()
    return digest[:12]


def _course_short(course: str) -> str:
    """과목 표시명에서 끝의 과목코드 ' (2150693001)' 를 제거한다."""
    return _COURSE_CODE_RE.sub("", course or "").strip()


def _build_event(
    *,
    date: str,
    time: str | None,
    summary: str,
    match_key: str,
    url: str,
    origin: str,
) -> dict:
    """Google Calendar event 본문 후보를 만든다(timed/all_day).

    description 에 LMS URL 과 source marker 를 남겨 재실행 시 중복 생성을 막는다.
    """
    fingerprint = make_fingerprint(date, summary, url)
    marker = f"{SOURCE_MARKER_PREFIX}{fingerprint}"
    description = f"LMS: {url}\n{marker}"
    y, mo, d = (int(x) for x in date.split("-"))

    if time:
        hh, mm = (int(x) for x in time.split(":"))
        start_dt = datetime(y, mo, d, hh, mm, 0, tzinfo=KST)
        end_dt = start_dt + timedelta(minutes=_TIMED_DURATION_MIN)
        start = {"dateTime": start_dt.isoformat(), "timeZone": "Asia/Seoul"}
        end = {"dateTime": end_dt.isoformat(), "timeZone": "Asia/Seoul"}
        when_type = "timed"
    else:
        next_day = (datetime(y, mo, d, tzinfo=KST) + timedelta(days=1)).date()
        start = {"date": date}
        end = {"date": next_day.isoformat()}
        when_type = "all_day"

    return {
        "summary": summary,
        "when_type": when_type,
        "start": start,
        "end": end,
        "description": description,
        "url": url,
        "fingerprint": fingerprint,
        "origin": origin,
        "match_key": match_key,
    }


# ── 항목별 분석 ────────────────────────────────────────────────────


def analyze_pending(item: dict, now: datetime) -> dict:
    """pending 항목(과제/퀴즈/토론/파일/동영상)을 분석한다 (순수 함수).

    - 날짜 없음 → skip(no_date)
    - 날짜 < 오늘 → skip(past)
    - 날짜 + 시간 → timed create / 날짜만 → all_day create
    """
    date = item.get("date")
    if not date:
        return {"action": "skip", "reason": "no_date", "title": item.get("title"), "url": item.get("url")}

    today = now.strftime("%Y-%m-%d")
    if date < today:
        return {"action": "skip", "reason": "past", "title": item.get("title"), "url": item.get("url")}

    title = item.get("title") or "(제목 없음)"
    short = _course_short(item.get("course") or "")
    time = item.get("time")
    summary = f"[{short}] {title} 마감" if time else f"[{short}] {title}"
    event = _build_event(
        date=date,
        time=time,
        summary=summary,
        match_key=title,
        url=item.get("url") or "",
        origin="pending",
    )
    return {"action": "create", "event": event}


def analyze_announcement(item: dict, now: datetime) -> dict:
    """공지 항목을 분석한다 — 본문/표/첨부 텍스트에서 일정 날짜를 추출한다 (순수 함수).

    - 본문(message HTML)의 산문에서 날짜를 추출하되, 명시적 `<table>` 일정표는 행
      단위로 파싱해 과생성 가드(too_many_dates)를 우회한다.
    - 첨부 PDF 추출 텍스트(`attachment_texts`)도 분석 대상에 포함한다.
    - 파싱 가능한 날짜 없음 → manual_review(no_parseable_date)
    - 산문 날짜가 과하게 많음 → manual_review(too_many_dates) (표/첨부는 가드 제외)
    - 날짜(+시간) 확실 → create(timed/all_day) 후보. 본문/표/첨부 중복은 합친다.
    """
    message = item.get("message")
    title = item.get("title") or "(제목 없음)"

    table_mentions = _table_row_mentions(message, now)
    # 표가 있으면 행 단위 구조화 일정으로 보고 산문 평탄화 파싱은 생략(중복 방지).
    prose_mentions: list[dict] = [] if table_mentions else parse_event_mentions(html_to_text(message), now)

    attach_mentions: list[dict] = []
    for at in item.get("attachment_texts") or []:
        if isinstance(at, str) and at.strip():
            attach_mentions.extend(parse_event_mentions(at, now))

    mentions = list(table_mentions) + list(prose_mentions) + attach_mentions
    if not mentions:
        return {
            "action": "manual_review",
            "reason": "no_parseable_date",
            "course": item.get("course"),
            "title": title,
            "url": item.get("url"),
        }
    # 과생성 가드는 비구조화 산문에만 적용한다(명시적 표/첨부는 제외).
    if not table_mentions and len(prose_mentions) > _MAX_ANNOUNCEMENT_MENTIONS:
        return {
            "action": "manual_review",
            "reason": "too_many_dates",
            "course": item.get("course"),
            "title": title,
            "url": item.get("url"),
            "dates": [m["date"] for m in prose_mentions],
        }

    short = _course_short(item.get("course") or "")
    url = item.get("url") or ""
    events: list[dict] = []
    seen: set[tuple] = set()
    for m in mentions:
        label = " ".join(m["keywords"]) if m["keywords"] else title
        summary = f"[{short}] {label}"
        # 본문/표/첨부에서 같은 (날짜, 시간, summary) 가 반복되면 한 이벤트로 합친다.
        key = (m["date"], m["time"], summary)
        if key in seen:
            continue
        seen.add(key)
        events.append(
            _build_event(
                date=m["date"],
                time=m["time"],
                summary=summary,
                match_key=label,
                url=url,
                origin="announcement",
            )
        )
    return {"action": "create", "events": events}


# ── 중복 판정 ──────────────────────────────────────────────────────


def _tokens(text: str) -> set[str]:
    """summary/label 을 길이 2 이상의 토큰 집합으로 분해한다(브래킷/구두점 제거)."""
    parts = re.split(r"[\s\[\]()·,/.\-:]+", (text or "").lower())
    return {p for p in parts if len(p) >= 2}


def _is_submission_like(text: str) -> bool:
    """자료 제출/업로드 마감처럼 발표 자체와 구분해야 하는 후보인지."""
    return any(kw in (text or "") for kw in ("업로드", "제출", "자료", "마감"))


def _is_presentation_like(text: str) -> bool:
    """발표 일정의 짧은 개인 캘린더 표현(`기말발`)까지 포함해 판정한다."""
    normalized = (text or "").replace(" ", "")
    return "발표" in normalized or "기말발" in normalized


def _summary_similar(candidate_key: str, existing_summary: str) -> bool:
    """후보 match_key 와 기존 일정 summary 가 '같은 일정'으로 볼 만큼 유사한지.

    - 공통 토큰 2개 이상 → 유사
    - 공통 토큰 1개 이상 + 한쪽이 단일 토큰 → 유사(짧은 개인 일정 제목 대응)
    - `기말발` 같은 축약 개인 일정은 발표 후보와 유사로 본다.
    - 단, `발표 자료 업로드/제출/마감`은 발표 자체 일정과 분리한다.
    보수적으로 동작(과탐 시 생성 보류로 안전 측 — 후보는 skipped 로 남아 사람이 검토).
    """
    candidate_submission = _is_submission_like(candidate_key)
    existing_submission = _is_submission_like(existing_summary)
    if candidate_submission != existing_submission and (
        _is_presentation_like(candidate_key) or _is_presentation_like(existing_summary)
    ):
        return False

    if _is_presentation_like(candidate_key) and _is_presentation_like(existing_summary):
        return True

    a, b = _tokens(candidate_key), _tokens(existing_summary)
    if not a or not b:
        return False
    shared = a & b
    if len(shared) >= 2:
        return True
    return bool(shared) and min(len(a), len(b)) <= 1


def _event_date(event: dict) -> str:
    """이벤트 시작 날짜('YYYY-MM-DD')를 반환한다(timed/all_day 공통)."""
    start = event["start"]
    return start["date"] if "date" in start else start["dateTime"][:10]


def _dedup_reason(event: dict, existing_events: list[dict]) -> str | None:
    """기존 일정과 중복이면 사유를, 아니면 None 을 반환한다.

    - 우리가 만든 이벤트(같은 source marker) → duplicate_marker
    - 같은 날짜 + 유사 summary → duplicate_existing
    """
    marker = f"{SOURCE_MARKER_PREFIX}{event['fingerprint']}"
    for e in existing_events:
        if marker in (e.get("description") or ""):
            return "duplicate_marker"
    date = _event_date(event)
    for e in existing_events:
        if (e.get("date") or "") == date and _summary_similar(event["match_key"], e.get("summary") or ""):
            return "duplicate_existing"
    return None


# ── 후보 빌드(통합) ─────────────────────────────────────────────────


def build_candidates(
    items: list[dict],
    now: datetime,
    existing_events: list[dict] | None = None,
    calendar_name: str = DEFAULT_CALENDAR,
) -> dict:
    """export items 를 분석해 create/manual_review/skipped 후보로 분류한다 (순수 함수).

    절대 write 하지 않는다(applied=False). 실제 생성은 호출부(--apply)의 책임.
    """
    existing = list(existing_events or [])
    create: list[dict] = []
    manual_review: list[dict] = []
    skipped: list[dict] = []

    for item in items:
        if item.get("type") == "announcement":
            res = analyze_announcement(item, now)
            if res["action"] == "manual_review":
                manual_review.append(
                    {
                        "course": res.get("course"),
                        "title": res.get("title"),
                        "url": res.get("url"),
                        "reason": res["reason"],
                    }
                )
                continue
            events = res["events"]
        else:
            res = analyze_pending(item, now)
            if res["action"] == "skip":
                skipped.append({"summary": res.get("title"), "url": res.get("url"), "reason": res["reason"]})
                continue
            events = [res["event"]]

        for event in events:
            reason = _dedup_reason(event, existing)
            if reason:
                skipped.append(
                    {
                        "summary": event["summary"],
                        "url": event["url"],
                        "date": _event_date(event),
                        "reason": reason,
                    }
                )
            else:
                create.append(event)
                existing.append(
                    {
                        "date": _event_date(event),
                        "summary": event.get("match_key") or event.get("summary") or "",
                        "description": event.get("description") or "",
                    }
                )

    return {
        "calendar": calendar_name,
        "applied": False,
        "create": create,
        "manual_review": manual_review,
        "skipped": skipped,
        "stats": {
            "create": len(create),
            "manual_review": len(manual_review),
            "skipped": len(skipped),
        },
    }


# ── 입력 로딩 ──────────────────────────────────────────────────────


def _load_items(input_path: str | None) -> list[dict]:
    """export 결과(JSON)를 파일 또는 stdin 에서 읽어 items 리스트를 반환한다.

    payload 가 {"items": [...]} 형태이거나 바로 [...] 인 경우 모두 지원한다.
    """
    if input_path:
        with open(input_path, encoding="utf-8") as f:
            raw = f.read()
    else:
        stdin_buffer = getattr(sys.stdin, "buffer", None)
        if stdin_buffer is not None:
            data = stdin_buffer.read()
            raw = data.decode("utf-8") if isinstance(data, bytes) else str(data)
        else:
            raw = sys.stdin.read()
    obj = json.loads(raw) if raw.strip() else {}
    if isinstance(obj, list):
        items = obj
    elif isinstance(obj, dict):
        items = obj.get("items") or []
    else:
        items = []
    return [it for it in items if isinstance(it, dict)]


def _load_existing_events(path: str | None) -> list[dict]:
    """기존 일정(중복 판정용)을 JSON 에서 읽는다. 각 항목: {date, summary, description?}."""
    if not path:
        return []
    with open(path, encoding="utf-8") as f:
        obj = json.load(f)
    events = obj if isinstance(obj, list) else (obj.get("events") or obj.get("items") or [])
    return [e for e in events if isinstance(e, dict)]


def _emit(payload: dict, *, pretty: bool) -> None:
    """payload 를 UTF-8 JSON 으로 stdout 에 출력한다(한국어 보존)."""
    print(json.dumps(payload, ensure_ascii=False, indent=2 if pretty else None))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LMS export 결과를 Google Calendar 후보로 캘린더화 (기본 dry-run)")
    parser.add_argument("--input", help="export JSON 파일 경로(미지정 시 stdin)")
    parser.add_argument("--existing-events", help="중복 판정용 기존 일정 JSON 경로(date/summary/description)")
    parser.add_argument("--calendar-name", default=DEFAULT_CALENDAR, help="대상 캘린더 이름")
    parser.add_argument("--apply", action="store_true", help="실제 Google Calendar 생성(기본 dry-run)")
    parser.add_argument("--token", help="Google OAuth 토큰 JSON 경로(--apply 시 필요)")
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

    try:
        items = _load_items(args.input)
        existing = _load_existing_events(args.existing_events)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        _log.error("입력 로딩 실패: %s", e, exc_info=True)
        _emit(
            {
                "ok": False,
                "generated_at": generated_at,
                "calendar": args.calendar_name,
                "applied": False,
                "error": f"입력 로딩 실패: {type(e).__name__}",
            },
            pretty=args.pretty,
        )
        return 2

    now = datetime.now(KST)
    result = build_candidates(items, now, existing_events=existing, calendar_name=args.calendar_name)

    if args.apply:
        # 실제 write 경로 — 부수효과. Hermes 부모만 호출(본 작업에서는 미실행).
        try:
            applied = _apply_to_google(result["create"], args.calendar_name, args.token)
            result["applied"] = True
            result["applied_count"] = applied
        except Exception as e:  # write 실패가 dry-run 후보 출력을 막지 않게
            _log.error("Google Calendar 생성 실패: %s", e, exc_info=True)
            result["applied"] = False
            result["apply_error"] = type(e).__name__

    payload = {"ok": True, "generated_at": generated_at, **result}
    _emit(payload, pretty=args.pretty)
    return 0


# ── Google Calendar write (--apply 전용, 부수효과) ──────────────────
# 본 경로는 stored OAuth token + requests REST 호출만 사용한다(SDK 의존성 추가 없음).
# 토큰/secret 값은 로그/stdout/이벤트 본문 어디에도 출력하지 않는다.


def _apply_to_google(events: list[dict], calendar_name: str, token_path: str | None) -> int:
    """create 후보를 실제 Google Calendar 에 생성한다. 생성 개수를 반환한다.

    중복 방지를 위해 대상 캘린더의 해당 날짜 이벤트를 먼저 조회해 source marker /
    summary 로 재확인한 뒤 insert 한다. (테스트 미적용 경로 — Hermes 부모가 호출)
    """
    if not token_path:
        raise ValueError("missing_token_path")
    import requests  # lazy — dry-run 경로는 requests 불필요

    access_token = _google_access_token(token_path, requests)
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {access_token}"})

    calendar_id = _find_calendar_id(session, calendar_name)
    if not calendar_id:
        raise ValueError("calendar_not_found")

    created = 0
    # 같은 날짜 이벤트를 후보마다 재조회(N+1)하지 않도록 날짜별 1회만 조회하고,
    # 생성분은 캐시에 반영해 같은 실행 안의 중복 판정을 결정적으로 만든다.
    day_events_cache: dict[str, list[dict]] = {}
    for event in events:
        date = _event_date(event)
        existing = day_events_cache.get(date)
        if existing is None:
            existing = _list_day_events(session, calendar_id, date)
            day_events_cache[date] = existing
        if _dedup_reason(event, existing):
            _log.info("이미 존재(중복) — 생성 생략: %s", event.get("summary"))
            continue
        body = {
            "summary": event["summary"],
            "description": event["description"],
            "start": event["start"],
            "end": event["end"],
        }
        resp = session.post(
            f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events",
            json=body,
            timeout=30,
        )
        resp.raise_for_status()
        existing.append(
            {
                "date": date,
                "summary": event.get("match_key") or event.get("summary") or "",
                "description": event.get("description") or "",
            }
        )
        created += 1
    return created


def _google_access_token(token_path: str, requests) -> str:
    """stored OAuth 토큰을 읽어(필요 시 refresh) access token 을 반환한다.

    토큰 파일은 google.oauth2 Credentials JSON 형식(refresh_token/client_id/
    client_secret/token_uri/token) 을 가정한다. 값은 절대 로그로 남기지 않는다.
    """
    with open(token_path, encoding="utf-8") as f:
        data = json.load(f)
    refresh_token = data.get("refresh_token")
    client_id = data.get("client_id")
    client_secret = data.get("client_secret")
    token_uri = data.get("token_uri") or "https://oauth2.googleapis.com/token"
    if not (refresh_token and client_id and client_secret):
        raise ValueError("incomplete_token")
    resp = requests.post(
        token_uri,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    resp.raise_for_status()
    token = resp.json().get("access_token")
    if not token:
        raise ValueError("token_refresh_failed")
    return token


def _find_calendar_id(session, calendar_name: str) -> str | None:
    """calendarList 에서 이름이 일치하는 캘린더 id 를 찾는다."""
    resp = session.get("https://www.googleapis.com/calendar/v3/users/me/calendarList", timeout=30)
    resp.raise_for_status()
    for cal in resp.json().get("items", []):
        if cal.get("summary") == calendar_name:
            return cal.get("id")
    return None


def _list_day_events(session, calendar_id: str, date: str) -> list[dict]:
    """대상 캘린더의 특정 날짜 이벤트를 {date, summary, description} 로 조회한다."""
    time_min = f"{date}T00:00:00+09:00"
    time_max = f"{date}T23:59:59+09:00"
    resp = session.get(
        f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events",
        params={"timeMin": time_min, "timeMax": time_max, "singleEvents": "true"},
        timeout=30,
    )
    resp.raise_for_status()
    out: list[dict] = []
    for ev in resp.json().get("items", []):
        start = ev.get("start", {})
        ev_date = start.get("date") or (start.get("dateTime") or "")[:10]
        out.append(
            {
                "date": ev_date,
                "summary": ev.get("summary") or "",
                "description": ev.get("description") or "",
            }
        )
    return out


if __name__ == "__main__":
    sys.exit(main())
