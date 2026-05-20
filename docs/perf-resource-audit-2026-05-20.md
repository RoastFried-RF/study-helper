# study-helper 성능·자원 Audit & UltraPlan (2026-05-20)

> Claude + Codex 3-Round 페어 감사. 열아홉개 finding.
> 이 UltraPlan 만 따르면 19개를 전부 수정 가능.

날짜: 2026-05-20
감사자: Claude Opus 4.7 + Codex (GPT-5.x xhigh)
대상 커밋: main HEAD (1da09d6)

---

## 1. Audit 결과 요약 테이블

| ID | 위치 | 주장 | 판정 | 신뢰도 |
|---|---|---|---|---|
| H1 | download_pipeline.py:168 | convert_to_mp3 동기 블로킹 | CONFIRMED | certain |
| H2 | download_pipeline.py:248 | notify_summary_complete 동기 블로킹 | CONFIRMED | certain |
| H3 | video_downloader.py:468,529 | _stream_download 동기 블로킹 | CONFIRMED | certain |
| M2 | progress_store.py:72,135 | lock/save 분리 — TOCTOU 가능 | CONFIRMED | certain |
| M3 | transcriber.py:35 | psutil 이 Docker cgroup limit 무시 | CONFIRMED | certain |
| M4 | transcriber.py:140 | WhisperModel cpu_threads 미설정 | CONFIRMED | certain |
| M5 | auto.py:381,537 + main.py:97 | 텔레그램 notify 동기 블로킹 | CONFIRMED | certain |
| M6 | auto.py:462,517,531,535 | per-item _save_store() 반복 호출 → 누적 O(N²) | CONFIRMED | likely |
| M7 | summarizer.py:79 | 긴 텍스트 토큰 한도 초과 미처리 | CONFIRMED | certain |
| M8 | config.py:332 | _save_env lock 범위 좌음 (read 제외) | CONFIRMED | certain |
| M9 | paths.py:60 | _find_course_dir 매 호출 O(N) 스캔 | CONFIRMED | likely |
| L1 | background_player.py:630 | cumulative_page=int(…) 가 0 산출 — `:1255` 의 max(1,…) 가드와 불일치 | CONFIRMED | certain |
| L2 | background_player.py:795 | add_init_script 가 재사용 page 에 강의마다 누적 (remove 없음) | CONFIRMED | certain |
| L3 | auto.py:259 | _input_listener stdin readline 스레드가 Ctrl+C 종료 시 block 잔류 | CONFIRMED | likely |
| L4 | summarizer.py:126 | Gemini client 명시적 close 없음 (GC 의존) | CONFIRMED | likely |
| L5 | crypto.py:136 | _fernet() 가 매 호출 _load_or_create_key() — key bytes 미캐시 | CONFIRMED | certain |
| L6 | deadline_checker.py:94,220 | load/save TOCTOU (lock 없음) | CONFIRMED | certain |
| L7 | scraper/models.py:전체 | @property 매 접근 재계산 | CONFIRMED | likely |
| L8 | api/routes/config.py, notify.py | 동기 블로킹 라우트 | CONFIRMED | certain |

**우선순위**: HIGH(H1-H3) — asyncio 루프 freeze, 즉시 수정 필수.
**MEDIUM(M2-M9)** — 데이터 일관성/리소스 한도/성능.
**LOW(L1-L8)** — 정확성·GC·최적화.

---
## 2. UltraPlan — 워크스트림별 Fix 설계

### WS-0: locked_transaction 헬퍼 추출 (XS, 선행 필수)

**대상 finding**: M2, M8, L6 공통 패턴
**파일**: `src/util/atomic_write.py`

```python
from contextlib import contextmanager

@contextmanager
def locked_transaction(path, *, load_fn, save_fn):
    """단일 file_lock 안에서 load -> yield(mutate) -> save 원자적 실행."""
    with file_lock(path):
        data = load_fn()
        ok = False
        try:
            yield data
            ok = True
        finally:
            if ok:
                save_fn(data)
```

**회귀 위험**: 기존 `file_lock` + `atomic_write_text` API 변경 없음.
**검증**: `tests/test_atomic_write.py` 동시성 테스트 추가.

> **[Codex 의견]** `finally` 에서 save 하면 mutate 중 예외 발생 시 corrupt 데이터 저장 위험.
> `ok` 플래그로 정상 완료 시에만 save (위 코드 반영).

> **[v2 정정 — 적대적 검토 BLOCKER 대응] `locked_transaction` 의 2가지 한계를 docstring·문서에 명시한다:**
> 1. **재진입 금지**: POSIX `flock(LOCK_EX)` 는 동일 프로세스가 같은 path 에 중첩 호출 시
>    self-deadlock (atomic_write.py file_lock 주석도 "재진입 보장 안 함"). `load_fn`/`save_fn`/
>    `yield` 본문 안에서 같은 path 의 `locked_transaction`·`file_lock` 재호출 **금지**.
> 2. **Windows 한계**: Windows `file_lock` 은 `msvcrt.locking(LK_NBLCK)` non-blocking advisory —
>    획득 실패 시 경고 후 락 없이 진행. 즉 **Windows 에서는 cross-process 직렬화를 보장하지
>    않는다**. 주 배포 환경은 Docker/Linux(flock 권위적)이며, Windows 네이티브 API 서버 +
>    동시 유지보수 스크립트 조합은 비권장 — 이 제약을 `.env.example`/README 운영 노트에 명시.
>    (Windows 동시성 강보장이 필요하면 `O_EXCL` 락파일 polling 등 별도 이슈로 분리.)

---

### WS-1: Event Loop Blocking 제거 (L, 의존성 없음)

**대상 finding**: H1, H2, H3, M5, L8

> **[Claude 검수]** `asyncio.get_event_loop()` 는 running loop 안에서 deprecated.
> 이 코드베이스는 이미 `download_pipeline.py:187` 에서 `asyncio.get_running_loop()` 사용 —
> 아래 모든 스니펫은 `get_running_loop()` 으로 통일한다.

