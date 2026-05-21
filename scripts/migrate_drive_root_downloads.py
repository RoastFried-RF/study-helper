"""Windows 드라이브 루트(D:/data/downloads) 에 저장된 기존 파일을
프로젝트 내부 data/downloads 로 이동하는 일회성 마이그레이션 스크립트.

Config.get_download_dir() 의 Windows 재매핑 수정 이후, 이전에 드라이브 루트에
저장돼 있던 파일을 새 기준 경로로 옮겨 연속성 확보.

사용법:
    python -m scripts.migrate_drive_root_downloads              # dry-run
    python -m scripts.migrate_drive_root_downloads --apply      # 실제 이동
    python -m scripts.migrate_drive_root_downloads --source "D:/data/downloads" --apply

동작 원칙:
  - 기본적으로 mv (shutil.move) 로 이동 — 디스크 여유 공간 부족 방지
  - 동일 파일이 target 에 있고 크기도 같으면 source 쪽 삭제 (중복 제거)
  - 동일 경로지만 크기 다르면 경고 후 skip (사용자 수동 확인)
  - 빈 source 디렉토리는 이동 후 자동 정리
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.config import Config


def _default_source() -> Path:
    """Windows 드라이브 루트 D:/data/downloads 를 기본 source 로 가정."""
    return Path("D:/data/downloads")


def _default_target() -> Path:
    """Config.get_download_dir() 현행 반환값."""
    return Path(Config.get_download_dir())


def _iter_files(root: Path):
    for p in root.rglob("*"):
        if p.is_file():
            yield p


def _relative_to_source(file_path: Path, source: Path) -> Path:
    return file_path.relative_to(source)


def migrate(source: Path, target: Path, apply: bool) -> tuple[int, int, int]:
    """(이동됨, 건너뜀, 충돌) 개수 반환."""
    if not source.is_dir():
        # CLI-F1: 정상 종료(exit 0) 경로이므로 [오류] 가 아닌 [안내] 로 표기
        print(f"  [안내] source 디렉토리가 없습니다 — 이관할 파일 없음: {source}")
        return 0, 0, 0
    if source.resolve() == target.resolve():
        print("  [안내] source 와 target 이 동일합니다 — 마이그레이션 불필요.")
        return 0, 0, 0

    moved = skipped = conflict = 0
    for src_file in sorted(_iter_files(source)):
        rel = _relative_to_source(src_file, source)
        dst_file = target / rel

        if dst_file.exists():
            try:
                src_size = src_file.stat().st_size
                dst_size = dst_file.stat().st_size
            except OSError as e:
                print(f"  [오류] stat 실패 {rel}: {e}")
                conflict += 1
                continue

            if src_size == dst_size:
                # 동일 파일 — source 중복 제거
                if apply:
                    try:
                        src_file.unlink()
                    except OSError as e:
                        print(f"  [오류] 중복 제거 실패 {rel}: {e}")
                        conflict += 1
                        continue
                print(f"  [중복] {rel} ({src_size:,} bytes) — source 측 {'제거' if apply else '제거 예정'}")
                skipped += 1
            else:
                print(
                    f"  [충돌] {rel} — src={src_size:,}B vs dst={dst_size:,}B — 수동 확인 필요"
                )
                conflict += 1
            continue

        # 이동
        if apply:
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(src_file), str(dst_file))
            except OSError as e:
                print(f"  [오류] 이동 실패 {rel}: {e}")
                conflict += 1
                continue
        print(f"  [이동] {rel}")
        moved += 1

    # 빈 디렉토리 정리
    if apply:
        for d in sorted(source.rglob("*"), reverse=True):
            if d.is_dir():
                try:
                    d.rmdir()
                except OSError:
                    pass
        try:
            source.rmdir()
        except OSError:
            pass

    return moved, skipped, conflict


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Windows 드라이브 루트 → 프로젝트 내부 다운로드 파일 마이그레이션",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--source", type=Path, default=_default_source(),
        help="원본 디렉토리 (default: D:/data/downloads)",
    )
    parser.add_argument(
        "--target", type=Path, default=None,
        help="대상 디렉토리 (default: Config.get_download_dir() 현행값)",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="실제로 이동. 미지정 시 dry-run.",
    )
    args = parser.parse_args()

    source = args.source.resolve()
    target = (args.target or _default_target()).resolve()

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] 마이그레이션")
    print(f"  source: {source}")
    print(f"  target: {target}")
    print()

    moved, skipped, conflict = migrate(source, target, apply=args.apply)

    print()
    print("=" * 60)
    print(f"  이동: {moved}, 중복 제거: {skipped}, 충돌: {conflict}")
    if not args.apply:
        print("  --apply 로 실제 실행")
    return 0 if conflict == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
