"""
영상 다운로드.

Playwright로 LMS 강의 페이지에서 video URL을 추출한 뒤,
requests로 청크 스트리밍 다운로드한다.
"""

import asyncio
import logging
import re
import time
from collections.abc import Callable
from http.client import IncompleteRead
from pathlib import Path
from urllib.parse import urlparse

import requests
from playwright.async_api import Page

from src.downloader.result import SSRFBlockedError, SuspiciousStubError
from src.player.background_player import click_play, dismiss_dialog, find_player_frame

_dl_log = logging.getLogger(__name__)

_MAX_RETRIES = 3
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


async def extract_video_url(page: Page, lecture_url: str) -> str | None:
    """
    LMS 강의 페이지에서 mp4 URL을 추출한다.

    Plan A: video 태그 src 폴링 (일반 타입)
    Plan B: Network 요청 가로채기 — mp4 URL이 포함된 요청 캡처 (readystream 등)
    """

    captured: dict[str, str | None] = {"url": None}
    _bg_task: asyncio.Task | None = None
    _content_parsed = False
    _observed_hls = False  # m3u8/HLS URL 감지 시 실패 원인 분류에 사용

    # 플레이어 초기화 단계에서 <video> 태그에 임시로 부착되는 stub 파일들.
    # 실제 강의가 아니므로 Plan A(DOM) / Plan B(network) 모두에서 제외한다.
    # BUG-FIX: intro.mp4 누락으로 Plan A가 stub을 진짜 URL로 오인하여
    # 재시도 불가 처리로 강의가 영구 누락되던 문제 수정.
    exclude_patterns = ("preloader.mp4", "preview.mp4", "thumbnail.mp4", "intro.mp4")

    def _is_valid_mp4(url: str) -> bool:
        return ".mp4" in url and not any(p in url for p in exclude_patterns)

    def _note_hls(url: str) -> None:
        nonlocal _observed_hls
        if not _observed_hls and (".m3u8" in url or "/hls/" in url):
            _observed_hls = True

    def _on_request(request):
        url = request.url
        _note_hls(url)
        if _is_valid_mp4(url) and captured["url"] is None:
            captured["url"] = url

    def _on_response(response):
        nonlocal _bg_task, _content_parsed
        url = response.url
        _note_hls(url)
        if _is_valid_mp4(url) and captured["url"] is None:
            captured["url"] = url
        # content.php 응답에서 미디어 URL 추출 (최초 1회만 파싱)
        if not _content_parsed and "content.php" in url and "commons.ssu.ac.kr" in url:
            _content_parsed = True

            async def _parse_content_php():
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
                except Exception as e:
                    _dl_log.debug("content.php 파싱 오류: %s", e)

            _bg_task = asyncio.create_task(_parse_content_php())
            _bg_task.add_done_callback(lambda t: t.exception() if not t.cancelled() and t.exception() else None)

    page.on("request", _on_request)
    page.on("response", _on_response)

    try:
        # print(f"  [DBG] 페이지 이동: {lecture_url[:80]}")
        await page.goto(lecture_url, wait_until="domcontentloaded", timeout=_PAGE_GOTO_TIMEOUT)
        # iframe + content.php 로드 대기 (비동기 파싱 완료까지)
        for _ in range(_CONTENT_PHP_POLL_MAX):
            await asyncio.sleep(_POLL_INTERVAL_SEC)
            if captured["url"]:
                break
        # print(f"  [DBG] 현재 페이지 URL: {page.url[:80]}")

        # content.php에서 미디어 URL이 추출됐으면 바로 반환
        if captured["url"]:
            # print(f"  [NET] content.php에서 미디어 URL 추출 성공: {captured['url']}")
            return captured["url"]

        player_frame = await find_player_frame(page)
        if not player_frame:
            # print("  [DBG] player frame을 찾지 못했습니다.")
            # for f in page.frames:
            #     print(f"  [DBG]   {f.url[:100]}")
            return None

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
                return captured["url"]

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
                            return src

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
                        return result
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

        if captured["url"] is None and _observed_hls:
            _dl_log.warning(
                "URL 추출 실패 — HLS(m3u8) 스트림만 감지됨. mp4 경로가 없어 현재 다운로더로는 저장 불가. url=%s",
                lecture_url,
            )
        elif captured["url"] is None:
            _dl_log.warning("URL 추출 실패 — 60초 폴링 후에도 mp4/HLS 모두 감지 안 됨. url=%s", lecture_url)
        else:
            # B2 진단: 추출된 URL의 호스트/경로를 남겨 가짜 webm 누출 조사에 사용
            _extracted_host = urlparse(captured["url"]).hostname or "?"
            _dl_log.info("URL 추출 성공 — host=%s path=%s", _extracted_host, urlparse(captured["url"]).path[:120])
        return captured["url"]

    finally:
        page.remove_listener("request", _on_request)
        page.remove_listener("response", _on_response)
        # fire-and-forget 파싱 태스크 정리
        if _bg_task is not None and not _bg_task.done():
            _bg_task.cancel()
            try:
                await _bg_task
            except (asyncio.CancelledError, Exception):
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


def download_video(
    url: str,
    save_path: Path,
    on_progress: Callable[[int, int], None] | None = None,
    cookies: dict[str, str] | None = None,
    referer: str | None = None,
) -> Path:
    """
    HTTP 스트리밍으로 영상을 다운로드한다.

    Args:
        url:         직접 다운로드 가능한 mp4 URL
        save_path:   저장 경로 (파일명 포함)
        on_progress: (downloaded_bytes, total_bytes) 콜백

    Returns:
        저장된 파일의 Path

    Raises:
        Exception: 최대 재시도 후에도 실패한 경우
    """
    _validate_media_url(url)
    save_path.parent.mkdir(parents=True, exist_ok=True)

    last_error: Exception | None = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            _stream_download(url, save_path, on_progress, attempt, cookies=cookies, referer=referer)
            return save_path.resolve()
        except (IncompleteRead, requests.exceptions.ChunkedEncodingError) as e:
            last_error = e
            _remove_partial(save_path)
            if attempt < _MAX_RETRIES:
                wait = 2**attempt
                time.sleep(wait)
        except Exception as e:
            last_error = e
            _remove_partial(save_path)
            break

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
    headers: dict[str, str] = {"Referer": referer} if referer else {}
    existing_size = 0

    # 재시도 시 기존 파일이 있으면 이어받기 시도
    if attempt > 1 and save_path.exists():
        existing_size = save_path.stat().st_size
        if existing_size > 0:
            headers["Range"] = f"bytes={existing_size}-"

    response = requests.get(url, stream=True, timeout=_TIMEOUT, cookies=cookies, headers=headers)

    def _safe_content_length(resp: requests.Response) -> int:
        try:
            return int(resp.headers.get("content-length", 0))
        except (ValueError, TypeError):
            return 0

    try:
        if response.status_code == 206:
            # 서버가 Range 지원 → 이어받기
            mode = "ab"
            total = existing_size + _safe_content_length(response)
            downloaded = existing_size
        elif response.status_code == 200:
            # 서버가 Range 미지원 또는 첫 시도 → 처음부터
            response.raise_for_status()
            mode = "wb"
            total = _safe_content_length(response)
            downloaded = 0
        else:
            response.raise_for_status()
            return

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