#### H1 — download_pipeline.py:165-172 (CONVERT 단계)
```python
# Before
result.mp3_path = convert_to_mp3(mp4_path)

# After
loop = asyncio.get_running_loop()
result.mp3_path = await loop.run_in_executor(None, lambda: convert_to_mp3(mp4_path))
```

#### H2 — download_pipeline.py:239-265 (NOTIFY 단계)
실제 호출 시그니처는 `notify_summary_complete(bot_token=, chat_id=, course_name=, week_label=,
lecture_title=, summary_text=, summary_path=, auto_delete_files=)` (telegram_notifier.py:229).
키워드 인자 전체를 lambda 로 캡처해 위임:
```python
# Before
ok = notify_summary_complete(bot_token=tg_token, chat_id=tg_chat_id, ...)

# After
loop = asyncio.get_running_loop()
ok = await loop.run_in_executor(
    None, lambda: notify_summary_complete(bot_token=tg_token, chat_id=tg_chat_id, ...)
)
```
> `summary_path.read_text()` (동기 디스크 읽기) 도 같은 executor 안으로 넣거나 위임 전 1회 수행.

#### H3 — video_downloader.py:493 (`_stream_download` **호출 1곳**)
> **[Claude 검수]** Codex 초안은 "468/493/529/566 4곳 모두 수정"이라 했으나 **오류**.
> `_stream_download` 는 **정의 1개 + 호출 1개(:493)** 뿐이다. `:529`(`requests.get`)·`:566`
> (`iter_content`) 는 `_stream_download` **정의 내부**라 별도 수정 대상이 아니다.
> `:468` 은 `async def download_video_with_browser` 시그니처. 수정은 **호출부 :493 한 곳**.
```python
# video_downloader.py:493 — Before (동기 호출, 재시도 루프 내부)
_stream_download(url, save_path, on_progress, attempt=attempt, cookies=cookies, referer=referer)

# After — 호출을 스레드로 위임
await asyncio.to_thread(
    _stream_download, url, save_path, on_progress,
    attempt=attempt, cookies=cookies, referer=referer,
)
```
> **[Codex 의견]** `on_progress` 콜백이 Rich Live 등 메인 스레드 UI 갱신을 하면
> `asyncio.to_thread` 워커 스레드에서 thread-safety 문제 발생.
> `loop.call_soon_threadsafe(on_progress, ...)` 로 래핑하거나 `asyncio.Queue` 경유 권장.
> **[Claude 검수]** 현 `on_progress` 는 `progress.update(task_id, ...)` (rich Progress) 호출
> (ui/download.py:196). rich 는 thread-safe 갱신을 보장하지 않으므로 PR-2 에서 콜백을
> `loop.call_soon_threadsafe` 로 래핑하는 것을 **필수**로 한다.

#### M5 — auto.py:381(deadline) · 537(download_gaps) · main.py:97(deadline)
```python
loop = asyncio.get_running_loop()
dl_count = await loop.run_in_executor(
    None, lambda: check_and_notify_deadlines(courses, details, token=tg[0], chat_id=tg[1])
)
```
> `_notify_download_gaps(missing_entries)` (auto.py:537) 도 동일하게 위임. `_tg_error_notify`/
> `_tg_quarantine_notify` 는 강의 처리 흐름 중 호출 — 동일 패턴 적용.

#### L8 — api/routes/config.py · notify.py (sync def 라우트의 blocking telegram)
> **[Claude 검수]** L8 은 **이벤트 루프 freeze 가 아니다**. Starlette 는 sync `def` 핸들러를
> 자동으로 threadpool 에서 실행하므로 `verify_telegram`·`send_notification` 의 blocking
> telegram 요청은 루프를 막지 않고 worker thread 만 점유한다. localhost 단일 사용자
> (Electron 연동) 환경에서 threadpool 고갈 위험은 사실상 없음.
> → **L8 은 선택적 개선**. 강제 수정 대상 아님. 적용한다면:
```python
# 라우트를 async def 로 바꾸고 blocking 호출만 위임
@router.post("/telegram/verify")
async def verify_telegram(body: TelegramUpdate) -> dict[str, object]:
    ok, error = await asyncio.to_thread(verify_bot, body.bot_token, body.chat_id)
    return {"ok": ok, "error": error}
```
> 우선순위: WS-1 내에서 **가장 낮음**. PR-2 범위에 넣되 시간 부족 시 후순위로 미뤄도 무방.

**SSOT 재사용**: `Config.RetryPolicy` / `dispatch_if_configured` 변경 없음.
**검증**: `pytest tests/ -k download_pipeline` + uvicorn 실행 후 `curl /health` 가 CONVERT 중에도 즉시 응답.

---

### WS-2: State File Transactions 강화 (S, WS-0 이후)

**대상 finding**: M2, M8, L6

