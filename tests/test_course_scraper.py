"""CourseScraper 의 강의 렌더 안정화 대기 로직 테스트.

`_wait_for_lecture_render` 는 LMS SPA 가 강의 항목을 먼저 그리고 완료/출석
상태 배지를 비동기 후속 렌더하는 것에 대응한다. 항목 수만으로 대기하면
배지 미렌더 상태로 파싱해 completion 을 incomplete 로 오판정한다(강의 수가
많은 과목에서 빈발 — "미시청" 카운트 오표시 버그의 root cause).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.scraper.course_scraper import CourseScraper


def _make_scraper() -> CourseScraper:
    return CourseScraper("test-id", "test-pw")


@pytest.mark.asyncio
async def test_wait_for_lecture_render_returns_when_stable():
    """항목 수·배지 수가 처음부터 안정이면 stable_checks 후 곧바로 반환."""
    scraper = _make_scraper()
    iframe = AsyncMock()
    # items / markers 모두 매 호출 5개 고정 → snapshot (5,5) 안정
    iframe.query_selector_all = AsyncMock(side_effect=lambda *a, **k: [object()] * 5)

    # timeout 에 걸리지 않고 정상 반환되면 통과
    await scraper._wait_for_lecture_render(
        iframe, timeout=2.0, stable_checks=3, interval=0.01
    )


@pytest.mark.asyncio
async def test_wait_for_lecture_render_waits_for_late_badges():
    """항목은 일찍 그려져도 상태 배지가 늦게 채워지면 배지 안정까지 대기.

    items 는 10 고정, markers 는 2→6→10 으로 증가 후 10 에서 안정.
    배지가 안정되기 전(2,6)에 반환하면 안 된다.
    """
    scraper = _make_scraper()
    iframe = AsyncMock()
    # (items, markers) 쌍 — 한 iteration 당 query_selector_all 2회 호출
    counts = iter([
        10, 2,    # iter1 — 배지 렌더 시작
        10, 6,    # iter2 — 배지 증가 중
        10, 10,   # iter3 — 배지 완료 (stable=0, prev 갱신)
        10, 10,   # iter4 — stable=1
        10, 10,   # iter5 — stable=2
        10, 10,   # iter6 — stable=3 → 반환
    ])
    iframe.query_selector_all = AsyncMock(
        side_effect=lambda *a, **k: [object()] * next(counts)
    )

    await scraper._wait_for_lecture_render(
        iframe, timeout=5.0, stable_checks=3, interval=0.01
    )
    # counts 가 정확히 6쌍 소진되어야 함 (배지 안정 전 조기 반환 없음)
    with pytest.raises(StopIteration):
        next(counts)


@pytest.mark.asyncio
async def test_wait_for_lecture_render_timeout_graceful():
    """렌더가 끝없이 변해도 timeout 후 예외 없이 graceful 반환."""
    scraper = _make_scraper()
    iframe = AsyncMock()
    grow = {"n": 0}

    def _grow(*a, **k):
        grow["n"] += 1
        return [object()] * grow["n"]  # 매번 증가 → 절대 안정 안 됨

    iframe.query_selector_all = AsyncMock(side_effect=_grow)

    # timeout 도달 시 RuntimeError 등 예외 없이 반환되어야 함
    await scraper._wait_for_lecture_render(
        iframe, timeout=0.3, stable_checks=3, interval=0.05
    )


@pytest.mark.asyncio
async def test_wait_for_lecture_render_ignores_zero_items():
    """항목이 0개인 동안은 안정으로 보지 않는다 (빈 DOM 조기 반환 방지)."""
    scraper = _make_scraper()
    iframe = AsyncMock()
    counts = iter([
        0, 0,    # iter1 — 아직 빈 DOM
        0, 0,    # iter2 — 여전히 빈 DOM (items==0 이라 stable 누적 안 함)
        7, 7,    # iter3
        7, 7,    # iter4
        7, 7,    # iter5
        7, 7,    # iter6 → stable=3
    ])
    iframe.query_selector_all = AsyncMock(
        side_effect=lambda *a, **k: [object()] * next(counts)
    )

    await scraper._wait_for_lecture_render(
        iframe, timeout=5.0, stable_checks=3, interval=0.01
    )
    # 0,0 구간에서 조기 반환하지 않고 7,7 안정까지 전부 소진
    with pytest.raises(StopIteration):
        next(counts)
