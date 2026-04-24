"""
영상 다운로드.

Playwright로 LMS 강의 페이지에서 video URL을 추출한 뒤,
requests로 청크 스트리밍 다운로드한다.
"""

import asyncio
import contextlib
import re
from collections.abc import Callable
from http.client import IncompleteRead
from pathlib import Path
from urllib.parse import urlparse

import requests
from playwright.async_api import Page

from src.config import RetryPolicy
from src.downloader.result import (
    REASON_UNSUPPORTED,
    REASON_URL_EXTRACT_CONTENT_PHP_MISSING,
    REASON_URL_EXTRACT_CONTENT_PHP_PARSE,
    REASON_URL_EXTRACT_EXCEPTION,
    REASON_URL_EXTRACT_HLS_ONLY,
    REASON_URL_EXTRACT_NO_PLAYER,
    REASON_URL_EXTRACT_TIMEOUT,
    ExtractionResult,
    SSRFBlockedError,
    SuspiciousStubError,
)
from src.logger import get_logger
from src.player.background_player import click_play, dismiss_dialog, find_player_frame

_dl_log = get_logger("downloader")

_MAX_RETRIES = RetryPolicy.STREAM
_TIMEOUT = (10, 60)  # (connect, read) seconds
_CHUNK_SIZE = 65536  # 64 KB

# B2: sanity check — 실제 LMS 강의 mp4는 수십 MB 이상. 이보다 작으면 가짜/stub 의심.
# 2 MB 기준은 "가장 짧은 공지/인트로 강의도 통상 이 크기를 넘는다"는 경험치.
_MIN_PLAUSIBLE_VIDEO_BYTES = 2 * 1024 * 1024
_PAGE_GOTO_TIMEOUT = 60_000  # ms — Playwright page.goto timeout
_CONTENT_PHP_POLL_MAX = 20  # content.php 파싱 대기 폴링 횟수 (x0.5s = 10s)
_VIDEO_POLL_MAX = 120  # video DOM 폴링 횟수 (x0.5s = 60s)
_DIALOG_SETTLE_SEC = 1  # 다이얼로그 렌더링 대기 (초)
_POLL_INTERVAL_SEC = 0.5  # 폴링 간격 (초)

# 다운로드 허용 도메인 (SSRF 방어)
_ALLOWED_SCHEMES = {"https", "http"}
_DEFAULT_ALLOWED_HOSTS_SUFFIX = (".ssu.ac.kr", ".commonscdn.com", ".commonscdn.net")

# DOWNLOAD_EXTRA_HOSTS에서 명시적으로 차단하는 공인 suffix — 운영자 실수로 TLD/eTLD를
# 입력해 전 인터넷이 허용되지 않도록 한다. 필요 시 docs/project-patterns.md에 추가.
# SEC-103: IDN TLD(`.xn--*`)는 PSL 검증이 없으므로 아예 패턴으로 차단한다.
_EXTRA_HOSTS_BLOCKLIST = frozenset(
    {
        ".com", ".net", ".org", ".io", ".co", ".kr", ".ac.kr", ".co.kr", ".or.kr", ".go.kr",
        ".jp", ".co.jp", ".cn", ".com.cn", ".uk", ".co.uk", ".de", ".fr", ".us",
    }
)

# 캐시 — 프로세스 생존 중 동일한 env 값에 대해 경고를 한 번만 출력한다.
_extra_hosts_cache: tuple[str, str, tuple[str, ...]] | None = None


