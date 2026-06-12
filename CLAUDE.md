# study-helper: LMS 백그라운드 학습 도구

숭실대학교 Canvas LMS(canvas.ssu.ac.kr)의 강의 영상을 Docker 컨테이너 기반 CUI 환경에서
백그라운드로 재생(출석 처리)하거나 다운로드/변환/요약할 수 있는 도구.

별도 Electron GUI 앱: [study-helper-app](https://github.com/TaeGyumKim/study-helper-app)

## 실행 방법

```bash
# CLI 모드 (기존 Docker CUI)
docker compose run --rm study-helper

# API 서버 모드 (Electron GUI 앱 연동용)
python -m src.api.server
```

- **`docker compose up` 사용 금지**: 로그 멀티플렉싱으로 TUI 깨짐. `run --rm`만 사용할 것
- `src/`는 볼륨 마운트되어 있어 코드 수정 후 재빌드 없이 재실행만 해도 반영됨
- `.env`, `.secret_key`는 볼륨 마운트로 호스트에 영속화됨
- 다운로드 파일은 `./data/`에 저장됨 (컨테이너 내 `/data/`)
- Whisper 모델, Playwright Chromium은 named volume에 캐시되어 재빌드 시 재다운로드 불필요

Docker Hub 릴리즈 이미지 사용 시: `docker-compose.yml` 상단 주석 참고.

### 유지보수 스크립트

`scripts/` 하위 CLI 는 컨테이너 안에서 `docker compose run --rm study-helper python scripts/<이름>.py` 로 실행.

| 스크립트 | 용도 |
|----------|------|
| `reconcile_progress.py` | 파일시스템 ↔ `auto_progress.json` drift 재조정 |
| `recover_missing.py` | `completed` 인데 파일 없는 강의 일괄 재다운로드 (ProgressStore 연동) |
| `sanitize_logs.py` | 기존 로그 파일에 `mask_sensitive` 소급 적용 (공유 전 필수) |
| `migrate_drive_root_downloads.py` | Windows 드라이브 루트 `\data` 트랩의 파일을 프로젝트 `data/` 로 이관 |

### 로그 기반 트러블슈팅

다운로드/STT/요약 실패 시 [docs/log-troubleshooting.md](docs/log-troubleshooting.md) 의 grep 레시피 → reason 매트릭스 → 수정 포인트 순서로 추적. `logs/study_helper.log` 에는 요약, `logs/YYYYMMDD_HHMMSS_download.log` 에는 traceback.

## 개발 환경 설정

의존성 추가 시 `pyproject.toml` 수정 후 `docker compose up`으로 재빌드.

torch는 `pyproject.toml`에 포함하지 않음 — Dockerfile에서 CPU wheel로 직접 설치.

## 절대 건드리면 안 되는 것들

- **Playwright headless Chromium 유지**: 시스템 Chrome 경로 하드코딩 금지. Docker에서는 Playwright 내장 Chromium만 사용.
- **이 프로젝트에 GUI 의존성 추가 금지**: flet, PyQt5 등 GUI 라이브러리 사용 금지. CUI 전용. GUI는 별도 Electron 프로젝트(study-helper-app)에서 담당.
- **비디오 셀렉터**: `video.vc-vplay-video1`로 영상 URL 추출. 변경 시 LMS 쪽 변경 확인 필요.
- **SSOT 재구현 금지**: `Config.get_data_base()` / `get_logs_path()` / `RetryPolicy` / `get_ai_api_key()` / `get_ai_model()` 는 개별 모듈에서 재구현 금지. 수치 튜닝은 `RetryPolicy` 내부 상수만 수정. 상세는 "공용 인프라" 섹션 참조.
- **원자 쓰기 경유 강제**: `.env`, `.secret_key`, `auto_progress.json`, `deadline_notified.json` 등 상태 파일은 반드시 `src/util/atomic_write.py` 로만 기록 — `atomic_write_text` + `file_lock`, 또는 load→mutate→save 가 필요하면 `locked_transaction`. 직접 `open(..., "w")` 후 rename 금지. (`.secret_key` 는 `crypto._load_or_create_key` 가 `file_lock` 안에서 atomic 생성 — cross-process 키 분기 race 방지)
- **로거 API 선택**: 신규 코드는 `get_logger("이름")` 만 사용 (LOG-SYS-1/4). `logging.getLogger(__name__)` 는 silent log loss 유발이므로 회귀 금지. `SensitiveFilter` 는 반드시 `handler.addFilter` 로 부착 — `logger.addFilter` 는 propagate 된 child 레코드를 놓친다 (Python logging semantics).

## 설계 의도

- **기본 엔진**: STT는 faster-whisper(로컬, CTranslate2 기반), 요약은 Gemini API. 키는 `.env`에서 로드.
- **다운로드 경로**: `과목명/N주차/강의명.mp4` 구조. 컨테이너 내 `/data/downloads/`.
- **출력 파일**: mp4(영상), mp3(음성, ffmpeg 변환), txt(STT 결과), `_summarized.txt`(요약).
- **백그라운드 재생**: video DOM 폴링(Plan A) + 진도 API 직접 호출(Plan B) 두 방식으로 구현. Plan A 실패 시 자동으로 Plan B로 전환.
- **암호화**: Fernet 대칭 암호화. 네이티브 앱에서는 OS 키체인(keyring) 우선, 파일 fallback.
- **자동 모드**: `service/scheduler.py` 의 스케줄 기반 미시청 강의 순차 처리 (재생→다운로드→STT→요약→텔레그램). 진행 상태는 `service/progress_store.py` (`auto_progress.json`, v1→v2 자동 마이그레이션) 의 `flush()` 가 `locked_transaction` 으로 디스크 최신본을 merge 후 원자 기록 — recover/reconcile 동시 실행 시 lost update 방지. 저장 주기는 `PROGRESS_SAVE_INTERVAL` (기본 5강의마다). 파일시스템 ↔ store drift 는 `service/download_state.py` 가 FS→store 단방향으로 정정.
- **수동 복구**: `service/recover_pipeline.py` 가 `completed` 인데 파일이 없는 강의를 재다운로드. UI(`src/ui/recover.py`) 와 CLI(`scripts/recover_missing.py`) 가 단일 파이프라인 공유.
- **마감 임박 알림**: 비디오 외 항목(퀴즈, 과제 등)의 마감 24h/12h 전 텔레그램 알림.
- **API 서버**: FastAPI HTTP + WebSocket. Electron GUI 앱에서 호출. 토큰 인증 fail-closed (SEC-002).

## 공용 인프라 (SSOT · 중복 구현 금지)

아래 모듈/헬퍼는 **단일 지점(Single Source of Truth)** 으로 의도됐다. 같은 기능이 필요하면 재구현 대신 여기부터 재사용할 것. 과거 분산 구현으로 인해 drift 가 반복 발생한 영역.

- **경로 해결**: `Config.get_data_base()` (data 루트) · `Config.get_data_path(name)` (data 파일) · `Config.get_logs_path()` (logs 루트). 개별 모듈에서 `Path("/data")` 직접 접근이나 `os.getenv("STUDY_HELPER_DATA_DIR")` 재파싱 금지.
- **재시도 정책**: `Config.RetryPolicy` — `PLAY` / `DOWNLOAD` / `URL_EXTRACT` / `STREAM` / `TELEGRAM` / `URL_RETRY_WAIT_SEC` / `TELEGRAM_BASE_DELAY` / `BROWSER_RESTART_INTERVAL`. 모듈 내부 `_MAX_*_RETRIES` 로컬 상수 재도입 금지.
- **AI 키/모델 조회**: `Config.get_ai_api_key()` · `Config.get_ai_model()` — `AI_AGENT == "gemini"` 분기 중복 금지.
- **미디어 파일 존재 판정**: `src/downloader/paths.py` 의 `media_present(path)` — 존재(`exists`) + 스텁 하한(`_MIN_VALID_MEDIA_BYTES`, 64KB) 통과해야 "있음". `file_present` 와 `service/download_state.list_missing_items` 가 공유하는 단일 판정 소스. 개별 모듈에서 `path.exists()` 직접 사용으로 존재 판정 금지 — 실패 다운로드가 남긴 수 KB 스텁을 "있음"으로 오판해 recover 재다운로드를 영구 skip 하는 stub masking 회귀 차단. 상세: docs/solutions(toolkit) `download-presence-check-stub-masking`.
- **원자 쓰기 / 크로스 프로세스 락 / 트랜잭션**: `src/util/atomic_write.py` — `atomic_write_text(path, text, mode=0o600)` (원자 교체), `file_lock(path)` (cross-process flock), `locked_transaction(path, load_fn=, save_fn=)` (load→mutate→save 를 단일 락으로 묶어 lost update 방지 — `progress_store.flush` / `config._save_env` / `deadline_checker` 가 사용). `.env`, `.secret_key`, `auto_progress.json`, `deadline_notified.json` 모두 이 모듈 사용. 직접 `open(..., "w")` 후 rename 패턴 금지. `locked_transaction` 의 `load_fn`/`save_fn` 안에서 같은 path 의 락 재호출 금지(self-deadlock).
- **텔레그램 디스패처**: `src/notifier/telegram_dispatch.py` 의 `dispatch_if_configured(notify_fn, **kwargs)` — credential 분기 보일러플레이트 단일화 (ARCH-004). 호출부에서 `Config.get_telegram_credentials()` + 분기 재구현 금지.
- **TUI 헤더**: `src/ui/_widgets.py` 의 `header_panel(title, ...)` — 중앙 정렬 Rich Panel. 각 화면에서 `Panel(Text(...))` 조립 금지.
- **STT 모델 해제**: `src/stt/transcriber.py::safe_unload()` — 예외 억제된 unload. 각 호출사이트 `try/import/bare except` 복제 금지.
- **로거 팩토리**: `src/logger.py::get_logger("이름")` 만 사용. `logging.getLogger(__name__)` 는 root 핸들러 부재로 silent log loss 유발 (LOG-SYS-1). `get_error_logger("action")` 은 기존 호환용 deprecated (LOG-SYS-4).
- **PII 마스킹 규칙**: `src/util/log_sanitize.py::_SENSITIVE_KEYS`. 새 민감 키 추가 시 이 정규식만 갱신. `SensitiveFilter` 는 `logger.py` 에서 **handler 단** 에 자동 부착되어 모든 로그 파일에 적용.

## 프로젝트 구조

```
study-helper/
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── src/
│   ├── main.py                       # CLI 진입점
│   ├── config.py                     # 환경변수 + 경로 해결 (get_data_base / get_logs_path) + RetryPolicy + AI 키 조회 헬퍼
│   ├── crypto.py                     # Fernet 암호화 (keyring namespace 사용자별 분리 · SEC-007)
│   ├── logger.py                     # 에러 로깅
│   ├── updater.py                    # 버전 체크
│   ├── auth/
│   │   └── login.py                  # Playwright 로그인 처리
│   ├── scraper/
│   │   ├── course_scraper.py         # 과목/주차/강의 스크래핑 (병렬 지원)
│   │   └── models.py                 # Course, LectureItem, Week 등 데이터 모델
│   ├── player/
│   │   ├── background_player.py      # 백그라운드 재생 (출석용)
│   │   └── fake_video.py             # Chromium H.264 우회용 VP8/WebM 더미 생성
│   ├── downloader/
│   │   ├── video_downloader.py       # 영상 URL 추출 + HTTP 스트리밍 다운로드
│   │   ├── paths.py                  # expected_paths / file_present / media_present — 파일 존재 판정(존재+스텁 하한) 단일 소스
│   │   └── result.py                 # DownloadResult + REASON_* 상수 (UNSUPPORTED / URL_EXTRACT_* / NETWORK 등)
│   ├── converter/
│   │   └── audio_converter.py        # mp4 → mp3 (ffmpeg)
│   ├── stt/
│   │   └── transcriber.py            # faster-whisper STT + safe_unload 헬퍼
│   ├── summarizer/
│   │   └── summarizer.py             # Gemini/OpenAI API 요약
│   ├── notifier/
│   │   ├── telegram_notifier.py      # 텔레그램 봇 알림
│   │   ├── telegram_dispatch.py      # credential 가드 디스패처 — dispatch_if_configured SSOT (ARCH-004). 호출부 재구현 금지
│   │   └── deadline_checker.py       # 마감 임박 알림 체크
│   ├── service/                      # UI 독립 서비스 레이어 (Electron 연동)
│   │   ├── download_pipeline.py      # 다운로드→변환→STT→요약→알림 파이프라인
│   │   ├── download_state.py         # 다운로드 상태 추적
│   │   ├── progress_store.py         # 자동 모드 진행 저장소 (원자 쓰기 + 파일락)
│   │   ├── recover_pipeline.py       # 미완료 다운로드 재개
│   │   └── scheduler.py              # 스케줄 관리
│   ├── util/                         # 공용 유틸리티 (URL 정제, 로그 마스킹, 원자 쓰기)
│   │   ├── atomic_write.py           # atomic_write_text + file_lock + locked_transaction
│   │   ├── log_sanitize.py           # PII/OAuth 마스킹 규칙
│   │   └── url.py                    # safe_url (쿼리 제거)
│   ├── api/                          # FastAPI 서버 (Electron 연동)
│   │   ├── server.py                 # 앱 + 토큰 인증 + CORS
│   │   └── routes/
│   │       ├── health.py             # GET /health, /version
│   │       ├── config.py             # 설정 CRUD
│   │       ├── download.py           # 변환/STT/요약 + WS 파이프라인
│   │       └── notify.py             # 텔레그램 알림
│   └── ui/                           # CUI 화면 (Rich TUI)
│       ├── _widgets.py               # header_panel 공용 헤더 위젯
│       ├── login.py
│       ├── courses.py
│       ├── player.py
│       ├── download.py
│       ├── auto.py                   # 자동 모드
│       ├── recover.py                # 수동 복구
│       └── settings.py
├── data/
│   └── downloads/                    # 과목명/N주차/강의명.mp4 구조
├── scripts/                          # 유지보수 CLI (컨테이너 내 `python scripts/<이름>.py` 실행)
│   ├── reconcile_progress.py         # 파일시스템 ↔ auto_progress.json drift 재조정
│   ├── recover_missing.py            # completed 인데 파일 없는 강의 일괄 재다운로드 (ProgressStore 연동)
│   ├── sanitize_logs.py              # 기존 로그 파일에 mask_sensitive 소급 적용
│   └── migrate_drive_root_downloads.py  # Windows 드라이브 루트 \data 트랩 → 프로젝트 data/ 이관
└── tests/                            # pytest — atomic_write / crypto / config / download_state / logger_* 등
```

## LMS 기술 메모

| 항목 | 값 |
|------|-----|
| 대시보드 URL | `https://canvas.ssu.ac.kr/` |
| 과목 목록 | `window.ENV.STUDENT_PLANNER_COURSES` (JS 평가) |
| 강의 목록 URL | `https://canvas.ssu.ac.kr/courses/{course_id}/external_tools/71` |
| 강의 목록 iframe | `iframe#tool_content` → `#root` (data-course_name, data-professors) |
| 주차/강의 파싱 | `.xnmb-module-list`, `.xnmb-module_item-outer-wrapper` 등 `.xnmb-*` 클래스 |
| 완료 여부 | `[class*='module_item-completed']` (completed / incomplete) |
| 출석 상태 | `[class*='attendance_status']` (attendance / late / absent / excused) |
| 비디오 | `video.vc-vplay-video1` |

## 환경 변수 (.env)

계정 정보와 설정은 최초 실행 시 TUI에서 입력하면 자동 저장됨. 직접 편집도 가능.

```
# 계정 (자동 저장, 암호화)
LMS_USER_ID=
LMS_PASSWORD=

# 다운로드 설정
DOWNLOAD_DIR=          # 비워두면 Docker: /data/downloads, macOS: ~/Downloads
DOWNLOAD_RULE=         # video / audio / both

# STT 설정
STT_ENABLED=           # true / false
STT_LANGUAGE=ko        # ko / en / 빈값(자동 감지)
WHISPER_MODEL=base     # tiny / base / small / medium / large
WHISPER_CPU_THREADS=2  # CTranslate2 cpu_threads 상한 (미설정 시 2). STT 전 코어 점유 방지

# AI 요약 설정
AI_ENABLED=            # true / false
AI_AGENT=              # gemini / openai
GEMINI_MODEL=          # gemini-2.5-flash 등
GOOGLE_API_KEY=
OPENAI_API_KEY=
SUMMARY_PROMPT_EXTRA=  # 요약 프롬프트 추가 지시사항

# 텔레그램 알림
TELEGRAM_ENABLED=      # true / false
TELEGRAM_BOT_TOKEN=    # 암호화 저장
TELEGRAM_CHAT_ID=
TELEGRAM_AUTO_DELETE=  # true / false (전송 후 파일 자동 삭제)

# API 서버 (Electron 앱 연동)
STUDY_HELPER_API_TOKEN=     # 필수 (SEC-002 fail-closed). 미설정 시 `python -m src.api.server` 부팅 거부.
                            # Electron 앱이 랜덤 토큰을 생성해 주입하는 것이 정상 경로.
STUDY_HELPER_API_ALLOW_NO_TOKEN=  # 개발 전용 우회 플래그 (1 설정 시만 토큰 없이 기동). 운영에서 설정 금지.
STUDY_HELPER_API_PORT=18090 # API 서버 포트
STUDY_HELPER_DATA_DIR=      # 데이터 디렉토리 (Electron: userData/core-data)

# 자동 모드
PROGRESS_SAVE_INTERVAL=5    # auto_progress.json 저장 주기 (N개 강의마다, 미설정 시 5)
```

## Git 커밋 규칙

형식: `type(scope): 한국어 설명` — 첫 줄 72자 이내

| type | 용도 |
|------|------|
| feat | 새 기능 |
| fix | 버그 수정 |
| refactor | 리팩토링 |
| docs | 문서 |
| test | 테스트 |
| chore | 빌드/도구 설정 |

## 보안 주의사항

아래 항목은 `.gitignore`에 등록되어 있음. 커밋 전 `git status`로 반드시 확인.

- `.env` — 실제 설정값 저장 파일. **절대 커밋 금지**. `.env.example`만 커밋 허용
- `.secret_key` — 계정/API 키 암호화에 사용하는 키. **절대 커밋 금지**
- `data/` — `data/downloads/`에 저장되는 다운로드 파일. **절대 커밋 금지**

**민감 정보 처리**: 학번, 비밀번호, API 키는 TUI 입력 즉시 `crypto.py`로 암호화되어 `.env`에 저장됨. 평문으로 저장되지 않음. 네이티브 앱에서는 OS 키체인(keyring) 우선 사용.

### 활성 하드닝 정책 (신규 조치 · 제거/완화 금지)

- **파일 권한 0o600** (POSIX): `.env`, `.secret_key`, `logs/*.log` 는 `atomic_write_text(..., mode=0o600)` 또는 `chmod(0o600)` 으로 저장 (SEC-001 / SEC-008). Windows 는 no-op.
- **API 토큰 fail-closed**: `STUDY_HELPER_API_TOKEN` 미설정 시 서버 부팅 거부 (SEC-002). `STUDY_HELPER_API_ALLOW_NO_TOKEN=1` 은 개발 전용 임시 우회. 운영 기본값에서 제거 금지.
- **WebSocket 보호**: 인증 없는 우회 경로 차단 (SEC-003), 페이로드 1MB 제한 (`ws_max_size`, SEC-004).
- **에러 응답 새니타이즈**: API 응답은 `stage_errors` 의 **고정 코드 (type 이름)** 만 노출 (SEC-005). 원본 메시지는 `stage_messages` 로 분리해 로컬 TUI/로그만 참조. Traceback/파일 경로를 클라이언트에 노출 금지.
- **Keyring namespace**: `fernet-key:{USERNAME}` 로 OS 사용자별 분리 (SEC-007). Legacy `fernet-key` 는 자동 migrate. 하드코딩 회귀 금지.
- **전역 로그 마스킹**: `SensitiveFilter` (LOG-SYS-3) 가 모든 **handler** 에 부착되어 PII/OAuth/봇 토큰/이메일/비밀번호 값을 `***REDACTED***` 로 치환. **주의**: filter 는 반드시 `Handler.addFilter()` 로 붙여야 child 로거 propagate 레코드에도 적용됨. `Logger.addFilter()` 는 Python logging semantics 상 propagate 레코드를 통과시키므로 사용 금지. 민감 값 키 추가 시 `src/util/log_sanitize.py` 의 `_SENSITIVE_KEYS` 에만 추가.
- **로그 파일 영속 보장**: 모든 모듈 로거는 `get_logger("모듈명")` 으로 `study_helper.*` 트리에 귀속(LOG-SYS-1). `logging.getLogger(__name__)` 는 root 핸들러 부재로 silent log loss 유발 → 신규 코드 금지.
- **에러 로그 보존 기간**: `logs/YYYYMMDD_HHMMSS_*.log` 는 14일 경과 시 자동 삭제 (LOG-SYS-2). 장기 증거가 필요하면 별도 아카이브.
