"""crypto.py 단위 테스트."""

from contextlib import contextmanager
from unittest.mock import Mock, patch

import pytest
from cryptography.fernet import Fernet


def _patch_key_path(key_file):
    """`_key_path()` 를 특정 경로를 반환하도록 패치하고 cache 초기화."""
    from src import crypto

    crypto._key_path.cache_clear()
    return patch.object(crypto, "_key_path", return_value=key_file)


def test_encrypt_decrypt_roundtrip(tmp_path):
    """암호화 후 복호화하면 원본과 동일해야 한다."""
    key_file = tmp_path / ".secret_key"
    with _patch_key_path(key_file):
        from src.crypto import decrypt, encrypt, is_encrypted

        original = "test_password_123!@#"
        encrypted = encrypt(original)
        assert is_encrypted(encrypted)
        assert encrypted.startswith("enc:")
        assert decrypt(encrypted) == original


def test_decrypt_plaintext():
    """enc: 접두사 없는 평문은 그대로 반환해야 한다."""
    from src.crypto import decrypt

    assert decrypt("plain_value") == "plain_value"


def test_is_encrypted():
    """enc: 접두사 판별이 정확해야 한다."""
    from src.crypto import is_encrypted

    assert is_encrypted("enc:abc123") is True
    assert is_encrypted("plain") is False
    assert is_encrypted("") is False


def test_encrypt_empty_string(tmp_path):
    """빈 문자열도 암호화/복호화 가능해야 한다."""
    key_file = tmp_path / ".secret_key"
    with _patch_key_path(key_file):
        from src.crypto import decrypt, encrypt

        encrypted = encrypt("")
        assert decrypt(encrypted) == ""


def test_different_keys_cannot_decrypt(tmp_path):
    """다른 키로는 복호화할 수 없어야 한다 (빈 문자열 반환).

    _fernet 캐시를 무효화하기 위해 키 파일 변경 사이에 _cached_fernet 을 리셋한다.
    """
    from src import crypto

    key_file_1 = tmp_path / "key1"
    key_file_2 = tmp_path / "key2"

    with _patch_key_path(key_file_1):
        crypto._cached_fernet = None
        crypto._cached_fernet_key = None
        from src.crypto import encrypt

        encrypted = encrypt("secret")

    with _patch_key_path(key_file_2):
        crypto._cached_fernet = None
        crypto._cached_fernet_key = None
        from src.crypto import decrypt

        assert decrypt(encrypted) == ""


# ── NEW-04: 손상된 .secret_key 는 빈 문자열로 흡수하지 않는다 ────────
#
# 버그 시나리오: `decrypt` 의 `_fernet()` 호출이 try 안에 있어, 키 파일이
# 손상돼 `Fernet()` 생성이 `ValueError` 를 던져도 빈 문자열로 흡수됐다.
# 그 결과 "설정 없음"으로 오분류 → 사용자가 재입력 → 새 키가 기존 키를
# 덮어쓰는 연쇄가 발생했다.
#
# `test_different_keys_cannot_decrypt`(위) 는 *유효하지만 다른* 키로 인한
# `InvalidToken` → 빈 문자열을 검증한다. 그건 정상 동작이라 유지한다.
# 본 케이스는 *유효하지 않은 키 인프라*(손상 키 바이트) — 다른 오류이며,
# 빈 문자열 흡수가 아니라 예외 전파 + 에러 로그가 기대 동작이다.


