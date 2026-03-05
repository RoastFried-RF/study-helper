# study-helper

숭실대학교 LMS(canvas.ssu.ac.kr) 강의 영상을 Docker 컨테이너 환경에서 관리하는 CUI 도구입니다.

---

## 주요 기능

- **백그라운드 재생** — 영상/소리 출력 없이 출석 처리 목적으로 강의를 자동 재생
- **영상 다운로드** — 강의 영상을 mp4로 저장
- **음성 추출** — 강의 영상에서 음성을 mp3로 추출
- **Speech to Text** — Whisper를 이용한 로컬 음성 텍스트 변환
- **AI 요약** — 변환된 텍스트를 Gemini 또는 OpenAI API로 요약

---

## 시작 전 필요한 것

| 항목 | 설명 |
|------|------|
| 숭실대 LMS 계정 | 학번 + 비밀번호 |
| Docker | 컨테이너 실행 환경 |
| Gemini API 키 | AI 요약 사용 시 필요 |

---

## 설치 및 실행

### 1. 저장소 클론

```bash
git clone https://github.com/your-repo/study-helper.git
cd study-helper
```

### 2. 환경 설정

```bash
cp .env.example .env
```

`.env` 파일을 열어 필요한 값을 입력합니다:

```env
LMS_USER_ID=학번
LMS_PASSWORD=비밀번호
GOOGLE_API_KEY=AIzaSy...      # AI 요약 사용 시
WHISPER_MODEL=base            # tiny / base / small / medium / large
```

> `.env` 파일은 절대 GitHub에 업로드하지 마세요.

### 3. 빌드 및 실행

```bash
# 최초 빌드 (수 분 소요 — Chromium, Whisper 모델 다운로드 포함)
docker compose build

# 실행
docker compose run --rm study-helper
```

---

## How to Use

### 빌드

최초 실행 전 또는 코드 변경 후 한 번만 실행합니다.

```bash
docker compose build
```

> 최초 빌드 시 Chromium 설치로 인해 수 분이 소요될 수 있습니다.

### 실행

```bash
docker compose run --rm study-helper
```

실행하면 TUI 화면이 자동으로 표시됩니다. 종료 후 재실행 시에도 동일한 명령어를 사용합니다.

### 종료

| 방법 | 동작 |
|------|------|
| 메뉴에서 `0` 입력 | 정상 종료 |
| `Ctrl + C` | 강제 종료 |

---

## 사용 방법

실행 후 CUI 메뉴가 표시됩니다.

```
수강 중인 과목 목록
─────────────────────────────────────
  1. 소프트웨어공학       미시청 3 / 전체 12
  2. 데이터베이스         미시청 0 / 전체 10
  3. 운영체제             미시청 5 / 전체 15
─────────────────────────────────────
과목을 선택하세요 (0: 종료):
```

과목 선택 후 주차별 강의 목록이 표시되며, 강의를 선택하면 다음 메뉴가 제공됩니다:

```
[1] 백그라운드 재생
[2] 다운로드
[0] 돌아가기
```

다운로드 선택 시:

```
[1] 영상 (mp4)
[2] 음성 (mp3)
[0] 돌아가기
```

음성 다운로드 완료 후 STT 변환 및 AI 요약을 선택할 수 있습니다.

---

## 다운로드 경로

다운로드된 파일은 프로젝트 디렉토리의 `data/downloads/` 경로에 저장됩니다.

```
data/
└── downloads/
    └── 강의명/
        ├── 강의명.mp4
        ├── 강의명.mp3
        ├── 강의명.txt
        └── 강의명_summarized.txt
```

---

## Whisper 모델 크기 선택

| 모델 | 크기 | 속도 | 정확도 |
|------|------|------|--------|
| tiny | ~39MB | 매우 빠름 | 낮음 |
| base | ~74MB | 빠름 | 보통 (기본값) |
| small | ~244MB | 보통 | 좋음 |
| medium | ~769MB | 느림 | 높음 |
| large | ~1.5GB | 매우 느림 | 최고 |

`.env`의 `WHISPER_MODEL` 값으로 변경 가능합니다.

---

## 기술 스택

| 영역 | 기술 |
|------|------|
| 언어 | Python 3.11 |
| 브라우저 자동화 | Playwright (headless Chromium) |
| CUI | rich + click |
| 음성 변환 | ffmpeg |
| STT | OpenAI Whisper (로컬) |
| AI 요약 | Google Gemini API / OpenAI API |
| 컨테이너 | Docker |

---

## 주의사항

- 본 도구는 개인 학습 목적으로만 사용하세요.
- LMS 서비스 약관을 준수하여 사용하시기 바랍니다.
- `.env` 파일에 포함된 계정 정보와 API 키를 외부에 노출하지 마세요.
