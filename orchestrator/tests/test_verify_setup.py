"""Regression coverage for the setup verifier's checked-in bindings."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "verify-setup.sh"


def test_setup_verifier_accepts_all_checked_in_workflow_bindings() -> None:
    env = {key: value for key, value in os.environ.items() if key != "GITHUB_TOKEN"}
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "[FAIL]" not in result.stdout, result.stdout
