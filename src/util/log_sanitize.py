"""로그 텍스트의 PII/secret 마스킹 공용 모듈.

background_player 의 sniff 로깅과 scripts/sanitize_logs.py 양쪽에서
공유. 규칙을 한 곳에서 관리해 drift 를 방지한다.

커버 범위:
  1. URL-encoded 또는 form body 의 `key=value` 형태 PII (oauth_*, csrf_*,
     custom_user_*, lis_person_*, user_email, user_id, password 등)
  2. 동일 key 의 URL-encoded 변형 (`%3D` 로 인코딩된 `=`)
  3. HTML meta/data 속성 PII (<meta name="csrf-token|user_name"
     content="...">, data-user_email="..." 등)
  4. Open Graph meta property URL 내부에 포함된 user_id/login
"""

from __future__ import annotations

import re

MASK = "***REDACTED***"

# 민감 키 이름 — 정규식 alternation 으로 한 번에 처리.
# 새 키 추가 시 여기에만 넣으면 plain + url-encoded 양쪽 자동 커버.
_SENSITIVE_KEYS = (
    r"oauth_(?:signature|nonce|timestamp|consumer_key|token)"
    r"|csrf[-_]?token"
    r"|custom_user_(?:email|login|name_full|name_family|name_given|id)"
    r"|custom_canvas_user_(?:id|login_id)"
    r"|lis_person_(?:contact_email_primary|name_full|name_given|name_family|sourcedid)"
    r"|tool_consumer_instance_guid"
    r"|user_image|user_id|user_login|user_email"
    r"|password|passwd|secret|api[_-]?key|authorization|access_token|refresh_token|token"
)

# Plain `key=value` (form body, 쿼리스트링 중 URL-decoded 섹션)
_SENSITIVE_KV_RE = re.compile(
    rf"(?i)({_SENSITIVE_KEYS})=([^&\s\"'<>]+)"
)

# URL-encoded `key%3Dvalue` — `%3D` 는 `=` 의 URL-인코딩. LTI URL 이
# body 안에 삽입되면 이중 인코딩되어 plain `=` 이 없기 때문에 별도 규칙 필요.
_SENSITIVE_KV_URLENC_RE = re.compile(
    rf"(?i)({_SENSITIVE_KEYS})%3D([^%&\s\"'<>]+)"
)

# HTML meta / data-* attribute 계열
# 1) <meta name="csrf-token|user_name|commons.user_name" content="...">
# 2) data-user_email|login|name|id|name_full="..."
_SENSITIVE_HTML_RE = re.compile(
    r"(?is)"
    r'(<meta\s+name="(?:csrf-token|user_name|commons\.user_name)"\s+content=")[^"]*(")'
    r'|(data-user_(?:email|login|name|id|name_full)=")[^"]*(")'
)


def mask_sensitive(text: str) -> str:
    """로그로 남기기 전에 OAuth/CSRF/PII 를 마스킹한다. 멀티패스 적용."""
    if not text:
        return text
    text = _SENSITIVE_KV_RE.sub(lambda m: f"{m.group(1)}={MASK}", text)
    text = _SENSITIVE_KV_URLENC_RE.sub(lambda m: f"{m.group(1)}%3D{MASK}", text)
    text = _SENSITIVE_HTML_RE.sub(
        lambda m: (m.group(1) or m.group(3) or "")
        + MASK
        + (m.group(2) or m.group(4) or ""),
        text,
    )
    return text


def count_sensitive(text: str) -> int:
    """마스킹 대상 매치 개수 — dry-run 통계용."""
    if not text:
        return 0
    return (
        len(_SENSITIVE_KV_RE.findall(text))
        + len(_SENSITIVE_KV_URLENC_RE.findall(text))
        + len(_SENSITIVE_HTML_RE.findall(text))
    )
