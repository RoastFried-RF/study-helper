"""
퀴즈 복구 스크립트: 누락된 강의 다운로드 → mp3 변환 → STT → AI 요약.

사용법:
  # 기본값(이전 하드코딩된 과목/주차) 실행
  .venv/Scripts/python.exe scripts/recover_downloads.py

  # 특정 과목·주차 지정
  .venv/Scripts/python.exe scripts/recover_downloads.py \\
      --course 44038 --weeks 3주차 4주차

환경변수:
  FFMPEG_PATH          ffmpeg 실행파일 또는 bin 디렉토리 경로.
                       미지정 시 PATH 에서 shutil.which 로 탐색.
  STUDY_HELPER_DOWNLOAD_DIR  다운로드 루트. 미지정 시 Config.get_download_dir().
"""

import argparse
import asyncio
import os
import shutil
import sys

# ffmpeg 동적 탐지 — FFMPEG_PATH env 우선, 없으면 PATH 에서 which.
# 과거 winget ffmpeg-8.1 경로를 하드코딩하던 것을 제거 (버전 업그레이드 시 파손).
_ffmpeg_env = os.environ.get("FFMPEG_PATH", "").strip()
if _ffmpeg_env:
    _candidate = _ffmpeg_env
    if os.path.isfile(_candidate):
        _candidate = os.path.dirname(_candidate)
    if os.path.isdir(_candidate):
        os.environ["PATH"] = _candidate + os.pathsep + os.environ.get("PATH", "")
elif not shutil.which("ffmpeg"):
    print("[경고] ffmpeg 를 찾을 수 없습니다. FFMPEG_PATH env 또는 PATH 에 추가하세요.")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# sys.path 수정 후에 src.* 를 import 해야 하므로 E402 비활성화.
from pathlib import Path  # noqa: E402

from src.config import Config  # noqa: E402
from src.scraper.course_scraper import CourseScraper  # noqa: E402

# ── 기본 대상 과목 / 강의 필터 (CLI 로 override 가능) ──────────
_DEFAULT_COURSE_ID = "44038"  # 4차산업혁명시대의기술혁신과AI
_DEFAULT_WEEKS = ["3주차", "4주차"]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="누락 강의 복구 파이프라인")
    parser.add_argument(
        "--course", default=_DEFAULT_COURSE_ID,
        help=f"대상 과목 ID (default: {_DEFAULT_COURSE_ID})",
    )
    parser.add_argument(
        "--weeks", nargs="+", default=_DEFAULT_WEEKS,
        help=f"대상 주차 (default: {' '.join(_DEFAULT_WEEKS)})",
    )
    return parser.parse_args()


_ARGS = _parse_args()
TARGET_COURSE_ID = _ARGS.course
TARGET_WEEKS = set(_ARGS.weeks)


