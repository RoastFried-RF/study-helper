# 로그 기반 다운로드 실패 트러블슈팅 가이드

> 목적: `logs/study_helper.log` 와 `logs/YYYYMMDD_HHMMSS_download.log` 만 보고 다운로드 실패 원인을 파악한 뒤, 어느 코드를 수정해야 하는지 바로 찾을 수 있도록 한다.
> 업데이트 기준: 2026-04-24.

## 먼저: 로그 위치와 검색 방법

```bash
# 세션 단위 통합 로그 — 모든 모듈의 info/warning/error 가 모임
tail -f logs/study_helper.log

# 다운로드·재생 실패 전용 상세 로그 — 동작별 타임스탬프 파일
ls -lt logs/*_download.log | head -3
cat logs/YYYYMMDD_HHMMSS_download.log  # 가장 최근

# 특정 강의의 모든 경로 추적
grep -i "강의_이름_일부" logs/study_helper.log
```

## 단계별 실패 신호 → 원인 → 수정 포인트

### 1. 다운로드 시작이 로그에 없다면

**신호**:
```
grep "다운로드 시작" logs/study_helper.log
# → 히트 없음
```

**원인**:
- `run_download` 가 호출되지 않음. 상위 플로우(자동 모드/TUI 선택) 문제.

**수정 포인트**:
- 자동 모드: [src/ui/auto.py](../src/ui/auto.py) `_run_download_step` 호출 경로.
- TUI: [src/main.py](../src/main.py) `LectureAction.DOWNLOAD` 분기.

### 2. 구조적 다운로드 불가 (`REASON_UNSUPPORTED`)

**신호**:
```
grep "다운로드 실패 reason=UNSUPPORTED" logs/study_helper.log
# → study_helper.ui.download: 다운로드 실패 reason=UNSUPPORTED — 구조적 미지원 ...type=learningx
```

**원인**:
- `LectureItem.is_downloadable = False`. `learningx` 등 LTI 전용 플레이어 강의는 mp4 URL 추출 불가.

**수정 포인트**:
- 신규 lecture type 지원 시: [src/scraper/models.py](../src/scraper/models.py) `LectureType` + `is_downloadable` property 수정.
- 로그에 나타난 `type=...` 값이 어떤 lecture_type 인지 확인하고 `models.py` 의 분기 추가.

### 3. URL 추출 실패 (`REASON_URL_EXTRACT_*`)

**신호**:
```
grep "URL 추출 실패" logs/study_helper.log
# → study_helper.ui.download: URL 추출 실패 — attempt=1/3 reason=CONTENT_PHP_MISSING diag={'content_php_seen': False, ...}
# → study_helper.ui.download: URL 추출 실패 — attempt=2/3 reason=TIMEOUT ...
# → study_helper.ui.download: URL 추출 실패 — attempt=3/3 reason=HLS_ONLY ...
# → study_helper.ui.download: 다운로드 실패 reason=URL_EXTRACT_HLS_ONLY — URL 추출 3회 실패 ...
```

**원인별 수정 포인트**:

| reason | 의미 | 수정 포인트 |
|--------|------|-------------|
| `URL_EXTRACT_NO_PLAYER` | player frame 미탐지 | [src/player/background_player.py](../src/player/background_player.py) `find_player_frame` — LMS iframe 구조 변경 확인. `_FRAME_FIND_TIMEOUT` 늘리기 고려 |
| `URL_EXTRACT_CONTENT_PHP_MISSING` | content.php 응답 한 번도 없음 | [src/downloader/video_downloader.py](../src/downloader/video_downloader.py) `_on_response` 필터 — LMS 가 content.php URL 을 다른 경로로 옮겼을 가능성 |
| `URL_EXTRACT_CONTENT_PHP_PARSE` | content.php 응답은 왔지만 XML 파싱 실패 | `diag.content_php_parse_error` 값 확인. `_parse_content_php` 의 XPath/필드명 변경 필요 |
| `URL_EXTRACT_HLS_ONLY` | mp4 없이 m3u8 스트림만 관측 | LMS 전환 (HLS only). 다운로더 재작성 (ffmpeg HLS) 필요 |
| `URL_EXTRACT_TIMEOUT` | 60초 폴링 시간 초과 | [src/downloader/video_downloader.py](../src/downloader/video_downloader.py) `_VIDEO_POLL_MAX` 늘리거나 Plan B 강화 |
| `URL_EXTRACT_EXCEPTION` | Playwright 자체 예외 | traceback (error 로그) 확인 |

