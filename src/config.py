import os
import sys
from datetime import timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

from src.crypto import decrypt, encrypt, is_encrypted

# ── 공용 상수 ─────────────────────────────────────────────────
KST = timezone(timedelta(hours=9))

# .env 파일 경로: STUDY_HELPER_DATA_DIR이 설정되면 그 안의 .env를 사용
_data_dir_env = os.getenv("STUDY_HELPER_DATA_DIR", "")
_env_path = Path(_data_dir_env) / ".env" if _data_dir_env else Path(__file__).parent.parent / ".env"
load_dotenv(_env_path)


def _load_credential(env_key: str) -> str:
    """
    환경변수를 읽어 복호화한다.
    복호화 실패 시(키 불일치 등) 빈 문자열을 반환하되, .env 값은 보존한다.
    """
    raw = os.getenv(env_key, "")
    if not raw:
        return ""
    if is_encrypted(raw):
        return decrypt(raw)
    return raw


def _default_download_dir() -> str:
    """OS별 기본 다운로드 경로를 반환한다."""
    if sys.platform == "win32":
        return str(Path.home() / "Downloads")
    # Docker 컨테이너 환경: /data 볼륨이 마운트된 경우 사용
    if Path("/data").exists() and str(Path.home()) == "/root":
        return "/data/downloads"
    # macOS / 일반 Linux
    return str(Path.home() / "Downloads")


def _read_version() -> str:
    """CHANGELOG.md의 첫 번째 ## [vX.Y.Z] 항목에서 버전을 읽어온다."""
    import re

    changelog = Path(__file__).parent.parent / "CHANGELOG.md"
    try:
        with open(changelog, encoding="utf-8") as f:
            for line in f:
                m = re.match(r"^## \[v(.+?)\]", line)
                if m:
                    return m.group(1)
    except Exception:
        pass
    return "unknown"


APP_VERSION = _read_version()


def _is_docker_with_data_volume() -> bool:
    """Docker 컨테이너 내부이면서 /data 볼륨이 마운트된 경우에만 True.

    Windows/macOS에서 우연히 드라이브 루트의 \\data 폴더(예: D:\\data)가 있어도
    `Path("/data")`가 해당 경로를 가리켜 오탐하는 문제를 방지하기 위해,
    Linux 플랫폼 + `/.dockerenv` 파일 존재 + `/data` 디렉토리 존재를 모두 검증한다.
    """
    if sys.platform != "linux":
        return False
    return Path("/.dockerenv").exists() and Path("/data").is_dir()


def get_data_path(filename: str) -> Path:
    """데이터 파일 경로를 반환한다.

    우선순위:
    1. STUDY_HELPER_DATA_DIR 환경변수 (Electron 앱이 설정)
    2. Docker 컨테이너(/data 마운트): /data
    3. 로컬: 프로젝트 루트/data (CWD 무관)
    """
    env_dir = os.getenv("STUDY_HELPER_DATA_DIR", "")
    if env_dir:
        base = Path(env_dir)
    elif _is_docker_with_data_volume():
        base = Path("/data")
    else:
        base = Path(__file__).resolve().parent.parent / "data"
    base.mkdir(parents=True, exist_ok=True)
    return base / filename


