"""T02 冒烟测试：最小骨架可导入、自检返回0且不落盘、测试可被发现。"""

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "main.py"
PY = sys.executable


def _load_main_module():
    spec = importlib.util.spec_from_file_location("relay_b_main", MAIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_main_imports_safely():
    module = _load_main_module()
    assert callable(module.main)
    assert module.APP_NAME.startswith("AI Relay B V3.0")


def test_self_check_returns_zero_and_creates_nothing():
    before = {p.name for p in ROOT.iterdir()}
    proc = subprocess.run(
        [PY, str(MAIN), "--self-check"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        cwd=ROOT,
    )
    assert proc.returncode == 0, proc.stderr
    assert "AI_RELAY_B_V3_SELF_CHECK_OK" in proc.stdout
    after = {p.name for p in ROOT.iterdir()}
    unexpected = after - before
    assert not unexpected, f"自检产生了预期外文件/目录: {unexpected}"
    assert not [p for p in ROOT.iterdir() if p.suffix in (".db", ".sqlite", ".sqlite3")]


def test_self_check_does_not_touch_executors_or_clipboard():
    source = MAIN.read_text(encoding="utf-8")
    self_check_fn = source.split("def run_self_check", 1)[1].split("def ", 1)[0]
    assert "PySide6" not in self_check_fn
    assert "requests" not in self_check_fn
    assert "openchamber" not in self_check_fn.lower()
    assert "reasonix" not in self_check_fn.lower()
    assert "clipboard" not in self_check_fn.lower()
    assert "sqlite" not in self_check_fn.lower()