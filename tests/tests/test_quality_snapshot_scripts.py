from __future__ import annotations

import json
import sys

import pytest

import scripts.preflight_check as preflight_check
import scripts.quality_snapshot as quality_snapshot


def test_clean_output_and_evaluate_hotspots():
    assert quality_snapshot._clean_output("ok\x00\n") == "ok"

    coverage_payload = {
        "totals": {"percent_covered": 81.5},
        "files": {
            "bot\\api\\whatsapp.py": {"summary": {"percent_covered": 80}},
            "bot\\channels\\whatsapp\\meta_cloud.py": {"summary": {"percent_covered": 76}},
            "bot/channels/whatsapp/outbound.py": {"summary": {"percent_covered": 74}},
            "bot/channels/whatsapp/green_api.py": {"summary": {"percent_covered": 90}},
            "bot/automation/reminders.py": {"summary": {"percent_covered": 88}},
            "bot/automation/scheduler.py": {"summary": {"percent_covered": 82}},
            "web/user_api.py": {"summary": {"percent_covered": 67}},
            "web/emailer.py": {"summary": {"percent_covered": 100}},
            "web/admin_api.py": {"summary": {"percent_covered": 61}},
            "bot/llm/llm_engine.py": {"summary": {"percent_covered": 70}},
            "bot/llm/tools.py": {"summary": {"percent_covered": 68}},
            "bot/llm/gemini.py": {"summary": {"percent_covered": 100}},
            "bot/db/database.py": {"summary": {"percent_covered": 72}},
        },
    }
    result = quality_snapshot._evaluate_hotspots(coverage_payload)
    assert result["overall_percent"] == 81.5
    assert result["evaluated_hotspots"] == len(quality_snapshot.CRITICAL_COVERAGE_RULES)
    assert any(item["path"] == "bot/channels/whatsapp/outbound.py" for item in result["failed_blockers"])


def test_write_report_includes_coverage_blockers(tmp_path):
    results = [
        quality_snapshot.CheckResult(
            name="Backend tests",
            command=["python", "-m", "pytest"],
            cwd="repo",
            returncode=0,
            duration_sec=1.2,
            stdout="all good",
            stderr="",
        )
    ]
    coverage = {
        "overall_percent": 62.0,
        "evaluated_hotspots": 2,
        "passed_hotspots": 1,
        "failed_blockers": [
            {
                "path": "bot/api/whatsapp.py",
                "coverage_percent": 50.0,
                "threshold_percent": 70.0,
            }
        ],
        "hotspots": [
            {
                "path": "bot/api/whatsapp.py",
                "zone": "WhatsApp",
                "coverage_percent": 50.0,
                "threshold_percent": 70.0,
                "evaluated": True,
                "ok": False,
            }
        ],
    }
    summary = quality_snapshot._write_report(tmp_path, "run123", results, coverage=coverage)
    assert summary["overall_status"] == "blocked"
    summary_json = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    summary_md = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert summary_json["release_blockers"]
    assert "Release Blockers" in summary_md
    assert "bot/api/whatsapp.py" in summary_md


def test_preflight_prints_hotspots_and_blockers(monkeypatch: pytest.MonkeyPatch, tmp_path, capsys):
    reports_dir = tmp_path / "reports" / "release-evidence"
    reports_dir.mkdir(parents=True)
    latest = reports_dir / "latest_summary.json"
    latest.write_text(
        json.dumps(
            {
                "readiness_percent": 83.33,
                "passed_checks": 5,
                "total_checks": 6,
                "overall_status": "blocked",
                "coverage": {"passed_hotspots": 10, "evaluated_hotspots": 13},
                "release_blockers": ["Coverage hotspot below threshold: bot/api/whatsapp.py (50.0% < 70.0%)"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(preflight_check, "run", lambda command, cwd: 1)
    monkeypatch.setattr(preflight_check.Path, "resolve", lambda self: tmp_path / "scripts" / "preflight_check.py")
    monkeypatch.setattr(sys, "argv", ["preflight_check.py"])
    code = preflight_check.main()
    output = capsys.readouterr().out
    assert code == 1
    assert "Critical hotspots: 10/13 passed" in output
    assert "Release blockers:" in output