**세션 만료 징후** (재로그인 중 실패):
```
grep "세션 만료\|재로그인" logs/study_helper.log
```
- `CourseScraper._fetch_lectures_on` 의 재로그인 경합이 원인. 단독 실행 시엔 드뭄. 자동 모드에서 병렬 스크래핑 중 발생 가능.

### 4. 경로 이스케이프 (`REASON_PATH_INVALID`)

**신호**:
```
grep "다운로드 실패 reason=PATH_INVALID" logs/study_helper.log
# → ... mp4_path=/tmp/evil/../etc/passwd base=/app/data/downloads course='...'
```

**원인**:
- `make_filepath` 가 `..` 등을 포함한 경로를 생성. 강의 제목에 경로 이스케이프 시퀀스 포함 가능.

**수정 포인트**:
- [src/downloader/video_downloader.py](../src/downloader/video_downloader.py) `_sanitize_filename` 보강 (현재 `re.sub(r'\.{2,}', '', name)` 로 `..` 제거 중).

### 5. 네트워크/프로토콜 실패 (`REASON_NETWORK`, `REASON_SSRF_BLOCKED`, `REASON_SUSPICIOUS_STUB`)

**신호**:
```
grep "다운로드 실패 reason=NETWORK" logs/study_helper.log
# → ... exc=ConnectionError ... : HTTPSConnectionPool(...): Max retries exceeded
# → Traceback 포함
```

**원인**:
- `ConnectionError`: 네트워크 끊김, DNS 실패. 대부분 재시도로 복구.
- `Timeout`: 서버 느림 또는 방화벽.
- `ChunkedEncodingError`: 중간에 연결 끊김 (CDN 이슈).

**수정 포인트**:
- 일시적이면 재시도 간격/횟수: [src/config.py](../src/config.py) `RetryPolicy.STREAM`, backoff.
- 영구적이면 URL 필터: `_validate_media_url` 허용 호스트 확인.

**SSRF 차단**:
```
grep "SSRFBlockedError" logs/study_helper.log
# → 허용되지 않는 호스트: evil.example.com
```
- LMS 가 CDN 을 교체한 경우. [src/downloader/video_downloader.py](../src/downloader/video_downloader.py) `_DEFAULT_ALLOWED_HOSTS_SUFFIX` 추가 또는 `DOWNLOAD_EXTRA_HOSTS` env 설정.

**Suspicious Stub** (파일이 너무 작음):
```
grep "SuspiciousStubError\|stub" logs/study_helper.log
```
- 2MB 미만 mp4 는 stub 판정. `_MIN_PLAUSIBLE_VIDEO_BYTES` 임계값 또는 Plan A 가 preloader/intro 파일을 잘못 집었을 가능성.
- [src/downloader/video_downloader.py](../src/downloader/video_downloader.py) `exclude_patterns` 에 새 stub 파일명 추가.

### 6. HTTP 상태 실패 (4xx/5xx)

**신호**:
```
grep "다운로드 HTTP 실패" logs/study_helper.log
# → study_helper.downloader: 다운로드 HTTP 실패 — status=403 content-type=application/json body[:200]='{"error":"token expired"}' path=xxx.mp4
```

**원인**:
- 403/401: CDN 토큰 만료 — URL 추출 단계에서 얻은 URL 이 만료됨. 추출~다운로드 간 시간 간격 확인.
- 404: URL 자체가 무효. Plan A 가 잘못된 src 를 잡음.
- 5xx: 서버 이슈 — 재시도 가치 있음.

**수정 포인트**:
- 403: URL 추출 직후 바로 다운로드하도록 순서 재조정 ([src/ui/download.py](../src/ui/download.py)).
- 404: `_is_valid_mp4` exclude 패턴 보강.

