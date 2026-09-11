"""telegram_notifier.verify_bot 회귀 테스트 (API-F4 / API-F5).

`docs/process-logic-review.md`:
- API-F4: `verify_bot` 이 텔레그램 5xx/네트워크 장애를 `INVALID_CHAT_ID` 로
  오분류하던 버그. 5xx → `TELEGRAM_API_ERROR`, network → `NETWORK_ERROR` 로
  구분돼야 한다.
- API-F5: 빈 `chat_id` 는 텔레그램 API 왕복 없이 즉시 `INVALID_CHAT_ID`.

`requests` 를 mock 해 실제 텔레그램 API 호출 없이 사유 분류만 검증한다.
재시도 backoff(`time.sleep`)도 patch 해 테스트를 지연 없이 실행한다.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import requests as _real_requests

# 형식상 유효한 봇 토큰 (숫자:영문숫자_하이픈) — _validate_token 통과용.
_VALID_TOKEN = "123456789:ABCdefGHIjklMNOpqrSTUvwxYZ0123456789"


def _resp(status_code: int, json_body: dict | None = None) -> MagicMock:
    """requests.Response 모의 객체.

    `.ok` 는 status_code < 400 으로 계산하고, `.json()` / `.close()` 도 제공.
    """
    m = MagicMock()
    m.status_code = status_code
    m.ok = status_code < 400
    m.json.return_value = json_body if json_body is not None else {}
    m.text = ""
    m.close = MagicMock()
    return m


# ─────────────────────────────────────────────────────────────────────
# API-F5: 빈 chat_id 선검증 — 텔레그램 API 왕복 없이 즉시 INVALID_CHAT_ID
# ─────────────────────────────────────────────────────────────────────


def test_verify_bot_empty_chat_id_returns_invalid_chat_id():
    """API-F5: 빈 chat_id 는 getMe/sendMessage 호출 전에 INVALID_CHAT_ID 로 거부."""
    from src.notifier import telegram_notifier

    with patch.object(telegram_notifier, "requests") as mock_requests:
        ok, error = telegram_notifier.verify_bot(_VALID_TOKEN, "")

        assert ok is False
        assert error == "INVALID_CHAT_ID"
        # 텔레그램 API 왕복이 전혀 발생하지 않아야 한다 (선검증).
        mock_requests.get.assert_not_called()
        mock_requests.post.assert_not_called()


def test_verify_bot_invalid_token_format_returns_format_error():
    """형식이 깨진 토큰은 API 왕복 없이 INVALID_TOKEN_FORMAT."""
    from src.notifier import telegram_notifier

    with patch.object(telegram_notifier, "requests") as mock_requests:
        ok, error = telegram_notifier.verify_bot("not-a-valid-token", "12345")

        assert ok is False
        assert error == "INVALID_TOKEN_FORMAT"
        mock_requests.get.assert_not_called()
        mock_requests.post.assert_not_called()


# ─────────────────────────────────────────────────────────────────────
# API-F4: 텔레그램 5xx 응답 → TELEGRAM_API_ERROR (INVALID_CHAT_ID 아님)
# ─────────────────────────────────────────────────────────────────────


def test_verify_bot_sendmessage_5xx_returns_telegram_api_error():
    """API-F4: getMe 성공 후 sendMessage 가 5xx 응답 → TELEGRAM_API_ERROR.

    버그 시나리오: 5xx 서버 장애를 INVALID_CHAT_ID 로 오분류 → 사용자가
    멀쩡한 chat_id 를 잘못된 것으로 오인.
    """
    from src.notifier import telegram_notifier

    getme_ok = _resp(200, {"ok": True, "result": {"username": "study_helper_bot"}})
    send_5xx = _resp(503)

    with patch.object(telegram_notifier, "requests") as mock_requests, patch.object(telegram_notifier.time, "sleep"):
        mock_requests.get.return_value = getme_ok
        mock_requests.post.return_value = send_5xx
        # _send_message_verify 의 `except requests.exceptions.RequestException` 가
        # 유효한 예외 클래스를 참조하도록 실제 requests.exceptions 를 보존한다.
        mock_requests.exceptions = _real_requests.exceptions

        ok, error = telegram_notifier.verify_bot(_VALID_TOKEN, "98765")

        assert ok is False
        assert error == "TELEGRAM_API_ERROR", f"5xx 가 잘못 분류됨 (API-F4 회귀): {error}"
        # 핵심: 5xx 를 INVALID_CHAT_ID 로 오분류하면 안 된다.
        assert error != "INVALID_CHAT_ID"


def test_verify_bot_sendmessage_4xx_returns_invalid_chat_id():
    """API-F4 대칭: getMe 성공 후 sendMessage 가 4xx → INVALID_CHAT_ID.

    4xx(잘못된 chat_id)는 정당하게 INVALID_CHAT_ID 로 분류돼야 한다 —
    5xx 와의 비대칭을 명시 검증.
    """
    from src.notifier import telegram_notifier

    getme_ok = _resp(200, {"ok": True, "result": {"username": "study_helper_bot"}})
    send_4xx = _resp(400)

    with patch.object(telegram_notifier, "requests") as mock_requests, patch.object(telegram_notifier.time, "sleep"):
        mock_requests.get.return_value = getme_ok
        mock_requests.post.return_value = send_4xx
        mock_requests.exceptions = _real_requests.exceptions

        ok, error = telegram_notifier.verify_bot(_VALID_TOKEN, "98765")

        assert ok is False
        assert error == "INVALID_CHAT_ID"


# ─────────────────────────────────────────────────────────────────────
# API-F4: 네트워크 예외 → NETWORK_ERROR (INVALID_CHAT_ID 아님)
# ─────────────────────────────────────────────────────────────────────


def test_verify_bot_getme_network_error_returns_network_error():
    """API-F4: getMe 단계의 네트워크 예외 → NETWORK_ERROR."""
    from src.notifier import telegram_notifier

    with patch.object(telegram_notifier, "requests") as mock_requests, patch.object(telegram_notifier.time, "sleep"):
        mock_requests.exceptions = _real_requests.exceptions
        mock_requests.get.side_effect = _real_requests.exceptions.ConnectionError("연결 실패")

        ok, error = telegram_notifier.verify_bot(_VALID_TOKEN, "98765")

        assert ok is False
        assert error == "NETWORK_ERROR", f"네트워크 예외가 잘못 분류됨: {error}"
        assert error != "INVALID_CHAT_ID"


def test_verify_bot_sendmessage_network_error_returns_network_error():
    """API-F4: getMe 성공 후 sendMessage 단계 네트워크 예외 → NETWORK_ERROR.

    재시도 소진 후에도 network 예외만 발생하면 NETWORK_ERROR (5xx 아님).
    """
    from src.notifier import telegram_notifier

    getme_ok = _resp(200, {"ok": True, "result": {"username": "study_helper_bot"}})

    with patch.object(telegram_notifier, "requests") as mock_requests, patch.object(telegram_notifier.time, "sleep"):
        mock_requests.exceptions = _real_requests.exceptions
        mock_requests.get.return_value = getme_ok
        mock_requests.post.side_effect = _real_requests.exceptions.ConnectionError("타임아웃")

        ok, error = telegram_notifier.verify_bot(_VALID_TOKEN, "98765")

        assert ok is False
        assert error == "NETWORK_ERROR"
        assert error != "INVALID_CHAT_ID"


# ─────────────────────────────────────────────────────────────────────
# getMe 단계 4xx 분류 (정상 동작 확인)
# ─────────────────────────────────────────────────────────────────────


def test_verify_bot_getme_401_returns_invalid_token():
    """getMe 가 401/404 → INVALID_TOKEN (토큰 무효)."""
    from src.notifier import telegram_notifier

    with patch.object(telegram_notifier, "requests") as mock_requests:
        mock_requests.get.return_value = _resp(401, {"description": "Unauthorized"})

        ok, error = telegram_notifier.verify_bot(_VALID_TOKEN, "98765")

        assert ok is False
        assert error == "INVALID_TOKEN"


def test_verify_bot_getme_5xx_returns_telegram_api_error():
    """getMe 가 5xx → TELEGRAM_API_ERROR (INVALID_CHAT_ID/INVALID_TOKEN 아님)."""
    from src.notifier import telegram_notifier

    with patch.object(telegram_notifier, "requests") as mock_requests:
        mock_requests.get.return_value = _resp(500, {"description": "server error"})

        ok, error = telegram_notifier.verify_bot(_VALID_TOKEN, "98765")

        assert ok is False
        assert error == "TELEGRAM_API_ERROR"
        assert error != "INVALID_CHAT_ID"


# ─────────────────────────────────────────────────────────────────────
# 정상 경로 — 전부 성공
# ─────────────────────────────────────────────────────────────────────


def test_verify_bot_success_path():
    """getMe + sendMessage 모두 성공 → (True, '')."""
    from src.notifier import telegram_notifier

    getme_ok = _resp(200, {"ok": True, "result": {"username": "study_helper_bot"}})
    send_ok = _resp(200, {"ok": True})

    with patch.object(telegram_notifier, "requests") as mock_requests, patch.object(telegram_notifier.time, "sleep"):
        mock_requests.get.return_value = getme_ok
        mock_requests.post.return_value = send_ok
        mock_requests.exceptions = _real_requests.exceptions

        ok, error = telegram_notifier.verify_bot(_VALID_TOKEN, "98765")

        assert ok is True
        assert error == ""


def test_format_weekly_lms_digest_caps_and_sections():
    """주간 LMS 다이제스트는 섹션별 상한(후보/수동확인 5, 오류 3)을 지킨다."""
    from src.notifier import telegram_notifier

    stats = {"pending": 7, "announcements": 4, "create": 6, "manual_review": 1, "skipped": 2}
    create_top = [f"06-1{i} 후보{i}" for i in range(6)]
    review_top = ["과목A 공지 (no_parseable_date)"]
    errors = ["오류1", "오류2", "오류3", "오류4"]

    text = telegram_notifier.format_weekly_lms_digest(stats, create_top, review_top, errors)

    assert text.startswith("[주간 LMS 일정] 미처리 7건 · 공지 4건")
    assert "캘린더 후보 6건 · 수동확인 1건 · 제외 2건" in text
    assert "  • 06-14 후보4" in text
    assert "06-15 후보5" not in text  # 상한 5 초과분은 요약줄로 대체
    assert "... 외 1건" in text
    assert "과목A 공지 (no_parseable_date)" in text
    assert text.count("⚠") == 3  # 오류는 앞 3건만


def test_format_weekly_lms_digest_minimal_stats_only():
    """후보/오류가 없으면 통계 두 줄만 출력된다."""
    from src.notifier import telegram_notifier

    text = telegram_notifier.format_weekly_lms_digest({"pending": 0}, [], [], None)
    assert text.splitlines() == [
        "[주간 LMS 일정] 미처리 0건 · 공지 0건",
        "캘린더 후보 0건 · 수동확인 0건 · 제외 0건",
    ]


def test_format_weekly_lms_digest_watch_line_when_watch_ran():
    """시청 단계가 수행된 실행은 시청 결과 줄이 둘째 줄로 들어간다."""
    from src.notifier import telegram_notifier

    stats = {"pending": 3, "watch_total": 15, "watched": 14, "watch_failed": 1, "create": 2}
    text = telegram_notifier.format_weekly_lms_digest(stats, [], [], None)
    assert text.splitlines()[1] == "강의 시청 14/15건 성공 · 실패 1건"


def test_format_weekly_lms_digest_summary_count():
    """양수인 요약·다운실패 건수만 붙이고 기존 입력은 그대로 둔다."""
    from src.notifier import telegram_notifier

    stats = {"watch_total": 3, "watched": 2, "watch_failed": 1}
    original = telegram_notifier.format_weekly_lms_digest(stats, [], [], None)
    for counts, suffix in [
        ({"summarized": 2, "downloaded": 3}, " · 요약 2건"),
        ({"summarized": 1, "download_failed": 0}, " · 요약 1건"),
        ({"summarized": 0, "downloaded": 2}, ""),
        ({"summarized": 0, "downloaded": 0}, ""),
        ({"summarized": 0, "download_failed": 0}, ""),
        ({"summarized": 0, "download_failed": 2}, " · 다운실패 2건"),
        ({"download_failed": 2}, " · 다운실패 2건"),
        ({"downloaded": 2}, ""),
        ({}, ""),
    ]:
        text = telegram_notifier.format_weekly_lms_digest({**stats, **counts}, [], [], None)
        expected = original.splitlines()
        expected[1] += suffix
        assert text == "\n".join(expected)


def test_format_weekly_lms_digest_summary_and_download_failure():
    """시청 성공과 별개로 요약 건수 및 다운로드 실패를 같은 줄에 표시한다."""
    from src.notifier import telegram_notifier

    stats = {"watch_total": 5, "watched": 5, "summarized": 3, "download_failed": 2}
    text = telegram_notifier.format_weekly_lms_digest(stats, [], [], None)

    assert text.splitlines()[1] == "강의 시청 5/5건 성공 · 실패 0건 · 요약 3건 · 다운실패 2건"