#### M2 — progress_store.py (단일 persistence 모델 — `flush()` merge)
> **[v2 정정 — BLOCKER: persistence 이중구조 제거]** v1 은 `locked_update`(mutation당
> load-mutate-save)와 M6 배치 flush 두 메커니즘을 두어 호출자별 사용처가 불명했다.
> v2 는 **단일 모델**로 통일: `ProgressStore` 는 in-memory 가 SoT, `save()` 를 폐기하고
> **`flush()` 하나**로 수렴. `flush()` 는 `locked_transaction` 안에서 **디스크 최신본을
> re-load → in-memory delta 를 entry(URL) 단위로 merge → atomic_write** 한다.
```python
class ProgressStore:
    def flush(self) -> None:
        """in-memory 변경분을 디스크에 cross-process 안전하게 반영.
        lock 안에서 디스크 최신본을 다시 읽어 merge — recover/reconcile 등 타 프로세스의
        변경을 덮어쓰지 않는다 (M2 lost-update 방지)."""
        if self._dirty_count == 0:
            return
        with locked_transaction(
            self.path,
            load_fn=lambda: _load_entries(self.path),   # 디스크 최신본
            save_fn=lambda merged: atomic_write_text(
                self.path,
                json.dumps({"version": 2,
                            "entries": {u: asdict(e) for u, e in merged.items()}},
                           ensure_ascii=False, sort_keys=True),
                mode=0o600,
            ),
        ) as disk_entries:
            # merge 규칙: 이번 프로세스가 건드린 URL 은 in-memory 가 우선,
            # 건드리지 않은 URL 은 디스크본 유지 → 타 프로세스 변경 보존.
            for url in self._touched_urls:
                disk_entries[url] = self.entries[url]
            self.entries = disk_entries          # in-memory 를 merge 결과로 동기화
        self._dirty_count = 0
        self._touched_urls.clear()
```
> 모든 mutator(`mark_played`/`mark_download_success`/…)는 `self._touched_urls.add(url)` +
> `self._dirty_count += 1` 만 추가. **`save()` 호출부(auto.py:462/517/531/535,
> recover_missing.py, reconcile_progress.py)는 전부 `flush()` 로 교체**.
> M6 의 배치(주기 flush)는 이 `flush()` 의 호출 빈도 조절(§WS-4 M6)일 뿐 별도 메커니즘이 아님.

**호출자별 flush 시점 (명세)**:
| 호출자 | flush 시점 |
|--------|-----------|
| `auto.py` 자동 모드 | `PROGRESS_SAVE_INTERVAL`(기본 5) 강의마다 + 사이클 종료 + `finally` |
| `recover_pipeline.run_recovery` | 호출자(스크립트/UI)가 종료 시 1회 (현행 유지) |
| `reconcile_progress.py --apply` | reconcile 후 1회 |
| `recover_missing.py` | `finally` 에서 1회 |

#### M8 — config.py:332
```python
# Before: read 는 lock 밖, write 만 lock 안
env_data = _read_env()       # lock 밖
with file_lock(path):
    atomic_write_text(...)   # lock 안

# After: locked_transaction 으로 통합
with locked_transaction(
    path,
    load_fn=_read_env,
    save_fn=lambda d: atomic_write_text(path, _serialize(d))
) as env_data:
    env_data.update(new_values)
```

#### L6 — deadline_checker.py:94, 220
```python
with locked_transaction(
    _NOTIFIED_PATH,
    load_fn=_load_notified,
    save_fn=_save_notified
) as notified:
    notified[key] = True
```

**SSOT 재사용**: `atomic_write_text`, `file_lock` (src/util/atomic_write.py).
**검증**: `tests/test_atomic_write.py` concurrent write 테스트.

---
### WS-3: 리소스 한도 감지 개선 (S, 의존성 없음)

**대상 finding**: M3, M4

#### M3 — transcriber.py:35 `_available_memory_mb`
> **[v2 정정 — MAJOR: limit ≠ available]** cgroup `memory.max`/`limit_in_bytes` 는 **한도**다.
> STT 로드 시점엔 Python(~100MB)+Chromium(~400MB)+버퍼가 이미 점유 중이므로 한도만
> 보면 여전히 OOM 가능. **`한도 - 현재사용량`** 을 available 로 계산한다 (cgroup v2
> `memory.current`, v1 `memory.usage_in_bytes`). host 와의 min 도 취해 보수적으로.
```python
from pathlib import Path

def _read_int(p: Path) -> int | None:
    try:
        v = p.read_text().strip()
        return None if v == "max" else int(v)
    except (OSError, ValueError):
        return None

def _cgroup_available_mb() -> int | None:
    # cgroup v2
    limit = _read_int(Path("/sys/fs/cgroup/memory.max"))
    used  = _read_int(Path("/sys/fs/cgroup/memory.current"))
    if limit is None:  # cgroup v1
        limit = _read_int(Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"))
        used  = _read_int(Path("/sys/fs/cgroup/memory/memory.usage_in_bytes"))
    if limit is None or limit >= 2**62:   # 미설정/무제한 sentinel
        return None
    free = limit - (used or 0)
    return max(0, free) // (1024 * 1024)

def _available_memory_mb() -> int | None:
    cg = _cgroup_available_mb()
    try:
        import psutil
        host = psutil.virtual_memory().available // (1024 * 1024)
    except ImportError:
        return cg              # cgroup 값만 (없으면 None → 기존 동작 유지)
    return min(cg, host) if cg is not None else host
```
> 컨테이너 안: cgroup free 와 host available 의 **min** → 가장 보수적 값으로 다운그레이드 판정.

#### M4 — transcriber.py:140
```python
model = WhisperModel(
    model_size, device="cpu", compute_type="int8",
    cpu_threads=int(os.environ.get("WHISPER_CPU_THREADS", "2")),
    num_workers=1,
)
```

**.env.example**: `WHISPER_CPU_THREADS=2   # CTranslate2 cpu_threads 상한 (기본 2)`

> **[Codex 의견]** `deploy.resources.limits.cpus`는 `docker compose up` (swarm) 전용.
> `run --rm` 진입인 이 프로젝트는 `WHISPER_CPU_THREADS` 만으로 충분.

**검증**: 컨테이너 내 `/sys/fs/cgroup/memory.max` 확인 + WhisperModel 로드 로그.

---

### WS-4: 최적화 묶음 (M, WS-0 이후)

**대상 finding**: M6, M9, L2, L5, L7

#### M6 — auto.py:462/517/531/535 (배치 flush — WS-2 `flush()` 빈도 조절)

