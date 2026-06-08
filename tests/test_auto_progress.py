"""ProgressStore 로드/저장/마이그레이션/상태 전이 단위 테스트."""

import json
from pathlib import Path

from src.service.progress_store import ProgressEntry, ProgressStore, _resolve_save_interval


def _new_store(tmp_path: Path) -> ProgressStore:
    return ProgressStore(path=tmp_path / "auto_progress.json")


# ── L1 regression: PROGRESS_SAVE_INTERVAL 환경변수 안전 파싱 ──────────────

def test_save_interval_default(monkeypatch):
    """미설정 시 기본값 5."""
    monkeypatch.delenv("PROGRESS_SAVE_INTERVAL", raising=False)
    assert _resolve_save_interval() == 5


def test_save_interval_valid(monkeypatch):
    """정상 정수는 그대로 반영."""
    monkeypatch.setenv("PROGRESS_SAVE_INTERVAL", "3")
    assert _resolve_save_interval() == 3


def test_save_interval_floor_one(monkeypatch):
    """0 이하는 최소 1 로 보정."""
    monkeypatch.setenv("PROGRESS_SAVE_INTERVAL", "0")
    assert _resolve_save_interval() == 1


def test_save_interval_non_numeric_falls_back(monkeypatch):
    """L1: 비숫자 값은 ValueError 로 import 를 깨지 않고 기본값 5 로 폴백한다."""
    monkeypatch.setenv("PROGRESS_SAVE_INTERVAL", "abc")
    assert _resolve_save_interval() == 5


def test_save_interval_empty_falls_back(monkeypatch):
    """빈 문자열도 기본값 5."""
    monkeypatch.setenv("PROGRESS_SAVE_INTERVAL", "")
    assert _resolve_save_interval() == 5


def test_load_missing_file(tmp_path: Path):
    """파일이 없으면 빈 entries 로 초기화된다."""
    store = _new_store(tmp_path)
    store.load()
    assert store.entries == {}


def test_load_corrupted_json(tmp_path: Path):
    """파손된 JSON은 빈 entries 로 안전 복구된다."""
    store = _new_store(tmp_path)
    store.path.write_text("not valid json", encoding="utf-8")
    store.load()
    assert store.entries == {}


def test_v1_to_v2_migration(tmp_path: Path):
    """v1 legacy 리스트 포맷은 played=True, downloaded=None 으로 마이그레이션된다."""
    store = _new_store(tmp_path)
    v1_urls = ["https://canvas.ssu.ac.kr/a", "https://canvas.ssu.ac.kr/b"]
    store.path.write_text(json.dumps(v1_urls), encoding="utf-8")
    store.load()
    assert set(store.entries.keys()) == set(v1_urls)
    for url in v1_urls:
        entry = store.entries[url]
        assert entry.played is True
        assert entry.downloaded is None
        assert entry.downloadable is None


def test_v2_roundtrip(tmp_path: Path):
    """v2 저장 후 로드하면 동일 상태로 복원된다."""
    store = _new_store(tmp_path)
    url = "https://canvas.ssu.ac.kr/courses/1/modules/items/100"
    store.mark_played(url)
    store.mark_download_success(url)
    store.save()

    reloaded = _new_store(tmp_path)
    reloaded.load()
    entry = reloaded.get(url)
    assert entry is not None
    assert entry.played is True
    assert entry.downloaded is True
    assert entry.downloadable is True


def test_unknown_format_fallback(tmp_path: Path):
    """version 키가 없거나 알 수 없는 포맷은 빈 entries 로 안전 복구된다."""
    store = _new_store(tmp_path)
    store.path.write_text(json.dumps({"some": "garbage"}), encoding="utf-8")
    store.load()
    assert store.entries == {}


def test_mark_played_then_failed(tmp_path: Path):
    """재생 완료 후 다운로드 실패 시 reason 이 기록되고 downloaded=False."""
    store = _new_store(tmp_path)
    url = "u1"
    store.mark_played(url)
    store.mark_download_failed(url, reason="network")
    entry = store.get(url)
    assert entry is not None
    assert entry.played is True
    assert entry.downloaded is False
    assert entry.downloadable is True
    assert entry.reason == "network"


