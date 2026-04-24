"""SSO 로그인 유틸. Playwright async API 기반."""

from __future__ import annotations

import logging

from playwright.async_api import Page

_log = logging.getLogger(__name__)


async def perform_login(page: Page, username: str, password: str) -> bool:
    """SSO 로그인 처리. 성공 시 True, 실패 시 False 반환.

    LOG-008: 실패 경로에서 exception 을 조용히 삼켰던 것을 warning 로그로
    변경해 네트워크 오류·셀렉터 변경·페이지 구조 변경 원인 추적이 가능하게 한다.
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


async def ensure_logged_in(page: Page, username: str, password: str) -> bool:
    """현재 페이지가 로그인 페이지이면 로그인을 수행."""
    if "login" not in page.url:
        return True
    return await perform_login(page, username, password)
