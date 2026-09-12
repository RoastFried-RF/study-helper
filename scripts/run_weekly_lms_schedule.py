"""매주 화요일 무인 실행용 주간 LMS 일정 러너.

Windows Task Scheduler(`SSU_LMS_Schedule_Weekly`) 가 매주 화요일 09:00 에
`uv run python -m scripts.run_weekly_lms_schedule` (WorkingDirectory=저장소 루트) 로
호출한다. 수동 점검 시에도 같은 명령으로 실행할 수 있다.

동작:
1. **미시청 강의 실제 재생(출석 처리)** — 기존 자동 모드의 재생 엔진(`run_player`,
   Plan A 실재생 → Plan B 진도 API 폴백)을 그대로 재사용해 needs_watch 영상을 순차
   시청한다. 재생 성공 후 `run_download` 로 호스트에서 다운로드→음성 변환→STT→
   AI 요약→텔레그램 요약 발송을 설정에 따라 수행한다(ffmpeg/whisper 필요).
   `--no-download` 로 다운로드 이후 단계를 생략하고, `--no-watch` 로 전체를 생략한다.
   이전 주를 포함해 재생했으나 다운로드하지 못한 강의는 재생 없이 재시도한다.
   `--watch-limit N` 은 재생만 제한하며, 0이어도 다운로드 재시도는 수행한다.
2. `scripts/export_lms_schedule` 를 **서브프로세스**로 실행해 미처리 항목/공지 JSON 수집.
   서브프로세스를 쓰는 이유: 문서화된 종료 코드(0/2/3)·stdout JSON 계약을 그대로
   재사용하고, Playwright/asyncio 수명주기를 러너 프로세스에서 격리하기 위함.
3. `calendarize_lms_schedule.build_candidates` (순수 함수) 로 캘린더 이벤트 후보 분류.
   **dry-run 전용** — 실제 Google Calendar 생성(`--apply`)은 이 러너가 절대 수행하지
   않는다(검증 주체가 별도 필요 + OAuth 토큰 미보유 전제).
4. 산출 JSON 2종(export/candidates)을 로그 디렉토리에 보존하고 30일 초과분은 정리.
5. 시청 결과 + 후보 요약을 텔레그램 다이제스트로 전송(`dispatch_if_configured`).
   `--no-telegram`은 주간 다이제스트만 생략하며 강의별 요약·오류 알림은 전송된다.

종료 코드: 0 정상 / 1 실패 (Task Scheduler '마지막 실행 결과' 로 식별).
시청 단계 전체 실패는 일정 수집을 계속하고 다이제스트 오류 항목으로 보고한 뒤 exit 1로 종료한다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from scripts.calendarize_lms_schedule import build_candidates

from src.config import KST, Config, get_data_path, get_logs_path
from src.logger import get_logger
from src.notifier.telegram_dispatch import dispatch_if_configured
from src.notifier.telegram_notifier import notify_weekly_lms_digest

_log = get_logger("run_weekly_lms_schedule")

# export 서브프로세스 상한 — 과목 수가 늘어도 무인 실행이 무한 대기하지 않게 한다.
_EXPORT_TIMEOUT_SEC = 20 * 60

# 강의당 다운로드·STT 대기 상한 — 한 강의에서 러너 진행이 무기한 멈추지 않게 한다.
_DOWNLOAD_TIMEOUT_SEC = 30 * 60

# 산출 JSON 보존 기간 — 초과분은 다음 실행에서 정리한다.
_RETENTION_DAYS = 30

# 산출 파일 접두사 (logs/ 하위. LOG-SYS-2 의 `*_*.log` 14일 정리와는 별개 규칙).
_ARTIFACT_PREFIX = "weekly_lms_"


def _repo_root() -> Path:
    """저장소 루트 — Task Scheduler 가 어디서 실행하든 경로를 고정한다."""
    return Path(__file__).resolve().parent.parent


async def _watch_pending_videos(limit: int | None, no_download: bool = False) -> dict:
    """미시청 영상을 실제 재생(출석 처리)한다 — 자동 모드 재생 엔진 재사용.

    - 대상 산출: `needs_watch` 영상 중 격리(REASON_PLAY_QUARANTINED)되지 않고
      **결석 확정(attendance == "absent")이 아닌 것**. 출석 인정 기간(+지각 기간)이
      이미 지난 강의는 재생해도 학습 시간만 기록될 뿐 출결이 바뀌지 않으므로
      (실측: 기간 경과 항목 재생 → 진도 98.78% 기록되나 출결 '결석' 유지) 제외한다.
      아직 기간이 오지 않은 강의는 `needs_watch` 가 is_upcoming 으로 이미 배제한다.
    - 재생: `run_player` (Plan A 실재생 → Plan B 진도 API 폴백), RetryPolicy.PLAY 회 재시도.
    - 상태: 성공 시 ProgressStore.mark_played, 실패 시 mark_play_failed(누적 임계
      초과 시 격리) — CUI 자동 모드와 같은 저장소(auto_progress.json)를 공유한다.
    - 브라우저 death 감지 시 자동 재시작 후 해당 강의는 건너뛴다(다음 실행에서 재시도).
    - 다운로드 결과를 저장하고, 재생 대상이 아닌 과거 강의의 다운로드 실패도 재시도한다.
      유효한 파일이 이미 있으면 저장소만 보정하며, no_download이면 두 단계 모두 생략한다.

    Returns:
        {"watch_total": 대상 수, "watched": 성공, "watch_failed": 실패,
         "watch_quarantined": 이번에 격리됨, "watch_skipped": 격리로 스킵,
         "downloaded": 다운로드 성공, "summarized": 요약 생성, "download_failed": 다운로드 실패} 통계 dict.
    """
    from src.config import RetryPolicy
    from src.downloader.result import REASON_PLAY_QUARANTINED
    from src.scraper.course_scraper import CourseScraper
    from src.service.progress_store import ProgressStore
    from src.ui.auto import _is_file_present, _recover_if_browser_dead
    from src.ui.player import run_player

    stats = {
        "watch_total": 0,
        "watched": 0,
        "watch_failed": 0,
        "watch_quarantined": 0,
        "watch_skipped": 0,
        "watch_skipped_absent": 0,
        "downloaded": 0,
        "summarized": 0,
        "download_failed": 0,
        "dl_retry_total": 0,
        "dl_retry_downloaded": 0,
    }

    def _stderr_log(msg: str) -> None:
        print(msg, file=sys.stderr, flush=True)

    scraper = CourseScraper(
        username=Config.LMS_USER_ID,
        password=Config.LMS_PASSWORD,
        headless=True,
        log_callback=_stderr_log,
    )
    await scraper.start()
    try:
        courses = await scraper.fetch_courses()
        details = await scraper.fetch_all_details(courses)

        store = ProgressStore(path=get_data_path("auto_progress.json"))
        try:
            store.load()
        except Exception as e:
            _log.warning("auto_progress.json 로드 실패(빈 상태로 진행): %s", e)

        pending = []
        for course, detail in zip(courses, details, strict=False):
            if detail is None:
                continue
            for lec in detail.all_video_lectures:
                if not lec.needs_watch:
                    continue
                # 출석 인정(+지각) 기간이 지나 결석 확정된 강의는 재생 무의미 — 제외.
                if lec.attendance == "absent":
                    stats["watch_skipped_absent"] += 1
                    continue
                entry = store.get(lec.full_url)
                if entry and entry.reason == REASON_PLAY_QUARANTINED:
                    stats["watch_skipped"] += 1
                    continue
                pending.append((course, lec))

        stats["watch_total"] = len(pending)
        targets = pending if limit is None else pending[: max(0, limit)]
        _log.info(
            "시청 대상 %d건 중 %d건 처리 시작 (결석 확정 제외 %d건)",
            len(pending),
            len(targets),
            stats["watch_skipped_absent"],
        )

        rule = (Config.DOWNLOAD_RULE or "both").strip().lower() or "both"

        async def _download(course, lec, label: str) -> bool:
            """두 단계가 같은 다운로드 실행·상태 기록·복구 경로를 공유한다."""
            try:
                from src.ui.download import run_download

                # wait_for는 코루틴 대기만 취소한다. executor의 ffmpeg/whisper 스레드는
                # 즉시 종료되지 않아 고아 스레드가 남을 수 있으나, 러너는 다음 강의로 진행한다.
                dl = await asyncio.wait_for(
                    run_download(scraper.page, lec, course, audio_only=rule == "audio", both=rule == "both"),
                    timeout=_DOWNLOAD_TIMEOUT_SEC,
                )
                if dl.ok:
                    store.mark_download_success(lec.full_url)
                    stats["downloaded"] += 1
                    if dl.summary_path:
                        stats["summarized"] += 1
                    return True
                store.mark_download_failed(lec.full_url, dl.reason or "")
                stats["download_failed"] += 1
                _log.warning("다운로드 실패: %s — reason=%s", label, dl.reason)
            except Exception as e:
                store.mark_download_failed(lec.full_url, type(e).__name__)
                stats["download_failed"] += 1
                _log.error("다운로드 예외: %s", label, exc_info=True)
                await _recover_if_browser_dead(scraper, e, label)
            return False

        stop_event = asyncio.Event()  # 무인 실행 — 외부 중단 신호 없음
        for course, lec in targets:
            label = f"[{course.long_name}] {lec.title}"
            _log.info("시청 시작: %s", label)
            played = False
            browser_died = False
            for attempt in range(1, RetryPolicy.PLAY + 1):
                if attempt > 1:
                    await asyncio.sleep(5 * attempt)
                    try:
                        await scraper.ensure_session()
                    except Exception:
                        pass
                try:
                    success, has_error = await run_player(scraper.page, lec, stop_event=stop_event)
                except Exception as e:
                    _log.error("재생 예외 (%d/%d): %s — %s", attempt, RetryPolicy.PLAY, label, e, exc_info=True)
                    # 브라우저가 죽었으면 재시작 후 이 강의는 건너뛴다(자동 모드 BUG-6 과 동일).
                    if await _recover_if_browser_dead(scraper, e, label):
                        browser_died = True
                        break
                    continue
                if success:
                    played = True
                    break
                _log.warning(
                    "재생 미완료 (%d/%d): %s%s", attempt, RetryPolicy.PLAY, label, " (오류)" if has_error else ""
                )

            if played:
                store.mark_played(lec.full_url)
                stats["watched"] += 1
                _log.info("시청 완료: %s", label)
            else:
                quarantined = store.mark_play_failed(lec.full_url)
                stats["watch_failed"] += 1
                if quarantined:
                    stats["watch_quarantined"] += 1
                    _log.warning("강의 격리(누적 재생 실패 임계 초과): %s", label)
            try:
                store.maybe_flush()
            except Exception as e:
                _log.warning("auto_progress.json 저장 실패: %s", e)

            if played and not browser_died and not no_download:
                await _download(course, lec, label)

        # 재생 제한과 별개로 전 주를 포함한 다운로드 누락을 처리한다.
        if not no_download:
            dl_targets = [
                (course, lec)
                for course, detail in zip(courses, details, strict=False)
                if detail is not None
                for lec in detail.all_video_lectures
                if not lec.needs_watch and store.needs_download_retry(lec.full_url)
            ]
            stats["dl_retry_total"] = len(dl_targets)
            for course, lec in dl_targets:
                label = f"[{course.long_name}] {lec.title}"
                if _is_file_present(course, lec, rule):
                    store.mark_download_confirmed_from_filesystem(lec.full_url)
                elif await _download(course, lec, label):
                    stats["dl_retry_downloaded"] += 1
                elif scraper.page is None or scraper.page.is_closed():
                    _log.warning("브라우저 종료로 다운로드 재시도 중단: %s — 다음 정기 실행에서 재시도", label)
                    break
                try:
                    store.maybe_flush()
                except Exception as e:
                    _log.warning("auto_progress.json 저장 실패: %s", e)

        try:
            store.flush()
        except Exception as e:
            _log.warning("auto_progress.json 최종 저장 실패: %s", e)
        _log.info(
            "다운로드 단계 완료: 성공 %d · 요약 %d · 실패 %d · 재시도 대상 %d · 재시도 성공 %d",
            stats["downloaded"],
            stats["summarized"],
            stats["download_failed"],
            stats["dl_retry_total"],
            stats["dl_retry_downloaded"],
        )
        return stats
    finally:
        await scraper.close()


def _run_export() -> tuple[int, dict | None, str]:
    """export 스크립트를 서브프로세스로 실행한다.

    Returns:
        (exit_code, stdout JSON payload(dict) 또는 None, stderr 끝부분)
    """
    proc = subprocess.Popen(
        [sys.executable, "-m", "scripts.export_lms_schedule"],
        cwd=_repo_root(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        # 자식의 stderr(한국어 진행 로그)가 Windows cp949 로 나가 utf-8 디코드 시
        # 깨지는 것을 방지 — 자식 프로세스 전체를 UTF-8 모드로 고정한다.
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    try:
        stdout, stderr = proc.communicate(timeout=_EXPORT_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        # 직접 자식만 죽이면 Playwright 가 띄운 Chromium 손자 프로세스가 고아로
        # 남는다(Windows) — 프로세스 트리 전체를 강제 종료한 뒤 예외를 올린다.
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False, timeout=30)
        proc.wait(timeout=30)
        raise
    stderr_tail = stderr.decode("utf-8", errors="replace")[-2000:]
    payload: dict | None = None
    try:
        obj = json.loads(stdout.decode("utf-8", errors="replace") or "{}")
        if isinstance(obj, dict):
            payload = obj
    except json.JSONDecodeError:
        payload = None
    return proc.returncode, payload, stderr_tail


def _event_line(event: dict) -> str:
    """create 후보 이벤트를 다이제스트 한 줄('MM-DD [HH:MM] summary')로 만든다."""
    start = event.get("start") or {}
    if "date" in start:
        date, time_part = start["date"], ""
    else:
        dt = start.get("dateTime") or ""
        date, time_part = dt[:10], f" {dt[11:16]}" if len(dt) >= 16 else ""
    return f"{date[5:]}{time_part} {event.get('summary') or ''}".strip()


def _review_line(row: dict) -> str:
    """manual_review 항목을 다이제스트 한 줄로 만든다."""
    course = (row.get("course") or "").strip()
    title = (row.get("title") or "").strip()
    reason = row.get("reason") or ""
    return f"{course} {title} ({reason})".strip()


def _write_artifacts(logs_dir: Path, stamp: str, export_payload: dict, candidates: dict) -> None:
    """산출 JSON 2종을 로그 디렉토리에 남긴다(진단·재검토용)."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    for name, obj in (("export", export_payload), ("candidates", candidates)):
        path = logs_dir / f"{_ARTIFACT_PREFIX}{stamp}_{name}.json"
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _cleanup_artifacts(logs_dir: Path) -> None:
    """보존 기간 초과 산출 JSON 을 정리한다. 실패해도 본 실행을 막지 않는다."""
    cutoff = time.time() - _RETENTION_DAYS * 86400
    for p in logs_dir.glob(f"{_ARTIFACT_PREFIX}*.json"):
        # 파일 하나의 권한/락 실패가 나머지 정리를 막지 않게 파일 단위로 삼킨다.
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            _log.warning("산출 JSON 정리 실패(%s) — 다음 실행에서 재시도", p.name)


