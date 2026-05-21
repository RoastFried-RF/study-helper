"""util/url.py 단위 테스트 — safe_url 의 민감정보 제거 회귀 방지.

NF-05 버그 시나리오: `safe_url` 이 query/fragment 만 제거하고 netloc 의
userinfo(`user:pass@host`)는 그대로 남겨 로그에 자격증명이 노출됐다.
수정 후에는 userinfo 가 제거되고 query/fragment 제거 동작은 유지된다.
"""

from __future__ import annotations

from src.util.url import safe_url


# ── NF-05: userinfo 제거 ──────────────────────────────────────────


def test_safe_url_strips_userinfo() -> None:
    """`user:pass@host` 형태의 userinfo 가 제거돼야 한다."""
    cleaned = safe_url("https://user:pass@host.example.com/path")
    assert "user" not in cleaned
    assert "pass" not in cleaned
    assert "host.example.com" in cleaned
    assert cleaned == "https://host.example.com/path"


def test_safe_url_strips_username_only_userinfo() -> None:
    """비밀번호 없이 username 만 있는 userinfo 도 제거돼야 한다."""
    cleaned = safe_url("https://secrettoken@host.example.com/path")
    assert "secrettoken" not in cleaned
    assert cleaned == "https://host.example.com/path"


def test_safe_url_preserves_port_after_userinfo_strip() -> None:
    """userinfo 를 떼어내도 host:port 는 유지돼야 한다."""
    cleaned = safe_url("https://user:pass@host.example.com:8443/path")
    assert "user" not in cleaned
    assert "pass" not in cleaned
    assert "host.example.com:8443" in cleaned


# ── query / fragment 제거 (기존 동작 유지 회귀 방지) ────────────────


def test_safe_url_strips_query() -> None:
    """세션 토큰이 담길 수 있는 query string 이 제거돼야 한다."""
    cleaned = safe_url("https://host.example.com/v?token=abc123&sid=xyz")
    assert "token" not in cleaned
    assert "abc123" not in cleaned
    assert cleaned == "https://host.example.com/v"


def test_safe_url_strips_fragment() -> None:
    """fragment 가 제거돼야 한다."""
    cleaned = safe_url("https://host.example.com/v#secret-fragment")
    assert "secret-fragment" not in cleaned
    assert cleaned == "https://host.example.com/v"


def test_safe_url_strips_userinfo_and_query_together() -> None:
    """userinfo + query + fragment 가 동시에 있어도 모두 제거."""
    cleaned = safe_url("https://user:pass@host.example.com/v?token=abc#frag")
    assert "user" not in cleaned
    assert "pass" not in cleaned
    assert "token" not in cleaned
    assert "abc" not in cleaned
    assert "frag" not in cleaned
    assert cleaned == "https://host.example.com/v"


def test_safe_url_plain_url_unchanged() -> None:
    """민감 정보가 없는 URL 은 경로까지 그대로 유지된다."""
    url = "https://host.example.com/courses/123/video.mp4"
    assert safe_url(url) == url
