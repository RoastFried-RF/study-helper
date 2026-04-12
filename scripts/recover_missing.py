"""누락 다운로드 복구 스크립트.

재생(출석)은 완료됐지만 data/downloads에 파일이 없는 강의를
전수 검사하고, 재생 단계를 건너뛴 채 다운로드→mp3→STT→요약 파이프라인만 재실행한다.

사용법:
    python -m scripts.recover_missing            # 대화형 (확인 후 실행)
    python -m scripts.recover_missing --dry-run  # 목록만 출력
    python -m scripts.recover_missing --course <course_id>  # 특정 과목만

구조적으로 다운로드 불가능한 항목(learningx)은 자동 제외된다.
복구 실행 중 실패한 항목은 reason별로 집계되어 마지막에 리포트로 출력된다.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter
from pathlib import Path

# ffmpeg PATH 추가 (Windows winget 설치 경로)
_FFMPEG_DIR = os.path.expandvars(
    r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1-full_build\bin"
)
if os.path.isdir(_FFMPEG_DIR):
    os.environ["PATH"] = _FFMPEG_DIR + os.pathsep + os.environ.get("PATH", "")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.config import Config  # noqa: E402
from src.downloader.result import REASON_UNSUPPORTED, DownloadResult  # noqa: E402
from src.downloader.video_downloader import make_filepath  # noqa: E402
from src.logger import get_logger  # noqa: E402
from src.scraper.course_scraper import CourseScraper  # noqa: E402
from src.ui.download import run_download  # noqa: E402

_log = get_logger("recover_missing")


def _expected_files(course_long_name: str, week_label: str, title: str, download_dir: str) -> tuple[Path, Path]:
    mp4_rel = make_filepath(course_long_name, week_label, title)
    mp4 = (Path(download_dir) / mp4_rel).resolve()
    mp3 = mp4.with_suffix(".mp3")
    return mp4, mp3


def _is_missing(mp4: Path, mp3: Path, rule: str) -> str | None:
    has_video = mp4.exists()
    has_audio = mp3.exists()
    if rule == "video" and not has_video:
        return "mp4"
    if rule == "audio" and not has_audio:
        return "mp3"
    if rule == "both" and not (has_video and has_audio):
        parts = []
        if not has_video:
            parts.append("mp4")
        if not has_audio:
            parts.append("mp3")
        return "+".join(parts)
    return None


async def _collect_missing(scraper: CourseScraper, course_filter: str | None) -> list[tuple]:
    """(course, lec, kind) 튜플 목록을 반환한다."""
    download_dir = Config.get_download_dir()
    rule = Config.DOWNLOAD_RULE or "both"

    courses = await scraper.fetch_courses()
    if course_filter:
        courses = [c for c in courses if c.id == course_filter]
        if not courses:
            print(f"[오류] course_id={course_filter} 과목을 찾을 수 없습니다.")
            return []

    print(f"  과목 {len(courses)}개 강의 정보 로딩 중...")
    details = await scraper.fetch_all_details(courses, concurrency=3)

    missing: list[tuple] = []
    for course, detail in zip(courses, details, strict=False):
        if detail is None:
            continue
        for lec in detail.all_video_lectures:
            if lec.completion != "completed":
                continue
            if not lec.is_downloadable:
                continue
            mp4, mp3 = _expected_files(course.long_name, lec.week_label, lec.title, download_dir)
            kind = _is_missing(mp4, mp3, rule)
            if kind:
                missing.append((course, lec, kind))
    return missing


async def _recover_one(scraper: CourseScraper, course, lec, rule: str) -> DownloadResult:
    audio_only = rule == "audio"
    both = rule == "both"
    try:
        return await run_download(scraper.page, lec, course, audio_only=audio_only, both=both)
    except Exception as e:
        _log.error("복구 예외: [%s] %s — %s", course.long_name, lec.title, e, exc_info=True)
        return DownloadResult(ok=False, reason=f"exception:{type(e).__name__}")


async def main():
    parser = argparse.ArgumentParser(description="누락 다운로드 복구")
    parser.add_argument("--dry-run", action="store_true", help="목록만 출력하고 종료")
    parser.add_argument("--course", type=str, default=None, help="특정 course_id만 대상")
    parser.add_argument("--yes", "-y", action="store_true", help="확인 프롬프트 생략")
    args = parser.parse_args()

    if not Config.has_credentials():
        print("[오류] LMS 자격증명이 설정되지 않았습니다. .env를 확인하세요.")
        return 1

    # 로컬 Windows 실행 시 Docker 경로 보정
    download_dir = Config.get_download_dir()
    if download_dir.startswith("/data") and sys.platform == "win32":
        local_fallback = Path("data/downloads").resolve()
        local_fallback.mkdir(parents=True, exist_ok=True)
        Config.DOWNLOAD_DIR = str(local_fallback)
        print(f"  [보정] DOWNLOAD_DIR = {Config.DOWNLOAD_DIR}")

    rule = Config.DOWNLOAD_RULE or "both"
    print(f"  다운로드 규칙: {rule}")
    print(f"  다운로드 경로: {Config.get_download_dir()}")
    print()

    scraper = CourseScraper(username=Config.LMS_USER_ID, password=Config.LMS_PASSWORD)
    try:
        print("  LMS 로그인 중...")
        await scraper.start()
        print("  → 로그인 완료")
        print()

        missing = await _collect_missing(scraper, args.course)

        if not missing:
            print("  누락된 다운로드가 없습니다.")
            return 0

        print()
        print(f"  누락 {len(missing)}건:")
        for course, lec, kind in missing:
            print(f"    - [{course.long_name}] {lec.week_label} {lec.title} ({kind})")
        print()

        if args.dry_run:
            print("  --dry-run: 종료")
            return 0

        if not args.yes:
            ans = input(f"  위 {len(missing)}건을 복구하시겠습니까? [y/N] ").strip().lower()
            if ans != "y":
                print("  취소")
                return 0

        # ── 순차 복구 실행 ─────────────────────────────────────
        _log.info("복구 시작: %d건", len(missing))
        success = 0
        reasons: Counter = Counter()
        for i, (course, lec, kind) in enumerate(missing, 1):
            label = f"[{course.long_name}] {lec.title}"
            print(f"\n  [{i}/{len(missing)}] {label}")
            _log.info("복구 중 (%d/%d): %s", i, len(missing), label)
            result = await _recover_one(scraper, course, lec, rule)
            if result.ok:
                success += 1
                _log.info("복구 성공: %s", label)
                print(f"    → 성공")
            else:
                reasons[result.reason] += 1
                _log.warning("복구 실패: %s — reason=%s", label, result.reason)
                print(f"    → 실패 (사유={result.reason})")

        # ── 리포트 ─────────────────────────────────────────────
        print()
        print("=" * 60)
        print(f"  복구 결과: 성공 {success}/{len(missing)}")
        if reasons:
            print("  실패 사유 분포:")
            for r, n in reasons.most_common():
                print(f"    {r}: {n}건")
        _log.info("복구 종료: 성공 %d/%d, 실패 분포=%s", success, len(missing), dict(reasons))
        return 0 if success == len(missing) else 2
    finally:
        await scraper.close()
        try:
            from src.stt.transcriber import unload_model

            unload_model()
        except Exception:
            pass


if __name__ == "__main__":
    # noqa: F401 — REASON_UNSUPPORTED import는 learningx 로직 참조용 placeholder
    _ = REASON_UNSUPPORTED
    sys.exit(asyncio.run(main()))
