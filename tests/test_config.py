"""config.py 단위 테스트."""

from unittest.mock import patch

import pytest

import src.config as config_mod
from src.config import Config, _default_download_dir, _read_version


def test_read_version():
    """CHANGELOG.md에서 버전을 정상적으로 파싱한다."""
    version = _read_version()
    assert version != "unknown"
    parts = version.split("-")[0].split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


def test_default_download_dir():
    """OS별 기본 다운로드 경로가 빈 문자열이 아니어야 한다."""
    path = _default_download_dir()
    assert path
    assert isinstance(path, str)


# ── R2-02 / NEW-07: _save_env 실패 시 클래스 변수 미갱신 ─────────────
#
# 버그 시나리오: save_settings/save_telegram/save_credentials 가 클래스
# 변수를 먼저 대입한 뒤 `_save_env` 를 호출했다 → 파일 쓰기가 실패해도
# 메모리(클래스 변수)는 이미 갱신돼 메모리·디스크가 불일치했다.
# 수정 후에는 `_save_env` 성공 후에만 클래스 변수를 갱신한다.


@pytest.fixture
def _restore_config_vars():
    """테스트가 건드리는 Config 클래스 변수를 원복한다."""
    saved = {
        k: getattr(Config, k)
        for k in (
            "DOWNLOAD_DIR",
            "DOWNLOAD_RULE",
            "STT_ENABLED",
            "AI_ENABLED",
            "AI_AGENT",
            "SUMMARY_PROMPT_EXTRA",
            "TELEGRAM_ENABLED",
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID",
            "TELEGRAM_AUTO_DELETE",
            "LMS_USER_ID",
            "LMS_PASSWORD",
        )
    }
    yield
    for k, v in saved.items():
        setattr(Config, k, v)