def test_decrypt_corrupted_key_raises_not_empty(tmp_path):
    """NEW-04: 손상된 키 바이트(Fernet 생성 실패) 는 빈 문자열로 삼키지 않고
    예외를 전파해야 한다."""
    import pytest

    from src import crypto

    key_file = tmp_path / ".secret_key"
    # 정상적인 base64 Fernet 키가 아닌 손상 바이트.
    key_file.write_bytes(b"not-a-valid-fernet-key")

    with _patch_key_path(key_file):
        # 캐시된 Fernet 무효화 — 손상 키로 재로드되게 한다.
        crypto._cached_fernet = None
        from src.crypto import decrypt

        # 손상 키 → _fernet() 의 Fernet() 생성이 ValueError.
        # 수정 후에는 decrypt 가 이를 빈 문자열로 흡수하지 않고 전파한다.
        with pytest.raises(Exception) as exc_info:
            decrypt("enc:c29tZXRoaW5n")
        # InvalidToken(정상 분기) 이 아니라 키 인프라 오류여야 한다.
        from cryptography.fernet import InvalidToken

        assert not isinstance(exc_info.value, InvalidToken), (
            "손상 키는 InvalidToken 이 아닌 키 인프라 오류로 전파돼야 함"
        )

    # 캐시 오염 방지 — 다음 테스트를 위해 리셋.
    crypto._cached_fernet = None


def test_decrypt_corrupted_key_logs_error(tmp_path):
    """NEW-04: 손상 키로 복호화 실패 시 에러 로그를 남긴다 (silent 금지)."""
    import pytest

    from src import crypto

    key_file = tmp_path / ".secret_key"
    key_file.write_bytes(b"corrupt")

    with _patch_key_path(key_file):
        crypto._cached_fernet = None
        from src.crypto import decrypt

        with patch("src.logger.get_logger") as mock_get_logger:
            with pytest.raises(ValueError):
                decrypt("enc:c29tZXRoaW5n")
        mock_get_logger.assert_called_once_with("crypto")
        mock_get_logger.return_value.error.assert_called_once()

    crypto._cached_fernet = None


def test_decrypt_invalid_token_still_returns_empty(tmp_path):
    """NEW-04 경계: *유효한* 키 + 손상된 *토큰* 은 여전히 빈 문자열 반환.

    키 인프라는 정상이고 토큰만 잘못된 경우(InvalidToken)는 기존대로
    빈 문자열을 반환해야 한다 — NEW-04 수정이 이 정상 동작을 깨면 안 된다.
    """
    from src import crypto

    key_file = tmp_path / ".secret_key"
    with _patch_key_path(key_file):
        crypto._cached_fernet = None
        from src.crypto import decrypt, encrypt

        # 정상 키 부트스트랩
        encrypt("warmup")
        # enc: 접두사는 있지만 base64 토큰이 깨진 값
        assert decrypt("enc:!!!not-base64!!!") == ""

    crypto._cached_fernet = None


# ── NEW-03: .secret_key 파일 쓰기는 atomic 하게 (부분 쓰기 없이) ──────
#
# 버그 시나리오: `.secret_key` 쓰기가 `atomic_write`/`file_lock` 미경유
# → API 서버 + CLI 동시 첫 기동 시 키 분기 race. 수정 후에는
# `_load_or_create_key` 가 `file_lock` + `atomic_write_text` 를 경유한다.


def test_secret_key_written_atomically(tmp_path):
    """NEW-03: 새 키 생성 시 atomic_write_text 를 경유해야 한다."""
    from src import crypto

    key_file = tmp_path / ".secret_key"

    with _patch_key_path(key_file):
        crypto._cached_fernet = None
        # keyring 은 환경에 따라 동작이 다르므로 비활성화 — 파일 경로 강제.
        with (
            patch.object(crypto, "_try_keyring_load", return_value=None),
            patch.object(crypto, "_try_keyring_save", return_value=False),
        ):
            with patch(
                "src.util.atomic_write.atomic_write_text",
                wraps=__import__("src.util.atomic_write", fromlist=["atomic_write_text"]).atomic_write_text,
            ) as spy:
                key = crypto._load_or_create_key()

    assert key, "키가 생성돼야 한다"
    # 새 키 생성 경로는 atomic_write_text 를 정확히 1회 호출한다.
    spy.assert_called_once()
    assert spy.call_args.args[0] == key_file
    crypto._cached_fernet = None


