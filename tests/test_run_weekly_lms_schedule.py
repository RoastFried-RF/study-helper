"""주간 러너의 재생 후 다운로드 연결을 브라우저·네트워크 없이 검증한다."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from scripts import run_weekly_lms_schedule as runner

from src.config import Config, RetryPolicy
from src.downloader.result import REASON_PLAY_QUARANTINED, DownloadResult
from src.service.progress_store import ProgressStore


@pytest.fixture
def watch_env(monkeypatch, tmp_path):
    from src.scraper import course_scraper
    from src.service import progress_store
    from src.ui import auto, download, player

    courses = [SimpleNamespace(long_name=f"과목{i}") for i in range(2)]
    lectures = [
        SimpleNamespace(title=f"강의{i}", full_url=f"https://canvas.ssu.ac.kr/lecture/{i}", needs_watch=True, attendance="")
        for i in range(2)
    ]
    scraper = SimpleNamespace(
        page=object(),
        start=AsyncMock(),
        close=AsyncMock(),
        ensure_session=AsyncMock(),
        fetch_courses=AsyncMock(return_value=courses),
        fetch_all_details=AsyncMock(return_value=[SimpleNamespace(all_video_lectures=[lec]) for lec in lectures]),
    )
    store = MagicMock()
    store.get.return_value = None
    store.needs_download_retry.return_value = False
    store.mark_play_failed.return_value = False
    play = AsyncMock(return_value=(True, False))
    dl = AsyncMock(return_value=DownloadResult(ok=True, summary_path=Path("요약.txt")))
    recover = AsyncMock(return_value=False)
    present = MagicMock(return_value=False)
    monkeypatch.setattr(course_scraper, "CourseScraper", MagicMock(return_value=scraper))
    monkeypatch.setattr(progress_store, "ProgressStore", MagicMock(return_value=store))
    monkeypatch.setattr(player, "run_player", play)
    monkeypatch.setattr(download, "run_download", dl)
    monkeypatch.setattr(auto, "_recover_if_browser_dead", recover)
    monkeypatch.setattr(auto, "_is_file_present", present)
    monkeypatch.setattr(runner, "get_data_path", lambda name: tmp_path / name)
    monkeypatch.setattr(runner, "_log", MagicMock())
    monkeypatch.setattr(Config, "LMS_USER_ID", "테스트")
    monkeypatch.setattr(Config, "LMS_PASSWORD", "테스트")
    monkeypatch.setattr(Config, "DOWNLOAD_RULE", "both")
    monkeypatch.setattr(RetryPolicy, "PLAY", 2)
    monkeypatch.setattr(runner.asyncio, "sleep", AsyncMock())
    return SimpleNamespace(
        courses=courses, lectures=lectures, scraper=scraper, store=store,
        play=play, dl=dl, recover=recover, present=present,
    )


@pytest.mark.parametrize(
    ("rule", "audio_only", "both"),
    [
        ("both", False, True),
        (" AUDIO ", True, False),
        ("video", False, False),
        ("", False, True),
        (None, False, True),
        (" \t ", False, True),
    ],
)
async def test_download_once_per_target_with_rule(watch_env, monkeypatch, rule, audio_only, both):
    env = watch_env
    monkeypatch.setattr(Config, "DOWNLOAD_RULE", rule)
    order = MagicMock()
    order.attach_mock(env.play, "play")
    order.attach_mock(env.dl, "download")

    stats = await runner._watch_pending_videos(None)

    assert env.dl.await_args_list == [
        call(env.scraper.page, lec, course, audio_only=audio_only, both=both)
        for course, lec in zip(env.courses, env.lectures, strict=True)
    ]
    assert [c[0] for c in order.mock_calls] == ["play", "download", "play", "download"]
    assert stats["watched"] == stats["downloaded"] == stats["summarized"] == 2
    assert stats["watch_failed"] == stats["download_failed"] == 0
    assert env.store.mark_download_success.call_args_list == [call(lec.full_url) for lec in env.lectures]
    env.store.mark_download_failed.assert_not_called()
    env.scraper.close.assert_awaited_once()


@pytest.mark.parametrize("failure", [DownloadResult(ok=False, reason="network"), RuntimeError("다운로드 실패")])
async def test_download_failure_continues_to_next_lecture(watch_env, failure):
    env = watch_env
    env.dl.side_effect = [failure, DownloadResult(ok=True)]

    stats = await runner._watch_pending_videos(None)

    assert env.dl.await_count == env.play.await_count == 2
    assert env.dl.await_args_list[1].args[1] is env.lectures[1]
    assert stats["download_failed"] == stats["downloaded"] == 1
    assert stats["summarized"] == stats["watch_failed"] == 0
    assert stats["watched"] == 2
    env.store.mark_play_failed.assert_not_called()
    env.store.mark_download_success.assert_called_once_with(env.lectures[1].full_url)
    reason = type(failure).__name__ if isinstance(failure, Exception) else failure.reason
    env.store.mark_download_failed.assert_called_once_with(env.lectures[0].full_url, reason)
    if isinstance(failure, Exception):
        assert runner._log.error.call_args.kwargs["exc_info"] is True
        env.recover.assert_awaited_once_with(env.scraper, failure, "[과목0] 강의0")
    else:
        assert runner._log.warning.call_args.args[-1] == "network"
        env.recover.assert_not_awaited()


async def test_download_browser_death_recovers_before_next_lecture(watch_env):
    env = watch_env
    failure = RuntimeError("Target page, context or browser has been closed")
    env.dl.side_effect = [failure, DownloadResult(ok=True)]
    recovered_page = object()

    async def recover(scraper, exc, label):
        scraper.page = recovered_page
        return True

    env.recover.side_effect = recover
    order = MagicMock()
    order.attach_mock(env.play, "play")
    order.attach_mock(env.dl, "download")
    order.attach_mock(env.recover, "recover")

    stats = await runner._watch_pending_videos(None)

    env.recover.assert_awaited_once_with(env.scraper, failure, "[과목0] 강의0")
    assert [c[0] for c in order.mock_calls] == ["play", "download", "recover", "play", "download"]
    assert env.play.await_args_list[1].args[:2] == (recovered_page, env.lectures[1])
    assert env.dl.await_args_list[1] == call(
        recovered_page, env.lectures[1], env.courses[1], audio_only=False, both=True
    )
    assert stats["download_failed"] == stats["downloaded"] == 1
    assert stats["watched"] == 2
    assert stats["watch_failed"] == stats["summarized"] == 0
    assert runner._log.error.call_args.kwargs["exc_info"] is True
    env.store.mark_play_failed.assert_not_called()
    env.scraper.close.assert_awaited_once()


async def test_no_download_preserves_watching(watch_env):
    stats = await runner._watch_pending_videos(None, no_download=True)

    watch_env.dl.assert_not_awaited()
    assert watch_env.play.await_count == stats["watched"] == 2
    assert stats["downloaded"] == stats["summarized"] == stats["download_failed"] == 0


async def test_browser_death_skips_download_only_for_affected_lecture(watch_env):
    env = watch_env
    env.play.side_effect = [RuntimeError("브라우저 종료"), (True, False)]
    env.recover.return_value = True

    stats = await runner._watch_pending_videos(None)

    env.recover.assert_awaited_once()
    env.dl.assert_awaited_once_with(env.scraper.page, env.lectures[1], env.courses[1], audio_only=False, both=True)
    env.store.mark_play_failed.assert_called_once_with(env.lectures[0].full_url)
    assert stats["watch_failed"] == stats["watched"] == stats["downloaded"] == 1
    assert stats["download_failed"] == 0


@pytest.mark.parametrize("play_result", [(False, True), RuntimeError("재생 실패")])
async def test_exhausted_play_retries_still_download_once(watch_env, play_result):
    env = watch_env
    env.play.side_effect = [play_result, play_result]
    env.store.mark_play_failed.return_value = True

    stats = await runner._watch_pending_videos(1)

    assert env.play.await_count == 2
    env.dl.assert_awaited_once_with(env.scraper.page, env.lectures[0], env.courses[0], audio_only=False, both=True)
    assert stats["watched"] == 0
    assert stats["watch_failed"] == stats["watch_quarantined"] == stats["downloaded"] == 1


@pytest.mark.parametrize("skip", ["completed", "absent", "quarantined"])
async def test_excluded_lectures_are_not_downloaded(watch_env, skip):
    env = watch_env
    if skip == "completed":
        env.lectures[0].needs_watch = False
    elif skip == "absent":
        env.lectures[0].attendance = "absent"
    else:
        env.store.get.side_effect = [SimpleNamespace(reason=REASON_PLAY_QUARANTINED), None]

    stats = await runner._watch_pending_videos(None)

    assert stats["watch_total"] == env.play.await_count == 1
    env.dl.assert_awaited_once_with(env.scraper.page, env.lectures[1], env.courses[1], audio_only=False, both=True)


@pytest.mark.parametrize(
    ("rule", "normalized", "audio_only", "both"),
    [("both", "both", False, True), (" AUDIO ", "audio", True, False),
     ("video", "video", False, False), ("", "both", False, True),
     (None, "both", False, True), (" \t ", "both", False, True)],
)
async def test_download_backlog_ignores_zero_watch_limit(watch_env, monkeypatch, rule, normalized, audio_only, both):
    env = watch_env
    for lec in env.lectures:
        lec.needs_watch = False
    env.store.needs_download_retry.return_value = True
    monkeypatch.setattr(Config, "DOWNLOAD_RULE", rule)

    stats = await runner._watch_pending_videos(0)

    env.play.assert_not_awaited()
    assert env.dl.await_args_list == [
        call(env.scraper.page, lec, course, audio_only=audio_only, both=both)
        for course, lec in zip(env.courses, env.lectures, strict=True)
    ]
    assert env.present.call_args_list == [
        call(course, lec, normalized) for course, lec in zip(env.courses, env.lectures, strict=True)
    ]
    assert env.store.mark_download_success.call_args_list == [call(lec.full_url) for lec in env.lectures]
    assert stats["dl_retry_total"] == stats["dl_retry_downloaded"] == stats["downloaded"] == stats["summarized"] == 2
    assert stats["watched"] == stats["download_failed"] == 0
    assert env.store.maybe_flush.call_count == 2
    env.store.flush.assert_called_once()


async def test_backlog_existing_files_confirmed_without_download(watch_env):
    env = watch_env
    env.lectures[0].needs_watch = False
    env.store.needs_download_retry.return_value = True
    env.present.return_value = True

    stats = await runner._watch_pending_videos(0)

    env.play.assert_not_awaited()
    env.dl.assert_not_awaited()
    env.store.mark_download_confirmed_from_filesystem.assert_called_once_with(env.lectures[0].full_url)
    env.store.mark_download_success.assert_not_called()
    env.store.maybe_flush.assert_called_once()
    env.store.flush.assert_called_once()
    assert stats["dl_retry_total"] == 1
    assert stats["downloaded"] == stats["summarized"] == stats["dl_retry_downloaded"] == 0


@pytest.mark.parametrize("failure", [DownloadResult(ok=False, reason="network"), RuntimeError("브라우저 종료")])
async def test_backlog_failure_recorded_and_next_download_continues(watch_env, failure):
    env = watch_env
    for lec in env.lectures:
        lec.needs_watch = False
    env.store.needs_download_retry.return_value = True
    env.dl.side_effect = [failure, DownloadResult(ok=True)]
    recovered_page = object()

    async def recover(scraper, exc, label):
        scraper.page = recovered_page
        return True

    env.recover.side_effect = recover
    stats = await runner._watch_pending_videos(None)

    env.play.assert_not_awaited()
    reason = type(failure).__name__ if isinstance(failure, Exception) else failure.reason
    env.store.mark_download_failed.assert_called_once_with(env.lectures[0].full_url, reason)
    env.store.mark_download_success.assert_called_once_with(env.lectures[1].full_url)
    if isinstance(failure, Exception):
        env.recover.assert_awaited_once_with(env.scraper, failure, "[과목0] 강의0")
        assert env.dl.await_args_list[1].args[0] is recovered_page
        assert runner._log.error.call_args.kwargs["exc_info"] is True
    else:
        env.recover.assert_not_awaited()
        assert runner._log.warning.call_args.args[-1] == "network"
    assert stats["download_failed"] == stats["downloaded"] == stats["dl_retry_downloaded"] == 1
    assert stats["dl_retry_total"] == env.store.maybe_flush.call_count == 2


async def test_no_download_skips_both_phases_with_backlog(watch_env):
    env = watch_env
    env.lectures[0].needs_watch = False
    env.store.needs_download_retry.return_value = True

    stats = await runner._watch_pending_videos(None, no_download=True)

    assert env.play.await_count == stats["watched"] == 1
    env.dl.assert_not_awaited()
    env.present.assert_not_called()
    env.store.mark_download_success.assert_not_called()
    env.store.mark_download_failed.assert_not_called()
    env.store.mark_download_confirmed_from_filesystem.assert_not_called()
    assert stats["dl_retry_total"] == stats["downloaded"] == stats["download_failed"] == 0


async def test_backlog_runs_after_play_browser_death(watch_env):
    env = watch_env
    env.lectures[1].needs_watch = False
    env.store.needs_download_retry.return_value = True
    env.play.side_effect = RuntimeError("브라우저 종료")
    env.recover.return_value = True

    stats = await runner._watch_pending_videos(None)

    env.recover.assert_awaited_once()
    env.dl.assert_awaited_once_with(env.scraper.page, env.lectures[1], env.courses[1], audio_only=False, both=True)
    assert stats["watch_failed"] == stats["dl_retry_downloaded"] == 1


async def test_download_failure_persists_into_cross_week_retry(watch_env, monkeypatch, tmp_path):
    from src.service import progress_store

    env = watch_env
    path = tmp_path / "auto_progress.json"
    stores = []

    def new_store(**kwargs):
        store = ProgressStore(**kwargs)
        stores.append(store)
        return store

    monkeypatch.setattr(progress_store, "ProgressStore", new_store)
    env.lectures[0].week_label = "1주차"
    env.dl.return_value = DownloadResult(ok=False, reason="network")
    first = await runner._watch_pending_videos(1)
    assert first["watched"] == first["download_failed"] == 1
    persisted = ProgressStore(path=path)
    persisted.load()
    assert persisted.needs_download_retry(env.lectures[0].full_url)

    # 다음 주 LMS 조회에서는 지난 강의가 시청 완료로 보인다.
    env.lectures[0].needs_watch = False
    env.lectures[1].week_label = "2주차"
    env.play.reset_mock()
    env.dl.reset_mock()
    env.dl.return_value = DownloadResult(ok=True, summary_path=Path("요약.txt"))
    second = await runner._watch_pending_videos(0)

    env.play.assert_not_awaited()
    env.dl.assert_awaited_once_with(env.scraper.page, env.lectures[0], env.courses[0], audio_only=False, both=True)
    assert stores[0] is not stores[1]
    assert second["dl_retry_total"] == second["downloaded"] == second["summarized"] == 1
    persisted.load()
    assert persisted.is_fully_done(env.lectures[0].full_url)
    assert not persisted.needs_download_retry(env.lectures[0].full_url)


@pytest.mark.parametrize("flags", [[], ["--no-download"], ["--no-watch"]])
def test_main_passes_download_flag_and_preserves_stats(monkeypatch, tmp_path, flags):
    watch = AsyncMock(return_value={"downloaded": 2, "summarized": 1, "download_failed": 0})
    notify = MagicMock()
    monkeypatch.setattr(runner, "_watch_pending_videos", watch)
    monkeypatch.setattr(runner, "_run_export", lambda: (0, {"ok": True, "items": []}, ""))
    monkeypatch.setattr(runner, "get_logs_path", lambda: tmp_path)
    monkeypatch.setattr(runner, "_write_artifacts", MagicMock())
    monkeypatch.setattr(runner, "_cleanup_artifacts", MagicMock())
    monkeypatch.setattr(runner, "_notify", notify)
    monkeypatch.setattr(runner, "_log", MagicMock())

    assert runner.main(["--watch-limit", "1", *flags]) == 0

    if "--no-watch" in flags:
        watch.assert_not_awaited()
        assert "summarized" not in notify.call_args.args[1]
    else:
        watch.assert_awaited_once_with(1, no_download="--no-download" in flags)
        assert notify.call_args.args[1]["summarized"] == 1
