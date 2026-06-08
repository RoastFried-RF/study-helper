"""background_player 의 순수 판정 헬퍼 단위 테스트.

이 헬퍼들은 거대한 async 재생 함수에서 추출한 SSOT 로직이라, 실제 재생 루프를
띄우지 않고도 회귀를 잡을 수 있다.
"""

from src.player.background_player import (
    _is_play_complete,
    _parse_duration,
    is_browser_dead_exception,
)

# ── TEST-002: 브라우저 death 판정 (SSOT) ──────────────────────────

class TestIsBrowserDeadException:
    def test_connection_closed_is_dead(self):
        assert is_browser_dead_exception(Exception("Connection closed while reading from the driver"))

    def test_case_insensitive(self):
        assert is_browser_dead_exception(Exception("CONNECTION CLOSED"))

    def test_browser_context_new_page_is_dead(self):
        assert is_browser_dead_exception(Exception("BrowserContext.new_page: Connection closed"))

    def test_target_closed_is_dead(self):
        assert is_browser_dead_exception(
            Exception("Target page, context or browser has been closed")
        )

    def test_unrelated_error_not_dead(self):
        assert not is_browser_dead_exception(Exception("404 Not Found"))

    def test_value_error_not_dead(self):
        assert not is_browser_dead_exception(ValueError("could not convert string to float"))

    def test_auto_alias_is_same_object(self):
        """auto.py 의 _is_browser_dead_exception 별칭이 SSOT 와 동일 객체여야 한다
        (재포크되어 분기되지 않도록 핀)."""
        import src.ui.auto as auto

        assert auto._is_browser_dead_exception is is_browser_dead_exception


# ── TEST-004: Plan B duration 안전 파싱 (M2b) ─────────────────────

class TestParseDuration:
    def test_numeric_string(self):
        assert _parse_duration("123.4") == 123.4

    def test_int(self):
        assert _parse_duration(60) == 60.0

    def test_non_numeric_falls_back_to_zero(self):
        """비숫자 문자열은 ValueError 로 죽지 않고 0.0 (fallback 위임)."""
        assert _parse_duration("N/A") == 0.0

    def test_none_falls_back_to_zero(self):
        assert _parse_duration(None) == 0.0

    def test_empty_string_falls_back_to_zero(self):
        assert _parse_duration("") == 0.0


# ── TEST-005: Plan A 재생 완료 판정 (M2b) ─────────────────────────

class TestIsPlayComplete:
    def test_short_video_zero_current_not_complete(self):
        """초단 영상(duration<=threshold)은 current=0 에도 완료로 오판하면 안 된다."""
        assert not _is_play_complete(duration=2.0, current=0.0)
        assert not _is_play_complete(duration=3.0, current=0.0)

    def test_long_video_near_end_complete(self):
        assert _is_play_complete(duration=2580.0, current=2578.0)

    def test_long_video_mid_not_complete(self):
        assert not _is_play_complete(duration=2580.0, current=100.0)

    def test_zero_duration_not_complete(self):
        """duration 이 아직 0(첫 폴링)이면 완료 아님 — 실제 ended 신호에 맡긴다."""
        assert not _is_play_complete(duration=0.0, current=0.0)