> **[v2 정정]** M6 은 별도 메커니즘이 아니라 **WS-2 `flush()` 의 호출 빈도 조절**이다.
> WS-2 의 `flush()` 가 이미 `locked_transaction` 기반 re-load+merge 라 cross-process 안전.
> M6 은 그 `flush()` 를 강의마다가 아니라 N개마다 호출하도록 mutator 에 카운터를 둔다.
```python
_SAVE_INTERVAL = int(os.environ.get("PROGRESS_SAVE_INTERVAL", "5"))

class ProgressStore:
    # mutator 공통 후처리 (mark_played / mark_download_success / mark_download_failed /
    # mark_play_failed / mark_unsupported / mark_incomplete 끝에서 호출)
    def _touch(self, url: str) -> None:
        self._touched_urls.add(url)
        self._dirty_count += 1

    def maybe_flush(self) -> None:
        """N개 누적 시 flush. auto.py 강의 루프에서 _save_store() 대신 호출."""
        if self._dirty_count >= _SAVE_INTERVAL:
            self.flush()      # WS-2 의 locked re-load+merge flush
```
> auto.py 변경: `_save_store(store)` 호출(462/517/531/535) → `store.maybe_flush()`.
> 사이클 종료 + `finally` 에서는 `store.flush()` 강제 호출 (잔여 dirty 반영).
> `flush()` 예외 시 `_dirty_count`/`_touched_urls` 보존 — 다음 호출에서 재시도.

**트레이드오프 (M6)**:

| 항목 | 내용 |
|---|---|
| 장점 | 강의 N개 처리 시 디스크 쓰기·fsync·lock N → ceil(N/5) 회 |
| 단점 | crash 시 최대 (PROGRESS_SAVE_INTERVAL-1)개 진행 상태 유실 |
| 완화 | `PROGRESS_SAVE_INTERVAL=1` → 현행 per-item 동작 복구. 사이클 종료/finally 는 항상 flush |
| 권장값 | 5 (기본). 안정성 우선 환경 1 |

> **[v2]** M6 은 WS-2(M2) 의 `flush()` 에 전적으로 의존 — PR 상 **PR-3 에 통합**(§4 참조).

#### M9 — paths.py:60 (course_dir 캐시)

```python
import threading
_course_dir_cache: dict = {}
_cache_lock = threading.Lock()

def clear_course_dir_cache() -> None:
    """스케줄 사이클 시작 시 호출. auto.py 진입부에서 clear."""
    with _cache_lock:
        _course_dir_cache.clear()

def _find_course_dir(course_name: str, download_dir) -> object:
    key = f"{course_name}::{download_dir}"
    with _cache_lock:
        if key in _course_dir_cache:
            return _course_dir_cache[key]
    result = _original_find_course_dir(course_name, download_dir)
    with _cache_lock:
        _course_dir_cache[key] = result
    return result
```

> **[Codex 의견]** `expected_paths(..., course_dir_override=None)` 명시 주입 방식 권장.
> 모듈 레벨 캐시 유지 시 `threading.Lock` 필수.

#### L2 — background_player.py:795 (page-level 1회 — context 이동 금지)
> **[v2 정정 — MAJOR: context 오염]** Codex 초안의 `context.add_init_script` 는 scraper 의
> dashboard·강의목록 페이지에까지 `canPlayType`/`MediaSource.isTypeSupported` override 를
>적용해 **스크래핑 페이지를 오염**시킨다. H.264 override 는 재생 전용이어야 한다.
> → context 가 아니라 **재사용 page 에 1회만** 등록한다. init script 는 duration 무관
> 정적 JS 이고 `window.__h264OverrideApplied` 가드가 이미 멱등이므로, page 객체에
> "등록됨" 플래그를 두고 최초 `play_lecture` 에서만 add:
```python
# play_lecture() 내 — _using_fake_video 분기
if not getattr(page, "_h264_override_added", False):
    await page.add_init_script(_H264_OVERRIDE_SCRIPT)
    page._h264_override_added = True   # 재사용 page 에 1회만 — 누적 제거
```
> 강의별 `page.route("**/*.mp4", _serve_fake)` (duration별 fake webm) 는 **현행 유지** —
> `_cleanup` 의 `page.unroute` 와 짝. route 는 누적 대상이 아니라 정상 cleanup 됨.
> 부작용 없음: 스크래핑 page 와 재생 page 는 동일 `scraper.page` 이나, override 는
> 멱등 가드 + 재생 시점에만 의미 있는 JS 라 스크래핑 DOM 파싱에 영향 없음.

#### L5 — crypto.py:57 (Fernet 캐시)
```python
_fernet_cache = None

def _get_fernet():
    global _fernet_cache
    if _fernet_cache is None:
        _fernet_cache = Fernet(_load_key())
    return _fernet_cache
```

#### L7 — scraper/models.py (호출부 local cache — `@cached_property` 금지)
> **[Claude 검수]** Codex 초안의 `@cached_property` 는 **부적절**. 두 가지 오류:
> (1) `all_video_lectures` 는 `Course` 가 아니라 **`CourseDetail`** 의 property.
> (2) `LectureItem.completion` 은 런타임에 mutate 됨 (`auto.py:649 lec.completion = "completed"`).
> `pending_video_count`/`needs_watch` 의존 property 를 캐시하면 **stale** 이 된다.
> → 모델에 캐시를 박지 말고 **호출부에서 지역 변수로 1회 계산** 후 재사용한다.
```python
# Before — download_state.py / auto.py 등에서 반복 접근
for lec in detail.all_video_lectures: ...      # 매번 리스트 재생성
n = detail.total_video_count                   # 또 재순회
m = detail.pending_video_count                 # 또 재순회

# After — 호출부 지역 변수
videos = detail.all_video_lectures             # 1회만
for lec in videos: ...
n = len(videos)
m = sum(1 for lec in videos if lec.needs_watch)
```
> 모델 dataclass 는 무변경. mutation 타이밍 안전성을 호출부가 보장.

**회귀 위험**: 없음 (순수 호출부 리팩토링).
**검증**: `pytest tests/ -k models` + auto 모드 2사이클 후 진행 파일 확인.

---
### WS-5: 긴 텍스트 요약 청킹 (L, 의존성 없음)