def _parse_extra_hosts(extra_raw: str) -> tuple[str, ...]:
    """DOWNLOAD_EXTRA_HOSTS 문자열을 검증된 suffix 튜플로 파싱한다.

    거부 규칙:
    - 빈 라벨, 와일드카드(`*`), IP 패턴 거부
    - 최소 2개 라벨 강제 (`a.b` 최소, `com` 같은 단일 라벨 차단)
    - 공인 TLD/eTLD 블록리스트 차단
    - 부적합 입력은 경고 로그와 함께 스킵 — 프로세스는 계속 진행
    """
    if not extra_raw.strip():
        return ()

    extras: list[str] = []
    for item in extra_raw.split(","):
        host = item.strip().lower()
        if not host:
            continue
        if "*" in host or any(c.isspace() for c in host):
            _dl_log.warning("DOWNLOAD_EXTRA_HOSTS: 잘못된 항목 스킵 (와일드카드/공백): %r", host)
            continue
        # IP 형태 거부 (숫자.숫자 패턴)
        label_tokens = host.lstrip(".").split(".")
        if label_tokens and all(tok.isdigit() for tok in label_tokens if tok):
            _dl_log.warning("DOWNLOAD_EXTRA_HOSTS: IP 형식 거부: %r", host)
            continue
        if not host.startswith("."):
            host = "." + host
        # 빈 라벨 검출 (".." 또는 ".foo..bar")
        if any(not tok for tok in host[1:].split(".")):
            _dl_log.warning("DOWNLOAD_EXTRA_HOSTS: 빈 라벨 포함 항목 거부: %r", host)
            continue
        # 최소 2 라벨 강제 (e.g., ".foo.bar" OK, ".com" 거부)
        label_count = host[1:].count(".") + 1
        if label_count < 2:
            _dl_log.warning("DOWNLOAD_EXTRA_HOSTS: 단일 라벨 거부(최소 2 라벨 필요): %r", host)
            continue
        # 공인 TLD/eTLD 블록리스트
        if host in _EXTRA_HOSTS_BLOCKLIST:
            _dl_log.warning("DOWNLOAD_EXTRA_HOSTS: 공인 TLD/eTLD 거부: %r", host)
            continue
        # SEC-103: IDN(xn-- 접두사) 라벨 포함 차단 — PSL 검증 없이 ccTLD 전부 허용되는
        # 위험 방지. 필요 시 명시적 allow-list를 별도로 관리.
        if any(tok.startswith("xn--") for tok in host[1:].split(".")):
            _dl_log.warning("DOWNLOAD_EXTRA_HOSTS: IDN(xn--) 라벨 거부: %r", host)
            continue
        extras.append(host)
    return tuple(extras)


def _allowed_hosts_suffix() -> tuple[str, ...]:
    """기본 허용 목록 + DOWNLOAD_EXTRA_HOSTS env 오버라이드를 합친 튜플.

    env 값 예시: ".cdn.example.com,.media.foo.net" (쉼표 구분, 리딩 dot 권장).
    알려지지 않은 새 CDN이 등장했을 때 재배포 없이 대응하기 위한 비상 출구.

    SEC-001 방어: `_parse_extra_hosts`에서 TLD/eTLD/단일 라벨/빈 라벨/와일드카드/IP를 거부한다.
    동일 env 값에 대해 최초 호출 시 최종 적용 suffix를 INFO 로그로 남겨 운영자 가시화.
    """
    global _extra_hosts_cache

    import os

    extra_raw = os.getenv("DOWNLOAD_EXTRA_HOSTS", "")
    cache_key = extra_raw
    if _extra_hosts_cache is not None and _extra_hosts_cache[0] == cache_key:
        return _extra_hosts_cache[2]

    parsed = _parse_extra_hosts(extra_raw)
    final = _DEFAULT_ALLOWED_HOSTS_SUFFIX + parsed
    _extra_hosts_cache = (cache_key, extra_raw, final)
    if parsed:
        _dl_log.info("DOWNLOAD_EXTRA_HOSTS 적용: %s (최종 허용=%s)", parsed, final)
    return final


def _validate_media_url(url: str) -> None:
    """다운로드 URL의 프로토콜과 호스트를 검증한다. 허용 외 URL이면 SSRFBlockedError."""
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise SSRFBlockedError(f"허용되지 않는 프로토콜: {parsed.scheme}")
    hostname = parsed.hostname or ""
    allowed = _allowed_hosts_suffix()
    if not any(hostname.endswith(suffix) for suffix in allowed):
        raise SSRFBlockedError(f"허용되지 않는 호스트: {hostname}")


