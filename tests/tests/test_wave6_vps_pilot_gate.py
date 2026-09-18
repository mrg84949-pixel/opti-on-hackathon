from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "wave6_vps_pilot_smoke.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("wave6_vps_pilot_smoke", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["wave6_vps_pilot_smoke"] = module
    spec.loader.exec_module(module)
    return module


def test_validate_public_url_https():
    mod = _load_module()
    ok, detail = mod.validate_public_url("https://pilot.example.com")
    assert ok is True
    assert detail == ""


def test_validate_public_url_rejects_missing_scheme():
    mod = _load_module()
    ok, detail = mod.validate_public_url("pilot.example.com")
    assert ok is False
    assert "http" in detail.lower()


def test_normalize_public_url_strips_trailing_slash():
    mod = _load_module()
    assert mod.normalize_public_url("https://pilot.example.com/") == "https://pilot.example.com"


def test_backup_and_incident_doc_checks():
    mod = _load_module()
    ok_backup, _ = mod._check_backup_cron_doc()
    ok_incident, _ = mod._check_incident_drill_doc()
    assert ok_backup is True
    assert ok_incident is True
