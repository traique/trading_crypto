"""The Botkeep upload and the entry point: what ships, within the host's limits, crypto only."""

import importlib.util
import re
import zipfile
from pathlib import Path

import pytest

from tests import test_bot_support as support

settings = support.settings

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]


def _bundle_module():
    spec = importlib.util.spec_from_file_location("build_botkeep_bundle", ROOT / "scripts" / "build_botkeep_bundle.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_bundle_ships_only_what_runs_within_the_limits(tmp_path):
    bundle = _bundle_module()
    out, zip_path = tmp_path / "botkeep", tmp_path / "bundle.zip"
    count, raw, zipped = bundle.build(out=out, zip_path=zip_path)
    assert bundle.check(count, raw, zipped) == []
    names = set(zipfile.ZipFile(zip_path).namelist())
    for needed in ("main_botkeep.py", "requirements.txt", ".env.example", "README.md", "LICENSE", "NOTICE",
                   "bot/trader.py", "bot/web.py", "cryptotrader/trading/ledger.py", "cryptotrader/strategies/base.py"):
        assert needed in names, needed
    assert not [n for n in names if n.startswith(("tests/", "research/", "data/")) or "__pycache__" in n]
    assert bundle.check(bundle.MAX_FILES + 1, 1, 1) and bundle.check(1, bundle.MAX_BYTES + 1, 1)


def test_the_entry_point_imports_and_wires_the_app(settings, monkeypatch):
    spec = importlib.util.spec_from_file_location("main_botkeep_under_test", ROOT / "main_botkeep.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.SETTINGS.data_dir == settings.data_dir and callable(module.main)


_VN_ONLY = re.compile(r"\b(HOSE|HNX|UPCoM|VNINDEX|VN30|VND|ATO|ATC|DNSE|CafeF|VCI|T\+2|Asia/Saigon)\b|cổ phiếu|phiên ATC")


def test_nothing_vn_specific_is_left_in_the_bot():
    files = list((ROOT / "bot").rglob("*.py")) + [ROOT / "main_botkeep.py", ROOT / ".env.example"]
    files += [p for p in (ROOT / "deploy").rglob("*") if p.is_file()] + list((ROOT / "scripts").rglob("*.py"))
    hits = []
    for path in files:
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _VN_ONLY.search(line):
                hits.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()}")
    assert hits == []
