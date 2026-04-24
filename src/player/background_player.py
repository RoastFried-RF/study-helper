"""
백그라운드 재생 모듈.

LMS 강의 페이지를 headless 브라우저로 열고, 영상을 재생하여
LMS가 수강 완료로 인식하도록 실제 재생 시간을 유지한다.

재생 흐름:
1. 강의 페이지 이동 (item_url)
2. 중첩 iframe 탐색 (tool_content → commons.ssu.ac.kr)
3. 이어보기 다이얼로그 자동 처리 (처음부터 재생)
4. 재생 버튼 클릭 후 video 요소의 currentTime / duration 폴링
5. 영상 끝날 때까지 진행 콜백 호출
"""

import asyncio
import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlencode, urlparse

from playwright.async_api import Frame, Page

from src.util.log_sanitize import mask_sensitive as _mask_sensitive

# ── 상수 ─────────────────────────────────────────────────────────
_POLL_INTERVAL = 1.0  # 진행 폴링 주기 (초)
_FRAME_FIND_TIMEOUT = 30  # iframe 탐색 최대 대기 (초)
_PLAY_TIMEOUT = 20  # 재생 버튼/영상 시작 대기 (초)
_END_THRESHOLD = 3  # 영상 끝 판정 여유 (초)
_RESUME_BTN = ".confirm-ok-btn"
_RESTART_BTN = ".confirm-cancel-btn"
_DIALOG_SEL = ".confirm-msg-box"
_PLAY_BTN = ".vc-front-screen-play-btn"
_VIDEO_SEL = "video.vc-vplay-video1"

# 호스트 허용 목록 — LMS/플레이어 서버 외 URL 차단 (SSRF 방지)
_ALLOWED_PLAYER_HOSTS = {"canvas.ssu.ac.kr", "commons.ssu.ac.kr"}

# body 읽기를 건너뛸 바이너리 Content-Type 접두사
_BINARY_CT = ("image/", "video/", "audio/", "font/", "application/octet-stream")

# PII/secret 마스킹은 src.util.log_sanitize.mask_sensitive 공용 모듈 사용.
# scripts/sanitize_logs.py 에서도 동일 규칙을 공유해 drift 방지.


def _build_progress_url(
    progress_url: str,
    *,
    state: int,
    duration_str: str,
    current_str: str,
    page: int,
    total_page: int,
    cumulative_page: int,
    callback_prefix: str = "jQuery111_",
) -> tuple[str, str]:
    """LMS progress API JSONP URL 을 조립한다. (url, callback_name) 반환.

    ARCH-006: 3개 호출사이트(완료 보고 / 30초 주기 / fake webm afterTimeUpdate)
    가 같은 템플릿을 반복하던 것을 통합. 사이트별 숫자 포맷 미세 차이를
    보존하기 위해 duration/current 는 pre-formatted 문자열을 받는다.
    """
    ts = int(time.time() * 1000)
    callback = f"{callback_prefix}{ts}"
    sep = "&" if "?" in progress_url else "?"
    url = (
        f"{progress_url}{sep}"
        f"callback={callback}"
        f"&state={state}"
        f"&duration={duration_str}"
        f"&currentTime={current_str}"
        f"&cumulativeTime={current_str}"
        f"&page={page}"
        f"&totalpage={total_page}"
        f"&cumulativePage={cumulative_page}"
        f"&_={ts}"
    )
    return url, callback


def _set_sl_param(url: str, value: str) -> str:
    """URL의 sl 쿼리 파라미터를 안전하게 변경한다."""
    p = urlparse(url)
    qs = parse_qs(p.query, keep_blank_values=True)
    qs["sl"] = [value]
    return p._replace(query=urlencode(qs, doseq=True)).geturl()


@dataclass
class PlaybackState:
    current: float = 0.0  # 현재 재생 위치 (초)
    duration: float = 0.0  # 전체 길이 (초)
    ended: bool = False
    error: str | None = None


# ── 브라우저 헬퍼 (player + downloader 공용) ──────────────────────


async def find_player_frame(page: Page) -> Frame | None:
    """
    tool_content 아래 commons.ssu.ac.kr frame을 찾는다.
    재생 버튼이 있는 초기 플레이어 선택 화면 frame.
    flashErrorPage는 제외한다.
    """
    for _ in range(_FRAME_FIND_TIMEOUT):
        outer = page.frame(name="tool_content")
        if outer:
            for frame in page.frames:
                if (
                    frame.parent_frame == outer
                    and "commons.ssu.ac.kr" in frame.url
                    and "flashErrorPage" not in frame.url
                ):
                    return frame
        await asyncio.sleep(1)
    return None


async def _find_video_frame(page: Page) -> Frame | None:
    """
    실제 video 태그가 있는 frame을 찾는다.
    재생 버튼 클릭 후 page 전체를 재스캔한다.

    commons.ssu.ac.kr에 속한 모든 frame (flashErrorPage 포함) 중
    video 태그가 존재하는 것을 반환한다.
    flashErrorPage 자체가 HTML5 video를 동적으로 생성할 수 있으므로 포함한다.
    """
    for _ in range(_FRAME_FIND_TIMEOUT):
        for frame in page.frames:
            if "commons.ssu.ac.kr" not in frame.url:
                continue
            try:
                count = await frame.evaluate("() => document.querySelectorAll('video').length")
                if count > 0:
                    return frame
            except Exception:
                pass
        await asyncio.sleep(1)
    return None


async def dismiss_dialog(frame: Frame, restart: bool = True) -> bool:
    """이어보기 다이얼로그가 표시되면 처리한다. 처리했으면 True 반환."""
    try:
        dialog = await frame.query_selector(_DIALOG_SEL)
        if not dialog or not await dialog.is_visible():
            return False
        # 처음부터 재생 (restart=True) 또는 이어보기 (restart=False)
        btn_sel = _RESTART_BTN if restart else _RESUME_BTN
        btn = await frame.query_selector(btn_sel)
        if btn:
            await btn.click()
            return True
    except Exception:
        pass
    return False


async def click_play(frame: Frame) -> bool:
    """재생 버튼을 클릭한다. 성공 시 True."""
    try:
        btn = await frame.wait_for_selector(_PLAY_BTN, timeout=_PLAY_TIMEOUT * 1000)
        if btn:
            await btn.click()
            return True
    except Exception:
        pass
    return False


async def _get_video_state(frame: Frame) -> dict | None:
    """video 요소의 현재 상태(currentTime, duration, ended, paused)를 반환한다."""
    try:
        return await frame.evaluate(
            """(sel) => {
                const v = document.querySelector(sel);
                if (!v) return null;
                return {
                    current: v.currentTime,
                    duration: v.duration || 0,
                    ended: v.ended,
                    paused: v.paused
                };
            }""",
            _VIDEO_SEL,
        )
    except Exception:
        return None


async def _ensure_playing(frame: Frame):
    """일시정지 상태면 JS로 강제 재생한다."""
    try:
        await frame.evaluate(
            """(sel) => {
                const v = document.querySelector(sel);
                if (v && v.paused && !v.ended) v.play();
            }""",
            _VIDEO_SEL,
        )
    except Exception:
        pass


