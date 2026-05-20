"""URL 공용 유틸리티."""

from urllib.parse import urlparse


def safe_url(url: str) -> str:
    """URL에서 민감 정보를 제거하여 로그 노출을 방지한다.

    제거 대상:
      - query 파라미터 / fragment (세션 토큰)
      - netloc 의 userinfo(`user:pass@host` 의 `user:pass`) (NF-05)

    한계: 경로(path) 세그먼트에 토큰이 박힌 형태
    (예: `/download/<token>/video.mp4`) 는 일반화된 판별이 불가능해
    제거하지 못한다. 경로 토큰이 우려되는 URL 은 호출 측에서 별도 마스킹할 것.
    """
    parsed = urlparse(url)
    # netloc 에서 userinfo 를 떼고 host[:port] 만 남긴다.
    netloc = parsed.hostname or ""
    if netloc and parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return parsed._replace(query="", fragment="", netloc=netloc).geturl()