def test_secret_key_no_partial_content_on_disk(tmp_path):
    """NEW-03: 디스크에 기록된 .secret_key 는 완결된 유효 Fernet 키여야 한다.

    atomic 교체이므로 부분 쓰기(잘린 키)가 관측되지 않는다 — 기록된 내용
    그대로 Fernet() 생성이 성공해야 한다.
    """
    from cryptography.fernet import Fernet

    from src import crypto

    key_file = tmp_path / ".secret_key"

    with _patch_key_path(key_file):
        crypto._cached_fernet = None
        with (
            patch.object(crypto, "_try_keyring_load", return_value=None),
            patch.object(crypto, "_try_keyring_save", return_value=False),
        ):
            crypto._load_or_create_key()

    assert key_file.exists()
    written = key_file.read_bytes().strip()
    # 부분 쓰기였다면 Fernet() 가 ValueError 를 던진다.
    Fernet(written)  # 예외 없이 통과해야 함
    crypto._cached_fernet = None


def test_secret_key_double_check_adopts_existing(tmp_path):
    """NEW-03: 락 안 double-check 로 이미 존재하는 키를 채택한다.

    다른 프로세스가 먼저 키를 만든 상황 — 락 대기 후 기존 키를 읽어
    덮어쓰지 않고 그대로 채택해야 한다 (cross-process 키 분기 방지).
    """
    from cryptography.fernet import Fernet

    from src import crypto

    key_file = tmp_path / ".secret_key"
    # 다른 프로세스가 먼저 만들어 둔 유효 키.
    existing_key = Fernet.generate_key()
    key_file.write_bytes(existing_key)

    with _patch_key_path(key_file):
        crypto._cached_fernet = None
        with (
            patch.object(crypto, "_try_keyring_load", return_value=None),
            patch.object(crypto, "_try_keyring_save", return_value=False),
        ):
            key = crypto._load_or_create_key()

    # 기존 키를 채택해야 한다 — 새 키로 덮어쓰면 안 됨.
    assert key == existing_key
    assert key_file.read_bytes().strip() == existing_key
    crypto._cached_fernet = None


@pytest.mark.parametrize("already_exists", [False, True])
def test_keyring_only_receives_canonical_file_key_under_lock(tmp_path, monkeypatch, already_exists):
    """락 대기 중 나타난 키와 기존 키 모두 후보 공개 없이 채택·복호화한다."""
    from src import crypto
    from src.util import atomic_write

    key_file = tmp_path / ".secret_key"
    existing = Fernet.generate_key()
    encrypted = "enc:" + Fernet(existing).encrypt(b"existing-credentials").decode()
    if already_exists:
        key_file.write_bytes(existing)
    locked = False
    published = []

    @contextmanager
    def lock(path):
        nonlocal locked
        assert path == key_file
        assert published == []
        locked = True
        if not already_exists:
            key_file.write_bytes(existing)
        try:
            yield
        finally:
            locked = False

    def save(key):
        assert locked
        assert key == existing == key_file.read_bytes()
        published.append(key)
        return True

    monkeypatch.setattr(crypto, "_key_path", lambda: key_file)
    monkeypatch.setattr(crypto, "_cached_fernet", None)
    monkeypatch.setattr(crypto, "_try_keyring_load", lambda: None)
    monkeypatch.setattr(crypto, "_try_keyring_save", save)
    monkeypatch.setattr(atomic_write, "file_lock", lock)
    assert crypto.decrypt(encrypted) == "existing-credentials"
    assert published == [existing]