async def _create_fake_webm(duration_sec: float) -> bytes:
    """VP8 WebM 더미 영상 생성 (Chromium H.264 미지원 우회).

    TemporaryDirectory 사용 — context manager 종료 시 자동 삭제.
    2×2 픽셀 검정 프레임, 1fps, 극소 용량.
    Chromium headless는 H.264를 지원하지 않지만 VP8/WebM은 기본 지원한다.
    commonscdn MP4 요청을 이 영상으로 교체하면 Plan A(video DOM 폴링)가 동작한다.
    """
    import os
    import tempfile
    from pathlib import Path as _Path

    with tempfile.TemporaryDirectory() as tmpdir:
        output_path = _Path(tmpdir) / "fake.webm"
        dur = str(int(duration_sec) + 2)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=black:s=2x2:r=1",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=8000:cl=mono",
            "-t",
            dur,
            "-c:v",
            "libvpx",
            "-b:v",
            "1k",
            "-c:a",
            "libopus",
            "-b:a",
            "8k",
            "-map",
            "0:v",
            "-map",
            "1:a",
            str(output_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            # SEC-010: subprocess 에 민감 env 상속 차단 — PATH 만 전달.
            env={"PATH": os.environ.get("PATH", "")},
        )
        await proc.communicate()
        if not output_path.exists() or output_path.stat().st_size == 0:
            raise RuntimeError("ffmpeg 더미 영상 생성 실패")
        return output_path.read_bytes()


# ── 진도 API 직접 호출 (Plan B) ──────────────────────────────────


def _parse_player_url(player_url: str) -> dict:
    """
    commons.ssu.ac.kr/em/ URL에서 재생 정보를 파싱한다.

    반환:
        {
            "content_id": str,
            "duration": float,       # endat 파라미터 (초)
            "progress_url": str,     # TargetUrl 디코딩값
        }
    """
    parsed = urlparse(player_url)
    qs = parse_qs(parsed.query)

    duration = float(qs.get("endat", ["0"])[0])
    target_url = unquote(qs.get("TargetUrl", [""])[0])

    # progress_url 호스트 검증 — LMS 서버 외 URL 차단 (SSRF 방지)
    if target_url:
        _parsed_host = urlparse(target_url).hostname
        if _parsed_host and _parsed_host not in _ALLOWED_PLAYER_HOSTS:
            target_url = ""

    # content_id는 path의 마지막 세그먼트
    content_id = parsed.path.rstrip("/").split("/")[-1]

    return {
        "content_id": content_id,
        "duration": duration,
        "progress_url": target_url,
    }


async def _call_progress_jsonp(frame: Frame, report_url: str, callback: str) -> str:
    """
    commons 프레임 내부에서 JSONP 스크립트 태그를 주입해 진도 API를 호출한다.

    실제 플레이어(uni-player.min.js)와 동일하게 commons.ssu.ac.kr origin에서
    canvas.ssu.ac.kr progress 엔드포인트를 호출함으로써 ErrAlreadyInView를 우회한다.

    보안 참고: report_url은 LMS가 제공하는 TargetUrl에서 파생되므로 LMS 서버를
    신뢰하는 구조. LMS 서버가 변조되면 임의 JS 실행 가능성이 있으나,
    그 시점에서는 LMS 자체가 침해된 것이므로 여기서 방어할 범위를 벗어남.
    """
    result = await frame.evaluate(
        """
        (args) => new Promise((resolve) => {
            var url = args[0];
            var cbName = args[1];
            window[cbName] = function(data) {
                delete window[cbName];
                if (s && s.parentNode) s.parentNode.removeChild(s);
                resolve(JSON.stringify(data));
            };
            var s = document.createElement('script');
            s.src = url;
            s.onerror = function() {
                delete window[cbName];
                if (s && s.parentNode) s.parentNode.removeChild(s);
                resolve(JSON.stringify({error: 'script_error'}));
            };
            document.head.appendChild(s);
            setTimeout(function() {
                delete window[cbName];
                if (s && s.parentNode) s.parentNode.removeChild(s);
                resolve(JSON.stringify({error: 'timeout'}));
            }, 10000);
        })
    """,
        [report_url, callback],
    )
    return result


async def _report_completion(
    page: Page,
    player_url: str,
    duration: float,
    log: Callable[[str], None],
    commons_frame: Frame | None = None,
    use_page_eval: bool = False,
):
    """
    Plan A/B 완료 후 progress API에 100% 진도를 한 번 직접 보고한다.

    플레이어 JS(uni-player-event.js)가 가짜 WebM 재생 중 progress API를 호출하지
    않는 경우를 대비한 안전망. Plan A가 성공하더라도 항상 호출한다.

    ErrAlreadyInView 처리:
    - use_page_eval=True (Plan A): page.evaluate fetch로 canvas.ssu.ac.kr 동일 오리진 호출.
      Plan A에서는 sl=1 세션이 활성 중이므로 JSONP 대신 이 방식을 사용.
    - commons_frame 있음 (Plan B): JSONP 방식으로 sl=0 세션에서 호출 (ErrAlreadyInView 우회).
    - 둘 다 없거나 실패 시: page.request.get으로 폴백.
    """
    info = _parse_player_url(player_url)
    progress_url = info["progress_url"]
    if not progress_url:
        log("  [완료 보고] TargetUrl 없음 — 건너뜀")
        return

    if duration <= 0:
        duration = info["duration"]
    if duration <= 0:
        log("  [완료 보고] duration 불명 — 건너뜀")
        return

    total_page = 15

    def _build_url() -> tuple[str, str]:
        return _build_progress_url(
            progress_url,
            state=3,
            duration_str=str(duration),
            current_str=f"{duration:.2f}",
            page=total_page,
            total_page=total_page,
            cumulative_page=total_page,
            callback_prefix="jQuery111_",
        )

    for attempt in range(3):
        if attempt > 0:
            log(f"  [완료 보고] 재시도 {attempt + 1}/3 (2초 대기 후)")
            await asyncio.sleep(2)

        log(f"  [완료 보고] 100% 진도 직접 전송 (duration={duration:.1f}s)")

        # Plan A: page.evaluate fetch (canvas.ssu.ac.kr 동일 오리진 — sl=1 세션 중에도 동작)
        if use_page_eval:
            report_url, _ = _build_url()
            try:
                result = await page.evaluate(f"""
                    async () => {{
                        try {{
                            const resp = await fetch({json.dumps(report_url)});
                            return {{s: resp.status, b: (await resp.text()).slice(0, 300)}};
                        }} catch(e) {{
                            return {{s: -1, b: e.message}};
                        }}
                    }}
                """)
                status = result.get("s")
                body = result.get("b", "")
                log(f"  [완료 보고] page ctx fetch: {status}  body={_mask_sensitive(body)!r}")
                if status == 200 and '"result":true' in body:
                    return
                log(f"  [완료 보고] page ctx fetch 실패 ({status}) — page.request.get으로 폴백")
            except Exception as e:
                log(f"  [완료 보고] page ctx fetch 오류: {e}")

        # Plan B: commons_frame JSONP (sl=0 세션 — ErrAlreadyInView 우회)
        elif commons_frame:
            report_url, callback = _build_url()
            try:
                body = await _call_progress_jsonp(commons_frame, report_url, callback)
                log(f"  [완료 보고] JSONP 응답: {body[:200]!r}")
                if '"result":true' in body:
                    return
                log("  [완료 보고] JSONP 결과 false — page.request.get으로 폴백")
            except Exception as e:
                log(f"  [완료 보고] JSONP 실패 ({e}) — page.request.get으로 폴백")

        # 폴백: page.request.get
        report_url_fb, _ = _build_url()
        try:
            response = await page.request.get(
                report_url_fb,
                headers={"Referer": "https://commons.ssu.ac.kr/"},
            )
            try:
                body = await response.text()
                log(f"  [완료 보고] request.get 응답: {response.status}  body={body[:200]!r}")
                if '"result":true' in body:
                    return
            finally:
                await response.dispose()
        except Exception as e:
            log(f"  [완료 보고] request.get 실패: {e}")

    log("  [완료 보고] 3회 시도 모두 실패 — 출석이 인정되지 않았을 수 있습니다")


