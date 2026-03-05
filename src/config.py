import os
from pathlib import Path
from dotenv import load_dotenv

# .env 파일 로드 (없으면 환경변수만 사용)
_env_path = Path(__file__).parent.parent / ".env"
load_dotenv(_env_path)


class Config:
    LMS_USER_ID: str = os.getenv("LMS_USER_ID", "")
    LMS_PASSWORD: str = os.getenv("LMS_PASSWORD", "")
    GOOGLE_API_KEY: str = os.getenv("GOOGLE_API_KEY", "")
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    WHISPER_MODEL: str = os.getenv("WHISPER_MODEL", "base")
    DOWNLOAD_DIR: str = os.getenv("DOWNLOAD_DIR", "/data/downloads")

    @classmethod
    def has_credentials(cls) -> bool:
        return bool(cls.LMS_USER_ID and cls.LMS_PASSWORD)

    @classmethod
    def save_credentials(cls, user_id: str, password: str) -> None:
        """계정 정보를 .env 파일에 저장"""
        cls.LMS_USER_ID = user_id
        cls.LMS_PASSWORD = password

        env_path = Path(__file__).parent.parent / ".env"
        lines = []

        # 기존 .env 내용 읽기
        if env_path.exists():
            with open(env_path, "r", encoding="utf-8") as f:
                lines = f.readlines()

        # LMS_USER_ID / LMS_PASSWORD 덮어쓰기
        keys_to_update = {"LMS_USER_ID": user_id, "LMS_PASSWORD": password}
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

        # 아직 없는 키 추가
        for key, value in keys_to_update.items():
            if key not in updated_keys:
                new_lines.append(f"{key}={value}\n")

        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