**대상 finding**: M7

> **[v2 정정 — BLOCKER: 시그니처 불일치]** Codex 초안의 `async def summarize(text, **kw)` /
> `_call_api` 는 가공된 가정. **실제 구조**(summarizer.py):
> - `summarize(txt_path: Path, agent, api_key, model, extra_prompt="") -> Path` — **동기**.
>   `summarize` 가 `txt_path.read_text()` → `_summarize_gemini`/`_summarize_openai` 분기.
> - `_summarize_gemini(api_key, model, system_prompt, user_content) -> str`,
>   `_summarize_openai(...)` 동일 시그니처.
> - prompt-injection 경계: system_prompt(신뢰) ↔ user_content(STT, 불신) 분리 (Gemini
>   `system_instruction`, OpenAI `role` 분리). **청킹 후에도 이 경계 유지 필수.**
>
> v2 설계: `summarize` 동기 유지(파이프라인이 이미 `run_in_executor` 로 호출 — H1 참조).
> 기존 `_summarize_gemini`/`_summarize_openai` 를 통일 호출하는 `_call_summary(agent,
> api_key, model, system_prompt, user_content)` dispatcher 를 추출하고, 청킹은
> `user_content` 만 분할 (system_prompt 는 모든 청크·merge 에 동일 적용 → 경계 유지).
```python
_MAX_CHUNK_CHARS = 12_000      # Gemini 1.5 Flash 단일 호출 안전선
_CHUNK_OVERLAP   = 500
_MERGE_DEPTH_MAX = 3           # merge pass 재귀 상한

def _chunk_text(text, max_chars=_MAX_CHUNK_CHARS, overlap=_CHUNK_OVERLAP) -> list[str]:
    chunks, start = [], 0
    step = max(1, max_chars - overlap)        # overlap >= max_chars 방어
    while start < len(text):
        chunks.append(text[start:start + max_chars])
        start += step
    return chunks

def _call_summary(agent, api_key, model, system_prompt, user_content) -> str:
    if agent == "gemini":
        return _summarize_gemini(api_key, model, system_prompt, user_content)
    if agent == "openai":
        return _summarize_openai(api_key, model, system_prompt, user_content)
    raise ValueError(f"지원하지 않는 AI 에이전트: {agent}")

def summarize(txt_path, agent, api_key, model, extra_prompt="") -> Path:
    text = txt_path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError("텍스트 파일이 비어 있습니다.")
    system_prompt = _SYSTEM_PROMPT + (_EXTRA_PROMPT_TEMPLATE.format(extra=extra_prompt)
                                      if extra_prompt else "")
    body = _summarize_chunked(agent, api_key, model, system_prompt, text, depth=0)
    out = txt_path.with_stem(txt_path.stem + "_summarized")
    out.write_text(body, encoding="utf-8")
    return out

def _summarize_chunked(agent, api_key, model, system_prompt, text, depth) -> str:
    if len(text) <= _MAX_CHUNK_CHARS:
        return _call_summary(agent, api_key, model, system_prompt,
                             _USER_PROMPT_HEADER + text)
    partials = [
        _call_summary(agent, api_key, model, system_prompt, _USER_PROMPT_HEADER + c)
        for c in _chunk_text(text)
    ]
    merged = "\n\n".join(partials)
    if len(merged) > _MAX_CHUNK_CHARS and depth < _MERGE_DEPTH_MAX:
        return _summarize_chunked(agent, api_key, model, system_prompt, merged, depth + 1)
    # depth 상한 도달 시: 잘라서 마지막 1회 통합
    return _call_summary(agent, api_key, model, system_prompt,
                         _USER_PROMPT_HEADER + merged[:_MAX_CHUNK_CHARS])
```
> **[Codex 의견]** merge pass 재귀 depth 상한 3 (반영). 12,000 자는 Flash 단일 호출 안전.
> **[Claude 검수]** partial 요약(= AI 출력)을 다시 user_content 로 넣어도 system_prompt
> 경계는 유지됨. 호출 횟수 = 청크 수 + merge — 토큰 비용 증가는 의도된 trade-off.
> 리팩토링 범위가 스케치보다 커 **사이즈 M→L 로 상향**.

**SSOT 재사용**: 호출부(`download_pipeline`/`api/routes`)는 `Config.get_ai_api_key()`/
`get_ai_model()` 그대로. `summarize` 시그니처 무변경 → 호출부 수정 불필요.
**검증**: 50,000 자 텍스트로 `summarize` 호출 → 청크 분할·merge·경계 유지 확인 (단위 테스트).

---

### WS-6: 정확성·GC·종료 소수 수정 (S, 의존성 없음)

**대상 finding**: L1, L3, L4

#### L1 — background_player.py:630 (진도 페이지 boundary)
```python
# Before  (current 가 작을 때 0 산출 → :1255 의 max(1,...) 가드와 불일치)
cumulative_page = total_page if current >= duration else int(current / duration * total_page)

# After
cumulative_page = (
    total_page if current >= duration
    else max(1, int(current / duration * total_page))
)
```
> `while current < duration` 루프 진입 전 `duration <= 0` 은 이미 early-return 처리됨 —
> ZeroDivision 은 비도달. 본 수정은 page=0 산출 방지(`:1255` 와 일관성)가 목적.

