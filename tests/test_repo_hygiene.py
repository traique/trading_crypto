"""Repository hygiene: the source tree must never be hidden by .gitignore."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("git") is None or not (ROOT / ".git").exists(), reason="needs a git checkout")
def test_source_packages_are_not_git_ignored():
    sources = [str(p.relative_to(ROOT)) for p in (ROOT / "cryptotrader").rglob("*.py")]
    sources += [str(p.relative_to(ROOT)) for p in (ROOT / "bot").rglob("*.py")]
    out = subprocess.run(["git", "check-ignore", *sources], cwd=ROOT, capture_output=True, text=True)
    assert out.stdout.strip() == "", f"ignored source files: {out.stdout}"
    # Runtime data at the repo root stays ignored.
    out = subprocess.run(["git", "check-ignore", "data/ohlcv/BTCUSDT-1d.csv", "research/data/x.csv"],
                         cwd=ROOT, capture_output=True, text=True)
    assert len(out.stdout.split()) == 2
