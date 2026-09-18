from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "pilot_post_deploy_check", ROOT / "scripts" / "pilot_post_deploy_check.py"
)
assert _spec and _spec.loader
pilot_check = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = pilot_check
_spec.loader.exec_module(pilot_check)


class _FakeClient:
    def __init__(self, responses: dict[str, httpx.Response]):
        self.responses = responses
        self.calls: list[str] = []

    def get(self, url: str) -> httpx.Response:
        self.calls.append(url)
        if url not in self.responses:
            raise KeyError(url)
        return self.responses[url]


def test_resolve_public_url_prefers_explicit():
    assert pilot_check.resolve_public_url("https://example.test") == "https://example.test"


def test_run_http_checks_all_pass():
    base = "https://pilot.example"
    frontend = "https://pilot.example"
    client = _FakeClient(
        {
            f"{base}/healthz": httpx.Response(200, json={"status": "ok"}),
            f"{base}/readyz": httpx.Response(200, json={"status": "ok", "checks": {}}),
            f"{frontend}/login": httpx.Response(200, text="login"),
            f"{frontend}/api/admin/org-integrations": httpx.Response(
                401, json={"error": "Unauthorized"}
            ),
        }
    )
    results = pilot_check.run_http_checks(base, client=client, frontend_url=frontend)
    assert len(results) == 4
    assert all(item.passed for item in results)
    assert results[0].name == "backend_liveness"
    assert results[3].name == "bff_org_integrations"


def test_run_http_checks_fails_on_readyz():
    base = "https://pilot.example"
    client = _FakeClient(
        {
            f"{base}/healthz": httpx.Response(200, json={"status": "ok"}),
            f"{base}/readyz": httpx.Response(503, json={"status": "error"}),
            f"{base}/login": httpx.Response(200, text="login"),
            f"{base}/api/admin/org-integrations": httpx.Response(401, json={}),
        }
    )
    results = pilot_check.run_http_checks(base, client=client, frontend_url=base)
    assert results[0].passed is True
    assert results[1].passed is False


def test_summarize():
    results = [
        pilot_check.CheckResult("a", "http://x", True, "ok"),
        pilot_check.CheckResult("b", "http://y", False, "bad"),
    ]
    summary = pilot_check.summarize(results)
    assert summary["passed"] == 1
    assert summary["total"] == 2
    assert summary["ok"] is False


def test_run_ui_smoke_without_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ADMIN_UI_TOKEN", raising=False)
    result = pilot_check.run_ui_smoke("https://pilot.example")
    assert result.passed is False
    assert "ADMIN_UI_TOKEN" in result.detail


def test_run_ui_smoke_invokes_subprocess(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_UI_TOKEN", "secret-token")
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(pilot_check.subprocess, "run", fake_run)
    result = pilot_check.run_ui_smoke("https://pilot.example")
    assert result.passed is True
    assert calls[0][-1].endswith("ui_smoke_check.py")