def _sanitize_filename(name: str) -> str:
    """파일명에 사용 불가한 문자를 제거한다."""
    sanitized = re.sub(r'[<>:"/\\|?*]', "", name)
    sanitized = re.sub(r"\.{2,}", "", sanitized)  # 상위 디렉토리 순회 방지
    sanitized = sanitized.strip(" .")
    sanitized = re.sub(r"\s+", " ", sanitized)
    return sanitized or "lecture"


async def extract_video_url_detailed(page: Page, lecture_url: str) -> ExtractionResult:
    """세분화된 실패 사유 + 진단 컨텍스트를 반환하는 URL 추출.

    Plan A: video 태그 src 폴링 (일반 타입)
    Plan B: Network 요청 가로채기 — mp4 URL이 포함된 요청 캡처 (readystream 등)
    Plan C: content.php 응답 XML 을 비동기 파싱해 main_media 필드 추출

    Returns:
        ExtractionResult — 성공 시 url 채워짐·reason=None.
        실패 시 reason 에 REASON_URL_EXTRACT_* 중 하나, diagnostics 에 관측된
        상태(content_php_seen, content_php_parse_error, hls_observed, 등)
    """

    captured: dict[str, str | None] = {"url": None}
    _bg_task: asyncio.Task | None = None
    _content_parsed = False
    _content_php_seen = False            # content.php 응답이 한 번이라도 도착했는가
    _content_php_parse_error: str | None = None  # Plan C 파싱 중 발생한 예외 메시지
    _observed_hls = False                # m3u8/HLS URL 감지 시 실패 원인 분류에 사용
    _observed_mp4_count = 0              # 관측된 mp4 URL 총 개수 (stub 포함)
    _first_mp4_url: str | None = None    # 관측된 첫 mp4 URL (진단용, stub 판정에 도움)

    # 플레이어 초기화 단계에서 <video> 태그에 임시로 부착되는 stub 파일들.
    # 실제 강의가 아니므로 Plan A(DOM) / Plan B(network) 모두에서 제외한다.
    # BUG-FIX: intro.mp4 누락으로 Plan A가 stub을 진짜 URL로 오인하여
    # 재시도 불가 처리로 강의가 영구 누락되던 문제 수정.
    exclude_patterns = ("preloader.mp4", "preview.mp4", "thumbnail.mp4", "intro.mp4")

    def _is_valid_mp4(url: str) -> bool:
        return ".mp4" in url and not any(p in url for p in exclude_patterns)

    def _note_observation(url: str) -> None:
        """관측된 URL 의 진단 메타데이터를 기록 (HLS·mp4 count 등)."""
        nonlocal _observed_hls, _observed_mp4_count, _first_mp4_url
        if not _observed_hls and (".m3u8" in url or "/hls/" in url):
            _observed_hls = True
        if ".mp4" in url:
            _observed_mp4_count += 1
            if _first_mp4_url is None:
                _first_mp4_url = url

    # 하위 호환용 alias (기존 이름 유지)
    _note_hls = _note_observation

    def _on_request(request):
        url = request.url
        _note_hls(url)
        if _is_valid_mp4(url) and captured["url"] is None:
            captured["url"] = url

    def _on_response(response):
        nonlocal _bg_task, _content_parsed, _content_php_seen
        url = response.url
        _note_observation(url)
        if _is_valid_mp4(url) and captured["url"] is None:
            captured["url"] = url
        # content.php 응답에서 미디어 URL 추출 (최초 1회만 파싱)
        if not _content_parsed and "content.php" in url and "commons.ssu.ac.kr" in url:
            _content_parsed = True
            _content_php_seen = True

            async def _parse_content_php():
                nonlocal _content_php_parse_error
                try:
                    from defusedxml.ElementTree import fromstring as _safe_fromstring

                    body = await response.text()
                    root = _safe_fromstring(body)
                    del body  # XML body 즉시 해제
                    media_uri = None

                    # 구조 A: content_playing_info > main_media > desktop/html5/media_uri
                    for path in (
                        "content_playing_info/main_media/desktop/html5/media_uri",
                        "content_playing_info/main_media/mobile/html5/media_uri",
                        ".//main_media//html5/media_uri",
                    ):
                        el = root.find(path)
                        if el is not None and el.text and el.text.strip():
                            candidate = el.text.strip()
                            if "[" not in candidate:
                                media_uri = candidate
                                break

                    # 구조 B: service_root > media > media_uri[@method="progressive"]
                    # [MEDIA_FILE] 플레이스홀더를 story_list의 실제 파일명으로 치환
                    if not media_uri:
                        media_uri_el = root.find("service_root/media/media_uri[@method='progressive']")
                        if media_uri_el is not None and media_uri_el.text:
                            url_template = media_uri_el.text.strip()
                            if "[MEDIA_FILE]" in url_template:
                                main_media_el = root.find(".//story_list/story/main_media_list/main_media")
                                if main_media_el is not None and main_media_el.text:
                                    media_uri = url_template.replace("[MEDIA_FILE]", main_media_el.text.strip())
                            elif "[" not in url_template:
                                media_uri = url_template
                    del root  # XML 트리 즉시 해제

                    if media_uri and captured["url"] is None:
                        captured["url"] = media_uri
                    elif not media_uri:
                        _content_php_parse_error = "파싱 성공 but main_media 필드 부재"
                        _dl_log.info(
                            "content.php 파싱됨 but main_media/media_uri 필드 없음 — url=%s", url,
                        )
                except Exception as e:
                    # DEBUG → INFO 승격: 이 정보가 파일 로그에 남아야 진단 가능
                    _content_php_parse_error = f"{type(e).__name__}: {e}"
                    _dl_log.info("content.php 파싱 오류: %s: %s", type(e).__name__, e)

            _bg_task = asyncio.create_task(_parse_content_php())
            _bg_task.add_done_callback(lambda t: t.exception() if not t.cancelled() and t.exception() else None)

    def _diagnostics() -> dict:
        """현재까지 관측한 진단 컨텍스트를 snapshot 한다."""
        return {
            "content_php_seen": _content_php_seen,
            "content_php_parse_error": _content_php_parse_error,
            "hls_observed": _observed_hls,
            "mp4_urls_observed": _observed_mp4_count,
            "first_mp4_url_head": (_first_mp4_url or "")[:120] if _first_mp4_url else None,
        }

    page.on("request", _on_request)
    page.on("response", _on_response)

    try:
        try:
            await page.goto(lecture_url, wait_until="domcontentloaded", timeout=_PAGE_GOTO_TIMEOUT)
        except Exception as e:
            # goto 자체 실패 — 네트워크/페이지 로드 타임아웃. 진단 컨텍스트와 함께 보고.
            _dl_log.warning("page.goto 실패: %s: %s", type(e).__name__, e)
            return ExtractionResult(
                url=None,
                reason=REASON_URL_EXTRACT_EXCEPTION,
                diagnostics={**_diagnostics(), "exception": f"{type(e).__name__}: {e}"},
            )

        # iframe + content.php 로드 대기 (비동기 파싱 완료까지)
        for _ in range(_CONTENT_PHP_POLL_MAX):
            await asyncio.sleep(_POLL_INTERVAL_SEC)
            if captured["url"]:
                break

        # content.php에서 미디어 URL이 추출됐으면 바로 반환
        if captured["url"]:
            _dl_log.info("URL 추출 성공 (Plan B/C): %s", captured["url"][:120])
            return ExtractionResult(url=captured["url"], diagnostics=_diagnostics())

        player_frame = await find_player_frame(page)
        if not player_frame:
            # learningx LTI 전용 플레이어 감지 — commons iframe 이 구조적으로 없는
            # 강의는 mp4 다운로드 불가(learningx 내부 스트림만 재생). 일반 재시도
            # 대상이 아니라 UNSUPPORTED 로 분류해 자동 모드가 영원히 루프 돌지
            # 않도록 한다. (lecture_type 가 MOVIE 라도 런타임에 감지되는 경우)
            all_frame_urls = [f.url for f in page.frames]
            has_learningx_lti = any(
                "learningx/lti/lecture_attendance" in url for url in all_frame_urls
            )
            has_commons = any("commons.ssu.ac.kr" in url for url in all_frame_urls)
            diag = {
                "frames": [u[:80] for u in all_frame_urls[:5]],
                "learningx_lti_only": has_learningx_lti and not has_commons,
                **_diagnostics(),
            }

            if has_learningx_lti and not has_commons:
                _dl_log.warning(
                    "다운로드 구조적 불가 — learningx LTI 전용 플레이어 (commons iframe 부재). "
                    "url=%s diag=%s", lecture_url, diag,
                )
                return ExtractionResult(
                    url=None,
                    reason=REASON_UNSUPPORTED,
                    diagnostics=diag,
                )

            _dl_log.warning("player frame 미탐지 — %s", diag)
            return ExtractionResult(
                url=None,
                reason=REASON_URL_EXTRACT_NO_PLAYER,
                diagnostics=diag,
            )

        # print(f"  [DBG] player frame 발견: {player_frame.url[:80]}")

        # 이어보기 다이얼로그 처리 후 재생 버튼 클릭
        await asyncio.sleep(_DIALOG_SETTLE_SEC)
        await dismiss_dialog(player_frame, restart=True)
        await click_play(player_frame)
        await asyncio.sleep(_DIALOG_SETTLE_SEC)
        await dismiss_dialog(player_frame, restart=True)

        # 최대 60초 폴링: Plan A(video DOM) + Plan B(network 캡처) 동시 확인
        # 재생 후 새로운 frame이 생성될 수 있으므로 page.frames 전체를 매번 재스캔
        # 이어보기 다이얼로그도 매 폴링마다 체크 (재생 도중 뒤늦게 뜨는 경우 대응)
        dialog_dismissed = False
        for _i in range(_VIDEO_POLL_MAX):
            # Plan B 먼저 확인 (network에서 이미 캡처됐을 수 있음)
            if captured["url"]:
                _dl_log.info("URL 추출 성공 (Plan A/B 폴링 중): %s", captured["url"][:120])
                return ExtractionResult(url=captured["url"], diagnostics=_diagnostics())

            # 이어보기 다이얼로그가 재생 도중 뒤늦게 뜨는 경우 처리
            if not dialog_dismissed:
                dialog_dismissed = await dismiss_dialog(player_frame, restart=True)

            # Plan A: 모든 commons frame에서 video 태그 src 확인 (재생 후 새 frame 포함)
            commons_frames = [f for f in page.frames if "commons.ssu.ac.kr" in f.url]
            # if i % 10 == 0:
            #     print(f"  [DBG] 폴링({i}): commons frame 수={len(commons_frames)}")
            #     for fi, f in enumerate(commons_frames):
            #         print(f"  [DBG]   commons[{fi}]: {f.url[:80]}")

            for frame in commons_frames:
                try:
                    # get_attribute 방식으로 직접 조회 (evaluate보다 안정적)
                    video_el = await frame.query_selector("video.vc-vplay-video1")
                    if video_el:
                        src = await video_el.get_attribute("src")
                        # BUG-FIX: stub 패턴 사전 차단 — 재생 초기 <video src="…preloader.mp4">
                        # 상태에서 Plan A가 반환하던 문제 해결.
                        if src and src.startswith("http") and _is_valid_mp4(src):
                            _dl_log.info("URL 추출 성공 (Plan A vc-vplay): %s", src[:120])
                            return ExtractionResult(url=src, diagnostics=_diagnostics())

                    # fallback: 모든 video 태그 확인
                    result = await frame.evaluate("""() => {
                        const videos = document.querySelectorAll('video');
                        for (const v of videos) {
                            const src = v.src || v.currentSrc || '';
                            if (src && src.startsWith('http') && src.includes('.mp4')) return src;
                        }
                        return null;
                    }""")
                    if result and _is_valid_mp4(result):
                        _dl_log.info("URL 추출 성공 (Plan A fallback): %s", result[:120])
                        return ExtractionResult(url=result, diagnostics=_diagnostics())
                except Exception:
                    pass  # if i % 10 == 0: print(f"  [DBG]   video 평가 오류: {e}")

            await asyncio.sleep(_POLL_INTERVAL_SEC)

        # 폴링 종료 (60초) — 아래 디버그 코드는 URL 추출 실패 시 원인 분석용
        # print("  [DBG] 60초 폴링 종료. player 설정 파일 분석...")

        # async def _fetch_text(url: str) -> str:
        #     try:
        #         resp = await page.request.get(url)
        #         if resp.status != 200:
        #             return ""
        #         raw = await resp.body()
        #         for enc in ("utf-8", "euc-kr", "cp949", "latin-1"):
        #             try:
        #                 return raw.decode(enc)
        #             except Exception:
        #                 continue
        #         return raw.decode("latin-1")
        #     except Exception as e:
        #         print(f"  [DBG] fetch 오류 {url}: {e}")
        #         return ""

        # uni-player.min.js — m3u8/HLS URL 조합 로직 분석
        # import re as _re
        # player_js_url = next((u for u in all_requests if "uni-player.min.js" in u), None)
        # if player_js_url:
        #     print(f"  [DBG] uni-player.min.js fetch 중...")
        #     text = await _fetch_text(player_js_url)
        #     print(f"  [DBG] uni-player.min.js 크기: {len(text)} bytes")
        #     matches = _re.findall(r'.{0,150}(?:m3u8|\.m3u|hls(?:Url|Path|Src)|readystream|stream_url|streamUrl|videoSrc|mediaSrc|contentUri|content_uri|upf|ssmovie).{0,150}', text)
        #     print(f"  [DBG] uni-player.min.js 관련 키워드 ({len(matches)}개):")
        #     for m in matches[:40]:
        #         print(f"  [DBG]   {m.strip()[:300]}")

        # 폴링 종료 후 성공한 경우
        if captured["url"]:
            _extracted_host = urlparse(captured["url"]).hostname or "?"
            _dl_log.info("URL 추출 성공 — host=%s path=%s", _extracted_host, urlparse(captured["url"]).path[:120])
            return ExtractionResult(url=captured["url"], diagnostics=_diagnostics())

        # LOG-006: _parse_content_php 가 fire-and-forget 로 돌고 있어 _diagnostics()
        # 호출 시점에 _content_php_parse_error 가 아직 세팅 안 된 경쟁 상태가 가능.
        # 실패 진단에 들어가기 직전에 background task 가 done 이면 await 하여
        # parse_error 상태 반영을 보장. (이미 끝났으면 즉시 리턴)
        if _bg_task is not None and _bg_task.done():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await _bg_task

        # 실패 — sub-reason 판정 우선순위:
        # 1) HLS 스트림만 관측 → HLS_ONLY (다운로더 미지원 알림)
        # 2) content.php 응답은 왔지만 파싱 실패 → CONTENT_PHP_PARSE
        # 3) content.php 응답이 아예 없음 → CONTENT_PHP_MISSING
        # 4) 그 외 (mp4 한 번도 관측 못함) → TIMEOUT
        diag = _diagnostics()
        if _observed_hls and _observed_mp4_count == 0:
            _dl_log.warning(
                "URL 추출 실패 (HLS only) — mp4 경로 없어 현재 다운로더 미지원. url=%s diag=%s",
                lecture_url, diag,
            )
            return ExtractionResult(url=None, reason=REASON_URL_EXTRACT_HLS_ONLY, diagnostics=diag)

        if _content_php_seen and _content_php_parse_error:
            _dl_log.warning(
                "URL 추출 실패 (content.php 파싱 실패) — %s. url=%s",
                _content_php_parse_error, lecture_url,
            )
            return ExtractionResult(url=None, reason=REASON_URL_EXTRACT_CONTENT_PHP_PARSE, diagnostics=diag)

        if not _content_php_seen:
            _dl_log.warning(
                "URL 추출 실패 (content.php 응답 없음) — 플레이어 iframe 로드 문제 가능. url=%s diag=%s",
                lecture_url, diag,
            )
            return ExtractionResult(url=None, reason=REASON_URL_EXTRACT_CONTENT_PHP_MISSING, diagnostics=diag)

        _dl_log.warning(
            "URL 추출 실패 (%ds 폴링 timeout) — mp4/HLS 모두 미관측. url=%s diag=%s",
            int(_VIDEO_POLL_MAX * _POLL_INTERVAL_SEC), lecture_url, diag,
        )
        return ExtractionResult(url=None, reason=REASON_URL_EXTRACT_TIMEOUT, diagnostics=diag)

    finally:
        page.remove_listener("request", _on_request)
        page.remove_listener("response", _on_response)
        # fire-and-forget 파싱 태스크 정리.
        # LOG-015: 우리가 직접 호출한 cancel() 의 결과로 받는 CancelledError 는
        # 정상 경로이므로 억제. Exception 도 정리 단계라 상위 전파 불필요.
        # 외부에서 상위 태스크가 cancel 될 경우에는 이 finally 자체가 실행되지 않고
        # 곧바로 상위로 전파되므로 suppression 이 cooperative cancellation 을 위반하지 않는다.
        if _bg_task is not None and not _bg_task.done():
            _bg_task.cancel()
            try:
                await _bg_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass


