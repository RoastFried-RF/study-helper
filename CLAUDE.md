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

## 개발 환경 설정

의존성 추가 시 `pyproject.toml` 수정 후 `docker compose up`으로 재빌드.

torch는 `pyproject.toml`에 포함하지 않음 — Dockerfile에서 CPU wheel로 직접 설치.

## 절대 건드리면 안 되는 것들

- **Playwright headless Chromium 유지**: 시스템 Chrome 경로 하드코딩 금지. Docker에서는 Playwright 내장 Chromium만 사용.
- **이 프로젝트에 GUI 의존성 추가 금지**: flet, PyQt5 등 GUI 라이브러리 사용 금지. CUI 전용. GUI는 별도 Electron 프로젝트(study-helper-app)에서 담당.
- **비디오 셀렉터**: `video.vc-vplay-video1`로 영상 URL 추출. 변경 시 LMS 쪽 변경 확인 필요.

## 설계 의도

- **기본 엔진**: STT는 faster-whisper(로컬, CTranslate2 기반), 요약은 Gemini API. 키는 `.env`에서 로드.
- **다운로드 경로**: `과목명/N주차/강의명.mp4` 구조. 컨테이너 내 `/data/downloads/`.
- **출력 파일**: mp4(영상), mp3(음성, ffmpeg 변환), txt(STT 결과), `_summarized.txt`(요약).
- **백그라운드 재생**: video DOM 폴링(Plan A) + 진도 API 직접 호출(Plan B) 두 방식으로 구현. Plan A 실패 시 자동으로 Plan B로 전환.
- **암호화**: Fernet 대칭 암호화. 네이티브 앱에서는 OS 키체인(keyring) 우선, 파일 fallback.
- **자동 모드**: 스케줄 기반 미시청 강의 순차 처리 (재생→다운로드→STT→요약→텔레그램).
- **마감 임박 알림**: 비디오 외 항목(퀴즈, 과제 등)의 마감 24h/12h 전 텔레그램 알림.
- **API 서버**: FastAPI HTTP + WebSocket. Electron GUI 앱에서 호출. 토큰 인증.

## 프로젝트 구조

```
study-helper/
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── src/
│   ├── main.py                       # CLI 진입점
│   ├── config.py                     # 환경변수 로드, KST 상수, get_data_path
│   ├── crypto.py                     # 암호화/복호화 (Fernet + keyring)
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
│   │   └── video_downloader.py       # 영상 URL 추출 + HTTP 스트리밍 다운로드
│   ├── converter/
│   │   └── audio_converter.py        # mp4 → mp3 (ffmpeg)
│   ├── stt/
│   │   └── transcriber.py            # faster-whisper STT + safe_unload 헬퍼
│   ├── summarizer/
│   │   └── summarizer.py             # Gemini/OpenAI API 요약
│   ├── notifier/
│   │   ├── telegram_notifier.py      # 텔레그램 봇 알림
│   │   ├── telegram_dispatch.py      # credential 가드 디스패처 (dispatch_if_configured)
│   │   └── deadline_checker.py       # 마감 임박 알림 체크
│   ├── service/                      # UI 독립 서비스 레이어 (Electron 연동)
│   │   ├── download_pipeline.py      # 다운로드→변환→STT→요약→알림 파이프라인
│   │   ├── download_state.py         # 다운로드 상태 추적
│   │   ├── progress_store.py         # 자동 모드 진행 저장소 (원자 쓰기 + 파일락)
│   │   ├── recover_pipeline.py       # 미완료 다운로드 재개
│   │   └── scheduler.py              # 스케줄 관리
│   ├── util/                         # 공용 유틸리티 (URL 정제, 로그 마스킹, 원자 쓰기)
│   │   ├── atomic_write.py           # atomic_write_text + cross-process file_lock
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
└── data/
    └── downloads/                    # 과목명/N주차/강의명.mp4 구조
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
STUDY_HELPER_API_TOKEN=     # Electron이 시작 시 자동 설정
STUDY_HELPER_API_PORT=18090 # API 서버 포트
STUDY_HELPER_DATA_DIR=      # 데이터 디렉토리 (Electron: userData/core-data)
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