#### L3 — auto.py:259 `_input_listener` (stdin 스레드 종료 시 block 잔류)
> **[v2 신규 — BLOCKER: L3 미배정 수정]** v1 은 L3 를 어느 WS/PR 에도 배정하지 않아
> "19개 전부 수정 가능" 주장이 거짓이었다. v2 에서 WS-6 에 편입.
>
> 원인: `loop.run_in_executor(None, sys.stdin.readline)` 가 default ThreadPoolExecutor
> (비-데몬 스레드) 사용 → Ctrl+C 종료 시 readline 에 묶인 스레드가 살아남아 인터프리터
> atexit join 에서 hang (사용자가 Enter 칠 때까지).
```python
# Before — auto.py:256-264 _input_listener 내부
line = await loop.run_in_executor(None, sys.stdin.readline)

# After — 전용 daemon 스레드 + asyncio.Queue 로 라인 수신
#  daemon=True 스레드는 인터프리터 종료를 막지 않는다 (atexit join 비대상).
def _spawn_stdin_reader(queue: asyncio.Queue, loop) -> None:
    def _reader():
        for line in sys.stdin:                 # EOF 시 자연 종료
            loop.call_soon_threadsafe(queue.put_nowait, line)
        loop.call_soon_threadsafe(queue.put_nowait, None)   # EOF sentinel
    threading.Thread(target=_reader, daemon=True).start()
```
> `_input_listener` 는 `queue.get()` 으로 라인 수신, `"0"` 이면 `stop_event.set()`.
> daemon 스레드라 종료 시 hang 없음. EOF(non-TTY)는 sentinel `None` 으로 처리(LOG-004 유지).

#### L4 — summarizer.py:126 `_summarize_gemini` (client 정리)
```python
# google-genai Client 는 close() 가 없을 수 있어 AttributeError 흡수
finally:
    try:
        close = getattr(client, "close", None)
        if callable(close):
            close()
    except Exception:
        pass
    del client
```
> OpenAI 경로(`_summarize_openai`)는 이미 `client.close()` 호출 — 무변경.

**검증**: `pytest tests/ -k summarizer` + L1 page≥1 단위 테스트 + L3 는
non-TTY(`echo "" | ...`) 및 Ctrl+C 시뮬레이션에서 프로세스가 hang 없이 종료되는지 확인.

---

## 3. 의존성 그래프 및 실행 순서

**(A) 코드 의존성**
```
WS-0 (locked_transaction 헬퍼)
  └─> WS-2 (M2/M8/L6 — locked_transaction 사용)
        └─> WS-4 의 M6 (flush 가 re-load+merge 위해 WS-2 flush() 에 의존)

WS-4 의 M9/L2/L5/L7 — 독립
WS-1 (event loop blocking) / WS-3 (리소스 한도) / WS-5 (청킹) / WS-6 (정확성·종료) — 독립
```
**(B) 파일 충돌 의존성 (v2 신규)** — `src/ui/auto.py` 를 4개 finding 이 수정:
```
PR-2(M5) ─> PR-3(M6) ─> PR-5(L3·L7)     # auto.py 직렬 머지 (rebase)
```

**권장 순서**:
1. **PR-1**(WS-0) 최우선 — WS-2 의 전제.
2. **PR-2**(WS-1, HIGH 3건) — 즉시 착수, auto.py 체인의 첫 머지.
3. **PR-3**(WS-2+M6) — PR-1 머지 + PR-2 머지 후 (코드의존 ∩ 파일충돌).
4. **PR-5**(WS-4 나머지+WS-6) — PR-3 머지 후 (auto.py 충돌 회피).
5. **PR-4**(WS-3) · **PR-6**(WS-5) — auto.py·WS-0 무관, 아무 때나 병렬.

> **[v2]** v1 의 "WS-1/3/5/6 전부 병렬" 은 auto.py 파일 충돌을 간과 — PR-2/3/5 는 직렬화한다.

---

## 4. PR 분할 권장

| PR | 워크스트림 | Finding | 사이즈 | 선행 조건 | auto.py 수정 |
|---|---|---|---|---|---|
| PR-1 | WS-0 | — (`locked_transaction` 헬퍼만) | XS | 없음 | — |
| PR-2 | WS-1 | H1, H2, H3, M5, L8 | L | 없음 | ✅ (M5) |
| PR-3 | WS-2 + WS-4(M6) | M2, M8, L6, **M6** | M | **PR-1** | ✅ (M6) |
| PR-4 | WS-3 | M3, M4 | S | 없음 | — |
| PR-5 | WS-4(나머지) + WS-6 | M9, L1, L2, **L3**, L4, L5, L7 | M | 없음 | ✅ (L3·L7) |
| PR-6 | WS-5 | M7 | M | 없음 | — |

> **[v2 정정 — BLOCKER: L3 배정]** v1 PR 표는 L3 누락 → 18/19 만 커버였다. v2 는 **L3 를
> PR-5(WS-6)에 추가** → 19/19 완전 커버.
>
> **[v2 정정 — MAJOR: auto.py 머지 충돌]** PR-2(M5)·PR-3(M6)·PR-5(L3·L7)가 모두
> `src/ui/auto.py` 를 수정한다. "완전 병렬"은 불가 — 동일 파일 충돌 필연.
> **권장 머지 순서**: `PR-1 → PR-2 → PR-3 → PR-5` (auto.py 를 건드리는 PR 을 직렬화,
> 뒤 PR 은 rebase). PR-4·PR-6 은 auto.py 무관이라 어느 시점에나 병렬 머지 가능.
> 각 PR 의 auto.py diff 가 작아(M5: telegram 호출 3곳 / M6: `_save_store`→`maybe_flush` /
> L3: `_input_listener` / L7: 지역변수) rebase 충돌 해소는 경미.

총 6 PR. 직렬 체인 = **PR-1 → PR-3**(의존) + **PR-2 → PR-3 → PR-5**(auto.py 충돌 회피).
HIGH 3건이 모두 PR-2 에 모여 있어 **PR-2 최우선**. PR-4·PR-6 은 완전 독립 — 즉시 병렬 착수.

---

## 5. 검증 전략

### 5-1. 단위 테스트 체크리스트

