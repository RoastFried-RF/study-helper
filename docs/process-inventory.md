# study-helper 진입점별 프로세스 경우의 수 인벤토리

> `/analyze` 후속 — 사용자 진입점 전수 파악 + 진입점별 프로세스 경우의 수 수집.
> 생성: 2026-05-20. 수집: 4 Explore agent fan-out (union diff 검증 통과, 미탐색 진입점 0).
> 용도: Codex 페어 검토의 체크리스트 SoT.

## 진입점 인벤토리 (SoT)

**1차 진입점 (프로세스 시작점) — 6개**

| # | 진입점 | 실행 경로 |
|---|--------|-----------|
| 1 | `src/main.py::main` | Docker ENTRYPOINT, `study-helper` 스크립트, CLI TUI |
| 2 | `src/api/server.py::main` | `python -m src.api.server` — FastAPI 서버 |
| 3 | `scripts/reconcile_progress.py` | FS↔auto_progress.json drift 재조정 |
| 4 | `scripts/recover_missing.py` | completed인데 파일 없는 강의 일괄 재다운로드 |
| 5 | `scripts/sanitize_logs.py` | 기존 로그 PII 소급 마스킹 |
| 6 | `scripts/migrate_drive_root_downloads.py` | Windows 드라이브 루트 \data 트랩 이관 |

**2차 진입점 (API 엔드포인트) — 14개**

| 라우트 | 메서드/경로 | 인증 |
|--------|-------------|------|
| health | GET /health, GET /version | ✗ |
| config | GET "", PUT "", PUT /telegram, POST /telegram/verify, GET /credentials | ✓ |
| download | POST /resolve-path, /convert, /transcribe, /summarize, WS /pipeline | ✓ |
| notify | POST /deadline-check, POST /telegram | ✓ |

> 잔재: `scripts/__pycache__/recover_downloads.cpython-312.pyc` — 소스 없는 .pyc (삭제된 스크립트 잔재, 진입점 아님).

---

## A. CLI 진입점 (`main.py`) — ~88 경우

상태 전이: 로그인 → 설정 체크 → 과목 로드 → 마감 알림 → 과목 선택 루프 → {재생|다운로드|자동|복구|설정}.

### A1. 인증 (main.py:39-74)
- P-AUTH-001.1 자동 로그인 성공 → 설정 체크 (main.py:43-47)
- P-AUTH-001.2 자동 로그인 실패 → 수동 입력 루프 (main.py:48-50)
- P-AUTH-001.3 CourseScraper.start() 예외 → None 반환 (main.py:160-172)
- P-AUTH-002.1 빈 입력 → 에러 표시 + attempts++ (main.py:55-63)
- P-AUTH-002.2 attempts==3 → sys.exit(1) (main.py:55-57)
- P-AUTH-002.3 입력 OK + 로그인 성공 → save_credentials (main.py:66-73)
- P-AUTH-002.4 입력 OK + 로그인 실패 → 재입력 (main.py:68-70)

### A2. 설정/로드 (main.py:75-103)
- P-SETTINGS-001.1 첫 실행 → run_settings() (main.py:76-79)
- P-SETTINGS-001.2 기존 설정 → skip (main.py:76)
- P-LOAD-001.1 과목+버전 병렬 로드 성공 (main.py:83-86)
- P-LOAD-001.2/3 로드 예외 → close() + sys.exit(1) (main.py:87-90)
- P-LOAD-001.1.1.3 fetch_all_details 부분 실패 → None 포함 리스트 (main.py:210, courses.py:126-128)
- P-DEADLINE-001.1 텔레그램 미설정 → skip (main.py:93-94)
- P-DEADLINE-001.2/3 텔레그램 설정 → 마감 알림 executor 위임 (main.py:98-102)

### A3. 과목/강의 선택 (courses.py)
- P-COURSE-001.1 "0" → 정상 종료
- P-COURSE-001.2 "setting" → run_settings()
- P-COURSE-001.3 "auto" → 자동 모드
- P-COURSE-001.4 "recover" → 복구 모드
- P-COURSE-001.5 숫자 → 강의 목록
- P-COURSE-001.6 범위 외 → 재입력
- P-WEEK-001.1 영상 강의 0개 → Enter 대기 → 복귀 (courses.py:171-175)
- P-WEEK-001.2~6 "0"/재생/다운로드/취소/범위 외

