# study-helper 진입점 프로세스 논리 오류 검토 리포트 (최종)

> 생성: 2026-05-20. 방법: 진입점 전수 파악 → 프로세스 경우의 수 수집(~270) → 5-라운드 페어 논리 오류 검토 (Claude 4 + Codex 1).
> 동반 문서: [process-inventory.md](process-inventory.md) — 진입점별 프로세스 경우의 수 SoT.

## 페어 방법론

| 라운드 | 수행 | 내용 |
|--------|------|------|
| Round 1 (blind) | Claude 3-agent | 진입점별 프로세스 8유형 순방향 체크리스트 검토 → 15건 |
| Round 2 (adversarial) | Claude agent X | Round 1 15건 반증 시도 (false positive 색출) |
| Round 2 (다른 관점) | Claude agent Y | cross-cutting 공유상태·실패 end-to-end·미탐색 하위모듈·시간순서 축 → 신규 12건 |
| 보강 (미탐색 메움) | Claude 2-agent | Round 1·2 스코프 밖이던 인프라/유틸 13모듈 검토 → 신규 13건 |
| 보강 (확정·커버리지) | Claude 2-agent | INCONCLUSIVE 3건 확정 해소 + 테스트 커버리지 갭 매핑 |
| 최종 (Codex 교차검증) | Codex codex-rescue | Claude 40건 독립 재검증 + Codex-only 발굴 → 39 동의 / 1 불일치(R2-04) / 신규 2건 |

> Round 2는 Codex quota 소진으로 **Claude의 다른 분석 관점**(반증 + 다른 분석축)으로 진행했고, Codex 독립 교차검증은 quota 리셋 후 최종 라운드로 완료했다.
>
> **모듈 단위 union diff**: `src/` 실질 모듈 40개 전수 검토 완료. `__init__.py` 14개는 패키지 마커(논리 없음)라 out-of-scope. 미탐색 모듈 0.

---

## Executive Summary

- **진입점**: 1차 6개 + 2차 14개. union diff 미탐색 0. 모듈 단위로도 `src/` 40개 전수 검토.
- **프로세스 경우의 수**: ~270개.
- **최종 논리 오류**: **CONFIRMED 41건** (HIGH 0 / **MEDIUM 12** / LOW 29) + REJECTED 3건. INCONCLUSIVE 0.
- **Codex 교차검증**: Claude 40건 중 Codex 동의 39 / 불일치 1건(R2-04 → REJECTED) + Codex-only 신규 2건(COD-N01/02). false positive 1건뿐 — Claude 검토 정확도 높음.
- **반증 결과**: Round 1 15건 중 OVERTURNED 0, UPHELD 11, REFINED 4. 보강·Codex에서 REJECTED 3건(R2-04/R2-07/NF-10).
- **테스트 커버리지**: 41건 중 완전 보호 **0건** / 부분 ~9 / 미보호 ~32. `api/`·`player/`·`notifier/` 테스트 파일 0개 — 수정 전 안전망 부재 (아래 D 참조).
- **결론**: 명백한 off-by-one·boolean 부정 누락·and/or 우선순위 오류 없음. 코드 견고성 양호. 발견 항목은 에러 분류 정확도·취소 응답성·상태 확정 시점·SSOT 규약·PII 마스킹 갭·UX 위주.

---

## Cross-Validation 결과

### A. Round 1 finding 반증 검증 (15건)

**UPHELD 11건** (반증 실패 — Round 1 정확):
API-F3, API-F4, API-F1, API-F2, API-F5, API-F6, CLI-F4, CLI-F5(INCONCLUSIVE 유지), CLI-F6, CLI-F9, SVC-F1

**REFINED 4건** (버그 실재하나 severity/범위 정정):
- **SVC-F6** — WS `/pipeline` 라우트가 `stage_errors`를 응답에 포함 → API 클라이언트는 정보 손실 없음. 위험은 `success` 단독 의존 호출자(TUI)로 **범위 축소**. MEDIUM 유지.
- **CLI-F1** — `migrate_drive_root_downloads.py`는 일회성 수동 스크립트 → "자동화 호출" 시나리오 비현실적. 실 위험 LOW 미만, 메시지 문구(`[오류]`→`[안내]`) 수정 사안.
- **CLI-F3** — scripts exit code는 표준 Unix 관례(0/1/2)와 부합, 강제 규약 부재 → 버그 아닌 **문서화 미흡**(cosmetic).
- **CLI-F14** — CLI는 LMS를 SoT로 삼는 **설계 분리**일 가능성 우세 → non-bug 가능성 높음, "설계 의도 확인 필요"로 하향.