async def download_video_with_browser(
    page: Page,
    url: str,
    save_path: Path,
    on_progress: Callable[[int, int], None] | None = None,
) -> Path:
    """Playwright 브라우저 컨텍스트의 쿠키를 사용해 영상을 스트리밍 다운로드한다."""
    _validate_media_url(url)
    save_path.parent.mkdir(parents=True, exist_ok=True)

    # Playwright 컨텍스트에서 쿠키 추출 → requests에 전달 (CDN 인증 자동 처리)
    context_cookies = await page.context.cookies()
    cookies = {c["name"]: c["value"] for c in context_cookies}

    referer = "https://commons.ssu.ac.kr/"
    # 재시도 가능한 오류: 네트워크 불안정, 청크 인코딩 오류 등
    _RETRYABLE = (
        IncompleteRead,
        requests.exceptions.ChunkedEncodingError,
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
    )
    last_error: Exception | None = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            _stream_download(url, save_path, on_progress, attempt=attempt, cookies=cookies, referer=referer)
            return save_path.resolve()
        except _RETRYABLE as e:
            last_error = e
            _remove_partial(save_path)
            if attempt < _MAX_RETRIES:
                await asyncio.sleep(2**attempt)
        except Exception as e:
            # 재시도 불가능한 오류 (ValueError, 인증 실패 등) → 즉시 중단
            last_error = e
            _remove_partial(save_path)
            break
    _remove_partial(save_path)
    if last_error is None:
        raise RuntimeError("다운로드 실패: 알 수 없는 오류")
    raise last_error