def test_mark_unsupported(tmp_path: Path):
    """구조적 다운로드 불가(learningx 등) 항목은 downloadable=False 로 고정."""
    store = _new_store(tmp_path)
    store.mark_unsupported("u2", reason="unsupported")
    entry = store.get("u2")
    assert entry is not None
    assert entry.downloadable is False
    assert entry.downloaded is False
    assert entry.reason == "unsupported"


def test_is_fully_done(tmp_path: Path):
    """재생 완료 + (다운로드 완료 OR 다운로드 불가)이면 True."""
    store = _new_store(tmp_path)
    # case A: 재생 + 다운로드 완료
    store.mark_played("a")
    store.mark_download_success("a")
    assert store.is_fully_done("a") is True

    # case B: 재생 + 구조적 다운로드 불가
    store.mark_unsupported("b")
    assert store.is_fully_done("b") is True

    # case C: 재생만 완료, 다운로드 미완
    store.mark_played("c")
    assert store.is_fully_done("c") is False

    # case D: 미존재
    assert store.is_fully_done("nonexistent") is False


def test_needs_download_retry(tmp_path: Path):
    """재생 완료 + downloadable≠False + downloaded≠True 이면 재시도 대상."""
    store = _new_store(tmp_path)
    store.mark_played("x")
    store.mark_download_failed("x", reason="network")
    assert store.needs_download_retry("x") is True

    store.mark_download_success("x")
    assert store.needs_download_retry("x") is False


def test_retain_only_removes_orphans(tmp_path: Path):
    """LMS 에서 사라진 URL 은 store 에서도 정리된다."""
    store = _new_store(tmp_path)
    for url in ("keep1", "keep2", "orphan"):
        store.mark_played(url)

    removed = store.retain_only({"keep1", "keep2"})
    assert removed == 1
    assert set(store.entries.keys()) == {"keep1", "keep2"}


def test_retain_only_empty_set_is_safe(tmp_path: Path):
    """BUG-2 안전망: 빈 set 으로 호출되면 catastrophic delete 대신 0 반환.

    호출자(auto.py)가 fetch 부분 실패 가드를 거치는 것이 1차 방어선이지만,
    회귀로 빈 set 이 흘러들어와도 store 가 통째로 비워지지 않게 한다.
    """
    store = _new_store(tmp_path)
    for url in ("a", "b", "c"):
        store.mark_played(url)
        store.mark_download_success(url)

    removed = store.retain_only(set())
    assert removed == 0
    assert set(store.entries.keys()) == {"a", "b", "c"}


def test_mark_incomplete_resets_played(tmp_path: Path):
    """LMS가 항목을 다시 미완료로 바꾸면 played=False 및 downloaded=None 복귀."""
    store = _new_store(tmp_path)
    store.entries["u"] = ProgressEntry(played=True, downloaded=True, downloadable=True)
    store.mark_incomplete("u")
    entry = store.get("u")
    assert entry is not None
    assert entry.played is False
    assert entry.downloaded is None


def test_save_atomic_no_tmp_remains(tmp_path: Path):
    """save() 성공 후 .tmp 파일이 남지 않는다 (atomic replace 확인)."""
    store = _new_store(tmp_path)
    store.mark_played("u")
    store.save()
    tmp_file = store.path.with_suffix(store.path.suffix + ".tmp")
    assert not tmp_file.exists()
    assert store.path.exists()


# ── BUG-5: 누적 재생 실패 격리 ────────────────────────────────


def test_mark_play_failed_increments_counter(tmp_path: Path):
    """재생 실패 호출마다 카운터가 1씩 증가하고 격리되지 않는다."""
    store = _new_store(tmp_path)
    url = "u-quarantine"
    for i in range(1, 5):
        quarantined = store.mark_play_failed(url, threshold=5)
        assert quarantined is False
        entry = store.get(url)
        assert entry is not None
        assert entry.play_fail_count == i


