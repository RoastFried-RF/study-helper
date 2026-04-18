"""
기존 logs/*.log 파일의 PII/secret 을 in-place 로 마스킹한다.

과거(Phase 2 이전) 로그에는 OAuth signature, CSRF 토큰, 학번, 이메일,
실명, tool_consumer_instance_guid 가 평문으로 기록되어 있다. 이 스크립트는
Phase 2 에서 추가한 `_mask_sensitive` 와 동일 규칙으로 기존 파일들을 치환한다.

사용법:
  # dry-run: 치환될 건수만 보고 실제 쓰지 않음
  .venv/Scripts/python.exe scripts/sanitize_logs.py

  # 실제 치환 (원본은 .orig 백업)
  .venv/Scripts/python.exe scripts/sanitize_logs.py --apply

  # 백업 없이 in-place
  .venv/Scripts/python.exe scripts/sanitize_logs.py --apply --no-backup

특정 디렉토리 지정:
  .venv/Scripts/python.exe scripts/sanitize_logs.py --logs-dir /path/to/logs --apply
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

# src.util.log_sanitize 공용 모듈 재사용 (마스킹 규칙 drift 방지)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from src.util.log_sanitize import count_sensitive, mask_sensitive


def mask(text: str) -> tuple[str, int]:
    """마스킹된 텍스트와 치환 건수를 반환."""
    count = count_sensitive(text)
    if count == 0:
        return text, 0
    return mask_sensitive(text), count


def process_file(path: Path, apply: bool, backup: bool) -> tuple[int, bool]:
    """(치환 건수, 실제 변경 여부) 반환."""
    try:
        original = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"  [ERROR] read 실패 {path.name}: {e}", file=sys.stderr)
        return 0, False

    sanitized, count = mask(original)
    if count == 0 or sanitized == original:
        return 0, False

    if not apply:
        return count, False

    if backup:
        backup_path = path.with_suffix(path.suffix + ".orig")
        try:
            shutil.copy2(path, backup_path)
        except OSError as e:
            print(f"  [ERROR] backup 실패 {path.name}: {e}", file=sys.stderr)
            return count, False

    try:
        path.write_text(sanitized, encoding="utf-8")
    except OSError as e:
        print(f"  [ERROR] write 실패 {path.name}: {e}", file=sys.stderr)
        return count, False
    return count, True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="기존 로그 파일의 PII/secret 마스킹",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--logs-dir", type=Path,
        default=Path(__file__).resolve().parent.parent / "logs",
        help="로그 디렉토리 경로 (default: 프로젝트/logs)",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="실제 파일을 수정. 미지정 시 dry-run.",
    )
    parser.add_argument(
        "--no-backup", action="store_true",
        help="--apply 시 .orig 백업을 만들지 않음.",
    )
    args = parser.parse_args()

    logs_dir: Path = args.logs_dir
    if not logs_dir.is_dir():
        print(f"[ERROR] 로그 디렉토리가 없습니다: {logs_dir}", file=sys.stderr)
        return 1

    # .log, .log.YYYY-MM-DD (rotation) 만 대상. .orig 백업은 재처리 방지 위해 제외.
    log_files = [
        p for p in sorted(logs_dir.glob("*.log"))
        if not p.name.endswith(".orig")
    ]
    log_files += [
        p for p in sorted(logs_dir.glob("*.log.*"))
        if not p.name.endswith(".orig")
    ]
    if not log_files:
        print(f"대상 파일 없음 ({logs_dir})")
        return 0

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] {len(log_files)} 파일 스캔 중: {logs_dir}")
    print()

    total_hits = 0
    total_changed = 0
    for log in log_files:
        hits, changed = process_file(log, apply=args.apply, backup=not args.no_backup)
        if hits:
            # Windows cp949 콘솔에서 Unicode 체크마크가 깨지지 않도록 ASCII만 사용.
            mark = "OK" if changed else "..."
            print(f"  [{mark}] {log.name}: {hits} hits")
            total_hits += hits
            if changed:
                total_changed += 1
        else:
            print(f"  [--] {log.name}: 0 hits")

    print()
    if args.apply:
        print(f"완료: {total_changed} 파일 수정, 총 {total_hits}건 마스킹")
        if not args.no_backup:
            print("      (원본은 .orig 확장자로 백업됨 — 검증 후 수동 삭제)")
    else:
        print(f"DRY-RUN: {total_hits}건이 마스킹 대상. --apply 로 실제 실행.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
