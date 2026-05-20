"""updater.py 단위 테스트 — NF-01 버전 비교 회귀 방지.

NF-01 버그 시나리오: `_VERSION_RE` 가 3-숫자 세그먼트만 허용해
`current_version` 이 `"unknown"`(CHANGELOG.md 누락) 이거나 사전릴리스/
4자리 태그면 파싱에 실패한다. 수정 전에는 이때 `check_update` 가 silent
하게 None 을 반환해 업데이트 알림이 사라졌다. 수정 후에는 "버전 판정 불가"
를 로그로 남기고 latest 를 그대로 반환해 사용자가 최신 버전을 인지한다.

`check_update` 는 네트워크(`fetch_latest_version`) 를 타므로 모든 테스트는
`fetch_latest_version` 을 patch 해 결정론적으로 검증한다.
"""

from __future__ import annotations

from unittest.mock import patch

from src.updater import _parse_version, check_update


def _patch_latest(tag: str | None):
    """`fetch_latest_version` 이 고정 태그를 반환하도록 patch."""
    return patch("src.updater.fetch_latest_version", return_value=tag)


# ── NF-01: current_version 파싱 불가 시 latest 안내 ─────────────────


def test_check_update_unknown_current_returns_latest() -> None:
    """`"unknown"` current_version 은 파싱 불가 → latest 를 반환해야 한다.

    수정 전에는 None(알림 누락) — 수정 후에는 사용자에게 최신 버전 안내.
    """
    with _patch_latest("v1.2.0"):
        assert check_update("unknown") == "v1.2.0"


def test_check_update_prerelease_current_returns_latest() -> None:
    """사전릴리스 태그(`1.2.0-rc1`) 도 3-숫자 정규식 탈락 → latest 반환."""
    with _patch_latest("v1.3.0"):
        assert check_update("1.2.0-rc1") == "v1.3.0"


def test_check_update_four_segment_current_returns_latest() -> None:
    """4자리 태그(`1.2.0.4`) 도 파싱 불가 → latest 반환."""
    with _patch_latest("v2.0.0"):
        assert check_update("1.2.0.4") == "v2.0.0"


def test_check_update_empty_current_returns_latest() -> None:
    """빈 문자열 current_version 도 latest 안내."""
    with _patch_latest("v1.0.1"):
        assert check_update("") == "v1.0.1"


# ── 정상 비교 경로 (회귀 방지) ──────────────────────────────────────


def test_check_update_newer_latest_returns_latest() -> None:
    """latest 가 current 보다 높으면 latest 반환."""
    with _patch_latest("v1.5.0"):
        assert check_update("1.4.0") == "v1.5.0"


def test_check_update_same_version_returns_none() -> None:
    """latest 와 current 가 같으면 None (알림 불필요)."""
    with _patch_latest("v1.4.0"):
        assert check_update("1.4.0") is None


def test_check_update_older_latest_returns_none() -> None:
    """latest 가 current 보다 낮으면 None."""
    with _patch_latest("v1.3.0"):
        assert check_update("1.4.0") is None


def test_check_update_fetch_failure_returns_none() -> None:
    """latest 조회 실패(None) 시 — current 가 unknown 이어도 None."""
    with _patch_latest(None):
        assert check_update("unknown") is None


def test_check_update_unparseable_latest_returns_none() -> None:
    """latest 자체가 파싱 불가하면 비교 불가 → None."""
    with _patch_latest("garbage-tag"):
        assert check_update("1.0.0") is None


def test_check_update_logs_when_current_unparseable() -> None:
    """NF-01: current 판정 불가 시 silent 가 아니라 경고 로그를 남긴다."""
    with _patch_latest("v1.2.0"):
        with patch("src.logger.get_logger") as mock_get_logger:
            result = check_update("unknown")
        assert result == "v1.2.0"
        mock_get_logger.assert_called_once_with("updater")
        mock_get_logger.return_value.warning.assert_called_once()


# ── _parse_version 단위 검증 ──────────────────────────────────────


def test_parse_version_accepts_three_segment() -> None:
    assert _parse_version("1.2.3") == (1, 2, 3)
    assert _parse_version("v1.2.3") == (1, 2, 3)


def test_parse_version_rejects_unknown() -> None:
    assert _parse_version("unknown") is None


def test_parse_version_rejects_prerelease_and_four_segment() -> None:
    assert _parse_version("1.2.3-rc1") is None
    assert _parse_version("1.2.3.4") is None
