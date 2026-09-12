"""C2/C3: 봇 검증 로그와 설정 비밀값 입력의 회귀 테스트."""

import logging
from io import StringIO
from unittest.mock import Mock

import pytest
import requests

from src.notifier import telegram_notifier


@pytest.mark.parametrize("failure", ["network", "response"])
def test_verify_bot_does_not_log_token(monkeypatch, failure):
    token = "123456789:" + "A" * 35
    leaked = f"https://api.telegram.org/bot{token}/getMe"
    stream = StringIO()
    logger = logging.Logger("telegram_security")
    logger.addHandler(logging.StreamHandler(stream))
    monkeypatch.setattr(telegram_notifier, "_log", logger)
    response = Mock(ok=False, status_code=401, text=leaked)
    response.json.return_value = {"description": leaked}
    get = Mock(return_value=response)
    if failure == "network":
        get.side_effect = requests.ConnectionError(leaked)
    monkeypatch.setattr(telegram_notifier.requests, "get", get)
    expected = "NETWORK_ERROR" if failure == "network" else "INVALID_TOKEN"
    assert telegram_notifier.verify_bot(token, "12345") == (False, expected)
    output = stream.getvalue()
    assert output
    assert token not in output
    if failure == "network":
        assert "ConnectionError" in output
    else:
        assert "401" in output
        response.close.assert_called_once()


def test_settings_secret_prompts_hide_input(monkeypatch, tmp_path):
    from src.ui import settings

    config = Mock(
        DOWNLOAD_RULE="video",
        AI_ENABLED="true",
        GOOGLE_API_KEY="",
        GEMINI_MODEL="",
        SUMMARY_PROMPT_EXTRA="",
        TELEGRAM_ENABLED="true",
        TELEGRAM_BOT_TOKEN="",
        TELEGRAM_CHAT_ID="",
        TELEGRAM_AUTO_DELETE="false",
    )
    config.get_download_dir.return_value = str(tmp_path)
    monkeypatch.setattr(settings, "Config", config)
    monkeypatch.setattr(settings, "console", Mock())
    answers = {"API 키": "synthetic-api-key", "봇 토큰": "synthetic-bot-token", "Chat ID": "12345"}
    ask = Mock(side_effect=lambda prompt, **kwargs: answers.get(prompt.strip(), kwargs.get("default", "")))
    monkeypatch.setattr(settings.Prompt, "ask", ask)
    monkeypatch.setattr(telegram_notifier, "verify_bot", Mock(return_value=(True, "")))
    settings.run_settings()
    secret_calls = [call for call in ask.call_args_list if call.args[0].strip() in {"API 키", "봇 토큰"}]
    assert len(secret_calls) == 2
    assert all(call.kwargs.get("password") is True for call in secret_calls)
    assert all(call.kwargs.get("default") == "" for call in secret_calls)
    for call in ask.call_args_list:
        if call not in secret_calls:
            assert not call.kwargs.get("password", False)
    assert config.save_settings.call_args.kwargs["api_key"] == "synthetic-api-key"
    assert config.save_telegram.call_args.kwargs["bot_token"] == "synthetic-bot-token"
