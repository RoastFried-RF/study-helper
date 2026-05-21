"""Docker Hub에서 최신 이미지 태그를 조회해 현재 버전과 비교한다."""

import re

_DOCKERHUB_TAGS_URL = (
    "https://hub.docker.com/v2/repositories/igor0670/study-helper/tags?page_size=25&ordering=last_updated"
)
_VERSION_RE = re.compile(r"^v?(\d+\.\d+\.\d+)$")


def _parse_version(tag: str) -> tuple[int, ...] | None:
    m = _VERSION_RE.match(tag)
    if m:
        return tuple(int(x) for x in m.group(1).split("."))
    return None


def fetch_latest_version(timeout: float = 5.0) -> str | None:
    """Docker Hub에서 최신 버전 태그를 반환한다. 실패 시 None."""
    try:
        import json
        import urllib.request

        req = urllib.request.Request(
            _DOCKERHUB_TAGS_URL,
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())

        best: tuple[int, ...] | None = None
        best_tag: str | None = None
        for result in data.get("results", []):
            tag = result.get("name", "")
            parsed = _parse_version(tag)
            if parsed and (best is None or parsed > best):
                best = parsed
                best_tag = tag
        # 'v' 접두사 통일
        if best_tag and not best_tag.startswith("v"):
            best_tag = f"v{best_tag}"
        return best_tag
    except Exception:
        return None


def check_update(current_version: str) -> str | None:
    """
    최신 버전이 현재 버전보다 높으면 최신 버전 문자열을 반환한다.
    같거나 낮으면 None. 조회 실패 시에도 None.

    NF-01: `current_version` 이 `_VERSION_RE`(3-숫자 세그먼트)로 파싱되지
    않는 경우(예: CHANGELOG.md 누락으로 `"unknown"`, 사전릴리스/4자리 태그)
    버전 비교가 불가능하다. 이때 업데이트 알림을 silent 하게 누락하지 않고
    "버전 판정 불가" 를 로그로 명시한 뒤 latest 를 그대로 반환해 사용자가
    최신 버전을 인지할 수 있게 한다.
    """
    latest = fetch_latest_version()
    if not latest:
        return None
    latest_parsed = _parse_version(latest)
    if latest_parsed is None:
        return None
    current_parsed = _parse_version(current_version)
    if current_parsed is None:
        # 현재 버전 판정 불가 — 비교 생략하고 최신 버전을 안내한다.
        from src.logger import get_logger

        get_logger("updater").warning(
            "현재 버전 판정 불가(%r) — 버전 비교 생략, 최신 버전 %s 안내",
            current_version, latest,
        )
        return latest
    if latest_parsed > current_parsed:
        return latest
    return None