### 7. 파이프라인 후속 단계 실패 (mp3/STT/요약/텔레그램)

**신호**:
```
grep "단계 실패" logs/study_helper.log
# → study_helper.service.download_pipeline: convert 단계 실패: FileNotFoundError: ...
# → study_helper.service.download_pipeline: transcribe 단계 실패: RuntimeError: CUDA out of memory
# → Traceback (most recent call last): ...
```

**원인별 수정**:

| stage | 흔한 exc | 원인 | 수정 |
|-------|----------|------|------|
| `convert` | `FileNotFoundError: ffmpeg` | ffmpeg 미설치 | Dockerfile apt install 또는 호스트 PATH |
| `convert` | `RuntimeError: mp3 변환 실패: ...stderr tail...` | ffmpeg 실행 실패 (코덱/입력 손상) | stderr tail 확인해 ffmpeg 옵션 수정 |
| `transcribe` | `RuntimeError: CUDA out of memory` | Whisper 모델 메모리 부족 | `WHISPER_MODEL` 작게 (large → base) 또는 `mem_limit` 상향 |
| `transcribe` | `ValueError: Audio is too short` | 무음/저음량 | [src/stt/transcriber.py](../src/stt/transcriber.py) 경계 처리 |
| `summarize` | `google.genai.errors.APIError` | API 키/쿼터 | `.env` `GOOGLE_API_KEY` 확인, `AI_AGENT=openai` fallback |
| `summarize` | `TRANSCRIPT_EMPTY` (stage_errors 만) | STT 결과 비어있음 (정상) | 수정 불필요 — 요약 자동 생략 |
| `notify` | `ConnectionError` | Telegram 네트워크 | 일시적. 재시도 후에도 실패면 봇 토큰/Chat ID 확인 |
| `notify` | `NOTIFY_FAILED` | Telegram API 5xx/429 | `_request_with_retry` 로그 확인 |

### 8. 파이프라인 진입/종료 로그로 전체 흐름 파악

```bash
grep "파이프라인" logs/study_helper.log
# → study_helper.service.download_pipeline: 파이프라인 시작 — mp4=... audio_only=False both=True stt=True ai=True tg=True
# → study_helper.service.download_pipeline: 파이프라인 종료 — success=True error='' stages_failed=[] mp3=True txt=True summary=True
```

- `stages_failed=[]` 이면 전부 성공.
- `stages_failed=['transcribe', 'summarize']` 면 STT/요약 실패했지만 convert 는 성공.
- `success=False error='CONVERT_FAILED'` 면 convert 단계에서 return 한 케이스.

## 한 화면에 원인 요약 뽑기

```bash
# 최근 10건의 다운로드 실패를 reason + exc + 주요 메시지로 요약
grep -E "(다운로드 실패 reason=|URL 추출 실패|HTTP 실패|단계 실패)" logs/study_helper.log | tail -20

# 특정 reason 의 traceback 만 추출 (에러 전용 파일에서)
cat logs/*_download.log | grep -A 30 "다운로드 실패:" | tail -60
```

## 아직 원인이 안 잡히면

1. `logs/study_helper.log` 의 해당 세션 전후 1~2분 컨텍스트를 통째로 캡처하고, 강의 URL 만 마스킹한 뒤 이슈 등록.
2. `LOG_LEVEL=DEBUG` 지원은 현재 없음 — 필요 시 [src/logger.py](../src/logger.py) 의 `_app_logger.setLevel(DEBUG)` 을 env 로 제어하는 개선 과제로 등록.

## PII/토큰 마스킹 확인

본 프로젝트는 `SensitiveFilter` 가 handler 단에 부착되어 있어 `oauth_signature`, `user_email`, `api_key`, `password` 등의 값이 로그 파일에 `***REDACTED***` 로 치환된다. 로그 공유 시 추가 마스킹 대개 불필요하지만, 새 민감 키가 등장하면 [src/util/log_sanitize.py](../src/util/log_sanitize.py) `_SENSITIVE_KEYS` 에 추가하고 [tests/test_logger_filter.py](../tests/test_logger_filter.py) 에 테스트 케이스 추가.
