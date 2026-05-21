"""누락 다운로드 복구 스크립트 (CLI).

재생(출석)은 완료됐지만 data/downloads에 파일이 없는 강의를 전수 검사한 뒤,
재생 단계를 건너뛴 채 다운로드→mp3→STT→요약 파이프라인만 재실행한다.

사용법:
    python -m scripts.recover_missing            # 대화형 (확인 후 실행)
    python -m scripts.recover_missing --dry-run  # 목록만 출력
    python -m scripts.recover_missing --course <course_id>         # 특정 과목만
    python -m scripts.recover_missing --weeks 3주차 4주차           # 특정 주차만
    python -m scripts.recover_missing --force-drift                # 파일 있지만 store 실패 기록된 drift 항목도 재다운로드
    python -m scripts.recover_missing --yes      # 확인 프롬프트 생략

환경변수:
    FFMPEG_PATH                ffmpeg 실행파일 또는 bin 디렉토리 경로
    STUDY_HELPER_DOWNLOAD_DIR  다운로드 루트 override

구조적으로 다운로드 불가능한 항목(learningx)은 자동 제외된다.
수집·실행·집계 로직은 src/service/recover_pipeline.py가 단일 소스로 제공한다.
ProgressStore 는 복구 성공/실패 시 자동 업데이트되어 auto 모드와의 drift 방지.

종료 코드 (CLI-F3):
    0  정상 종료 — 누락 없음 / dry-run / 사용자 취소 / 전건 복구 성공
    1  사전 조건 실패 — 자격증명 미설정, 지정한 course_id 미발견
    2  부분 실패 — 복구를 실행했으나 일부 항목이 실패 (success != total)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
from pathlib import Path

# ffmpeg 동적 탐지 — FFMPEG_PATH env 우선, 없으면 PATH 에서 which.
# 과거 winget ffmpeg-8.1 경로 하드코딩은 버전 업그레이드 시 파손되어 제거.
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

from src.config import Config, get_data_path  # noqa: E402
from src.downloader.result import DownloadResult  # noqa: E402
from src.logger import get_logger  # noqa: E402
from src.scraper.course_scraper import CourseScraper  # noqa: E402
from src.service.progress_store import ProgressStore  # noqa: E402
from src.service.recover_pipeline import MissingItem, collect_missing, run_recovery  # noqa: E402

_log = get_logger("recover_missing_cli")


async def main() -> int:
    """누락 다운로드 복구 진입점.

    반환값(프로세스 종료 코드, CLI-F3):
        0  누락 없음 / dry-run / 사용자 취소 / 전건 복구 성공
        1  사전 조건 실패 (자격증명 미설정, course_id 미발견)
        2  복구 실행 후 일부 항목 실패 (success != total)
    """
    parser = argparse.ArgumentParser(description="누락 다운로드 복구")
    parser.add_argument("--dry-run", action="store_true", help="목록만 출력하고 종료")
    parser.add_argument("--course", type=str, default=None, help="특정 course_id만 대상")
    parser.add_argument(
        "--weeks", nargs="+", default=None,
        help="특정 주차만 대상 (예: --weeks 3주차 4주차)",
    )
    parser.add_argument(
        "--force-drift", action="store_true",
        help="파일은 있지만 progress_store 에 실패 reason 이 남은 drift 항목도 재다운로드",
    )
    parser.add_argument("--yes", "-y", action="store_true", help="확인 프롬프트 생략")
    args = parser.parse_args()

    if not Config.has_credentials():
        print("[오류] LMS 자격증명이 설정되지 않았습니다. .env를 확인하세요.")
        return 1

    # 우선순위: STUDY_HELPER_DOWNLOAD_DIR env > Config.get_download_dir()
    # Windows 에서 Docker 경로(/data) 로 잘못 판정되면 ~/Downloads/study-helper 로 fallback
    env_override = os.environ.get("STUDY_HELPER_DOWNLOAD_DIR", "").strip()
    if env_override:
        Config.DOWNLOAD_DIR = env_override
        Path(env_override).mkdir(parents=True, exist_ok=True)
        print(f"  [env override] DOWNLOAD_DIR = {env_override}")
    else:
        download_dir = Config.get_download_dir()
        if download_dir.startswith("/data") and sys.platform == "win32":
            local_fallback = Path.home() / "Downloads" / "study-helper"
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

        courses = await scraper.fetch_courses()
        if args.course:
            courses = [c for c in courses if c.id == args.course]
            if not courses:
                print(f"[오류] course_id={args.course} 과목을 찾을 수 없습니다.")
                return 1

        print(f"  과목 {len(courses)}개 강의 정보 로딩 중...")
        details = await scraper.fetch_all_details(courses, concurrency=3)

        # ProgressStore 로드 — 복구 결과를 자동 반영해 auto 모드와 drift 방지.
        store = ProgressStore(path=get_data_path("auto_progress.json"))
        store.load()

        # --force-drift: 파일 있지만 store 에 실패 reason 남은 항목도 missing 으로 포함
        missing = collect_missing(
            courses, details,
            store=store,
            include_store_drift=args.force_drift,
        )

        # --weeks 필터 (예: ["3주차", "4주차"] → 해당 주차 접두사 매칭)
        if args.weeks:
            week_prefixes = set(args.weeks)

            def _week_prefix(lec_week_label: str) -> str:
                # "3주차(총 8주 중)" → "3주차"
                return lec_week_label.split("(")[0].strip() if lec_week_label else ""

            before = len(missing)
            missing = [m for m in missing if _week_prefix(m.lec.week_label) in week_prefixes]
            print(f"  --weeks {sorted(week_prefixes)} 필터 적용: {before} → {len(missing)}건")

        if not missing:
            print("  누락된 다운로드가 없습니다.")
            # drift 만 있었을 수 있으므로 store 저장 (reconcile 효과가 있었다면 유지)
            try:
                store.save()
            except Exception as save_exc:
                _log.warning("progress_store 저장 실패: %s", save_exc)
            return 0

        print()
        print(f"  누락 {len(missing)}건:")
        for item in missing:
            drift_tag = f" [drift: reason={item.store_reason}]" if item.store_reason else ""
            print(
                f"    - [{item.course.long_name}] {item.lec.week_label} {item.lec.title} ({item.kind}){drift_tag}"
            )
        print()

        if args.dry_run:
            print("  --dry-run: 종료")
            return 0

        if not args.yes:
            ans = input(f"  위 {len(missing)}건을 복구하시겠습니까? [y/N] ").strip().lower()
            if ans != "y":
                print("  취소")
                return 0

        def _on_progress(
            index: int, total: int, item: MissingItem, result: DownloadResult | Exception | None
        ) -> None:
            label = f"[{item.course.long_name}] {item.lec.title}"
            if result is None:
                print(f"\n  [{index}/{total}] {label}")
                return
            if isinstance(result, Exception):
                print(f"    → 실패 (예외={type(result).__name__})")
                return
            if result.ok:
                print("    → 성공")
            else:
                print(f"    → 실패 (사유={result.reason})")

        _log.info("CLI 복구 시작: %d건", len(missing))
        try:
            report = await run_recovery(
                scraper, missing, on_progress=_on_progress, store=store,
            )
        finally:
            # run_recovery 중간에 예외나 사용자 Ctrl+C 발생해도 지금까지의 상태는 저장
            try:
                store.save()
            except Exception as save_exc:
                _log.warning("progress_store 저장 실패: %s", save_exc)

        print()
        print("=" * 60)
        print(f"  복구 결과: 성공 {report.success}/{report.total}")
        if report.failed_by_reason:
            print("  실패 사유 분포:")
            for r, n in report.failed_by_reason.most_common():
                print(f"    {r}: {n}건")
        return 0 if report.success == report.total else 2
    finally:
        await scraper.close()
        # CLI-F6: safe_unload() SSOT — 예외 억제 unload 헬퍼 재사용
        from src.stt.transcriber import safe_unload

        safe_unload()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
