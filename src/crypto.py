"""
민감 정보 암호화/복호화 유틸리티.

암호화 키 저장 우선순위:
1. OS 키체인 (keyring 패키지 사용 가능 시) — 네이티브 앱 환경
2. .secret_key 파일 — Docker / CLI 환경

암호화된 값은 "enc:" 접두사로 구별한다.
"""

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

_PREFIX = "enc:"

# 키 경로: STUDY_HELPER_DATA_DIR이 설정되면 그 안의 .secret_key 사용
_data_dir = os.getenv("STUDY_HELPER_DATA_DIR", "")
_KEY_PATH = Path(_data_dir) / ".secret_key" if _data_dir else Path(__file__).parent.parent / ".secret_key"

_KEYRING_SERVICE = "study-helper"
_KEYRING_KEY = "fernet-key"


def _try_keyring_load() -> bytes | None:
    """OS 키체인에서 Fernet 키를 로드한다. 키체인 미지원 시 None."""
    try:
        import keyring

        stored = keyring.get_password(_KEYRING_SERVICE, _KEYRING_KEY)
        if stored:
            return stored.encode()
    except Exception:
        pass
    return None


def _try_keyring_save(key: bytes) -> bool:
    """OS 키체인에 Fernet 키를 저장한다. 성공 시 True."""
    try:
        import keyring

        keyring.set_password(_KEYRING_SERVICE, _KEYRING_KEY, key.decode())
        return True
    except Exception:
        return False


def _resolve_key_file() -> Path:
    """키 파일 경로를 결정한다. .secret_key가 디렉토리일 때 내부 key 파일 사용."""
    if _KEY_PATH.is_dir():
        return _KEY_PATH / "key"
    return _KEY_PATH


def _load_or_create_key() -> bytes:
    """암호화 키를 로드하거나 새로 생성한다.

    우선순위:
    1. OS 키체인 (keyring)
    2. .secret_key 파일
    3. 새 키 생성 후 키체인 → 파일 순으로 저장
    """
    # 1. keyring에서 시도
    key = _try_keyring_load()
    if key:
        return key

    # 2. 파일에서 시도
    key_file = _resolve_key_file()
    if key_file.exists() and key_file.is_file():
        key = key_file.read_bytes().strip()
        # 파일에 있으면 키체인에도 동기화 시도
        _try_keyring_save(key)
        return key

    # 3. 새 키 생성
    key = Fernet.generate_key()

    # 키체인에 저장 시도
    _try_keyring_save(key)

    # 파일에도 저장 (Docker/CLI fallback)
    key_file = _resolve_key_file()
    try:
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_bytes(key)
        key_file.chmod(0o600)
    except OSError:
        pass  # Windows chmod 또는 읽기 전용 파일시스템
    return key


# 의도적 캐시: 반복 호출 시 키 파일 I/O 절약. 프로세스 종료 시 자동 소멸.
_cached_fernet: Fernet | None = None
_cached_fernet_key: bytes | None = None


def _fernet() -> Fernet:
    global _cached_fernet, _cached_fernet_key
    key = _load_or_create_key()
    if _cached_fernet is None or _cached_fernet_key != key:
        _cached_fernet = Fernet(key)
        _cached_fernet_key = key
    return _cached_fernet


def encrypt(plaintext: str) -> str:
    """평문을 암호화하고 'enc:<base64>' 형태의 문자열을 반환한다."""
    token = _fernet().encrypt(plaintext.encode())
    return _PREFIX + token.decode()


def decrypt(value: str) -> str:
    """
    'enc:<base64>' 형태의 값을 복호화한다.
    접두사가 없으면 평문 그대로 반환한다 (하위 호환).
    복호화 실패 시 빈 문자열 반환.
    """
    if not value.startswith(_PREFIX):
        return value
    token = value[len(_PREFIX) :]
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, Exception):
        return ""


def is_encrypted(value: str) -> bool:
    return value.startswith(_PREFIX)
