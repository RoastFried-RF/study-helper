"""LOG-SYS-3 회귀 방지: SensitiveFilter 가 PII/OAuth 를 마스킹하는지."""

from __future__ import annotations

import logging

from src.logger import SensitiveFilter


def _make_record(msg: str, args: tuple | dict | None = None) -> logging.LogRecord:
    return logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="x",
        lineno=1,
        msg=msg,
        args=args,
        exc_info=None,
    )


def test_filter_masks_plain_kv() -> None:
    f = SensitiveFilter()
    record = _make_record("user_email=foo@bar.com extra")
    f.filter(record)
    assert "foo@bar.com" not in record.msg
    assert "REDACTED" in record.msg


def test_filter_masks_urlencoded_kv() -> None:
    f = SensitiveFilter()
    record = _make_record("oauth_signature%3DabCdEf123 trailing")
    f.filter(record)
    assert "abCdEf123" not in record.msg


def test_filter_masks_args_tuple() -> None:
    f = SensitiveFilter()
    record = _make_record("body=%s", ("user_email=x@y.com",))
    f.filter(record)
    assert "x@y.com" not in record.args[0]


def test_filter_idempotent() -> None:
    """이미 마스킹된 값은 재적용해도 안전해야 한다."""
    f = SensitiveFilter()
    once = "***REDACTED***"
    record = _make_record(once)
    f.filter(record)
    assert record.msg == once


def test_filter_passes_through_non_sensitive() -> None:
    f = SensitiveFilter()
    original = "safe message with no secrets"
    record = _make_record(original)
    f.filter(record)
    assert record.msg == original


# ── NF-03: LMS 로그인 폼 실제 필드명(userid / pwd) 마스킹 ─────────────
#
# 버그 시나리오: `_SENSITIVE_KEYS` 가 `user_id`/`password` 만 가지고 있어
# 숭실대 LMS 로그인 폼의 실제 필드명 `userid`(언더스코어 없음)·`pwd` 가
# 로그(폼 body / URL)에 남으면 학번·비밀번호가 평문 노출됐다.


def test_filter_masks_lms_form_userid() -> None:
    """NF-03: 로그인 폼 필드 `userid=<학번>` 이 마스킹돼야 한다."""
    f = SensitiveFilter()
    record = _make_record("login form userid=2150012601&pwd=x submitted")
    f.filter(record)
    assert "2150012601" not in record.msg, f"학번 평문 노출: {record.msg}"
    assert "REDACTED" in record.msg


def test_filter_masks_lms_form_pwd() -> None:
    """NF-03: 로그인 폼 필드 `pwd=<비밀번호>` 가 마스킹돼야 한다."""
    f = SensitiveFilter()
    record = _make_record("userid=2150012601&pwd=secret_pw_123 trailing")
    f.filter(record)
    assert "secret_pw_123" not in record.msg, f"비밀번호 평문 노출: {record.msg}"
    assert "REDACTED" in record.msg


def test_filter_masks_pwd_in_urlencoded_body() -> None:
    """NF-03: URL-encoded 폼 body 의 `pwd%3D...` 도 마스킹돼야 한다."""
    f = SensitiveFilter()
    record = _make_record("body=userid%3D2150012601&pwd%3Dtopsecret end")
    f.filter(record)
    assert "topsecret" not in record.msg
    assert "2150012601" not in record.msg


def test_filter_userid_does_not_overmatch_substring() -> None:
    """NF-03: `userid` 추가가 무관한 단어를 과탐하지 않아야 한다.

    KV 정규식이 `key=` 형태로 키 직후 `=` 를 요구하므로 `=` 가 붙지
    않은 단순 단어(`builderidle` 등)는 마스킹되지 않는다.
    """
    f = SensitiveFilter()
    original = "the builderidle process finished"
    record = _make_record(original)
    f.filter(record)
    assert record.msg == original


