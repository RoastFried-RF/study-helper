import os
import sys
from pathlib import Path
from dotenv import load_dotenv

from src.crypto import decrypt, encrypt

# .env 파일 로드 (없으면 환경변수만 사용)
_env_path = Path(__file__).parent.parent / ".env"
load_dotenv(_env_path)


def _default_download_dir() -> str:
    """OS별 기본 다운로드 경로를 반환한다."""
    if sys.platform == "win32":
        return str(Path.home() / "Downloads")
    else:
        # macOS / Linux
        return str(Path.home() / "Downloads")


class Config:
    LMS_USER_ID: str = decrypt(os.getenv("LMS_USER_ID", ""))
    LMS_PASSWORD: str = decrypt(os.getenv("LMS_PASSWORD", ""))
    GOOGLE_API_KEY: str = os.getenv("GOOGLE_API_KEY", "")
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    WHISPER_MODEL: str = os.getenv("WHISPER_MODEL", "base")
    DOWNLOAD_DIR: str = os.getenv("DOWNLOAD_DIR", "")

    @classmethod
    def has_credentials(cls) -> bool:
        return bool(cls.LMS_USER_ID and cls.LMS_PASSWORD)

    @classmethod
    def has_download_dir(cls) -> bool:
        return bool(cls.DOWNLOAD_DIR)

    @classmethod
    def get_download_dir(cls) -> str:
        """저장된 경로가 없으면 OS 기본 다운로드 폴더를 반환한다."""
        return cls.DOWNLOAD_DIR or _default_download_dir()

    @classmethod
    def save_download_dir(cls, download_dir: str) -> None:
        """다운로드 경로를 .env 파일에 저장"""
        cls.DOWNLOAD_DIR = download_dir
        cls._save_env({"DOWNLOAD_DIR": download_dir})

    @classmethod
    def save_credentials(cls, user_id: str, password: str) -> None:
        """계정 정보를 암호화해서 .env 파일에 저장"""
        cls.LMS_USER_ID = user_id
        cls.LMS_PASSWORD = password
        cls._save_env({
            "LMS_USER_ID": encrypt(user_id),
            "LMS_PASSWORD": encrypt(password),
        })

    @classmethod
    def _save_env(cls, keys_to_update: dict) -> None:
        """지정한 키/값을 .env 파일에 저장(덮어쓰기)한다."""
        env_path = Path(__file__).parent.parent / ".env"
        lines = []

        if env_path.exists():
            with open(env_path, "r", encoding="utf-8") as f:
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

        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