def _stream_download(
    url: str,
    save_path: Path,
    on_progress: Callable[[int, int], None] | None,
    attempt: int,
    cookies: dict[str, str] | None = None,
    referer: str | None = None,
) -> None:
    # LOG-012: Range 이어받기 로직 제거. 상위 재시도 루프가 매번 _remove_partial()
    # 을 호출하여 save_path 를 삭제하므로 이어받기가 성립할 조건 자체가 없었다.
    # 실제로는 매번 처음부터 다시 다운로드하고 있었음. attempt 파라미터는 로깅용으로만 사용.
    headers: dict[str, str] = {"Referer": referer} if referer else {}

    response = requests.get(url, stream=True, timeout=_TIMEOUT, cookies=cookies, headers=headers)

    def _safe_content_length(resp: requests.Response) -> int:
        try:
            return int(resp.headers.get("content-length", 0))
        except (ValueError, TypeError):
            return 0

    try:
        response.raise_for_status()
        mode = "wb"
        total = _safe_content_length(response)
        downloaded = 0

        # B2 진단: CDN 응답의 Content-Type + Content-Length를 로깅
        _ct = response.headers.get("content-type", "?")
        _dl_log.info(
            "다운로드 응답 — status=%s content-type=%s content-length=%s path=%s",
            response.status_code, _ct, total, save_path.name,
        )

        with open(save_path, mode) as f:
            for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if on_progress and total > 0:
                        on_progress(downloaded, total)
    finally:
        response.close()

    # B2 sanity check: 컨테이너 시그니처/최소 크기 검증 — 실패 시 SuspiciousStubError 발생
    _validate_downloaded_file(save_path)