| 대상 | 테스트 파일 | 검증 포인트 |
|---|---|---|
| locked_transaction | tests/test_atomic_write.py | concurrent write race 없음 + 예외 시 save skip + 중첩 금지 |
| `flush()` merge | tests/test_progress_store.py | 디스크 최신본 + in-memory delta 가 둘 다 보존 (lost update 없음) |
| `maybe_flush` 배치 | tests/test_progress_store.py | `_dirty_count` ≥ INTERVAL 시 flush, finally 강제 flush |
| _available_memory_mb | tests/test_transcriber.py | cgroup v1/v2 mock → `limit-current` 반환, host 와 min |
| _chunk_text / _summarize_chunked | tests/test_summarizer.py | 빈 문자열·overlap≥max·merge depth 상한·system_prompt 경계 유지 |
| _find_course_dir 캐시 | tests/test_paths.py | 2회 호출 시 FS 스캔 1회만 발생 |
| L1 진도 페이지 | tests/test_*player* | current 작을 때 page ≥ 1 |
| L3 stdin reader | (수동) | non-TTY EOF·Ctrl+C 시 프로세스 hang 없이 종료 |

### 5-2. 통합 검증 명령

```bash
# WS-1 핵심 검증: CONVERT(ffmpeg) 진행 중에도 /health 가 즉시 응답하는가
#  1) API 서버 기동 후 큰 mp4 로 /download/pipeline WS 호출 (변환 단계 진입)
#  2) 변환 진행 중 별도 터미널에서 아래가 1초 내 응답하면 PASS
python -m src.api.server &
curl -m 2 http://localhost:18090/health      # CONVERT 중에도 2초 내 응답해야 함

# 회귀: 기존 단위 테스트 전체
docker compose run --rm study-helper python -m pytest tests/ -q
```
> **[Claude 검수]** `run_pipeline` 에 `dry_run` 인자는 없다 — 위 명령은 실제 파이프라인을
> 띄워 CONVERT 단계 동안 `/health` 응답성을 측정하는 것으로 대체했다.

### 5-3. 회귀 방지 체크리스트

- [ ] `PROGRESS_SAVE_INTERVAL=1` 시 per-item 저장 동작 복구됨 + 사이클 종료·finally 는 항상 flush
- [ ] `WHISPER_CPU_THREADS` 미설정 시 기본값 2 적용됨
- [ ] H3 `on_progress` 콜백을 `loop.call_soon_threadsafe` 로 래핑 — rich Progress 갱신 thread-safe
- [ ] L7 은 호출부 지역 변수 캐시만 — 모델에 `@cached_property` 미적용 확인 (mutation stale 방지)
- [ ] M6/M2 `flush()` 가 `locked_transaction` re-load+merge 경유 — recover/reconcile 동시 실행 시 lost update 없음 (`save()` API 잔존 0건 grep 확인)
- [ ] `locked_transaction` 예외 시 lock 해제 + save 건너뜀 확인 + 동일 path 중첩 호출 없음
- [ ] L2 H.264 override 가 재사용 page 에 1회만 등록 (`_h264_override_added` 가드)
- [ ] CLI 자동 모드: CONVERT/STT 중에도 `0`+Enter 종료 신호가 합리적 지연 내 반영 (TUI freeze 해소)
- [ ] M3: 컨테이너 `mem_limit:2g` + host 16G 환경에서 `large` 요청 시 다운그레이드 발동 (실측)

### 5-4. 통합 검증 보강 (v2)

- **H2/H3/M5 blocking 직접 검증**: WS-1 적용 전/후로 download/STT/telegram 단계에서
  `asyncio` 루프 tick 간격(또는 동시 `/health` 응답시간)을 측정해 freeze 해소를 정량 비교.
- **M7**: public `summarize(txt_path, ...)` 진입점으로 50,000 자 파일 테스트 (내부 mock 아님).
- **WS-0 Windows**: Windows 에서 `file_lock` advisory 실패 경로는 직렬화 미보장이므로
  통과 기준에서 제외 — Linux(flock) 에서만 concurrent race 0 을 PASS 기준으로 한다.

---

## 6. Negative Findings (수정 불필요)