def test_save_settings_failure_does_not_mutate_class_vars(_restore_config_vars):
    """R2-02/NEW-07: _save_env 가 실패하면 save_settings 가 클래스 변수를
    갱신하지 않아야 한다 (메모리·디스크 불일치 방지)."""
    Config.DOWNLOAD_RULE = "video"  # 알려진 시작 상태

    with patch.object(Config, "_save_env", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            Config.save_settings(
                download_dir="/new/dir",
                download_rule="both",
                stt_enabled=True,
                ai_enabled=False,
                ai_agent="gemini",
                api_key="",
            )

    # _save_env 가 실패했으므로 클래스 변수는 시작 상태 그대로여야 한다.
    assert Config.DOWNLOAD_RULE == "video", "_save_env 실패 후 클래스 변수가 오염됨"


def test_save_telegram_failure_does_not_mutate_class_vars(_restore_config_vars):
    """R2-02/NEW-07: save_telegram 도 _save_env 실패 시 클래스 변수 미갱신."""
    Config.TELEGRAM_ENABLED = "false"
    Config.TELEGRAM_CHAT_ID = "original_chat"

    with patch.object(Config, "_save_env", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            Config.save_telegram(
                enabled=True,
                bot_token="new_token",
                chat_id="new_chat",
                auto_delete=True,
            )

    assert Config.TELEGRAM_ENABLED == "false"
    assert Config.TELEGRAM_CHAT_ID == "original_chat"


def test_save_credentials_failure_does_not_mutate_class_vars(_restore_config_vars):
    """R2-02/NEW-07: save_credentials 도 _save_env 실패 시 클래스 변수 미갱신."""
    Config.LMS_USER_ID = "original_id"

    with patch.object(Config, "_save_env", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            Config.save_credentials(user_id="2150012601", password="pw")

    assert Config.LMS_USER_ID == "original_id"


def test_save_settings_success_updates_class_vars(_restore_config_vars, tmp_path):
    """정상 경로 회귀 방지: _save_env 성공 시 클래스 변수가 갱신된다."""
    env_path = tmp_path / ".env"
    with patch.object(config_mod, "_env_path", env_path):
        Config.save_settings(
            download_dir="/ok/dir",
            download_rule="audio",
            stt_enabled=False,
            ai_enabled=False,
            ai_agent="gemini",
            api_key="",
        )
    assert Config.DOWNLOAD_RULE == "audio"
    assert Config.DOWNLOAD_DIR == "/ok/dir"


# ── COD-N02: .env value 의 newline 등 특수문자 무결성 ────────────────
#
# 버그 시나리오: `_merge_and_write` 가 `.env` 기록 시 `f"{key}={value}\n"`
# 로 escaping 없이 썼다 → value 에 newline 이 들어가면 다음 줄로 번지거나
# 의도치 않은 키가 삽입돼 `.env` 가 손상됐다 (SUMMARY_PROMPT_EXTRA 등
# 자유 텍스트). 수정 후에는 newline 포함 값을 quoting 해 round-trip 보존.


def _save_env_to(tmp_path, keys: dict) -> None:
    """tmp .env 로 _save_env 를 격리 실행."""
    env_path = tmp_path / ".env"
    with patch.object(config_mod, "_env_path", env_path):
        Config._save_env(keys)


def test_save_env_value_with_newline_round_trips(tmp_path):
    """COD-N02: newline 이 포함된 value 가 손상 없이 저장·복원된다."""
    env_path = tmp_path / ".env"
    multiline = "첫 줄 지시\n두번째 줄 지시\n세번째"

    _save_env_to(tmp_path, {"SUMMARY_PROMPT_EXTRA": multiline})

    # python-dotenv 로 다시 읽어 값이 그대로 복원되는지 확인.
    from dotenv import dotenv_values

    loaded = dotenv_values(env_path)
    assert loaded["SUMMARY_PROMPT_EXTRA"] == multiline, (
        f".env round-trip 손상: {loaded.get('SUMMARY_PROMPT_EXTRA')!r}"
    )


def test_save_env_newline_value_does_not_corrupt_other_keys(tmp_path):
    """COD-N02 핵심: newline 값이 다음 줄로 번져 다른 키를 오염시키지 않는다."""
    env_path = tmp_path / ".env"
    # value 안에 newline + 'KEY=value' 패턴을 의도적으로 삽입.
    malicious = "정상 텍스트\nDOWNLOAD_RULE=video\nTELEGRAM_ENABLED=true"

    _save_env_to(
        tmp_path,
        {"SUMMARY_PROMPT_EXTRA": malicious, "DOWNLOAD_RULE": "audio"},
    )

    from dotenv import dotenv_values

    loaded = dotenv_values(env_path)
    # SUMMARY_PROMPT_EXTRA 는 통째로 보존.
    assert loaded["SUMMARY_PROMPT_EXTRA"] == malicious
    # value 안의 'DOWNLOAD_RULE=video' 가 진짜 키를 덮어쓰면 안 된다.
    assert loaded["DOWNLOAD_RULE"] == "audio", (
        "newline value 안의 KEY=value 패턴이 별도 키로 주입됨"
    )
    # 마찬가지로 TELEGRAM_ENABLED 가 잘못 삽입되면 안 됨.
    assert loaded.get("TELEGRAM_ENABLED") != "true"


def test_save_env_value_with_quotes_round_trips(tmp_path):
    """COD-N02: 큰따옴표가 포함된 값도 escaping 되어 round-trip."""
    env_path = tmp_path / ".env"
    quoted = 'prompt with "quoted" word'

    _save_env_to(tmp_path, {"SUMMARY_PROMPT_EXTRA": quoted})

    from dotenv import dotenv_values

    loaded = dotenv_values(env_path)
    assert loaded["SUMMARY_PROMPT_EXTRA"] == quoted


def test_save_env_plain_value_stays_unquoted(tmp_path):
    """COD-N02: 특수문자 없는 평범한 값은 quoting 없이 기존 포맷 유지."""
    env_path = tmp_path / ".env"
    _save_env_to(tmp_path, {"DOWNLOAD_RULE": "both"})

    content = env_path.read_text(encoding="utf-8")
    assert "DOWNLOAD_RULE=both\n" in content, f"평범한 값에 불필요한 quoting: {content!r}"
