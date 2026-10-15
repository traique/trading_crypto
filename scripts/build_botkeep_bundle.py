"""Build the Botkeep upload: only what runs, within 250 files and 4 MB.

    python scripts/build_botkeep_bundle.py

Writes ``dist/botkeep/`` (the folder to deploy) and
``dist/trading-crypto-botkeep.zip`` (the same, zipped for upload), then fails
if either limit is crossed. Tests, research, docs and data are left out;
dependencies are installed by Botkeep from ``requirements.txt``.
"""

from __future__ import annotations

import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
OUT = DIST / "botkeep"
ZIP = DIST / "trading-crypto-botkeep.zip"
MAX_FILES = 250
MAX_BYTES = 4 * 1024 * 1024

PACKAGES = ("cryptotrader", "bot")
EXTRA = {
    "main_botkeep.py": "main_botkeep.py",
    "deploy/botkeep/requirements.txt": "requirements.txt",
    "deploy/botkeep/.env.example": ".env.example",
    "deploy/botkeep/README.md": "README.md",
    "LICENSE": "LICENSE",
    "NOTICE": "NOTICE",
}
SKIP_DIRS = {"__pycache__"}
KEEP_SUFFIXES = {".py", ".svg", ".json", ".md", ".txt"}


def collect(root: Path = ROOT) -> list[tuple[Path, str]]:
    """(source, path in the bundle) of every file to ship."""
    files = []
    for package in PACKAGES:
        for path in sorted((root / package).rglob("*")):
            if path.is_file() and path.suffix in KEEP_SUFFIXES and not SKIP_DIRS & set(path.parts):
                files.append((path, path.relative_to(root).as_posix()))
    for source, target in EXTRA.items():
        if (root / source).exists():
            files.append((root / source, target))
        else:
            print(f"missing: {source}", file=sys.stderr)
    return files


def check(count: int, raw: int, zipped: int) -> list[str]:
    problems = []
    if count > MAX_FILES:
        problems.append(f"{count} files > {MAX_FILES}")
    if max(raw, zipped) > MAX_BYTES:
        problems.append(f"{max(raw, zipped)} bytes > {MAX_BYTES}")
    return problems


def build(root: Path = ROOT, out: Path = OUT, zip_path: Path = ZIP) -> tuple[int, int, int]:
    files = collect(root)
    if out.exists():
        shutil.rmtree(out)
    for source, target in files:
        dest = out / target
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for _source, target in files:
            archive.write(out / target, target)
    raw = sum((out / t).stat().st_size for _, t in files)
    return len(files), raw, zip_path.stat().st_size


def main() -> int:
    count, raw, zipped = build()
    print(f"files: {count} (limit {MAX_FILES})")
    print(f"size: {raw / 1024:.0f} KB unpacked, {zipped / 1024:.0f} KB zipped (limit {MAX_BYTES // 1024} KB)")
    print(f"folder: {OUT}\nzip:    {ZIP}")
    problems = check(count, raw, zipped)
    if problems:
        print("OVER THE BOTKEEP LIMIT: " + "; ".join(problems), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
