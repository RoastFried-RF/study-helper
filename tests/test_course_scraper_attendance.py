"""출석 상태 공통 클래스가 개별 상태를 가리지 않는지 검증한다."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.scraper.course_scraper import CourseScraper


@pytest.mark.parametrize(
    ("classes", "expected"),
    [
        ("xnmb-attendance_status absent", "absent"),
        ("xnmb-attendance_status late", "late"),
        ("xnmb-attendance_status excused", "excused"),
        ("xnmb-attendance_status attendance", "attendance"),
        ("xnmb-attendance_status", "attendance"),
        (None, "none"),
    ],
)
async def test_parse_item_attendance(classes, expected):
    title = SimpleNamespace(
        text_content=AsyncMock(return_value="강의"), get_attribute=AsyncMock(return_value="/lecture/1")
    )
    attendance = None if classes is None else SimpleNamespace(get_attribute=AsyncMock(return_value=classes))
    elements = {
        "a.xnmb-module_item-left-title": title,
        "[class*='attendance_status']": attendance,
    }
    element = SimpleNamespace(query_selector=AsyncMock(side_effect=elements.get))
    scraper = CourseScraper(username="test", password="test")

    item = await scraper._parse_item(element)

    assert item is not None
    assert item.attendance == expected
    if attendance is not None:
        attendance.get_attribute.assert_awaited_once_with("class")
