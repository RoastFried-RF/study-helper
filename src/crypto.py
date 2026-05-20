"""
민감 정보 암호화/복호화 유틸리티.

암호화 키 저장 우선순위:
1. OS 키체인 (keyring 패키지 사용 가능 시) — 네이티브 앱 환경
2. .secret_key 파일 — Docker / CLI 환경

암호화된 값은 "enc:" 접두사로 구별한다.
"""

import getpass
import threading
from functools import cache
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

_PREFIX = "enc:"


@cache
def _key_path() -> Path:
    """키 경로를 반환한다 (ARCH-009).

    `get_data_base` 를 지연 import 로 호출해 config.py 와의 circular import 를 피한다.
    config 가 crypto.decrypt 를 import 하므로 crypto 가 config 를 top-level import
    하면 순환이 성립. 함수 호출 시점에는 모듈 초기화가 끝나 있어 안전.
    """
    from src.config import get_data_base

    return get_data_base() / ".secret_key"

_KEYRING_SERVICE = "study-helper"

# SEC-007: 사용자별 keyring namespace. 동일 머신에서 여러 OS 사용자가
# study-helper 를 쓰면 "fernet-key" 단일 키로는 서로의 키를 덮어쓴다.
# `{username}` 접미사로 namespace 를 분리한다.
# 기존 하드코딩 키("fernet-key") 로 저장된 값이 있을 수 있으므로
# _try_keyring_load 가 legacy 키를 fallback 으로 읽어 migrate 한다.
try:
    _CURRENT_USER = getpass.getuser() or "default"
except Exception:
    _CURRENT_USER = "default"

_KEYRING_KEY = f"fernet-key:{_CURRENT_USER}"
_LEGACY_KEYRING_KEY = "fernet-key"


def _try_keyring_load() -> bytes | None:
    """OS 키체인에서 Fernet 키를 로드한다. 키체인 미지원 시 None.

    SEC-007: 새 namespace 에 없으면 legacy "fernet-key" 에서 읽어 migrate.
    """
    try:
        import keyring

        stored = keyring.get_password(_KEYRING_SERVICE, _KEYRING_KEY)
        if stored:
            return stored.encode()
        # Legacy namespace fallback — 발견 시 새 namespace 로 migrate
        legacy = keyring.get_password(_KEYRING_SERVICE, _LEGACY_KEYRING_KEY)
        if legacy:
            try:
                keyring.set_password(_KEYRING_SERVICE, _KEYRING_KEY, legacy)
            except Exception:
                pass
            return legacy.encode()
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
    key_path = _key_path()
    if key_path.is_dir():
        return key_path / "key"
    return key_path


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


# L5: Fernet 객체 + key bytes 를 프로세스 캐시. 최초 1회만 keyring/파일 I/O 수행.
# 키는 프로세스 수명 동안 불변(rotation 미지원)이므로 재로드하지 않는다.
# 프로세스 종료 시 자동 소멸.
_cached_fernet: Fernet | None = None
_cache_lock = threading.Lock()


def _fernet() -> Fernet:
    global _cached_fernet
    # fast path — 캐시 적중 시 lock·키 I/O 없음.
    if _cached_fernet is not None:
        return _cached_fernet
    with _cache_lock:
        # double-checked locking — 경합 시 중복 생성 방지.
        if _cached_fernet is None:
            _cached_fernet = Fernet(_load_or_create_key())
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