### A4. 재생 (player.py:61-181)
- P-PLAY-001.1 정상 완료 → completion="completed" + 알림 (player.py:165-167)
- P-PLAY-001.2 오류 → 오류 로그 + 알림 (player.py:151-163)
- P-PLAY-001.3 미완료 → 미완료 로그 + 알림 (player.py:169-180)
- P-PLAY-001.4 Ctrl+C → 정상 종료 (main.py:149-150)

### A5. 다운로드 (download.py:48-328)
- P-DOWNLOAD-001.1 is_downloadable=False → REASON_UNSUPPORTED (download.py:83-99)
- P-DOWNLOAD-001.2 URL 추출 3회 실패 → 오류 로그 + 알림 (download.py:137-162)
- P-DOWNLOAD-001.3 경로 이스케이프 → REASON_PATH_INVALID (download.py:168-174)
- P-DOWNLOAD-001.4 mp4 다운로드 예외 (network/SSRF/stub) (download.py:204-246)
- P-DOWNLOAD-001.5 mp3 변환 실패 → REASON_MP3_FAILED (download.py:294-298)
- P-DOWNLOAD-001.6 다운로드 성공 + 파이프라인 (부분 실패 포함) (download.py:300-328)
- P-URL-EXTRACT-001.1~6 추출 재시도 분기 (download.py:108-135)

### A6. 종료 (main.py:149-261)
- P-FINAL-001.1/2 KeyboardInterrupt/CancelledError → 정상 종료
- P-FINAL-001.3 finally → scraper.close() + STT 해제
- P-ENTRY-001.1~3 main() 최상위 예외

---

## B. API 서버 + config/notify — ~40 경우

### B1. 부팅 (server.py:106-128)
- BOOT-001 토큰 미설정 + ALLOW_NO_TOKEN≠1 → RuntimeError (server.py:32-37)
- BOOT-002 토큰 설정 → 정상 (server.py:29)
- BOOT-003 ALLOW_NO_TOKEN=1 → 인증 우회 (server.py:30,42-44)
- BOOT-004 preferred_port 사용 가능 → bind (server.py:111)
- BOOT-005 포트 점유 → +0~+9 fallback (server.py:111-114)
- BOOT-006 +9까지 점유 → preferred_port 반환 → bind 실패 (server.py:103)
- BOOT-007 ws_max_size=1MB 제한 (server.py:123)

### B2. 토큰 인증 (server.py:40-49)
- AUTH-001 ALLOW_NO_TOKEN=1 → 통과
- AUTH-002 헤더 없음 → 401
- AUTH-003 "Bearer " 접두사 없음 → 401
- AUTH-004 잘못된 토큰 → 403 (constant-time 비교)
- AUTH-005 정확한 토큰 → 통과
- AUTH-006 /health,/version → 인증 미적용

### B3. health (health.py)
- HEALTH-001 → 200 {"status":"ok"}
- VERSION-001 CHANGELOG 파싱 성공 → 200 {version}
- VERSION-002 CHANGELOG 없음/미일치 → 200 {"version":"unknown"}

### B4. config (config.py)
- CONFIG_GET-001~003 인증 통과 → SettingsResponse (DOWNLOAD_DIR 기본값 분기 포함)
- CONFIG_PUT-001 잘못된 JSON → 422
- CONFIG_PUT-002 타입 오류 → 422
- CONFIG_PUT-003 정상 → save_settings() → 200
- CONFIG_PUT-004 .env 쓰기 실패 → 500
- CONFIG_TG-001~004 telegram 설정 저장 (동일 분기)
- CONFIG_VERIFY-003 토큰 형식 오류 → INVALID_TOKEN_FORMAT
- CONFIG_VERIFY-004 네트워크 실패 → NETWORK_ERROR
- CONFIG_VERIFY-005 JSON 파싱 실패 → TELEGRAM_API_ERROR
- CONFIG_VERIFY-006 401/404 → INVALID_TOKEN
- CONFIG_VERIFY-007 5xx/429 → TELEGRAM_API_ERROR
- CONFIG_VERIFY-008 getMe+sendMessage 성공 → ok:true
- CONFIG_VERIFY-009 sendMessage 실패 → INVALID_CHAT_ID
- CONFIG_CRED-001~003 자격증명 유무 (복호화 실패 → false 분기 포함)

