"""
AI 요약기.

STT로 생성된 .txt 파일을 Gemini 또는 OpenAI API로 요약한다.
결과는 동일 경로에 _summarized.txt로 저장된다.
"""

from pathlib import Path

_SUMMARY_PROMPT = """\
당신은 대학교 강의 내용을 정리하는 전문 학습 보조 AI입니다.
아래는 강의를 음성 인식(STT)으로 변환한 텍스트입니다. STT 특성상 오탈자나 문장이 부자연스러운 부분이 있을 수 있으니 문맥을 고려해 이해해 주세요.

다음 형식에 맞춰 한국어로 요약해 주세요.

---

## 📌 강의 핵심 주제
(이번 강의에서 다루는 핵심 주제를 1~2문장으로 서술)

## 🗂️ 주요 내용 정리
(강의 흐름에 따라 핵심 내용을 항목별로 정리. 소주제가 있으면 소제목으로 구분)

## 📖 핵심 용어 / 개념 정의
(강의에서 중요하게 다룬 용어나 개념을 정의와 함께 정리. 없으면 생략)

## ✅ 학습 포인트 요약
(시험이나 과제에서 중요할 것 같은 내용을 3~5개 항목으로 요약)

---

강의 텍스트:
{text}
"""

_GEMINI_MODELS = [
    ("gemini-2.5-flash", "Gemini 2.5 Flash  (무료 티어 지원, 권장)"),
    ("gemini-2.0-flash", "Gemini 2.0 Flash  (무료 티어 지원)"),
    ("gemini-1.5-flash", "Gemini 1.5 Flash  (무료 티어 지원)"),
    ("gemini-1.5-pro",   "Gemini 1.5 Pro    (유료)"),
]

# 외부에서 모델 목록 참조용
GEMINI_MODEL_IDS = [m[0] for m in _GEMINI_MODELS]
GEMINI_MODEL_LABELS = [m[1] for m in _GEMINI_MODELS]
GEMINI_DEFAULT_MODEL = GEMINI_MODEL_IDS[0]


def summarize(txt_path: Path, agent: str, api_key: str, model: str) -> Path:
    """
    텍스트 파일을 AI로 요약한다.

    Args:
        txt_path: STT 결과 .txt 파일 경로
        agent:    "gemini" 또는 "openai"
        api_key:  해당 에이전트 API 키
        model:    사용할 모델 ID

    Returns:
        생성된 _summarized.txt 파일 경로
    """
    text = txt_path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError("텍스트 파일이 비어 있습니다.")

    prompt = _SUMMARY_PROMPT.format(text=text)

    if agent == "gemini":
        summary = _summarize_gemini(api_key, model, prompt)
    elif agent == "openai":
        summary = _summarize_openai(api_key, model, prompt)
    else:
        raise ValueError(f"지원하지 않는 AI 에이전트: {agent}")

    out_path = txt_path.with_stem(txt_path.stem + "_summarized")
    out_path.write_text(summary, encoding="utf-8")
    return out_path


def _summarize_gemini(api_key: str, model: str, prompt: str) -> str:
    try:
        import google.generativeai as genai
    except ImportError:
        raise RuntimeError(
            "google-generativeai 패키지가 설치되어 있지 않습니다.\n"
            "설치: pip install google-generativeai"
        )

    genai.configure(api_key=api_key)
    client = genai.GenerativeModel(model)
    response = client.generate_content(prompt)
    return response.text


def _summarize_openai(api_key: str, model: str, prompt: str) -> str:
    try:
        from openai import OpenAI
    except ImportError:
        raise RuntimeError(
            "openai 패키지가 설치되어 있지 않습니다.\n"
            "설치: pip install openai"
        )

    client = OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content
