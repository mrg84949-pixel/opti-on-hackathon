from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]

_post_spec = importlib.util.spec_from_file_location(
    "pilot_post_deploy_check", ROOT / "scripts" / "pilot_post_deploy_check.py"
)
assert _post_spec and _post_spec.loader
post_deploy = importlib.util.module_from_spec(_post_spec)
sys.modules[_post_spec.name] = post_deploy
_post_spec.loader.exec_module(post_deploy)

_launch_spec = importlib.util.spec_from_file_location(
    "pilot_launch_verify", ROOT / "scripts" / "pilot_launch_verify.py"
)
assert _launch_spec and _launch_spec.loader
launch_verify = importlib.util.module_from_spec(_launch_spec)
sys.modules[_launch_spec.name] = launch_verify
_launch_spec.loader.exec_module(launch_verify)


def _ok_check(name: str) -> post_deploy.CheckResult:
    return post_deploy.CheckResult(name=name, url="http://x", passed=True, detail="ok")


def test_manual_signoff_items_present():
    assert len(launch_verify.MANUAL_SIGNOFF_ITEMS) >= 6
    ids = {item["id"] for item in launch_verify.MANUAL_SIGNOFF_ITEMS}
    assert "tg_inbound_ai" in ids
    assert "wa_inbound_ai" in ids


def test_run_launch_verify_all_automated_pass(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        launch_verify.post_deploy,
        "run_http_checks",
        lambda _url, **kwargs: [
            _ok_check("backend_liveness"),
            _ok_check("backend_readiness"),
            _ok_check("frontend_login"),
            _ok_check("bff_org_integrations"),
        ],
    )

    payload = launch_verify.run_launch_verify("https://pilot.example", ui_smoke=False)

    assert payload["ok"] is True
    assert payload["signed_off_ready"] is True
    assert payload["automated"]["passed"] == 4
    assert len(payload["manual_pending"]) == len(launch_verify.MANUAL_SIGNOFF_ITEMS)


def test_run_launch_verify_fails_when_http_check_fails(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        launch_verify.post_deploy,
        "run_http_checks",
        lambda _url, **kwargs: [
            _ok_check("backend_liveness"),
            post_deploy.CheckResult(
                name="backend_readiness",
                url="http://x/readyz",
                passed=False,
                detail="status=503",
            ),
        ],
    )

    payload = launch_verify.run_launch_verify("https://pilot.example", ui_smoke=False)

    assert payload["ok"] is False
    assert payload["signed_off_ready"] is False


def test_run_launch_verify_includes_ui_smoke(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        launch_verify.post_deploy,
        "run_http_checks",
        lambda _url, **kwargs: [_ok_check("backend_liveness")],
    )
    monkeypatch.setattr(
        launch_verify.post_deploy,
        "run_ui_smoke",
        lambda _url: _ok_check("ui_smoke"),
    )

    payload = launch_verify.run_launch_verify("https://pilot.example", ui_smoke=True)

    assert payload["automated"]["total"] == 2
    assert any(c["name"] == "ui_smoke" for c in payload["automated"]["checks"])


def test_write_report(tmp_path: Path):
    payload = {"ok": True, "manual_pending": []}
    out = tmp_path / "signoff.json"
    launch_verify.write_report(payload, out)
    assert out.exists()
    assert '"ok": true' in out.read_text(encoding="utf-8").lower()


def test_main_exit_code(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        launch_verify,
        "run_launch_verify",
        lambda *_a, **_k: {"ok": False, "automated": {"checks": []}, "manual_pending": []},
    )
    monkeypatch.setattr(launch_verify, "main", launch_verify.main)
    monkeypatch.setattr(
        sys,
        "argv",
        ["pilot_launch_verify.py", "--json-only", "--public-url", "http://localhost"],
    )
    assert launch_verify.main() == 1
