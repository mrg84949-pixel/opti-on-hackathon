from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "wave5_go_no_go_smoke.py"
WAVE3_SCRIPT = ROOT / "scripts" / "wave3_regression_smoke.py"

_FIXTURE = """
| ID | Title | Sev | Status | Owner | Test/Commit |
|----|-------|-----|--------|-------|-------------|
| KI-01 | Confirm raw tool text | P1 | **fixed** | T | x |
| KI-03 | Rate limits | P3 | open | T | x |
| STAB-X | Open critical | P0 | open | T | x |
| STAB-Y | Open high | P1 | in_progress | T | x |
"""


def _load_module():
    spec = importlib.util.spec_from_file_location("wave5_go_no_go_smoke", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["wave5_go_no_go_smoke"] = module
    spec.loader.exec_module(module)
    return module


def test_gate_names_constant():
    mod = _load_module()
    assert tuple(mod.GATE_NAMES) == ("code", "data", "security", "ops", "product")


def test_parse_defect_registry_open_p0_p1():
    mod = _load_module()
    result = mod.parse_defect_registry(_FIXTURE)
    assert result["ok"] is False
    assert "STAB-X" in result["open_p0"]
    assert "STAB-Y" in result["open_p1"]


def test_parse_defect_registry_no_open_critical():
    mod = _load_module()
    clean = """
| ID | Title | Sev | Status | Owner | Test/Commit |
|----|-------|-----|--------|-------|-------------|
| KI-01 | fixed | P1 | **fixed** | T | x |
| KI-03 | backlog | P3 | open | T | x |
"""
    result = mod.parse_defect_registry(clean)
    assert result["ok"] is True
    assert result["open_p0"] == []
    assert result["open_p1"] == []


def _env_without_pythonpath() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k.upper() != "PYTHONPATH"}


def test_wave5_cli_imports_without_pythonpath():
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=ROOT,
        env=_env_without_pythonpath(),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr


def test_wave3_cli_imports_without_pythonpath():
    proc = subprocess.run(
        [sys.executable, str(WAVE3_SCRIPT), "--help"],
        cwd=ROOT,
        env=_env_without_pythonpath(),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