def test_mark_play_failed_quarantines_at_threshold(tmp_path: Path):
    """누적 재생 실패가 임계에 도달하면 격리된다 (downloadable=False, reason=play_quarantined)."""
    from src.downloader.result import REASON_PLAY_QUARANTINED

    store = _new_store(tmp_path)
    url = "u-q"
    for _ in range(4):
        store.mark_play_failed(url, threshold=5)

    quarantined = store.mark_play_failed(url, threshold=5)
    assert quarantined is True
    entry = store.get(url)
    assert entry is not None
    assert entry.played is True
    assert entry.downloadable is False
    assert entry.downloaded is False
    assert entry.reason == REASON_PLAY_QUARANTINED


def test_mark_play_failed_no_double_quarantine(tmp_path: Path):
    """이미 격리된 강의는 재호출되어도 다시 격리 트리거하지 않는다 (알림 중복 방지)."""
    store = _new_store(tmp_path)
    url = "u-q"
    for _ in range(5):
        store.mark_play_failed(url, threshold=5)
    again = store.mark_play_failed(url, threshold=5)
    assert again is False


def test_mark_played_resets_play_fail_count(tmp_path: Path):
    """PROBLEM-A 회귀 방지: 정상 재생 성공 시 누적 실패 카운터가 0 으로 reset 된다.

    LMS 일시 토글 + 일시 driver crash 가 반복되어 카운터가 누적된 강의가 결국
    재생에 성공했을 때, 다음 사이클의 누적 카운트가 false-positive 로 격리
    임계에 도달하지 않도록 보장.
    """
    store = _new_store(tmp_path)
    url = "u-recover"

    # 4회 일시 실패 누적 (임계 5 미만)
    for _ in range(4):
        store.mark_play_failed(url, threshold=5)
    entry = store.get(url)
    assert entry is not None
    assert entry.play_fail_count == 4

    # 정상 재생 성공 → reset
    store.mark_played(url)
    entry = store.get(url)
    assert entry is not None
    assert entry.played is True
    assert entry.play_fail_count == 0

    # 그 이후 4회 실패가 다시 누적되어도 임계 미달 (reset 효과 검증)
    for _ in range(4):
        quarantined = store.mark_play_failed(url, threshold=5)
        assert quarantined is False
    entry = store.get(url)
    assert entry is not None
    assert entry.play_fail_count == 4
    assert entry.reason != "play_quarantined"


def test_load_v2_handles_missing_play_fail_count(tmp_path: Path):
    """기존 v2 데이터에 play_fail_count 필드가 없어도 0 으로 안전하게 로드된다."""
    store = _new_store(tmp_path)
    legacy_v2 = {
        "version": 2,
        "entries": {
            "u1": {
                "played": True, "downloaded": True, "downloadable": True,
                "reason": None, "ts": "2026-04-01T00:00:00+09:00",
            },
        },
    }
    store.path.write_text(json.dumps(legacy_v2), encoding="utf-8")
    store.load()
    e1 = store.get("u1")
    assert e1 is not None
    assert e1.play_fail_count == 0


def test_load_v2_handles_corrupted_play_fail_count(tmp_path: Path):
    """play_fail_count 가 비정상 값이어도 0 으로 fallback."""
    store = _new_store(tmp_path)
    legacy_v2 = {
        "version": 2,
        "entries": {
            "u1": {"played": True, "ts": "", "play_fail_count": "not-a-number"},
            "u2": {"played": True, "ts": "", "play_fail_count": None},
        },
    }
    store.path.write_text(json.dumps(legacy_v2), encoding="utf-8")
    store.load()
    e1 = store.get("u1")
    e2 = store.get("u2")
    assert e1 is not None and e1.play_fail_count == 0
    assert e2 is not None and e2.play_fail_count == 0


# ── M2/M6: flush() delta-merge + maybe_flush 배치 ──────────────