def _notify(no_telegram: bool, stats: dict, create_top: list[str], review_top: list[str], errors: list[str]) -> None:
    """텔레그램 다이제스트를 전송한다(--no-telegram 시 생략)."""
    if no_telegram:
        return
    dispatch_if_configured(
        notify_weekly_lms_digest,
        stats=stats,
        create_top=create_top,
        review_top=review_top,
        errors=errors,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="주간 LMS 무인 러너 (미시청 강의 재생 → export → dry-run 캘린더화 → 텔레그램)"
    )
    parser.add_argument(
        "--no-telegram", action="store_true", help="주간 다이제스트 전송 생략(강의별 요약·오류 알림은 전송됨)"
    )
    parser.add_argument("--no-watch", action="store_true", help="강의 재생(출석) 단계 생략")
    parser.add_argument("--no-download", action="store_true", help="다운로드/STT/요약 단계 생략(수동 점검용)")
    parser.add_argument("--watch-limit", type=int, default=None, help="이번 실행에서 재생할 강의 수 상한(수동 점검용)")
    args = parser.parse_args(argv)

    started = datetime.now(KST)
    stamp = started.strftime("%Y%m%d_%H%M%S")
    _log.info("주간 LMS 일정 러너 시작")

    try:
        # ── 1단계: 미시청 강의 실제 재생(출석) — 실패해도 일정 수집은 계속한다 ──
        watch_stats: dict = {}
        watch_errors: list[str] = []
        if not args.no_watch:
            try:
                watch_stats = asyncio.run(_watch_pending_videos(args.watch_limit, no_download=args.no_download))
                _log.info(
                    "시청 단계 완료: 대상 %s · 성공 %s · 실패 %s · 격리 %s · 스킵 %s"
                    " · 다운로드 %s · 요약 %s · 다운실패 %s",
                    watch_stats.get("watch_total", 0),
                    watch_stats.get("watched", 0),
                    watch_stats.get("watch_failed", 0),
                    watch_stats.get("watch_quarantined", 0),
                    watch_stats.get("watch_skipped", 0),
                    watch_stats.get("downloaded", 0),
                    watch_stats.get("summarized", 0),
                    watch_stats.get("download_failed", 0),
                )
            except Exception as e:
                _log.error("시청 단계 실패 — 일정 수집은 계속 진행", exc_info=True)
                watch_errors.append(f"시청 단계 실패: {type(e).__name__}")

        # ── 2단계: 일정/공지 수집 ──
        try:
            code, payload, stderr_tail = _run_export()
        except subprocess.TimeoutExpired:
            _log.error("export 시간 초과(%d초) — 이번 주기 실행 중단", _EXPORT_TIMEOUT_SEC)
            _notify(
                args.no_telegram,
                watch_stats,
                [],
                [],
                [*watch_errors, f"export 시간 초과({_EXPORT_TIMEOUT_SEC}초)"],
            )
            return 1

        if code != 0 or payload is None or not payload.get("ok", False):
            errors = [str(e) for e in (payload or {}).get("errors") or []] or [f"export 실패(exit {code})"]
            _log.error("export 실패: exit=%s errors=%s stderr_tail=%s", code, errors, stderr_tail[-500:])
            _notify(args.no_telegram, watch_stats, [], [], watch_errors + errors)
            return 1

        items = [it for it in payload.get("items") or [] if isinstance(it, dict)]
        candidates = build_candidates(items, datetime.now(KST))

        logs_dir = get_logs_path()
        _write_artifacts(logs_dir, stamp, payload, candidates)
        _cleanup_artifacts(logs_dir)

        stats = {**watch_stats, **(payload.get("stats") or {}), **(candidates.get("stats") or {})}
        create_top = [_event_line(e) for e in candidates.get("create") or []]
        review_top = [_review_line(r) for r in candidates.get("manual_review") or []]
        export_errors = watch_errors + [str(e) for e in payload.get("errors") or []]

        _log.info(
            "주간 LMS 일정 완료: 미처리 %s · 공지 %s · 후보 %s · 수동확인 %s · 제외 %s",
            stats.get("pending", 0),
            stats.get("announcements", 0),
            stats.get("create", 0),
            stats.get("manual_review", 0),
            stats.get("skipped", 0),
        )
        _notify(args.no_telegram, stats, create_top, review_top, export_errors)
        if watch_errors:
            return 1
        return 0
    except Exception as e:
        # 무인 실행 — 어떤 예외도 exit 1 + 로그로 수렴시키고, 침묵 실패가 되지 않도록
        # 텔레그램으로도 실패 사실(예외 타입명만)을 알린다 (export 실패 경로와 대칭).
        _log.error("주간 LMS 일정 러너 실패", exc_info=True)
        _notify(args.no_telegram, {}, [], [], [f"러너 실패: {type(e).__name__}"])
        return 1


if __name__ == "__main__":
    sys.exit(main())