async def _play_via_learningx_api(
    page: Page,
    learningx_url: str,
    on_progress: Callable[[PlaybackState], None] | None,
    log: Callable[[str], None],
    fallback_duration: float = 0.0,
    *,
    learningx_frame: Frame | None = None,
) -> PlaybackState:
    """
    learningx 플레이어 전용 Plan B.

    learningx /api/v1/courses/{course_id}/attendance_items/{item_id} 에서
    viewer_url을 가져오면 commons TargetUrl이 포함되어 있어,
    기존 _play_via_progress_api를 그대로 재사용할 수 있다.

    learningx_url 예시:
      https://canvas.ssu.ac.kr/learningx/lti/lecture_attendance/items/view/764082
    """
    import re as _re

    state = PlaybackState()

    # URL에서 item_id, course_id 추출
    m = _re.search(r"/lecture_attendance/items/view/(\d+)", learningx_url)
    if not m:
        log(f"  [LX] item_id 파싱 실패: {learningx_url}")
        state.error = "learningx item_id를 파싱하지 못했습니다."
        return state

    item_id = m.group(1)

    # course_id는 페이지 URL에서 추출
    cm = _re.search(r"/courses/(\d+)/", page.url)
    if not cm:
        log(f"  [LX] course_id 파싱 실패: {page.url}")
        state.error = "learningx course_id를 파싱하지 못했습니다."
        return state

    course_id = cm.group(1)
    api_url = f"https://canvas.ssu.ac.kr/learningx/api/v1/courses/{course_id}/attendance_items/{item_id}"
    log(f"  [LX] learningx item API 호출: {api_url}")

    # 세션 갱신 시 원래 강의 페이지로 복귀해야 LTI iframe도 갱신됨
    lecture_page_url = page.url

    # learningx iframe에서 CSRF 토큰 포함하여 fetch — 401 방지
    # tool_content frame은 canvas.ssu.ac.kr/learningx 오리진으로,
    # learningx API가 요구하는 X-CSRF-TOKEN을 <meta> 태그에서 추출할 수 있다.
    fetch_frame = learningx_frame or page.frame(name="tool_content")

    # 401 등 일시적 세션 오류에 대비해 최대 3회 재시도
    body = ""
    status = -1
    for attempt in range(3):
        try:
            target = fetch_frame if fetch_frame and not fetch_frame.is_detached() else page
            result = await target.evaluate(f"""
                async () => {{
                    try {{
                        const meta = document.querySelector('meta[name="csrf-token"]');
                        const headers = {{}};
                        if (meta) headers['X-CSRF-TOKEN'] = meta.content;
                        const resp = await fetch({json.dumps(api_url)}, {{ headers }});
                        return {{s: resp.status, b: await resp.text()}};
                    }} catch(e) {{
                        return {{s: -1, b: e.message}};
                    }}
                }}
            """)
            status = result.get("s")
            body = result.get("b", "")
            log(f"  [LX] API 응답: {status} (frame={'iframe' if target is not page else 'main'})")
            if status == 200:
                break
            if status in (401, 403) and attempt < 2:
                log(f"  [LX] {status} 응답 — {attempt + 1}/3회 재시도 (3초 대기)")
                await asyncio.sleep(3)
                # 세션 갱신: 강의 페이지를 재로드하여 LTI iframe(learningx)도 갱신
                try:
                    await page.goto(lecture_page_url, wait_until="domcontentloaded", timeout=20000)
                    await asyncio.sleep(2)
                    # 재로드된 tool_content frame 재취득
                    fetch_frame = page.frame(name="tool_content")
                    if fetch_frame and not fetch_frame.is_detached():
                        log(f"  [LX] 세션 갱신 완료 — iframe 재취득: {fetch_frame.url[:80]}")
                    else:
                        fetch_frame = None
                        log("  [LX] 세션 갱신 후 tool_content frame 없음 — main frame으로 폴백")
                except Exception as nav_e:
                    log(f"  [LX] 세션 갱신 중 오류: {nav_e}")
                continue
        except Exception as e:
            if attempt < 2:
                log(f"  [LX] API 호출 오류 — {attempt + 1}/3회 재시도: {e}")
                await asyncio.sleep(3)
                continue
            log(f"  [LX] API 호출 최종 실패: {e}")
            state.error = "learningx API 호출 실패"
            return state

    if status != 200:
        log(f"  [LX] API 3회 재시도 모두 실패 ({status})")
        state.error = f"learningx API 오류: {status}"
        return state

    try:
        data = json.loads(body)
    except Exception:
        log(f"  [LX] JSON 파싱 실패: {body[:200]!r}")
        state.error = "learningx API 응답 파싱 실패"
        return state

    viewer_url = data.get("viewer_url", "")
    if not viewer_url:
        log("  [LX] viewer_url 없음")
        state.error = "learningx viewer_url 없음"
        return state

    # viewer_url 호스트 검증 — SSRF 방지
    _viewer_host = urlparse(viewer_url).hostname or ""
    if _viewer_host not in _ALLOWED_PLAYER_HOSTS:
        log(f"  [LX] viewer_url 호스트 불허: {_viewer_host!r}")
        state.error = "viewer_url 호스트 검증 실패"
        return state

    duration = float(data.get("item_content_data", {}).get("duration", 0) or 0)
    log(f"  [LX] viewer_url={viewer_url}")
    log(f"  [LX] duration={duration:.1f}s — Plan B로 전환")

    return await _play_via_progress_api(
        page,
        viewer_url,
        on_progress,
        log,
        fallback_duration=duration if duration > 0 else fallback_duration,
    )