### B5. notify (notify.py)
- NOTIFY_DL-001/002 JSON/필드 오류 → 422
- NOTIFY_DL-003 텔레그램 미설정 → {"sent":0,"message":"텔레그램 미설정"}
- NOTIFY_DL-004 텔레그램 설정 → stub ("Electron 측 데이터 연동 필요") ← TODO 미완
- NOTIFY_TG-001/002 JSON/필드 오류 → 422
- NOTIFY_TG-003 텔레그램 미설정 → {"ok":false}
- NOTIFY_TG-004~006 playback_complete/playback_error/download_error
- NOTIFY_TG-007 알 수 없는 message_type → {"ok":false,"error":"알 수 없는..."}
- _send_message 재시도: 4xx 즉시 실패 / 5xx·429 최대 3회 (1s,2s,4s backoff)

---

## C. download 라우트 + 파이프라인 — ~69 경우

### C1. 엔드포인트 (download.py)
- /resolve-path: 정상 / 경로 이스케이프 400 / None 반환 실패
- /convert: 정상 / 경로검증 400 / mp4 없음 500 / ffmpeg 미설치 / 변환 실패 / mp3 이미 존재 skip
- /transcribe: 정상 / 경로검증 400 / model_size 무효 400 / faster-whisper 미설치 / RAM 부족 자동 다운그레이드 / 전부 부족 / 빈 결과 / 예외 후 safe_unload (finally 보장)
- /summarize: 정상(Gemini/OpenAI) / 경로검증 400 / agent 무효 400 / API 키 무효 / 빈 텍스트 ValueError / 청크 분할 / 재귀 상한 / 타임아웃
- WS /pipeline:
  - 5.1 정상 완료 → complete + close
  - 5.2 토큰 불일치 → error + close(4003)
  - 5.3 토큰 미설정 + 우회 아님 → close(4003)
  - 5.4 mp4_path 경로검증 실패 → HTTPException 400
  - 5.5 클라이언트 disconnect → pipeline_task.cancel()
  - 5.6 파이프라인 예외 → cancel + error(PIPELINE_ERROR)
  - 5.7 send_json 실패 (연결 닫힘) → 무시

### C2. run_pipeline 단계 전이 (download_pipeline.py:101-284)
- CONVERT: 정상 / FileNotFoundError·RuntimeError → CONVERT_FAILED return / 건너뜀(audio_only=F·both=F) / mp3_path+mp4 유지
- TRANSCRIBE: 정상 / 무음 빈 결과 / 예외 → stage_errors + finally safe_unload → continue / 건너뜀(mp3 없음·stt_disabled)
- SUMMARIZE: 정상 / is_transcript_usable=False → TRANSCRIPT_EMPTY continue / API 실패·타임아웃 → stage_errors continue / 청크 분할 / 건너뜀
- NOTIFY: 정상 / 청크 부분 전송 실패 → 파일 첨부 fallback / 전부 실패 → NOTIFY_FAILED continue / 50MB 초과 / 토큰 형식 오류 / 5xx 재시도 / 4xx 즉시 / 건너뜀 / auto_delete 삭제 실패 무시

### C3. recover_pipeline (recover_pipeline.py:56-159)
- collect_missing: 정상 / drift 포함(include_store_drift=T) / drift 제외(기본)
- run_recovery: 전부 성공 / 부분 / 예외 항목 / unsupported → mark_unsupported / 일시 실패 → mark_download_failed(retriable) / 콜백 예외 격리 / store=None

---

## D. scripts 4개 + 자동 모드 — ~80 경우

### D1. reconcile_progress.py
- RP-1 자격증명 없음 → exit 1
- RP-2 dry-run (--apply 미지정) → 영향 분석만
- RP-3 --apply → store.save()
- RP-3a save 예외 → exit 1
- RP-3b LMS 로그인 실패 → finally close()