def test_flush_merges_concurrent_disk_changes(tmp_path: Path):
    """M2: flush 가 디스크 최신본을 re-load + merge 한다.

    프로세스 A 가 store 를 로드해 보유하는 동안 프로세스 B 가 다른 URL 을
    디스크에 기록해도, A 의 flush 가 B 의 변경을 덮어쓰지 않아야 한다.
    """
    path = tmp_path / "auto_progress.json"

    # 프로세스 A — 로드 후 url-a 변경 (아직 flush 안 함)
    a = ProgressStore(path=path)
    a.load()
    a.mark_played("url-a")

    # 그 사이 프로세스 B — url-b 를 디스크에 기록
    b = ProgressStore(path=path)
    b.load()
    b.mark_played("url-b")
    b.flush()

    # A flush — A 의 url-a + B 의 url-b 둘 다 보존돼야 함 (lost update 없음)
    a.flush()

    reloaded = ProgressStore(path=path)
    reloaded.load()
    assert reloaded.get("url-a") is not None
    assert reloaded.get("url-b") is not None


def test_flush_propagates_retain_only_deletion(tmp_path: Path):
    """M2: retain_only 로 삭제한 entry 가 flush 후 디스크에서도 사라진다."""
    path = tmp_path / "auto_progress.json"
    store = ProgressStore(path=path)
    for url in ("keep1", "keep2", "orphan"):
        store.mark_played(url)
    store.flush()

    store.retain_only({"keep1", "keep2"})
    store.flush()

    reloaded = ProgressStore(path=path)
    reloaded.load()
    assert reloaded.get("orphan") is None
    assert reloaded.get("keep1") is not None
    assert reloaded.get("keep2") is not None


def test_flush_noop_when_no_changes(tmp_path: Path):
    """변경(_dirty)이 없으면 flush 는 파일을 만들지 않는다."""
    store = ProgressStore(path=tmp_path / "auto_progress.json")
    store.load()
    store.flush()
    assert not store.path.exists()


def test_maybe_flush_batches_writes(tmp_path: Path, monkeypatch):
    """M6: maybe_flush 는 _dirty 누적이 임계 이상일 때만 flush 한다."""
    import src.service.progress_store as ps

    monkeypatch.setattr(ps, "_SAVE_INTERVAL", 3)
    store = ps.ProgressStore(path=tmp_path / "auto_progress.json")

    store.mark_played("a")          # _dirty=1
    store.maybe_flush()             # 1 < 3 → 미기록
    assert not store.path.exists()

    store.mark_played("b")          # _dirty=2
    store.mark_played("c")          # _dirty=3
    store.maybe_flush()             # 3 >= 3 → 기록
    assert store.path.exists()

    reloaded = ps.ProgressStore(path=store.path)
    reloaded.load()
    assert {"a", "b", "c"} <= set(reloaded.entries.keys())


def test_remove_then_mark_reactivates_entry(tmp_path: Path):
    """remove 후 같은 URL 재마킹 시 flush 가 재삽입한다 (_touch 가 _removed 무효화)."""
    path = tmp_path / "auto_progress.json"
    store = ProgressStore(path=path)
    store.mark_played("u")
    store.flush()

    store.remove("u")          # _removed = {u}
    store.mark_played("u")     # _touch → _removed.discard(u), _touched = {u}
    store.flush()

    reloaded = ProgressStore(path=path)
    reloaded.load()
    assert reloaded.get("u") is not None


def test_mark_then_remove_deletes_entry(tmp_path: Path):
    """mark 후 remove 시 flush 가 디스크에서 삭제한다 (_mark_removed 가 _touched 무효화)."""
    path = tmp_path / "auto_progress.json"
    store = ProgressStore(path=path)
    store.mark_played("u")
    store.flush()

    store.mark_download_success("u")  # _touched = {u}
    store.remove("u")                 # _mark_removed → _touched.discard(u), _removed = {u}
    store.flush()

    reloaded = ProgressStore(path=path)
    reloaded.load()
    assert reloaded.get("u") is None