@pytest.mark.parametrize(("file_saved", "keyring_saved"), [(False, False), (False, True), (True, False), (True, True)])
def test_new_key_requires_at_least_one_persistent_store(tmp_path, monkeypatch, file_saved, keyring_saved):
    """두 저장소 실패는 캐시 없이 예외, 한 곳 이상 성공하면 재시작 후에도 복호화한다."""
    from src import crypto
    from src.util import atomic_write

    key_file = tmp_path / ".secret_key"
    locked = False
    stored_key = None
    real_write = atomic_write.atomic_write_text

    @contextmanager
    def lock(path):
        nonlocal locked
        assert path == key_file
        locked = True
        try:
            yield
        finally:
            locked = False

    def write(path, text, **kwargs):
        assert locked
        if not file_saved:
            raise PermissionError("synthetic file write denial")
        real_write(path, text, **kwargs)

    def save(key):
        nonlocal stored_key
        assert locked
        if file_saved:
            assert key_file.read_bytes() == key
        if keyring_saved:
            stored_key = key
        return keyring_saved

    monkeypatch.setattr(crypto, "_key_path", lambda: key_file)
    monkeypatch.setattr(crypto, "_cached_fernet", None)
    monkeypatch.setattr(crypto, "_try_keyring_load", lambda: stored_key)
    monkeypatch.setattr(crypto, "_try_keyring_save", save)
    monkeypatch.setattr(atomic_write, "file_lock", lock)
    monkeypatch.setattr(atomic_write, "atomic_write_text", write)
    if not file_saved and not keyring_saved:
        with pytest.raises(OSError, match="암호화 키를 파일 또는 OS 키체인에 저장할 수 없습니다"):
            crypto.encrypt("credentials")
        assert crypto._cached_fernet is None
        assert not key_file.exists()
        return
    encrypted = crypto.encrypt("credentials")
    crypto._cached_fernet = None
    assert crypto.decrypt(encrypted) == "credentials"


def test_keyring_rechecked_after_lock_wait(tmp_path, monkeypatch):
    """다른 프로세스가 keyring 에만 저장했으면 새 키로 덮어쓰지 않는다."""
    from src import crypto

    existing = Fernet.generate_key()
    monkeypatch.setattr(crypto, "_key_path", lambda: tmp_path / ".secret_key")
    monkeypatch.setattr(crypto, "_try_keyring_load", Mock(side_effect=[None, existing]))
    save = Mock()
    monkeypatch.setattr(crypto, "_try_keyring_save", save)
    assert crypto._load_or_create_key() == existing
    save.assert_not_called()


def test_existing_key_still_loads_when_lock_file_unwritable(tmp_path, monkeypatch):
    """이미 영속된 키의 읽기는 락 파일 생성 권한 실패에 영향받지 않는다."""
    from src import crypto

    existing = Fernet.generate_key()
    key_file = tmp_path / ".secret_key"
    key_file.write_bytes(existing)
    monkeypatch.setattr(crypto, "_key_path", lambda: key_file)
    monkeypatch.setattr(crypto, "_try_keyring_load", lambda: None)
    save = Mock()
    monkeypatch.setattr(crypto, "_try_keyring_save", save)
    with patch("src.util.atomic_write.file_lock", side_effect=PermissionError("synthetic lock denial")):
        assert crypto._load_or_create_key() == existing
    save.assert_not_called()


def test_existing_key_decrypts_during_atomic_write_import(tmp_path, monkeypatch):
    """atomic_write 초기화 중 순환 import 가 발생해도 기존 자격증명을 복호화한다."""
    import sys
    from types import ModuleType

    from src import crypto

    existing = Fernet.generate_key()
    key_file = tmp_path / ".secret_key"
    key_file.write_bytes(existing)
    encrypted = "enc:" + Fernet(existing).encrypt(b"existing-credentials").decode()
    monkeypatch.setattr(crypto, "_key_path", lambda: key_file)
    monkeypatch.setattr(crypto, "_cached_fernet", None)
    monkeypatch.setattr(crypto, "_try_keyring_load", lambda: None)
    save = Mock()
    monkeypatch.setattr(crypto, "_try_keyring_save", save)
    monkeypatch.setitem(sys.modules, "src.util.atomic_write", ModuleType("src.util.atomic_write"))
    assert crypto.decrypt(encrypted) == "existing-credentials"
    save.assert_not_called()