### D2. recover_missing.py
- RCV-1 자격증명 없음 → exit 1
- RCV-2/2a DOWNLOAD_DIR override / Windows /data fallback
- RCV-3 --course 필터 (부재 시 exit 1)
- RCV-4 누락 0건 → exit 0
- RCV-5 --dry-run → 목록만 → exit 0
- RCV-6/6a/6b 대화형 확인 (y/n)
- RCV-7 복구 중 예외 → finally store.save()
- RCV-7a 부분 실패 → exit 2 / RCV-7b 전부 성공 → exit 0

### D3. sanitize_logs.py
- SL-1 로그 디렉토리 부재 → exit 1
- SL-2 대상 파일 없음 → exit 0
- SL-3 dry-run / SL-4 --no-backup / SL-5 backup
- SL-5a 백업 실패 → skip / SL-6 read 실패 skip / SL-7 write 실패 skip
- SL-8/9 완료 (0건 / N건)

### D4. migrate_drive_root_downloads.py
- MD-1 source 부재 → exit 0
- MD-2 source==target → exit 0
- MD-3 dry-run / MD-4 --apply
- MD-4a 크기 동일 → source 삭제 / MD-4b 크기 다름 → 충돌 skip
- MD-4c 이동 성공 / MD-4d stat 실패 / MD-4e move 실패
- MD-5 conflict=0 → exit 0 / MD-6 conflict>0 → exit 2

### D5. 자동 모드 (auto.py)
- AUTO-PRE-1~6 필수 조건 (STT/AI/텔레그램/자격증명/AI키) 검사
- AUTO-SCHED-1~5 스케줄 설정 (기본/커스텀/무효/즉시 실행 y·n)
- AUTO-LOOP-1 "0" 입력 → 루프 탈출
- AUTO-LOOP-2 stdin EOF (non-TTY)
- AUTO-LOOP-3 스케줄 미도달 → 대기줄 갱신
- AUTO-LOOP-4 스케줄 도달 → 사이클
- AUTO-LOOP-5/5a 주기적 브라우저 재시작 (실패 시 stop_event.set)
- AUTO-FETCH-1~3 과목/강의 갱신 (성공/추가제거/실패 유지/강의 실패 → 60s 대기 continue)
- AUTO-COLLECT-1~5 강의 수집 (needs_watch / 격리 / FS 확정 / dl_only / 재토글 미완)
- FULL-1~7 _process_lecture 재생 (세션 / 재생 1회차 / 성공 / 재시도 / 브라우저 death / 최종 실패 / 중단 / 다운로드 진입)
- DL-1~6 _run_download_step (시도 / 성공 / 구조적 실패 즉시 / 일시 실패 재시도 / 예외 / 전부 실패)
- DL_ONLY-1~3 _process_download_only
- STATE-1~7a _apply_play_result + mark_play_failed (격리 임계 5회)
- RECON-1~3 reconcile + list_missing + notify
- AUTO-END-1~3 사이클 종료 / Ctrl+C / STT 언로드

### D6. ProgressStore 영속화 (progress_store.py)
- PS-1 파일 부재 → entries={}
- PS-2 v1 리스트 → v2 마이그레이션
- PS-3 v2 dict 로드
- PS-4 손상 JSON → entries={} 안전 회복
- PS-5 mark_*() → _touched/_dirty 누적 (메모리만)
- PS-6 flush() + _dirty==0 → 즉시 반환
- PS-7 flush() + _dirty>0 → locked_transaction merge
- PS-8 maybe_flush() → _dirty<interval skip
- PS-9 동시 실행 (auto+recover) → locked_transaction 직렬화 lost-update 방지

---

## Codex 페어 검토 대상 (논리 오류 후보 영역)

검토 시 특히 주목할 교차 관심사:
1. **재시도 카운트 경계** — RetryPolicy 적용 지점 off-by-one (URL 추출, 재생, 다운로드, 텔레그램)
2. **상태 전이 일관성** — ProgressStore mark_* 호출 순서, 격리 임계 transition-edge
3. **실패 경로 후속 처리** — 한 단계 실패 시 후속 단계 skip vs continue 일관성
4. **취소/disconnect race** — WS pipeline cancel, run_in_executor blocking
5. **빈 입력/엣지 케이스** — 과목 0개, 강의 0개, 누락 0건
6. **종료 상태 코드** — exit 0/1/2 의미 일관성 (scripts)
7. **동시성** — locked_transaction, 파일락, Windows advisory lock 한계
