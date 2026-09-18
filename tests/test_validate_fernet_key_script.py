from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validate_fernet_key.py"


def _subenv() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("TENANT_SECRETS_MASTER_KEY", None)
    return env


def test_validate_fernet_key_require_fails_without_key(tmp_path):
    # --env-file points at an empty file so this is isolated from whatever a
    # real developer .env happens to have (the whole point of this test is
    # the "no key configured" path, not "read the real repo .env").
    empty_env_file = tmp_path / "empty.env"
    empty_env_file.write_text("", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--require", "--env-file", str(empty_env_file)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=_subenv(),
    )
    assert proc.returncode == 1
    assert "empty" in proc.stderr.lower() or "valid Fernet" in proc.stderr


def test_validate_fernet_key_accepts_valid_key():
    key = Fernet.generate_key().decode()
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--require", "--key", key],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert "valid Fernet" in proc.stdout