class Config:
    LMS_USER_ID: str = _load_credential("LMS_USER_ID")
    LMS_PASSWORD: str = _load_credential("LMS_PASSWORD")
    GOOGLE_API_KEY: str = _load_credential("GOOGLE_API_KEY")
    OPENAI_API_KEY: str = _load_credential("OPENAI_API_KEY")
    WHISPER_MODEL: str = os.getenv("WHISPER_MODEL", "base")
    # STT 언어: ko, en, 빈 문자열(자동 감지)
    STT_LANGUAGE: str = os.getenv("STT_LANGUAGE", "ko")
    DOWNLOAD_DIR: str = os.getenv("DOWNLOAD_DIR", "")
    # 다운로드 규칙: video / audio / both
    DOWNLOAD_RULE: str = os.getenv("DOWNLOAD_RULE", "")
    # STT 사용 여부: true / false
    STT_ENABLED: str = os.getenv("STT_ENABLED", "")
    # AI 요약 사용 여부: true / false
    AI_ENABLED: str = os.getenv("AI_ENABLED", "")
    # AI 에이전트 종류: gemini / openai
    AI_AGENT: str = os.getenv("AI_AGENT", "")
    # Gemini 모델 ID
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "")
    # 요약 프롬프트 추가 지시사항
    SUMMARY_PROMPT_EXTRA: str = os.getenv("SUMMARY_PROMPT_EXTRA", "")
    # 텔레그램 봇 연동
    TELEGRAM_ENABLED: str = os.getenv("TELEGRAM_ENABLED", "")
    TELEGRAM_BOT_TOKEN: str = _load_credential("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
    # 텔레그램 전송 후 파일 자동 삭제
    TELEGRAM_AUTO_DELETE: str = os.getenv("TELEGRAM_AUTO_DELETE", "")
    # 다운로드 SSRF 허용 도메인 suffix 추가 (쉼표 구분, 예: ".cdn.example.com")
    DOWNLOAD_EXTRA_HOSTS: str = os.getenv("DOWNLOAD_EXTRA_HOSTS", "")

    @classmethod
    def get_telegram_credentials(cls) -> tuple[str, str] | None:
        """텔레그램이 활성화되고 credential이 유효하면 (token, chat_id) 반환."""
        if cls.TELEGRAM_ENABLED != "true":
            return None
        if not cls.TELEGRAM_BOT_TOKEN or not cls.TELEGRAM_CHAT_ID:
            return None
        return cls.TELEGRAM_BOT_TOKEN, cls.TELEGRAM_CHAT_ID

    @classmethod
    def has_credentials(cls) -> bool:
        return bool(cls.LMS_USER_ID and cls.LMS_PASSWORD)

    @classmethod
    def has_settings(cls) -> bool:
        """최초 설정이 완료됐는지 확인 (다운로드 규칙 기준)."""
        return bool(cls.DOWNLOAD_RULE)

    @classmethod
    def get_download_dir(cls) -> str:
        """저장된 경로가 없으면 OS 기본 다운로드 폴더를 반환한다.

        Windows 가드(SEC-DRIVE-ROOT-TRAP):
            `DOWNLOAD_DIR=/data/downloads` 같은 Docker 용 unix 절대경로가
            Windows 네이티브 실행 시 드라이브 루트(`D:\\data\\downloads`)로
            해석된다. 이 자체는 유효한 Windows 경로이고, 실제 사용자가 Docker
            와 native 를 혼용하는 환경에서는 이미 해당 위치에 파일을 쌓아
            둔 경우가 많다. 따라서 fallback 으로 `~/Downloads` 같은 다른
            위치로 바꾸지 않는다 — 기존 파일과의 정합성이 깨지기 때문.
            대신 `Path.resolve()` 로 Windows 절대경로 포맷으로 변환하고
            최초 한 번 경고 로그만 남긴다.
        """
        raw = cls.DOWNLOAD_DIR
        if not raw:
            return _default_download_dir()

        if (
            sys.platform == "win32"
            and (raw.startswith("/") or raw.startswith("\\"))
            and not _is_docker_with_data_volume()
        ):
            # Windows 에서 unix 스타일 절대경로는 현재 CWD 의 드라이브 루트로
            # 해석된다. 명시적으로 resolve 해서 이후 파일시스템 비교가 일관되도록.
            resolved = str(Path(raw).resolve())
            if not cls._drive_root_trap_warned:
                import logging as _logging

                _logging.getLogger(__name__).warning(
                    "DOWNLOAD_DIR=%r 은 Docker unix 절대경로 — Windows 네이티브에서는 "
                    "드라이브 루트 %r 로 해석됩니다. 의도한 위치가 맞는지 확인하세요. "
                    "다른 곳에 저장하려면 `.env` DOWNLOAD_DIR 을 Windows 경로(예: "
                    "`C:/Users/...`)로 수정하세요.",
                    raw, resolved,
                )
                cls._drive_root_trap_warned = True
            return resolved
        return raw

    # Windows drive-root trap 경고 중복 방지 플래그
    _drive_root_trap_warned: bool = False

    @classmethod
    def save_settings(
        cls,
        download_dir: str,
        download_rule: str,
        stt_enabled: bool,
        ai_enabled: bool,
        ai_agent: str,
        api_key: str,
        gemini_model: str = "",
        summary_prompt_extra: str = "",
    ) -> None:
        """설정 항목을 .env 파일에 저장한다."""
        cls.DOWNLOAD_DIR = download_dir
        cls.DOWNLOAD_RULE = download_rule
        cls.STT_ENABLED = "true" if stt_enabled else "false"
        cls.AI_ENABLED = "true" if ai_enabled else "false"
        cls.AI_AGENT = ai_agent
        cls.SUMMARY_PROMPT_EXTRA = summary_prompt_extra
        if gemini_model:
            cls.GEMINI_MODEL = gemini_model
        # API 키는 선택한 에이전트에 맞게 저장
        if ai_enabled and ai_agent == "gemini":
            cls.GOOGLE_API_KEY = api_key
        elif ai_enabled and ai_agent == "openai":
            cls.OPENAI_API_KEY = api_key

        to_save: dict = {
            "DOWNLOAD_DIR": download_dir,
            "DOWNLOAD_RULE": download_rule,
            "STT_ENABLED": cls.STT_ENABLED,
            "AI_ENABLED": cls.AI_ENABLED,
            "AI_AGENT": ai_agent,
            "SUMMARY_PROMPT_EXTRA": summary_prompt_extra,
        }
        if gemini_model:
            to_save["GEMINI_MODEL"] = gemini_model
        if ai_enabled and ai_agent == "gemini":
            to_save["GOOGLE_API_KEY"] = encrypt(api_key) if api_key else ""
        elif ai_enabled and ai_agent == "openai":
            to_save["OPENAI_API_KEY"] = encrypt(api_key) if api_key else ""
        cls._save_env(to_save)

    @classmethod
    def save_telegram(cls, enabled: bool, bot_token: str, chat_id: str, auto_delete: bool) -> None:
        """텔레그램 설정을 .env 파일에 저장한다."""
        cls.TELEGRAM_ENABLED = "true" if enabled else "false"
        cls.TELEGRAM_BOT_TOKEN = bot_token
        cls.TELEGRAM_CHAT_ID = chat_id
        cls.TELEGRAM_AUTO_DELETE = "true" if auto_delete else "false"
        cls._save_env(
            {
                "TELEGRAM_ENABLED": cls.TELEGRAM_ENABLED,
                "TELEGRAM_BOT_TOKEN": encrypt(bot_token) if bot_token else "",
                "TELEGRAM_CHAT_ID": chat_id,
                "TELEGRAM_AUTO_DELETE": cls.TELEGRAM_AUTO_DELETE,
            }
        )

    @classmethod
    def save_credentials(cls, user_id: str, password: str) -> None:
        """계정 정보를 암호화해서 .env 파일에 저장"""
        cls.LMS_USER_ID = user_id
        cls.LMS_PASSWORD = password
        cls._save_env(
            {
                "LMS_USER_ID": encrypt(user_id),
                "LMS_PASSWORD": encrypt(password),
            }
        )

    @classmethod
    def _save_env(cls, keys_to_update: dict) -> None:
        """지정한 키/값을 .env 파일에 저장(덮어쓰기)한다."""
        env_path = _env_path
        lines = []

        if env_path.exists():
            with open(env_path, encoding="utf-8") as f:
                lines = f.readlines()

        updated_keys = set()
        new_lines = []

        for line in lines:
            stripped = line.strip()
            if "=" in stripped and not stripped.startswith("#"):
                key = stripped.split("=", 1)[0].strip()
                if key in keys_to_update:
                    new_lines.append(f"{key}={keys_to_update[key]}\n")
                    updated_keys.add(key)
                    continue
            new_lines.append(line)

        for key, value in keys_to_update.items():
            if key not in updated_keys:
                new_lines.append(f"{key}={value}\n")

        # atomic write: 임시 파일에 먼저 쓴 뒤 rename으로 대체 (쓰기 중 크래시 시 원본 보존)
        import tempfile

        fd, tmp_str = tempfile.mkstemp(dir=env_path.parent, prefix=".env.", suffix=".tmp")
        tmp_path = Path(tmp_str)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.writelines(new_lines)
                f.flush()
                os.fsync(f.fileno())
            tmp_path.replace(env_path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