async def main():
    print("=" * 60)
    print("  퀴즈 복구: 누락 강의 다운로드 + STT + AI 요약")
    print("=" * 60)
    print()

    user_id = Config.LMS_USER_ID
    password = Config.LMS_PASSWORD
    if not user_id or not password:
        print("[오류] .env에 LMS_USER_ID / LMS_PASSWORD가 설정되어 있지 않습니다.")
        return

    # 우선순위: env STUDY_HELPER_DOWNLOAD_DIR > Config.get_download_dir()
    # 로컬(Windows) 실행 시 Docker 경로(/data)로 잘못 판정되면 user Downloads 로 fallback.
    env_override = os.environ.get("STUDY_HELPER_DOWNLOAD_DIR", "").strip()
    if env_override:
        download_dir = env_override
    else:
        download_dir = Config.get_download_dir()
        if download_dir.startswith("/data") and sys.platform == "win32":
            download_dir = str(Path.home() / "Downloads" / "study-helper")
    Path(download_dir).mkdir(parents=True, exist_ok=True)
    print(f"  다운로드 경로: {download_dir}")
    print()

    async with CourseScraper(user_id, password, headless=True) as scraper:
        print("[1/6] LMS 로그인 중...")
        await scraper.start()
        print("  → 로그인 완료")

        # 과목 목록에서 대상 과목 찾기
        print("[2/6] 과목 목록 로딩...")
        courses = await scraper.fetch_courses()
        target_course = None
        for c in courses:
            if c.id == TARGET_COURSE_ID:
                target_course = c
                break

        if not target_course:
            print(f"[오류] 과목 ID {TARGET_COURSE_ID}를 찾을 수 없습니다.")
            return

        print(f"  → 대상 과목: {target_course.long_name}")

        # 강의 목록 로딩
        print("[3/6] 강의 목록 로딩...")
        detail = await scraper.fetch_lectures(target_course)
        if not detail:
            print("[오류] 강의 목록을 불러올 수 없습니다.")
            return

        # 대상 주차의 비디오 강의 필터링
        target_lectures = []
        for lec in detail.all_video_lectures:
            week = lec.week_label.split("(")[0].strip() if lec.week_label else ""
            if week in TARGET_WEEKS:
                target_lectures.append(lec)

        print(f"  → 대상 강의 {len(target_lectures)}개:")
        for lec in target_lectures:
            print(f"     [{lec.week_label}] {lec.title} (출석: {lec.attendance}, 완료: {lec.completion})")
        print()

        # 각 강의별 다운로드 → 변환 → STT → 요약
        from src.converter.audio_converter import convert_to_mp3
        from src.downloader.video_downloader import download_video_with_browser, extract_video_url, make_filepath

        results = []

        for i, lec in enumerate(target_lectures, 1):
            print(f"[4/6] ({i}/{len(target_lectures)}) {lec.title}")

            # 파일 경로
            mp4_relpath = make_filepath(target_course.long_name, lec.week_label, lec.title)
            mp4_path = (Path(download_dir) / mp4_relpath).resolve()
            mp3_path = mp4_path.with_suffix(".mp3")
            txt_path = mp4_path.with_suffix(".txt")
            summary_path = Path(str(mp4_path).replace(".mp4", "_summarized.txt"))

            # 이미 요약까지 완료된 경우 스킵
            if summary_path.exists():
                print(f"  → 이미 완료: {summary_path.name}")
                results.append(("완료(기존)", lec.title, summary_path))
                continue

            # mp4 다운로드 (없는 경우만)
            if not mp4_path.exists():
                print("  → 영상 URL 추출 중...")
                video_url = await extract_video_url(scraper.page, lec.full_url)
                if not video_url:
                    print("  → [실패] URL 추출 실패")
                    results.append(("URL실패", lec.title, None))
                    continue

                print(f"  → 다운로드 중... ({mp4_path.name})")
                try:
                    await download_video_with_browser(
                        scraper.page, video_url, mp4_path,
                        on_progress=lambda d, t: print(f"\r  → {d*100//t}%", end="", flush=True) if t > 0 else None,
                    )
                    print()
                except Exception as e:
                    print(f"\n  → [실패] 다운로드 오류: {e}")
                    results.append(("다운로드실패", lec.title, None))
                    continue
            else:
                print(f"  → mp4 이미 존재: {mp4_path.name}")

            # mp3 변환
            if not mp3_path.exists():
                print("  → mp3 변환 중...")
                try:
                    convert_to_mp3(mp4_path)
                    print(f"  → mp3 완료: {mp3_path.name}")
                except Exception as e:
                    print(f"  → [실패] mp3 변환: {e}")
                    results.append(("mp3실패", lec.title, None))
                    continue
            else:
                print("  → mp3 이미 존재")

            # STT
            if not txt_path.exists():
                print("  → STT 변환 중... (시간이 걸립니다)")
                try:
                    from src.stt.transcriber import transcribe
                    transcribe(mp3_path, model_size=Config.WHISPER_MODEL or "base", language=Config.STT_LANGUAGE or "ko")
                    print(f"  → STT 완료: {txt_path.name}")
                except Exception as e:
                    print(f"  → [실패] STT: {e}")
                    results.append(("STT실패", lec.title, None))
                    continue
            else:
                print("  → txt 이미 존재")

            # AI 요약
            if not summary_path.exists():
                api_key = Config.GOOGLE_API_KEY
                if not api_key:
                    print("  → [스킵] AI 요약: API 키 없음")
                    results.append(("요약스킵", lec.title, txt_path))
                    continue
                print("  → AI 요약 중...")
                try:
                    from src.summarizer.summarizer import GEMINI_DEFAULT_MODEL, summarize
                    summary_path = summarize(
                        txt_path,
                        agent=Config.AI_AGENT or "gemini",
                        api_key=api_key,
                        model=Config.GEMINI_MODEL or GEMINI_DEFAULT_MODEL,
                        extra_prompt=Config.SUMMARY_PROMPT_EXTRA or "",
                    )
                    print(f"  → 요약 완료: {summary_path.name}")
                    results.append(("완료", lec.title, summary_path))
                except Exception as e:
                    print(f"  → [실패] AI 요약: {e}")
                    results.append(("요약실패", lec.title, txt_path))
                    continue
            else:
                print("  → 요약 이미 존재")
                results.append(("완료(기존)", lec.title, summary_path))

            print()

        # 결과 요약
        print()
        print("=" * 60)
        print("  처리 결과")
        print("=" * 60)
        for status, title, path in results:
            if path:
                print(f"  [{status}] {title}")
                print(f"           → {path}")
            else:
                print(f"  [{status}] {title}")

        # 텔레그램 전송
        tg = Config.get_telegram_credentials()
        if tg:
            from src.notifier.telegram_notifier import _send_document, _send_message
            print()
            print("[6/6] 텔레그램으로 요약 전송 중...")
            for status, title, path in results:
                if path and path.exists() and "완료" in status:
                    text = path.read_text(encoding="utf-8").strip()
                    _send_message(tg[0], tg[1], f"[복구 완료] {title}\n\n{text[:4000]}")
                    _send_document(tg[0], tg[1], path, caption=f"{title} 요약")
                    print(f"  → 전송: {title}")

        print()
        print("완료!")


if __name__ == "__main__":
    asyncio.run(main())