**OVERTURNED 0건** — false positive로 뒤집힌 finding 없음.

### B. Round 2 신규 finding (12건)

cross-cutting / 실패 end-to-end / 미탐색 하위모듈(`scraper`·`player`·`downloader`·`auth`) / 시간순서 축에서 발굴. Round 1 스코프가 안 본 영역.

---

## 최종 논리 오류 목록 (severity별)

### MEDIUM (7건)

| ID | 위치 | 내용 | confidence |
|----|------|------|------------|
| **R2-01** | [src/auth/login.py:9](src/auth/login.py#L9) | `logging.getLogger(__name__)` 사용 — LOG-SYS-1 회귀, silent log loss. 모든 로그인/재로그인 경고가 파일 미기록 + SensitiveFilter 우회. **첫 `/analyze`에서 Codex도 독립 지적**(atomic_write.py:26 포함) — 신뢰도 최고 | certain |
| **R2-03** | [src/player/background_player.py:620](src/player/background_player.py#L620) | Plan B 재생 루프(`_play_via_progress_api`)가 `stop_event` 미검사 → 사용자가 긴 강의 재생 중 "0" 입력해도 영상 길이만큼 종료 지연 | certain |
| **R2-11** | [src/ui/auto.py:835](src/ui/auto.py#L835) | 디스크 가득참 등으로 STT/요약만 실패해도 `mark_download_success` → store `downloaded=True` 확정 → STT/요약 결과물 영구 누락(재시도 안 됨) | likely |
| **API-F3** | [src/api/routes/download.py:104](src/api/routes/download.py#L104) | `/convert` 예외 미가공 500 노출 — `RuntimeError`의 ffmpeg `stderr_tail`(경로 포함 가능)이 클라이언트로. SEC-005 위반 | certain |
| **API-F4** | [src/notifier/telegram_notifier.py:346](src/notifier/telegram_notifier.py#L346) | `verify_bot`이 텔레그램 5xx/네트워크 장애를 `INVALID_CHAT_ID`로 오분류 (getMe 경로는 정확 — 비대칭) | certain |
| **SVC-F6** | [src/service/download_pipeline.py:176](src/service/download_pipeline.py#L176) | `run_pipeline` `success=True`인데 STT/요약/알림 단계 실패 미반영 (CONVERT만 `success=False`). 위험: TUI 등 `success` 단독 의존 호출자 | certain |
| **CLI-F5** | [src/main.py:142](src/main.py#L142) | `input()` executor 위임 — Ctrl+C가 executor 스레드에 미도달 시 `asyncio.run` shutdown join hang 가능 (플랫폼 의존, 런타임 검증 필요) | uncertain (INCONCLUSIVE) |

### LOW (18건)

| ID | 위치 | 내용 |
|----|------|------|
| R2-02 | config.py:273 | `_save_env` 파일 락은 잡으나 `Config` 클래스 변수 대입은 락 밖 — 멀티프로세스 시 메모리 stale |
| R2-05 | background_player.py:230 | `_parse_player_url`의 `float(endat)` — 비숫자 `endat`(예: `endat=abc`) 시 ValueError로 재생 완료 보고 전체 중단 |
| R2-06 | course_scraper.py:375 | `_parse_weeks` week_num fallback이 스킵된 항목으로 어긋남 (사용처 제한적) |
| R2-08 | background_player.py:336 | `_report_completion` 3회 재시도가 status 무관 — 4xx 영구 실패도 6초+ 왕복 |
| R2-09 | deadline_checker.py:176 | 강의가 마감 12h 이내 구간에서 처음 관측되면 24h·12h 알림 2건 동시 발송 |
| R2-10 | video_downloader.py:218 | `content.php` 최초 1회만 파싱 — 첫 응답이 placeholder면 진짜 미디어 URL 영구 누락 |
| R2-12 | fake_video.py:55 | `create_fake_webm`이 ffmpeg `returncode` 미검사 — 부분 생성 손상 webm 미검출 (폭발반경 제한적) |
| API-F1 | download.py:168 | WS 인증 전 `receive_json()` 비-JSON 입력 → `PIPELINE_ERROR`로 오표기 |
| API-F2 | download.py:181 | WS 컨텍스트에서 `HTTPException`은 HTTP 400 안 됨 → `PIPELINE_ERROR`로 close, 클라이언트가 경로 오류 구분 불가 |
| API-F5 | telegram_notifier.py:316 | 빈 `chat_id` 선검증 부재 — 불필요한 텔레그램 API 왕복 (동작은 안전) |
| API-F6 | crypto.py:166 | `except (InvalidToken, Exception)` — `InvalidToken` 명시 무의미(죽은 코드), 키 인프라 오류까지 silent |
| CLI-F1 | migrate_drive_root_downloads.py:54 | source 부재/동일 시 `[오류]` 출력하면서 exit 0 (수동 스크립트라 실위험 낮음) |
| CLI-F3 | recover_missing.py:197 | scripts 간 exit 2 의미 불일치 (문서화 미흡, cosmetic) |
| CLI-F4 | main.py:89 | 과목 로드 실패 경로 `scraper.close()`가 try 보호 없음 |
| CLI-F6 | recover_missing.py:201 | `unload_model` raw import — `safe_unload()` SSOT 위반 |
| CLI-F9 | courses.py:115 | 과목 0개 시 안내 메시지 없음 (무한 hang은 아님) |
| CLI-F14 | main.py:137 | CLI 재생 성공이 ProgressStore 미연동 (설계 의도 가능성 — 확인 필요) |
| SVC-F1 | auto.py `_apply_play_result` | download-only 경로 `REASON_BROWSER_RESTARTED`가 `mark_download_failed` reason 오염 |

### REJECTED / 확정 해소 (3건)

- **R2-07 → REJECTED** (certain) — `SUSPICIOUS_STUB`은 `_NO_RETRY_REASONS`에서 의도적으로 제외돼 있고 `auto.py` 재시도 루프가 실제로 재추출/재다운로드함. finding 전제가 코드와 반대. Codex도 동의. (부수: `auto.py:758` docstring stale — cosmetic)
- **NF-10 → REJECTED** (certain) — `notify_*` 9개 함수 전부 일관 `bool` 반환 + `dispatch_if_configured` 호출부 8곳 전수가 반환값을 버림 → 모호성을 소비하는 코드 부재. Codex도 동의.
- **R2-04 → REJECTED** (certain) — Claude는 `_cleanup`의 `page.unroute` 실패를 "silent"로 CONFIRMED했으나, Codex가 `background_player.py:921-927`의 `except`가 `log()` + `get_logger("player.background").error()`로 명시적 ERROR 로깅함을 지적. 부모 컨텍스트 직접 확인 결과 코드가 ERROR 레벨 기록 → "silent" claim 부정확. §2.6 Disagree 룰로 CONFIRMED → REJECTED.
- (NEW-08은 INCONCLUSIVE→CONFIRMED LOW로 확정되어 위 LOW 표에 편입됨)

---

## C. 미탐색 보강 라운드 신규 finding (13건)

> Round 1·2의 명시 스코프 밖이던 인프라/유틸 13모듈(`logger`/`updater`/`downloader.paths`/`downloader.result`/`telegram_dispatch`/`log_sanitize`/`url`/`scraper.models` + `config`/`crypto`/`audio_converter`/`transcriber`/`summarizer`)을 전수 검토.

### MEDIUM (5건)

| ID | 위치 | 내용 | confidence |
|----|------|------|------------|
| **NF-03** | [src/util/log_sanitize.py:31](src/util/log_sanitize.py#L31) | PII 마스킹 SoT `_SENSITIVE_KEYS`가 실제 LMS 로그인 폼 필드명 `userid`(언더스코어 없음)·`pwd`를 누락 — `user_id`/`password`만 있음. 폼 body/URL이 로그에 남으면 학번·비밀번호 평문 노출 | likely |
| **NF-04** | [src/util/log_sanitize.py:42](src/util/log_sanitize.py#L42) | URL-encoded 마스킹 규칙 값 클래스가 `%`를 제외 → `%XX` 인코딩 시퀀스 이후가 평문 잔존 (이메일/토큰 부분 노출) | likely |
| **NEW-02** | [src/summarizer/summarizer.py:161](src/summarizer/summarizer.py#L161) | `_summarize_chunked` depth 상한(3) 도달 시 `merged[:_MAX_CHUNK_CHARS]` 무조건 절단 → 긴 강의 후반부 요약 silent 손실 (재귀 종료는 보장됨) | likely |
| **NEW-03** | [src/crypto.py:114](src/crypto.py#L114) | `.secret_key` 파일 쓰기가 `atomic_write`/`file_lock` 미경유 → API 서버+CLI 동시 첫 기동 시 키 분기 race, 먼저 암호화한 `.env` 값 영구 복호화 불가 | likely |
| **NEW-04** | [src/crypto.py:165](src/crypto.py#L165) | `decrypt`의 `_fernet()` 호출이 try 안 → 키 손상(`ValueError`)·파일 오류(`OSError`)까지 빈 문자열로 흡수 → "설정 없음"으로 오분류, 사용자 재입력 시 새 키 덮어쓰기 연쇄 | certain |

### LOW (8건)

| ID | 위치 | 내용 |
|----|------|------|
| NF-01 | updater.py:8 | `_VERSION_RE`가 3-숫자 세그먼트만 허용 — `"unknown"`·사전릴리스·4자리 태그 탈락 → 업데이트 알림 silent 누락 |
| NF-05 | util/url.py:6 | `safe_url`이 query/fragment만 제거 — netloc userinfo(`user:pass@`)·경로 토큰 미제거 |
| NF-06 | scraper/models.py:88 | `is_downloadable`의 `"learningx" not in full_url` substring 매칭 — URL 다른 위치에 우연히 등장 시 과탐 |
| NF-07 | downloader/paths.py:182 | `file_present`의 규칙 미설정 fallback이 `mp4 or mp3` (OR) — 빈 `DOWNLOAD_RULE` 시 mp3 누락을 "완료"로 오판정 |
| NEW-01 | summarizer.py:161 | 청크 안전선 계산이 `_MERGE_INSTRUCTION`/헤더 prefix 미포함 → 12,000자 상한 미세 초과 |
| NEW-05 | audio_converter.py:89 | ffmpeg 실패 시 부분 mp3 정리가 `st_size==0`만 — 비-0 손상 mp3 잔존 → 다음 실행 skip 가드가 오인 |
| NEW-06 | transcriber.py:225 | STT segment 루프 중 디코드 예외 시 부분 txt 잔존 → 재실행 시 `is_transcript_usable` 통과해 불완전 요약 |
| NEW-07 | config.py:273 | `save_settings/telegram/credentials`가 클래스 변수 대입 후 `_save_env` 호출 → 파일 쓰기 실패 시 메모리·디스크 불일치 (R2-02 확장) |
| NEW-08 | transcriber.py:217 | `/transcribe` 동시 요청 시 `model.transcribe`가 `_model_lock` 밖 → 같은 캐시 `WhisperModel` 병렬 호출 race (확정 해소: INCONCLUSIVE→CONFIRMED). 정상 경로는 직렬이라 LOW |

---

## Codex 교차검증 (페어 최종 라운드 — §2.6 Cross-Validation)

> Codex(codex-rescue, GPT-5.x)가 Claude 40건 CONFIRMED + 2 REJECTED를 독립 재검증 + Codex-only finding 발굴. quota 리셋 후 실행.

### Agreed (39건)
Claude 40건 CONFIRMED 중 R2-04를 제외한 **39건 전부 Codex 동의** — 최고 confidence. false positive 단 1건 → Claude 4-라운드 검토 정확도 높음.

### Disagree (1건) — §2.6 룰로 Codex 보존적 판단 채택
- **R2-04 → REJECTED** — 위 "REJECTED / 확정 해소" 참조. Claude "silent" claim이 부정확, 코드는 ERROR 로깅. 부모 컨텍스트 직접 확인 완료.

### REJECTED 재확인 (2건) — Codex 일치
- R2-07, NF-10 — Codex도 REJECTED 동의 (`_NO_RETRY_REASONS` 포함 / `notify_*` 일관 bool 반환).

### Codex-only 신규 (2건) — 부모 컨텍스트 직접 확인 후 surface

| ID | 위치 | 내용 | severity | confidence |
|----|------|------|----------|------------|
| **COD-N01** | [src/notifier/deadline_checker.py:90](src/notifier/deadline_checker.py#L90) | `_make_dedup_key`가 `sha256(course.id:lecture.title)` — dedup 키가 강의 제목 의존. 제목이 변경/수정되면 기존 발송 알림이 다른 키로 인식돼 마감 알림 중복 발송. docstring은 "안정적"이라 주장하나 제목 변경엔 불안정 | LOW | likely |
| **COD-N02** | [src/config.py:359](src/config.py#L359) | `_merge_and_write`가 `.env` 기록 시 `f"{key}={value}\n"` — value escaping/quoting 없음. `SUMMARY_PROMPT_EXTRA` 등 자유 텍스트에 newline·`KEY=value` 패턴이 들어가면 `.env` 손상 또는 의도치 않은 키 삽입 | LOW | likely |

---

## 구현·테스트 중 추가 발견 (1건 — 수정 완료)

- **IMPL-01** — `src/api/server.py:79` `download` 라우터가 `dependencies=[Depends(_verify_token)]`로 등록되는데, FastAPI router-level dependency는 **WebSocket route에도 적용**된다. WS `/pipeline` 핸들러는 자체적으로 첫 메시지 기반 토큰 인증을 구현(`download.py:164-191`)했으나, 라우터 dependency가 HTTP `Authorization` 헤더를 먼저 강제 → 헤더 없는 WS 연결이 401로 막혀 핸들러 내부 인증이 도달 불가(dead code)가 됨. **5-라운드 검토 + Codex가 모두 놓침 — 회귀 테스트 작성 중 `WebSocketDenialResponse`로 드러남.** 수정: WS route를 별도 `ws_router`로 분리해 헤더 dependency에서 제외 (핸들러 내부 메시지 인증이 의도대로 동작). severity MEDIUM.

## 정상 확인 영역 (양 라운드 검증, 오류 없음)

- 재시도 경계 — URL 추출/재생/다운로드/브라우저 재시작/텔레그램 재시도 off-by-one 없음, RetryPolicy 상수 일치.
- 인증 — `secrets.compare_digest` constant-time, `Bearer ` 슬라이스 정확, `ALLOW_NO_TOKEN` 부팅↔런타임 가드 정합.
- 동시성 — `locked_transaction` self-deadlock 없음(3개 사용처 확인), `flush` delta merge·`_touched`/`_removed` 정합, cross-process lost-update 방지.
- 상태 전이 — 격리 카운터 transition-edge bool 이중 알림 방지, `mark_played`/`mark_incomplete` 설계 의도 일치.
- SSRF — `_validate_media_url` scheme/host suffix, `_parse_extra_hosts` TLD/IDN/IP 거부 다층 방어.
- WS 취소 — `pipeline_task.cancel()` + `await` 회수, task 누수 없음.
- `extract_video_url_detailed` fire-and-forget 태스크 — `finally` cancel + shield timeout 정리.
- `course_scraper` 병렬 재로그인 — `_login_lock` + `_session_restored` 경합 차단.
- 기존 solution 회귀 — `memoization-skips-side-effect`, `daemon-stdin-reader-steals-input` 회귀 없음.

---

## D. 테스트 커버리지 갭

41건 CONFIRMED finding 각각이 기존 회귀 테스트(`tests/` 16파일)로 보호되는지 매핑한 결과 — **수정 시 회귀를 잡아줄 안전망이 거의 없다**. (Codex-only 2건 포함)

- **완전 보호 0건 / 부분 ~9건 / 미보호 ~31건.**
- **테스트 파일이 0개인 영역** (finding 밀집): `src/api/` 전체(API-F1~F6, SEC-002~005 하드닝), `src/player/`(R2-03/04/05/08/12), `src/auth/login.py`(R2-01), `src/service/download_pipeline.py`(SVC-F6), `src/service/recover_pipeline.py`·`scheduler.py`, `src/ui/`(R2-11/SVC-F1/CLI-F9), `src/main.py`(CLI-F4/5/14), `src/notifier/`(API-F4/F5, R2-09), `src/updater.py`(NF-01), `src/util/url.py`(NF-05).

**미보호 + 회귀 위험 HIGH** (수정 전 테스트 선행 강력 권장):

| finding | 이유 |
|---------|------|
| NEW-04 | `test_crypto.py`가 "빈 문자열 반환=정상"을 고정 → 수정 시 기존 테스트가 잘못 깨짐. 손상 키 주입 테스트 필요 |
| NF-03 | `test_logger_filter.py`에 `userid=`/`pwd=` 마스킹 assert 전무 |
| API-F3 | `tests/`에 API 테스트 0개 — `/convert` 에러 응답 새니타이즈 미검증 |
| NEW-03 | `.secret_key` 동시 기동 race / atomic 경유 검증 없음 |
| SVC-F6 | `run_pipeline`의 `success` 의미 검증 테스트 없음 |

**최저비용 보강** (수정 비용 대비 효과 최고):
- **R2-01** — `test_logger_tree.py`의 `_EXPECTED_MODULE_LOGGERS`에 `src.auth.login` 한 줄 추가
- **NF-03 / NF-04** — `test_logger_filter.py`에 `userid=`/`pwd=` + 값 내부 `%XX` 입력 케이스 추가
- 가장 시급한 신규 파일: `tests/test_api_*.py` (API-F1~F6 6건 + SEC 하드닝 정책 회귀 보호)

## Action Items (Tier · 시간 환산 없음)

**Tier HIGH** (즉시 조치, 의존성 없음, 수정 비용 낮음)
1. **R2-01** — `auth/login.py:9` + `util/atomic_write.py:26` `logging.getLogger` → `get_logger("이름")` (LOG-SYS-1 회귀, 1줄 교체. Codex도 독립 지적)
2. **NEW-04** — `crypto.py:165` `_fernet()`를 try 밖으로 + `except`를 `InvalidToken`으로 좁힘 (키 손상→재입력→복호화 불가 연쇄 차단. confidence certain)
3. **NF-03** — `log_sanitize.py:31` `_SENSITIVE_KEYS`에 `userid`·`pwd` 추가 (학번·비밀번호 PII 마스킹 SoT 갭)
4. **API-F3** — `/convert` try/except + 고정 에러 코드 (SEC-005 정합)
5. **CLI-F6** — `recover_missing.py:201` `safe_unload()` SSOT 복귀 (1줄 교체)

**Tier MEDIUM**
6. **NEW-03** — `.secret_key` 파일 쓰기를 `atomic_write_text`+`file_lock` 경유 (cross-process 키 race). CLAUDE.md "원자 쓰기 강제" 대상에 `.secret_key` 명시 추가 권장
7. **R2-03** — `play_lecture`/`_play_via_progress_api`에 `stop_event` 전파 (재생 중 종료 응답성)
8. **API-F4** — `verify_bot` 실패 사유 분류 (5xx/network ≠ INVALID_CHAT_ID)
9. **SVC-F6 + R2-11** — `run_pipeline`의 `success` 의미 통일 또는 `stage_errors`→`success=False`, STT/요약 완료를 store 별도 필드로 분리
10. **NF-04** — URL-encoded 마스킹 값 클래스를 `(?:[^%&\s"'<>]|%[0-9A-Fa-f]{2})+`로 (`%XX` 시퀀스 포함)
11. **NEW-02** — `_summarize_chunked` depth 상한 절단 시 로그 경고 + 결과물에 절단 마커, 또는 재청크 분할
12. **CLI-F5** — `input()` executor hang 런타임 재현 검증 후 daemon+Future 패턴 적용

**Tier LOW** — R2-02/05/06/08/09/10/12, API-F1/F2/F5/F6, CLI-F1/F3/F4/F9/F14, SVC-F1, NF-01/05/06/07, NEW-01/05/06/07/08, COD-N01/N02 (위 표 참조)

**수정 전 안전망** (HIGH/MEDIUM 코드 수정에 선행 권장)
- NEW-04·NF-03·API-F3·NEW-03·SVC-F6는 미보호 + 회귀 위험 HIGH → 수정 전 회귀 테스트 추가. 특히 NEW-04는 기존 `test_crypto.py`가 "빈 문자열=정상"을 고정하므로 테스트 의도 재정의 동반.
- 최저비용: R2-01(`test_logger_tree.py` 1줄), NF-03/NF-04(`test_logger_filter.py` 입력 추가).
- 신규 `tests/test_api_*.py` — API-F1~F6 + SEC-002~005 하드닝 회귀 보호.

**Codex 교차검증 — 완료**
- Codex 최종 라운드 완료: Claude 40건 중 39 동의 / 1 false positive(R2-04 → REJECTED) + Codex-only 신규 2건(COD-N01/02, 둘 다 LOW). 페어 5-라운드 종결.