# ── NF-04: URL-encoded 값에 끼인 %XX 시퀀스가 끝까지 마스킹 ──────────
#
# 버그 시나리오: URL-encoded 마스킹 규칙의 값 클래스가 `%` 를 통째로
# 제외해, `%XX` 인코딩 시퀀스(예: `%40`=`@`)에서 매칭이 끊겨 그 이후의
# 평문(이메일/토큰 잔여 부분)이 마스킹되지 않고 잔존했다.


def test_filter_masks_urlencoded_value_with_percent_sequence() -> None:
    """NF-04: 값 안에 `%40` 같은 인코딩 시퀀스가 있어도 끝까지 마스킹."""
    f = SensitiveFilter()
    # user_email%3D 다음 값에 %40(=@) 인코딩 시퀀스가 들어있다.
    record = _make_record("body=user_email%3Dhong%40ssu.ac.kr trailing")
    f.filter(record)
    assert "ssu.ac.kr" not in record.msg, f"%XX 이후 평문 잔존: {record.msg}"
    assert "hong" not in record.msg
    assert "%40" not in record.msg
    assert "REDACTED" in record.msg


def test_filter_masks_urlencoded_token_with_multiple_percent() -> None:
    """NF-04: 여러 개의 %XX 시퀀스가 섞인 토큰도 전부 마스킹."""
    f = SensitiveFilter()
    record = _make_record("access_token%3DaB%2Bc%2Fd%3D%3Dxyz end")
    f.filter(record)
    assert "xyz" not in record.msg
    assert "aB" not in record.msg
    assert "REDACTED" in record.msg


def test_filter_urlencoded_stops_at_delimiter() -> None:
    """NF-04: %XX 포함 매칭이 `&` 구분자에서 멈춰 다음 키를 삼키지 않는다."""
    f = SensitiveFilter()
    record = _make_record("user_email%3Dhong%40ssu.ac.kr&next=keepme")
    f.filter(record)
    assert "ssu.ac.kr" not in record.msg
    # 구분자 뒤의 무관한 값은 보존돼야 한다.
    assert "keepme" in record.msg


def test_filter_applies_to_propagated_records_via_handler(tmp_path, monkeypatch) -> None:
    """회귀 방지: Python logging 은 로거에 붙인 filter 가 propagate 레코드에
    적용되지 않는다. handler 에 filter 를 붙여야 child 로거의 로그도 마스킹된다.

    이 테스트는 child 로거(study_helper.downloader) 에서 민감 값을 찍고,
    실제 파일에 마스킹된 내용이 기록되는지 확인한다.
    """
    import importlib
    import logging as _std_logging

    import src.logger as logger_mod

    # 기존 app logger 초기화 (다른 테스트에서 이미 부트스트랩됐을 수 있음)
    if logger_mod._app_logger is not None:
        for h in list(logger_mod._app_logger.handlers):
            h.close()
            logger_mod._app_logger.removeHandler(h)
        for f in list(logger_mod._app_logger.filters):
            logger_mod._app_logger.removeFilter(f)
    logger_mod._app_logger = None

    # logs dir 을 tmp_path 로 강제
    monkeypatch.setattr(logger_mod, "_logs_dir", lambda: tmp_path)
    importlib.reload(logger_mod)
    monkeypatch.setattr(logger_mod, "_logs_dir", lambda: tmp_path)

    # 부트스트랩 + 민감 로그 기록
    app = logger_mod.get_logger("main")
    child = logger_mod.get_logger("downloader")  # study_helper.downloader
    child.info("oauth_signature=TOPSECRET123 body=xyz")

    for h in app.handlers:
        h.flush()

    content = (tmp_path / "study_helper.log").read_text(encoding="utf-8")
    assert "TOPSECRET123" not in content, f"PII masking 실패 — child 로거 propagate 경로에서 마스킹 누락: {content[-200:]}"
    assert "REDACTED" in content or "oauth_signature=***" in content

    # 다음 테스트 오염 방지 — app logger 다시 초기화
    for h in list(app.handlers):
        h.close()
        app.removeHandler(h)
    logger_mod._app_logger = None
    _std_logging.Logger.manager.loggerDict.pop("study_helper", None)
