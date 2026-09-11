"""SSO 로그인 유틸. Playwright async API 기반."""

from __future__ import annotations

from playwright.async_api import Page

from src.logger import get_logger

# R2-01 / LOG-SYS-1: logging.getLogger(__name__) 는 root 핸들러 부재로
# silent log loss + SensitiveFilter 우회. 앱 전역 로거 트리에 귀속한다.
_log = get_logger("auth.login")


async def perform_login(page: Page, username: str, password: str) -> bool:
    """SSO 로그인 처리. 성공 시 True, 실패 시 False 반환.

    LOG-008: 실패 경로에서 exception 을 조용히 삼켰던 것을 warning 로그로
    변경해 네트워크 오류·셀렉터 변경·페이지 구조 변경 원인 추적이 가능하게 한다.

    SEC-011: 반환 직전에 password 지역 변수를 덮어쓰고 del 하여 heap 잔존 window
    를 단축한다. CPython 에서는 참조 카운트 기반 GC 이므로 완전한 zeroize 는
    보장되지 않지만(다른 레퍼런스/internal copy 가 있을 수 있음) best-effort 로 수행.
    """
    try:
        # 2026-09 LMS 개편: canvas 접속 시 '로그인 할 사이트 선택'(canvas-discovery)
        # 페이지가 먼저 뜬다. '숭실대학교'(a.btn-ssu-main) 를 눌러 SSO 선택 페이지로
        # 진입한다(디스커버리가 없으면 기존 플로우 그대로).
        discovery = await page.query_selector("a.btn-ssu-main")
        if discovery:
            await discovery.click()
            await page.wait_for_load_state("networkidle")

        # SSO 선택 페이지(xn-sso/login.php)의 '통합 로그인'(.login_btn a) 링크로
        # smartid(smln.asp) 폼에 진입한다 — 개편 전후 공통 스텝.
        login_button = await page.query_selector(".login_btn a")
        if login_button:
            await login_button.click()
            await page.wait_for_load_state("networkidle")

        # smartid 구형 폼(#userid) 이 기본이나, 향후 변형(#login_user_id) 도 병행 대기.
        await page.wait_for_selector("input#userid, input#login_user_id", timeout=30_000)
        if await page.query_selector("input#userid"):
            # smartid Symtra SSO 폼 (2026-09 실측 유효)
            await page.fill("input#userid", username)
            await page.fill("input#pwd", password)
            await page.click("a.btn_login")
        else:
            # xn-sso 일반 로그인 계열 폼 폴백
            await page.fill("input#login_user_id", username)
            await page.fill("input#login_user_password", password)
            await page.click("#general_login_btn")

        # 성공 판정은 positive check — canvas 도메인 복귀를 직접 기다린다 (감사 지적 반영).
        # (a) 종전 `"login" in url` negative check 는 smln.asp 재표시(비밀번호 오류)를
        #     성공으로 오판했고, (b) expect_navigation(networkidle) 은 상시 XHR 이 도는
        #     대시보드/중간 콜백에서 navigation 완료를 못 보고 타임아웃난다.
        try:
            await page.wait_for_url(lambda u: "canvas.ssu.ac.kr" in u, timeout=30_000)
        except Exception:
            pass
        if "canvas.ssu.ac.kr" not in page.url:
            _log.warning("로그인 실패: 제출 후에도 SSO/login 페이지 체류 (%s)", page.url)
            return False

        await page.wait_for_load_state("load")
        return True

    except Exception as e:
        _log.warning("로그인 실패: %s: %s", type(e).__name__, e)
        return False
    finally:
        # SEC-011: password 참조 조기 해제.
        # 주의: Python str 은 immutable 이라 `password = "..."` 는 새 객체를 만들어
        # 이름을 rebind 할 뿐 원본 string 의 heap 바이트는 GC 시점까지 잔존한다.
        # 또한 page.fill("input#pwd", password) 로 Playwright 가 이미 자체 사본을
        # 보유한 상태이므로 Python 측 zeroize 의 실효성은 제한적이다.
        # 실질 효과는 지역 변수 참조를 스택 프레임에서 즉시 제거하는 것에 한정.
        try:
            del password
        except NameError:
            pass


async def ensure_logged_in(page: Page, username: str, password: str) -> bool:
    """현재 페이지가 로그인 페이지이면 로그인을 수행."""
    if "login" not in page.url:
        return True
    return await perform_login(page, username, password)
