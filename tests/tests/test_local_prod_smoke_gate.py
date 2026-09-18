from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "local_prod_smoke.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("local_prod_smoke", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["local_prod_smoke"] = module
    spec.loader.exec_module(module)
    return module


def test_normalize_public_url_localhost():
    mod = _load_module()
    assert mod.normalize_public_url("http://localhost/") == "http://localhost"


def test_compose_cmd_includes_local_override():
    mod = _load_module()
    cmd = mod._compose_cmd("ps")
    assert "docker-compose.local-prod.yml" in cmd
    assert "docker-compose.prod.yml" in cmd


def test_incident_drill_doc():
    mod = _load_module()
    ok, _ = mod._check_incident_drill_doc()
    assert ok is True


def test_backup_drill_not_labeled_skip_on_prior_failure(monkeypatch: pytest.MonkeyPatch):
    mod = _load_module()
    monkeypatch.setattr(mod, "_append_run_log", lambda _record: None)

    def fake_curl(url: str, *, verbose: bool) -> tuple[bool, str]:
        if url.endswith("/healthz") or url.endswith("/readyz"):
            return True, "200"
        return False, "unexpected curl"

    def fake_run(cmd: list[str], *, verbose: bool = False, timeout: int | None = None):
        joined = " ".join(str(part) for part in cmd)
        if "pilot_post_deploy_check.py" in joined:
            return 1, "post deploy failed"
        return 0, ""

    monkeypatch.setattr(mod, "_curl_ok", fake_curl)
    monkeypatch.setattr(mod, "_run_cmd", fake_run)

    code, record = mod.execute_local_prod_smoke(
        run_label="gate-test",
        manual_stack=True,
        skip_backup=False,
    )
    assert code == 1
    backup_steps = [s for s in record.steps if s["name"] == "backup_drill"]
    assert backup_steps == []


def test_backup_drill_labeled_skip_when_flag_set(monkeypatch: pytest.MonkeyPatch):
    mod = _load_module()
    monkeypatch.setattr(mod, "_append_run_log", lambda _record: None)
    monkeypatch.setattr(mod, "_curl_ok", lambda url, *, verbose: (True, "200"))
    monkeypatch.setattr(mod, "_run_cmd", lambda cmd, *, verbose=False, timeout=None: (0, ""))

    code, record = mod.execute_local_prod_smoke(
        run_label="gate-test",
        manual_stack=True,
        skip_backup=True,
    )
    assert code == 0
    backup_steps = [s for s in record.steps if s["name"] == "backup_drill"]
    assert len(backup_steps) == 1
    assert backup_steps[0]["status"] == "SKIP"
    assert backup_steps[0]["detail"] == "--skip-backup"