def _validate_downloaded_file(save_path: Path) -> None:
    """저장된 mp4 파일의 시그니처와 크기를 검사해 가짜/stub 파일을 차단한다.

    차단 조건:
    - WebM/Matroska EBML 시그니처(`1A 45 DF A3`) — 플레이어 단계의 fake webm이 mp4로 저장된 경우
    - 파일 크기가 _MIN_PLAUSIBLE_VIDEO_BYTES 미만 — CDN 인증 실패 stub 가능

    검증 실패 시 파일을 삭제하고 SuspiciousStubError를 raise 해 파이프라인이
    쓰레기 파일로 진행되지 않도록 한다.
    """
    try:
        with open(save_path, "rb") as _fh:
            _head = _fh.read(16)
    except OSError as _e:
        _dl_log.warning("파일 시그니처 확인 실패: %s", _e)
        return

    _size = save_path.stat().st_size if save_path.exists() else 0
    _magic_hex = _head.hex() if _head else ""

    # WebM/Matroska 시그니처 감지 — 가짜 webm이 mp4로 저장됨
    if _head[:4] == b"\x1a\x45\xdf\xa3":
        _dl_log.error(
            "다운로드 파일이 WebM(EBML) 시그니처 — fake webm 누출 의심. magic=%s size=%d path=%s",
            _magic_hex, _size, save_path,
        )
        _remove_partial(save_path)
        raise SuspiciousStubError(
            f"WebM 시그니처가 감지된 mp4 — 플레이어 fake video가 다운로드에 누출됨 (size={_size})"
        )

    # MP4 ftyp 시그니처 부재 — 알 수 없는 컨테이너
    if len(_head) < 8 or _head[4:8] != b"ftyp":
        _dl_log.warning("다운로드 파일 시그니처 미상 — magic=%s size=%d path=%s", _magic_hex, _size, save_path)
        # 시그니처 미상이지만 크기가 충분하면 일단 통과 (관측 목적)
        # 크기까지 작으면 아래 분기에서 차단

    # 비정상적으로 작은 파일 — CDN stub 또는 빈 응답 의심
    if _size < _MIN_PLAUSIBLE_VIDEO_BYTES:
        _dl_log.error(
            "다운로드 파일 크기 비정상 (< %d bytes) — CDN stub 가능. size=%d path=%s",
            _MIN_PLAUSIBLE_VIDEO_BYTES, _size, save_path,
        )
        _remove_partial(save_path)
        raise SuspiciousStubError(
            f"다운로드 파일 크기 비정상 ({_size} bytes) — 실제 강의가 아닌 stub 가능"
        )

    _dl_log.debug("다운로드 파일 검증 통과 — magic=%s size=%d", _magic_hex, _size)


def _remove_partial(path: Path):
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def make_filepath(course_name: str, week_label: str, lecture_title: str) -> Path:
    """'과목명/N주차/강의명.mp4' 형식의 상대 경로를 생성한다."""
    course = _sanitize_filename(course_name)
    title = _sanitize_filename(lecture_title)

    # week_label에서 "N주차" 추출 (예: "1주차(총 8주 중)" → "1주차")
    week_match = re.match(r"(\d+주차)", week_label or "")
    week_dir = week_match.group(1) if week_match else _sanitize_filename(week_label or "") or "기타"

    return Path(course) / week_dir / f"{title}.mp4"