| ID | 위치 | 주장 | 판정 | 사유 |
|---|---|---|---|---|
| N1 | scheduler.py | 스케줄 drift | FALSE | 이미 drift 보정 로직 존재 (PR #1 fix) |
| N2 | crypto.py | keyring 동기 블로킹 | DISMISSED | 호출 빈도 낮음 — 실측 영향 미미 |
| N3 | logger.py | log rotation 없음 | DISMISSED | LOG-SYS-2 14일 자동 삭제로 충분 |

---

## 7. Codex Round 3 크로스체크 — 실제 소스 라인 확인

| 파일 | 확인 라인 | 내용 |
|---|---|---|
| download_pipeline.py | 168, 248 | convert_to_mp3, notify_summary_complete |
| video_downloader.py | 468, 493, 529, 566 | _stream_download 계열 4곳 |
| progress_store.py | 72, 119, 135 | file_lock 시작, load, save |
| config.py | 332, 362 | file_lock 시작, atomic_write_text |
| deadline_checker.py | 94, 105, 220, 251 | load, mutate, save, mutate |
| auto.py | 249, 381, 462, 517, 531-537, 646, 777 | 블로킹 호출들 |
| transcriber.py | 35, 57, 140 | _available_memory_mb, psutil, WhisperModel |
| summarizer.py | 79, 104, 126, 147 | generate_content, 청크 없음, long path, close 없음 |
| paths.py | 60, 82, 107, 125 | _find_course_dir 진입, FS 스캔, match, return |
| background_player.py | 630, 795, 889, 1255 | ZeroDivision, add_init_script x2, BrowserContext |

> **[Claude 검수 — Codex 초안 정정]** `_stream_download` 는 **정의 1개 + 호출 1개**뿐이다.
> `:529`(`requests.get`)·`:566`(`iter_content`) 는 `_stream_download` **정의 본문 내부**라
> 별도 수정 대상이 아니다. `:468` 은 `async def download_video_with_browser` 시그니처.
> PR-2 의 H3 수정은 **호출부 :493 한 곳**을 `await asyncio.to_thread(...)` 로 감싸는 것.

---

## 8. 적대적 검토 기록 (PLAN v1 → v2)

UltraPlan v1 초안에 대해 **Claude·Codex 가 각각 독립 적대 검토(adversarial review)** 를 수행.
두 모델이 독립적으로 동일 결함에 수렴 → BLOCKER 4 + MAJOR 4 도출. v2 에서 전부 반영.

| # | 결함 | Claude | Codex | v2 정정 |
|---|------|--------|-------|---------|
| B1 | **L3 미배정** — WS/PR 어디에도 없어 "19개 전부" 거짓 | BLOCKER | BLOCKER | WS-6 + PR-5 에 L3 편입 (daemon stdin reader) |
| B2 | **persistence 이중구조** — `locked_update`↔배치 flush 호출자 미명세 | MAJOR | BLOCKER | 단일 `flush()` 모델로 통일 (locked re-load+merge), 호출자별 시점 표 명세 |
| B3 | **WS-0 Windows lock 미작동** — `LK_NBLCK` 실패 시 락 없이 진행 + 중첩 deadlock | MINOR | BLOCKER | WS-0 에 재진입 금지 + Windows best-effort 한계 명시 |
| B4 | **WS-5 M7 시그니처 불일치** — 실제 `summarize`/`_summarize_gemini` 와 다름 | MINOR | BLOCKER | 실제 시그니처 기반 재설계, `_call_summary` dispatcher, injection 경계 유지, M→L |
| M-a | M3 cgroup `limit`≠`available` | MAJOR | MAJOR | `limit - current` + host min 으로 재설계 |
| M-b | L2 `add_init_script` context 오염 | (놓침) | MAJOR | context 이동 철회 → page-level 1회 등록 (`_h264_override_added`) |
| M-c | PR-2/3/5 모두 `auto.py` 수정 → 머지 충돌 | (놓침) | MAJOR | §3·§4 에 auto.py 직렬 머지 순서 명시 |
| M-d | `Config._save_env` class변수↔파일 정합 | (놓침) | MAJOR | M8 범위 외(기존 이슈) 로 분리 — `flush()` 는 파일을 SoT 로 merge, class변수 staleness 는 별도 이슈 |

**검토 산물**: v1 의 "전부 수정 가능" 은 18/19 (거짓) → **v2 는 19/19 (참)**.
fix 설계 4건이 코드와 충돌하던 것을 실제 시그니처/플랫폼 제약에 맞춰 재설계.

---

## 9. 구현 결과 (UltraPlan v2 실행)

브랜치 `perf/ultraplan-v2` 에 19개 finding 전부 구현 완료. Codex 페어 리뷰 → 수정 반영.

### 구현 범위
- 18개 src 파일 + 2개 test 파일 + `.env.example` (843 insertions / 236 deletions)
- WS-0~WS-6 전부. 19/19 finding 코드 반영.

### Codex 페어 리뷰 (구현 후 전체 diff 적대 검토) → 수정 반영

| 항목 | Codex 판정 | 처리 |
|------|-----------|------|
| WS-2 progress_store flush merge | MINOR | 수용 (아래 잔여 리스크) |
| main.py playback notify 동기 | **MAJOR** | ✅ 수정 — `_tg_notify_playback_*` async + `to_thread` |
| download.py dispatch 3곳 동기 | **MAJOR** | ✅ 수정 — `await asyncio.to_thread(dispatch_if_configured, ...)` |
| auto.py L3 daemon stdin 입력 가로채기 | **MAJOR** | ✅ 수정 — `for line in sys.stdin` 영구 루프 → 반복마다 1회용 daemon 스레드 (정상 종료 시 메인 메뉴 입력 비간섭) |
| download_pipeline summary read_text blocking | MINOR | ✅ 수정 — read_text 를 executor lambda 안으로 |
| deadline at-most-once 미보장 | MINOR | 수용 (아래) |

### 검증 결과
- **pytest: 126 passed, 3 skipped** (3 skip = Windows 전용 lock 직렬화 테스트). 신규 테스트 8개 추가 (`locked_transaction` 3, `flush()` merge/배치 5).
- **ruff: 변경 파일 전부 통과** (신규 lint 위반 0 — pre-existing UP037 23건은 범위 외).
- M9 캐시가 BUG-7 마커 stamping 부수효과를 건너뛰던 회귀 1건 — 구현 중 테스트가 검출 → 즉시 수정 (stamp 를 캐시 wrapper 로 이동).

### 잔여 리스크 (수용 — MINOR, 문서화)
- **R1 progress_store cross-process 삭제 역전**: 프로세스 A 가 stale `_touched` URL 보유 중 B 가 `retain_only` 삭제 후 먼저 flush 하면, A 의 flush 가 해당 URL 을 재삽입할 수 있다. "touched wins" merge 정책상 A 가 해당 URL 을 이번 사이클에 실제 처리했으므로 방어 가능하나, 완전한 tombstone 기반 해결은 미구현. 발생 조건: auto 모드 + 유지보수 스크립트 동시 실행 + LMS 응답 불일치 동시 — 매우 드묾.
- **R2 deadline at-most-once 미보장**: 두 프로세스가 동시에 deadline 체크 시 같은 항목을 각각 1회 발송 가능 (lost-update 는 L6 로 해소, 중복 발송은 잔존). 영향: 마감 알림 중복 — benign.
- 두 리스크 모두 tombstone / lock-claim 도입으로 해결 가능하나 MINOR + 발생 조건 희소 → 별도 이슈로 분리.

---

*문서 끝. Claude Opus 4.7 + Codex (GPT-5.x xhigh) 페어 audit & UltraPlan & 구현.*
*프로세스: audit 3-Round → UltraPlan v1 → Claude 검수 6건 → 적대적 검토 → v2 정정(BLOCKER 4 + MAJOR 4) → **구현 → Codex 페어 리뷰 → MAJOR 3 + MINOR 1 수정**. 2026-05-20*