async def _play_via_progress_api(
    page: Page,
    player_url: str,
    on_progress: Callable[[PlaybackState], None] | None,
    log: Callable[[str], None],
    fallback_duration: float = 0.0,
) -> PlaybackState:
    """
    headless에서 플레이어 로드에 실패할 때 사용하는 Plan B.

    진도 API(TargetUrl)를 주기적으로 호출해서 LMS가 수강 완료로 인식하도록 한다.

    ErrAlreadyInView 우회 전략:
    - sl=1 파라미터로 commons.ssu.ac.kr에 뷰 세션이 등록된 상태에서
      canvas.ssu.ac.kr 컨텍스트에서 직접 progress API를 호출하면 ErrAlreadyInView가 반환됨.
    - commons 프레임(flashErrorPage.html)이 아직 살아있을 때,
      그 프레임 내부에서 JSONP 스크립트 태그를 주입해 호출하면
      실제 플레이어와 동일한 commons.ssu.ac.kr origin으로 요청이 전송되어 우회 가능.
    - commons 프레임이 없으면 대시보드로 이동 후 page.request.get으로 폴백.
    """
    state = PlaybackState()
    info = _parse_player_url(player_url)
    duration = info["duration"]
    progress_url = info["progress_url"]

    if not progress_url:
        log("  [API] TargetUrl 파싱 실패 — 재생 불가")
        state.error = "진도 API URL을 파싱하지 못했습니다."
        return state

    if duration <= 0 and fallback_duration > 0:
        log(f"  [API] endat 파라미터 없음 — LectureItem.duration 사용: {fallback_duration:.1f}s")
        duration = fallback_duration

    # sl=1 세션 해제: player_url의 sl=1을 sl=0으로 교체해 commons를 재로드.
    # sl=1은 서버에 "현재 시청 중" 세션을 등록해 ErrAlreadyInView를 유발하므로,
    # sl=0으로 재방문하면 세션 충돌 없이 진도 API를 호출할 수 있다.
    # 재로드 후 그 commons 프레임 내부에서 JSONP로 progress를 보고한다.
    sl0_url = _set_sl_param(player_url, "0")
    commons_frame: Frame | None = None
    try:
        log(f"  [API] sl=0으로 commons 재로드: {sl0_url[:80]}...")
        await page.goto(sl0_url, wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(2)
        # sl=0으로 로드된 commons frame 탐색
        for f in page.frames:
            if "commons.ssu.ac.kr" in f.url:
                commons_frame = f
                break
        log(f"  [API] commons frame({'발견' if commons_frame else '없음'})")
    except Exception as e:
        log(f"  [API] commons 재로드 실패 ({e}) — page.request.get으로 폴백")

    # duration이 아직 없으면 sl=0으로 로드된 commons frame에서 video.duration 조회
    if duration <= 0 and commons_frame:
        log("  [API] endat/fallback 없음 — video 엘리먼트에서 duration 조회 시도")
        try:
            vid_dur = await commons_frame.evaluate(
                "() => { const v = document.querySelector('video'); "
                "return (v && v.duration && isFinite(v.duration)) ? v.duration : 0; }"
            )
            if vid_dur and vid_dur > 0:
                duration = float(vid_dur)
                log(f"  [API] video.duration에서 추출 성공: {duration:.1f}s")
        except Exception as e:
            log(f"  [API] video.duration 조회 실패: {e}")

    if duration <= 0:
        log("  [API] duration 파싱 실패 — URL/fallback/video 모두 없음")
        state.error = "영상 길이를 알 수 없습니다."
        return state

    log("  [API] 진도 API 방식으로 재생 시뮬레이션")
    log(f"  [API] duration={duration:.1f}s  progress_url={progress_url}")

    state.duration = duration
    current = 0.0
    report_interval = 30.0  # 30초마다 진도 보고
    next_report = report_interval

    # 총 페이지 수는 실제 요청에서 totalpage=15로 고정 (LMS 플레이어 기본값)
    total_page = 15

    while current < duration:
        await asyncio.sleep(_POLL_INTERVAL)
        current = min(current + _POLL_INTERVAL, duration)
        state.current = current

        if on_progress:
            on_progress(state)

        # 30초마다 진도 API 호출
        if current >= next_report or current >= duration:
            try:
                cumulative_page = total_page if current >= duration else int(current / duration * total_page)
                page_num = min(cumulative_page, total_page)

                report_target, callback = _build_progress_url(
                    progress_url,
                    state=3,
                    duration_str=str(duration),
                    current_str=f"{current:.2f}",
                    page=page_num,
                    total_page=total_page,
                    cumulative_page=cumulative_page,
                    callback_prefix="jQuery111_",
                )
                log(f"  [API] 진도 보고: {int(current)}s/{int(duration)}s")

                if commons_frame:
                    try:
                        body = await _call_progress_jsonp(commons_frame, report_target, callback)
                        log(f"  [API] 응답 (JSONP): {body[:200]!r}")
                    except Exception as je:
                        log(f"  [API] JSONP 실패 ({je}) — page.request.get으로 폴백")
                        commons_frame = None
                        response = await page.request.get(
                            report_target,
                            headers={"Referer": "https://commons.ssu.ac.kr/"},
                        )
                        try:
                            body = await response.text()
                            log(f"  [API] 응답 (fallback): {response.status}  body={_mask_sensitive(body[:200])!r}")
                        finally:
                            await response.dispose()
                else:
                    response = await page.request.get(
                        report_target,
                        headers={"Referer": "https://commons.ssu.ac.kr/"},
                    )
                    try:
                        body = await response.text()
                        log(f"  [API] 응답: {response.status}  body={_mask_sensitive(body[:200])!r}")
                    finally:
                        await response.dispose()
                next_report = current + report_interval
            except Exception as e:
                log(f"  [API] 진도 보고 실패: {e} — 다음 폴링에서 재시도")

    state.ended = True
    if on_progress:
        on_progress(state)

    # 재생 루프 종료 후 100% 완료 보고 — commons_frame 재사용으로 ErrAlreadyInView 방지
    await _report_completion(page, player_url, state.duration, log, commons_frame)

    return state


# ── 공개 API ─────────────────────────────────────────────────────


async def _debug_page_state(page: Page, frame: Frame | None, log: Callable[[str], None]):
    """현재 페이지/프레임 상태를 상세 출력한다."""
    log(f"  [현재 URL] {page.url}")
    log(f"  [전체 프레임 수] {len(page.frames)}")
    for i, f in enumerate(page.frames):
        parent_name = f.parent_frame.name if f.parent_frame else "root"
        log(f"    frame[{i}] name={f.name!r}  parent={parent_name!r}  url={f.url}")

    # 모든 commons.ssu.ac.kr frame에 대해 video 조회
    log("  [commons frame별 video 조회]")
    for i, f in enumerate(page.frames):
        if "commons.ssu.ac.kr" not in f.url:
            continue
        try:
            all_videos = await f.evaluate("""() => {
                return Array.from(document.querySelectorAll('video')).map(v => ({
                    class: v.className,
                    src: v.src || v.currentSrc || '(없음)',
                    readyState: v.readyState,
                    duration: v.duration,
                    paused: v.paused,
                    error: v.error ? v.error.code : null
                }));
            }""")
            body_html = await f.evaluate("() => document.body ? document.body.innerHTML.slice(0, 500) : '(body 없음)'")
            log(f"    frame[{i}] url={f.url}")
            log(f"      video 수={len(all_videos)}")
            for j, v in enumerate(all_videos):
                log(
                    f"      video[{j}] class={v['class']!r}  src={v['src'][:100]!r}  "
                    f"readyState={v['readyState']}  duration={v['duration']}  "
                    f"paused={v['paused']}  error={v['error']}"
                )
            log(f"      body(첫 500자)={body_html!r}")
        except Exception as e:
            log(f"    frame[{i}] 조회 오류: {e}")

    if frame is None:
        log("  [지정 video frame] 없음")
        return

    log(f"  [지정 video frame] url={frame.url}")

    # 재생 버튼 존재 여부
    try:
        play_btn = await frame.query_selector(_PLAY_BTN)
        log(f"  [재생 버튼] {'있음' if play_btn else '없음'}")
    except Exception as e:
        log(f"  [재생 버튼 조회 오류] {e}")

    # 이어보기 다이얼로그 존재 여부
    try:
        dialog = await frame.query_selector(_DIALOG_SEL)
        visible = await dialog.is_visible() if dialog else False
        log(f"  [이어보기 다이얼로그] {'표시 중' if visible else ('DOM 있음(숨김)' if dialog else '없음')}")
    except Exception as e:
        log(f"  [다이얼로그 조회 오류] {e}")


async def play_lecture(
    page: Page,
    lecture_url: str,
    on_progress: Callable[[PlaybackState], None] | None = None,
    debug: bool = False,
    fallback_duration: float = 0.0,
    log_fn: Callable | None = None,
) -> PlaybackState:
    """
    강의 URL을 headless 브라우저로 재생한다.

    Args:
        page:         CourseScraper가 관리하는 Playwright Page.
        lecture_url:  LectureItem.full_url
        on_progress:  재생 진행 시 주기적으로 호출되는 콜백. PlaybackState 전달.
        debug:        True이면 단계별 진단 로그를 출력한다.
        log_fn:       debug 출력에 사용할 로그 함수. 미지정 시 print 사용.

    Returns:
        최종 PlaybackState.
    """
    log = log_fn if log_fn else (print if debug else (lambda *a, **k: None))
    state = PlaybackState()

    # 0. H.264 우회: VP8 WebM 더미 영상으로 commonscdn MP4 인터셉트
    # Chromium headless(ARM64 포함)는 H.264 미지원 → flashErrorPage.html 로드 → Plan A 실패
    # VP8 WebM을 대신 제공하면 Chromium이 정상 재생 → Plan A 동작 → LTI 세션 내에서 progress 보고
    _using_fake_video = False
    _fake_video_bytes = None
    if fallback_duration > 0:
        log(f"[0] H.264 우회: VP8 더미 영상 생성 중 (duration={fallback_duration:.0f}s)...")
        try:
            _fake_video_bytes = await _create_fake_webm(fallback_duration)
            log(f"[0] 더미 영상 생성 완료 ({len(_fake_video_bytes):,} bytes)")

            async def _serve_fake(route, request):
                await route.fulfill(
                    status=200,
                    headers={"Content-Type": "video/webm"},
                    body=_fake_video_bytes,
                )

            await page.route("**/*.mp4", _serve_fake)
            # canPlayType / isTypeSupported 오버라이드:
            # Chromium은 H.264 미지원 → canPlayType("video/mp4; codecs=avc1") = ""
            # 플레이어가 이 값을 보고 MP4 요청 없이 바로 flashErrorPage로 분기.
            # init script로 'probably'를 반환하게 속이면 MP4를 실제로 요청하고,
            # 그 요청을 위 route가 VP8 WebM으로 대체한다.
            await page.add_init_script("""
                (function() {
                    if (window.__h264OverrideApplied) return;
                    window.__h264OverrideApplied = true;
                    if (window.MediaSource && MediaSource.isTypeSupported) {
                        var _origMSE = MediaSource.isTypeSupported.bind(MediaSource);
                        MediaSource.isTypeSupported = function(type) {
                            if (type && (type.indexOf('avc') !== -1 || type.indexOf('mp4') !== -1)) return true;
                            return _origMSE(type);
                        };
                    }
                    var _origCPT = HTMLVideoElement.prototype.canPlayType;
                    HTMLVideoElement.prototype.canPlayType = function(type) {
                        if (type && (type.indexOf('mp4') !== -1 || type.indexOf('avc') !== -1 || type.indexOf('h264') !== -1)) return 'probably';
                        return _origCPT.call(this, type);
                    };
                })();
            """)
            _using_fake_video = True
            log("[0] MP4 인터셉트 (*.mp4 전체) + canPlayType 오버라이드 등록 완료")
        except Exception as e:
            log(f"[0] 더미 영상 생성 실패 ({e}) — 원본 스트림으로 계속")

    # 1. 강의 페이지로 이동
    log(f"[1] 강의 페이지 이동: {lecture_url}")

    # 네트워크 요청/응답 스니핑 (commons.ssu.ac.kr + canvas learningx 전체)
    # page 객체가 재사용되므로 리스너는 반드시 finally에서 제거해야 누적 방지
    _on_request = None
    _on_response = None
    if debug:

        def _on_request(request):
            url = request.url
            if "google-analytics" in url or "gtm" in url:
                return
            if "commons.ssu.ac.kr" in url or "learningx" in url:
                log(f"  [SNIFF→REQ] {request.method} {url}")
                if request.post_data:
                    # PII/secret 마스킹 후 200자로 제한
                    _masked = _mask_sensitive(request.post_data)
                    log(f"  [SNIFF→REQ] body={_masked[:200]!r}")

        _FULL_BODY_KEYWORDS = (
            "attendance_items",
            "content.php",
            "chapter.xml",
            "progress",
            "lessons",
            "lecture_attendance",
        )

        async def _on_response(response):
            url = response.url
            if "google-analytics" in url or "gtm" in url:
                return
            if "commons.ssu.ac.kr" in url or "learningx" in url:
                log(f"  [SNIFF←RES] {response.status} {url}")
                # body는 중요 API 또는 4xx 에러만 선별적으로 읽어 메모리 절약
                is_important = any(kw in url for kw in _FULL_BODY_KEYWORDS)
                if is_important or response.status >= 400:
                    # 바이너리 응답은 body 읽기 스킵 (읽기 실패 방지)
                    try:
                        ct = response.headers.get("content-type", "")
                    except Exception:
                        ct = ""
                    if any(ct.startswith(prefix) for prefix in _BINARY_CT):
                        try:
                            await response.dispose()
                        except Exception:
                            pass
                        return
                    try:
                        body = await response.text()
                    except Exception:
                        # 스트림 소비 완료/연결 끊김 등 — 디버그 로그이므로 무시
                        try:
                            await response.dispose()
                        except Exception:
                            pass
                        return
                    # PII/secret 마스킹 — HTML meta·data-* 속성, OAuth/CSRF 값 제거
                    _masked_body = _mask_sensitive(body)
                    # HTML 전체 덤프는 과도하므로 2000자로 절단 (4xx 는 원인 분석 위해 더 길게 1000자)
                    if response.status >= 400:
                        log(f"  [SNIFF←RES] body(4xx)={_masked_body[:1000]!r}")
                    elif _masked_body:
                        log(f"  [SNIFF←RES] body={_masked_body[:2000]!r}")
                    del body
                    del _masked_body

        page.on("request", _on_request)
        page.on("response", _on_response)

    async def _cleanup():
        nonlocal _fake_video_bytes
        if _on_request:
            try:
                page.remove_listener("request", _on_request)
            except Exception:
                pass
        if _on_response:
            try:
                page.remove_listener("response", _on_response)
            except Exception:
                pass
        if _using_fake_video:
            # B2 진단: unroute 실패가 다운로드 단계로 누출되는지 추적.
            # 실패 시 ERROR 레벨로 남겨 이후 다운로드 파일이 가짜 webm일 가능성을 알림.
            try:
                await page.unroute("**/*.mp4")
                log("[cleanup] page.unroute('**/*.mp4') 성공")
            except Exception as _unroute_e:
                log(f"[cleanup] page.unroute 실패 — 다음 다운로드에 fake webm 누출 가능: {_unroute_e}")
                from src.logger import get_logger

                get_logger("player.background").error(
                    "page.unroute('**/*.mp4') 실패 — 다운로드 단계에서 fake webm 가능: %s", _unroute_e,
                )
        # 더미 영상 바이트 즉시 해제
        _fake_video_bytes = None

    try:
        return await _play_lecture_inner(
            page,
            lecture_url,
            on_progress,
            debug,
            fallback_duration,
            log,
            state,
            _using_fake_video,
        )
    finally:
        await _cleanup()


async def _play_lecture_inner(
    page: Page,
    lecture_url: str,
    on_progress: Callable[[PlaybackState], None] | None,
    debug: bool,
    fallback_duration: float,
    log: Callable[[str], None],
    state: PlaybackState,
    _using_fake_video: bool,
) -> PlaybackState:
    """play_lecture()의 실제 재생 로직. try-finally로 _cleanup() 보장을 위해 분리."""
    # 0.5. 세션 유효성 체크 — 만료 시 대시보드 접근으로 리다이렉트 감지
    log("[0.5] 세션 상태 확인...")
    try:
        await page.goto("https://canvas.ssu.ac.kr/", wait_until="domcontentloaded", timeout=15000)
        if "login" in page.url:
            log("    → 세션 만료 감지 — 재로그인 필요")
            from src.auth.login import ensure_logged_in
            from src.config import Config

            user_id = Config.LMS_USER_ID
            password = Config.LMS_PASSWORD
            if user_id and password:
                ok = await ensure_logged_in(page, user_id, password)
                if not ok:
                    state.error = "세션 만료 후 재로그인 실패"
                    return state
                log("    → 재로그인 완료")
            else:
                log("    → 저장된 자격증명 없음 — 재로그인 불가")
                state.error = "세션 만료 — 저장된 자격증명 없음"
                return state
        else:
            log("    → 세션 유효")
    except Exception as e:
        log(f"    → 세션 확인 오류 ({e}), 계속 시도...")

    await page.goto(lecture_url, wait_until="domcontentloaded", timeout=60000)
    log(f"    → 현재 URL: {page.url}")

    # 2. 초기 플레이어 선택 화면 frame 탐색 (재생 버튼이 있는 곳)
    log("[2] 플레이어 선택 화면 frame 탐색 중...")
    player_frame = await find_player_frame(page)
    if not player_frame:
        log("    → 실패: tool_content 또는 commons.ssu.ac.kr frame 없음")
        log("    → 현재 프레임 목록:")
        for f in page.frames:
            log(f"       name={f.name!r}  url={f.url}")

        # learningx 플레이어 감지: tool_content가 learningx URL인 경우
        # learningx API에서 viewer_url(commons TargetUrl 포함)을 가져와 Plan B로 실행
        tool_frame = page.frame(name="tool_content")
        if tool_frame and "learningx" in tool_frame.url:
            log(f"    → learningx 플레이어 감지: {tool_frame.url}")
            lx_state = await _play_via_learningx_api(
                page, tool_frame.url, on_progress, log, fallback_duration,
                learningx_frame=tool_frame,
            )
            # learningx API 실패 시 (401 등) 페이지 재로드 후 commons frame 재탐색
            if lx_state.error and not lx_state.ended:
                log(f"    → learningx API 실패 ({lx_state.error}), 페이지 재로드 후 commons frame 재탐색...")
                try:
                    await page.goto(lecture_url, wait_until="domcontentloaded", timeout=60000)
                    await asyncio.sleep(3)
                    retry_frame = await find_player_frame(page)
                    if retry_frame:
                        log(f"    → commons frame 발견: {retry_frame.url}")
                        return await _play_via_progress_api(page, retry_frame.url, on_progress, log, fallback_duration)
                except Exception as retry_e:
                    log(f"    → 재로드 후 재탐색 실패: {retry_e}")
            return lx_state

        state.error = "비디오 프레임을 찾지 못했습니다."
        return state
    # frame이 나중에 navigate되면 URL이 바뀌므로 지금 즉시 저장
    player_url_snapshot = player_frame.url
    log(f"    → 성공: {player_url_snapshot}")

    # 3. 이어보기 다이얼로그 처리 (처음부터 재생)
    await asyncio.sleep(1)
    dismissed = await dismiss_dialog(player_frame, restart=True)
    log(f"[3] 이어보기 다이얼로그: {'처리됨' if dismissed else '없음'}")

    # 4. 재생 버튼 클릭
    log(f"[4] 재생 버튼({_PLAY_BTN}) 클릭 시도...")
    clicked = await click_play(player_frame)
    log(f"    → {'클릭 성공' if clicked else '버튼 없음 또는 타임아웃'}")

    # 이어보기 다이얼로그가 재생 버튼 클릭 후 뜨는 경우도 처리
    await asyncio.sleep(1)
    dismissed2 = await dismiss_dialog(player_frame, restart=True)
    log(f"[4b] 재생 후 이어보기 다이얼로그: {'처리됨' if dismissed2 else '없음'}")

    # 5. 재생 버튼 클릭 후 video 태그가 있는 frame을 새로 탐색
    log("[5] video 태그가 있는 frame 재스캔 중 (재생 후 frame 구조 변경 대응)...")
    log("    → 현재 전체 frame 목록:")
    for f in page.frames:
        log(f"       name={f.name!r}  url={f.url}")

    # video frame 탐색: 최대 10초만 기다림 (실패 시 빠르게 진단)
    frame = None
    for _ in range(10):
        for f in page.frames:
            if "commons.ssu.ac.kr" not in f.url:
                continue
            try:
                count = await f.evaluate("() => document.querySelectorAll('video').length")
                if count > 0:
                    frame = f
                    break
            except Exception:
                pass
        if frame:
            break
        await asyncio.sleep(1)

    if not frame:
        log("    → video frame 없음. 진도 API 직접 호출 방식으로 전환...")
        log(f"    → player URL: {player_url_snapshot}")
        return await _play_via_progress_api(page, player_url_snapshot, on_progress, log, fallback_duration)
    log(f"    → video frame 발견: {frame.url}")

    # 6. video 요소 duration 대기
    log(f"[6] video 요소({_VIDEO_SEL}) duration 대기 (최대 {_PLAY_TIMEOUT}초)...")
    deadline = asyncio.get_running_loop().time() + _PLAY_TIMEOUT
    while asyncio.get_running_loop().time() < deadline:
        info = await _get_video_state(frame)
        if debug:
            log(f"    poll: info={info}")
        if info and info["duration"] > 0:
            log(f"    → 영상 시작 확인: duration={info['duration']:.1f}s")
            break
        await asyncio.sleep(0.5)
    else:
        log("[6] 타임아웃. 페이지 상태 진단:")
        await _debug_page_state(page, frame, log)
        # video error=4 (MEDIA_ERR_SRC_NOT_SUPPORTED) 등으로 영상 로드 실패 시
        # Plan B(progress API)로 전환 — commons frame URL 사용 (sl=1 포함)
        plan_b_url = frame.url if frame and "commons.ssu.ac.kr" in frame.url else player_url_snapshot
        log(f"[6] 영상 로드 실패 → Plan B(진도 API) 전환 시도 (url={plan_b_url[:80]}...)")
        try:
            return await _play_via_progress_api(page, plan_b_url, on_progress, log, fallback_duration)
        except Exception as plan_b_e:
            log(f"[6] Plan B 전환 실패: {plan_b_e}")
            state.error = "영상이 시작되지 않았습니다."
            return state

    # 6.5. GetCurrentTime/GetTotalDuration 오버라이드
    # 가짜 WebM 재생 시 GetCurrentTime()이 apiManager 내부 상태(=0)를 반환해
    # afterTimeUpdate의 2초 진행 조건이 충족되지 않아 진도 API가 호출되지 않는 문제 수정.
    # video 요소에서 직접 읽도록 오버라이드하면 실제 재생 시간이 반영되어 진도 보고가 동작한다.
    if _using_fake_video:
        try:
            await frame.evaluate(
                """(sel) => {
                if (typeof GetCurrentTime !== 'undefined') {
                    GetCurrentTime = function() {
                        var v = document.querySelector(sel);
                        return v ? v.currentTime : 0;
                    };
                }
                if (typeof GetTotalDuration !== 'undefined') {
                    GetTotalDuration = function() {
                        var v = document.querySelector(sel);
                        return v ? v.duration : 0;
                    };
                }
                // sendPlayedTime 교체:
                // 원본 함수는 GetCumulativePlayedPage() = 10000000000000 (apiManager 비정상값)을
                // 그대로 URL에 포함 → 서버 400. 전역 접근 가능하므로 올바른 파라미터로 재구성한다.
                if (typeof sendPlayedTime !== 'undefined') {
                    sendPlayedTime = function(stateVal) {
                        if (typeof lms_url === 'undefined' || !lms_url) return;
                        var v = document.querySelector(sel);
                        if (!v) return;
                        var curTime = v.currentTime;
                        var totalPage = typeof GetTotalPage !== 'undefined' ? GetTotalPage() : 14;
                        var cumPage = Math.max(1, Math.ceil(curTime / v.duration * totalPage));
                        var ts = Date.now();
                        var cbName = 'jQuery111_' + ts;
                        var sep = lms_url.indexOf('?') >= 0 ? '&' : '?';
                        var url = lms_url + sep +
                            'callback=' + cbName +
                            '&state=' + stateVal +
                            '&duration=' + v.duration.toFixed(2) +
                            '&currentTime=' + curTime.toFixed(2) +
                            '&cumulativeTime=' + curTime.toFixed(2) +
                            '&page=' + cumPage +
                            '&totalpage=' + totalPage +
                            '&cumulativePage=' + cumPage +
                            '&_=' + ts;
                        window[cbName] = function(d) { delete window[cbName]; if (s && s.parentNode) s.parentNode.removeChild(s); };
                        var s = document.createElement('script');
                        s.src = url;
                        s.onerror = function() { delete window[cbName]; if (s && s.parentNode) s.parentNode.removeChild(s); };
                        document.head.appendChild(s);
                        setTimeout(function() { if (window[cbName]) { delete window[cbName]; if (s && s.parentNode) s.parentNode.removeChild(s); } }, 10000);
                    };
                }
                // isPlayedContent: 플레이어가 "재생 시작" 이벤트로 설정하는 플래그.
                // 가짜 WebM에서는 apiManager가 이 이벤트를 발생시키지 않으므로 강제로 true로 설정.
                if (typeof isPlayedContent !== 'undefined') {
                    isPlayedContent = true;
                }
                // afterPlayStateChange: 재생 시작 이벤트 강제 전송.
                // 서버가 START(play) 이벤트 수신 후에만 UPDATE 요청을 수락하는 경우 대비.
                // 가짜 WebM에서는 apiManager가 play state change를 발생시키지 않으므로 수동 호출.
                try {
                    if (typeof afterPlayStateChange === 'function') {
                        afterPlayStateChange('play');
                    }
                } catch(e) {}
            }""",
                _VIDEO_SEL,
            )
            log("[6.5] GetCurrentTime / GetTotalDuration 오버라이드 + isPlayedContent = true 설정 완료")
        except Exception as e:
            log(f"[6.5] 오버라이드 실패: {e}")

    # 6.6. lms_url / total_page 추출
    # 진도 API를 page 컨텍스트(canvas.ssu.ac.kr, 동일 오리진)에서 직접 호출하기 위해
    # commons frame에서 lms_url을 읽어 Python 변수로 저장한다.
    _lms_url: str = ""
    _total_page: int = 14
    if _using_fake_video:
        try:
            _lms_url = await frame.evaluate("() => typeof lms_url !== 'undefined' ? lms_url : ''")
            _total_page = int(await frame.evaluate("() => typeof GetTotalPage !== 'undefined' ? GetTotalPage() : 14"))
            log(f"[6.6] lms_url={_lms_url[:80]!r}... total_page={_total_page}")
        except Exception as e:
            log(f"[6.6] lms_url 추출 실패: {e}")

    if debug:
        try:
            js_info = await frame.evaluate("""() => {
                var funcs = [];
                if (window.apiManager) {
                    Object.keys(window.apiManager).forEach(function(k) {
                        if (typeof window.apiManager[k] === 'function') funcs.push(k);
                    });
                }
                return JSON.stringify({
                    afterTimeUpdate: typeof afterTimeUpdate,
                    afterTimeUpdateFull: typeof afterTimeUpdate !== 'undefined'
                        ? afterTimeUpdate.toString() : null,
                    afterPlayStateChange: typeof afterPlayStateChange,
                    apiManagerType: typeof window.apiManager,
                    apiManagerFunctions: funcs.slice(0, 30),
                    launcherType: typeof window.launcher,
                    playTime: typeof play_time !== 'undefined' ? play_time : 'undefined',
                    lmsUrl: typeof lms_url !== 'undefined'
                        ? (lms_url.length > 0 ? lms_url.slice(0, 120) : '(empty)') : 'undefined',
                    getCurrentTimeResult: typeof GetCurrentTime !== 'undefined'
                        ? GetCurrentTime() : 'undefined',
                    getTotalDurationResult: typeof GetTotalDuration !== 'undefined'
                        ? GetTotalDuration() : 'undefined',
                    isPlayedContent: typeof isPlayedContent !== 'undefined'
                        ? isPlayedContent : 'undefined(closure?)',
                    percentStep1: typeof PERCENT_STEP1 !== 'undefined'
                        ? PERCENT_STEP1 : 'undefined(closure?)',
                    percentStep2: typeof PERCENT_STEP2 !== 'undefined'
                        ? PERCENT_STEP2 : 'undefined(closure?)',
                    isPercentStep1Complete: typeof isPercentStep1Complete !== 'undefined'
                        ? isPercentStep1Complete : 'undefined(closure?)',
                    sendPlayedTimeDefined: typeof sendPlayedTime !== 'undefined',
                    afterPlayStateChangeFull: typeof afterPlayStateChange !== 'undefined'
                        ? afterPlayStateChange.toString() : null,
                });
            }""")
            log(f"  [진단] 플레이어 JS 상태: {js_info}")
        except Exception as e:
            log(f"  [진단] 플레이어 JS 상태 조회 실패: {e}")

    # 7. 재생 완료까지 폴링
    log("[7] 재생 루프 시작")
    _AFTER_UPDATE_INTERVAL = 30.0  # afterTimeUpdate 수동 호출 주기 (초)
    _last_after_update = asyncio.get_running_loop().time() - _AFTER_UPDATE_INTERVAL  # 즉시 첫 호출
    while True:
        info = await _get_video_state(frame)
        if info is None:
            # frame이 언로드된 경우
            log("[7] video state가 None — frame 언로드됨")
            break

        state.current = info["current"]
        state.duration = info["duration"]
        state.ended = info["ended"]

        if on_progress:
            on_progress(state)

        if info["ended"]:
            log("[7] 영상 ended=True — 완료")
            break

        # duration - threshold 이상 재생됐으면 완료로 간주
        if state.duration > 0 and state.current >= state.duration - _END_THRESHOLD:
            state.ended = True
            if on_progress:
                on_progress(state)
            log("[7] 재생 완료 기준 도달")
            break

        # 일시정지 상태면 강제 재생 (LMS 자동 정지 방지)
        if info["paused"]:
            log("[7] 일시정지 감지 → 강제 재생")
            await _ensure_playing(frame)

        # 30초마다 afterTimeUpdate() 수동 호출
        # 가짜 WebM 재생 시 apiManager가 timeupdate 이벤트를 발생시키지 않아
        # afterTimeUpdate가 자동으로 호출되지 않는 경우를 보완한다.
        # afterTimeUpdate는 commons frame 내에서 sl=1 세션 컨텍스트로 실행되므로
        # 직접 진도 API를 호출해도 ErrAlreadyInView가 발생하지 않는다.
        if _using_fake_video:
            now = asyncio.get_running_loop().time()
            if now - _last_after_update >= _AFTER_UPDATE_INTERVAL:
                # ── 진도 API를 page 컨텍스트(canvas.ssu.ac.kr, 동일 오리진)에서 fetch ──
                # frame 컨텍스트(commons.ssu.ac.kr)에서 script 태그로 호출하면
                # 크로스오리진 요청이 되어 SameSite 쿠키가 전송되지 않음 → 빈 400.
                # page 컨텍스트는 canvas.ssu.ac.kr 동일 오리진이므로 쿠키가 자동 포함된다.
                if _lms_url and state.duration > 0:
                    cur = state.current
                    dur = state.duration
                    cum_page = max(1, math.ceil(cur / dur * _total_page))
                    progress_url, _ = _build_progress_url(
                        _lms_url,
                        state=8,
                        duration_str=f"{dur:.2f}",
                        current_str=f"{cur:.2f}",
                        page=cum_page,
                        total_page=_total_page,
                        cumulative_page=cum_page,
                        callback_prefix="_cb_",
                    )
                    try:
                        result = await page.evaluate(f"""
                            async () => {{
                                try {{
                                    const resp = await fetch({json.dumps(progress_url)});
                                    return {{s: resp.status, b: (await resp.text()).slice(0, 200)}};
                                }} catch(e) {{
                                    return {{s: -1, b: e.message}};
                                }}
                            }}
                        """)
                        log(
                            f"[7] 진도 API (page ctx): {result.get('s')} "
                            f"{result.get('b', '')!r} "
                            f"({cur:.0f}s / {dur:.0f}s)"
                        )
                    except Exception as e:
                        log(f"[7] 진도 API (page ctx) 실패: {e}")

                # ── afterTimeUpdate: play_time 상태 유지용 ──
                try:
                    await frame.evaluate("""() => {
                        try { isPlayedContent = true; } catch(e) {}
                        try {
                            // play_time을 현재 시간 직전으로 리셋:
                            // afterTimeUpdate의 seek 분기 조건 |cur - play_time| > 2 우회.
                            if (typeof play_time !== 'undefined' && typeof GetCurrentTime !== 'undefined') {
                                play_time = Math.max(0, GetCurrentTime() - 1);
                            }
                        } catch(e) {}
                        if (typeof afterTimeUpdate === 'function') afterTimeUpdate();
                    }""")
                    log(f"[7] afterTimeUpdate() 호출 ({state.current:.0f}s / {state.duration:.0f}s)")
                except Exception as e:
                    log(f"[7] afterTimeUpdate() 실패: {e}")
                _last_after_update = now

        await asyncio.sleep(_POLL_INTERVAL)

    # Plan A가 예상보다 훨씬 일찍 끝난 경우 (fake webm 고속 재생 등)
    # duration의 50% 미만에서 ended되면 Plan B로 전환해 progress API를 직접 호출한다.
    if state.ended and state.duration > 0 and state.current < state.duration * 0.5:
        log(f"[7] 영상이 예상보다 일찍 종료 ({state.current:.1f}s / {state.duration:.1f}s) — Plan B로 전환")
        return await _play_via_progress_api(page, player_url_snapshot, on_progress, log, fallback_duration)

    # Plan A 완료 후 progress API에 100% 직접 보고
    # 플레이어 JS가 가짜 WebM 재생 중 progress API를 호출하지 않는 경우 대비
    await _report_completion(page, player_url_snapshot, state.duration, log, use_page_eval=True)

    return state
