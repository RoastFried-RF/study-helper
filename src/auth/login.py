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
        login_button = await page.query_selector(".login_btn a")
        if login_button:
            await login_button.click()
            await page.wait_for_load_state("networkidle")

        await page.fill("input#userid", username)
        await page.fill("input#pwd", password)

        async with page.expect_navigation(wait_until="networkidle"):
            await page.click("a.btn_login")

        if "login" in page.url:
            _log.warning("로그인 실패: 제출 후에도 여전히 login 페이지 (%s)", page.url)
            return False

        await page.wait_for_load_state("networkidle")
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
