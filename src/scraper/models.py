"""Canvas LMS 스크래핑 결과의 데이터 모델 (dataclass/Enum)."""

from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import urlparse

_BASE_URL = "https://canvas.ssu.ac.kr"


class LectureType(Enum):
    """LMS 강의 항목 타입 — 영상/과제/퀴즈 등 처리 방식 분기에 사용."""

    MOVIE = "movie"
    READYSTREAM = "readystream"
    SCREENLECTURE = "screenlecture"
    EVERLEC = "everlec"
    ZOOM = "zoom"
    MP4 = "mp4"
    ASSIGNMENT = "assignment"
    WIKI_PAGE = "wiki_page"
    QUIZ = "quiz"
    DISCUSSION = "discussion"
    FILE = "file"
    OTHER = "other"


VIDEO_LECTURE_TYPES = {
    LectureType.MOVIE,
    LectureType.READYSTREAM,
    LectureType.SCREENLECTURE,
    LectureType.EVERLEC,
    LectureType.MP4,
}


@dataclass
class Course:
    id: str
    long_name: str
    href: str
    term: str
    is_favorited: bool = False

    @property
    def full_url(self) -> str:
        return f"{_BASE_URL}{self.href}"

    @property
    def lectures_url(self) -> str:
        return f"{_BASE_URL}/courses/{self.id}/external_tools/71"


@dataclass
class LectureItem:
    title: str
    item_url: str
    lecture_type: LectureType
    week_label: str = ""
    lesson_label: str = ""
    duration: str | None = None
    attendance: str = "none"
    completion: str = "incomplete"
    content_type_label: str = ""
    is_upcoming: bool = False
    start_date: str | None = None
    end_date: str | None = None

    @property
    def is_video(self) -> bool:
        return self.lecture_type in VIDEO_LECTURE_TYPES

    @property
    def full_url(self) -> str:
        if self.item_url.startswith("http"):
            return self.item_url
        return f"{_BASE_URL}{self.item_url}"

    @property
    def needs_watch(self) -> bool:
        return self.is_video and self.completion != "completed" and not self.is_upcoming

    @property
    def is_downloadable(self) -> bool:
        """구조적으로 다운로드 가능한지 여부.

        learningx 플레이어는 mp4/HLS URL 노출이 없어 다운로드 불가.
        이 판정은 URL 패턴만으로 수행되며, 실제 추출 실패는 별도 경로에서 처리.

        NF-06: `"learningx" not in full_url` 의 단순 substring 매칭은 쿼리
        파라미터 등 URL 의 무관한 위치에 우연히 "learningx" 가 등장해도 과탐했다.
        hostname 라벨 / 경로 세그먼트 단위로 좁혀 오탐을 줄인다.
        """
        if not self.is_video:
            return False
        parsed = urlparse(self.full_url)
        hostname = (parsed.hostname or "").lower()
        # hostname 라벨 단위 매칭 (예: learningx.example.com)
        if "learningx" in hostname.split("."):
            return False
        # 경로 세그먼트 단위 매칭 (예: /learningx/lti/...)
        path_segments = [seg.lower() for seg in parsed.path.split("/") if seg]
        if "learningx" in path_segments:
            return False
        return True

    # 경로 계산(expected_paths/file_present)은 scraper → downloader 역방향 의존을 피하기
    # 위해 src/downloader/paths.py의 순수 함수로 이동했다. 호출부는 `from
    # src.downloader.paths import expected_paths, file_present` 사용.


@dataclass
class Week:
    title: str
    week_number: int
    lectures: list[LectureItem] = field(default_factory=list)

    @property
    def video_lectures(self) -> list[LectureItem]:
        return [lec for lec in self.lectures if lec.is_video]

    @property
    def pending_count(self) -> int:
        return sum(1 for lec in self.lectures if lec.needs_watch)


@dataclass
class CourseDetail:
    course: Course
    course_name: str
    professors: str
    weeks: list[Week] = field(default_factory=list)

    @property
    def all_video_lectures(self) -> list[LectureItem]:
        result = []
        for week in self.weeks:
            result.extend(week.video_lectures)
        return result

    @property
    def total_video_count(self) -> int:
        return len(self.all_video_lectures)

    @property
    def pending_video_count(self) -> int:
        return sum(1 for lec in self.all_video_lectures if lec.needs_watch)
