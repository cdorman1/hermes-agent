"""CI diagnostics for the real install/update E2E."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
E2E_SCRIPT = REPO_ROOT / "tests" / "install" / "install-update-e2e.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None,
    reason="needs bash",
)


def _extract_reporter() -> str:
    text = E2E_SCRIPT.read_text()
    match = re.search(r"report_ci_log_tail\(\) \{.*?\n\}", text, re.DOTALL)
    assert match is not None, "report_ci_log_tail() not found"
    return match.group(0)


def _run_reporter(log: Path, *, github_actions: bool) -> subprocess.CompletedProcess[str]:
    script = f"{_extract_reporter()}\nreport_ci_log_tail \"$1\"\n"
    env = {"GITHUB_ACTIONS": "true"} if github_actions else {}
    return subprocess.run(
        ["bash", "-c", script, "bash", str(log)],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def test_reporter_is_silent_outside_github_actions(tmp_path: Path) -> None:
    log = tmp_path / "install.log"
    log.write_text("fatal: example failure\n")
    result = _run_reporter(log, github_actions=False)
    assert result.stderr == ""


def test_reporter_escapes_annotation_data_and_uses_only_last_30_lines(
    tmp_path: Path,
) -> None:
    log = tmp_path / "install.log"
    lines = [f"line {number}" for number in range(1, 32)]
    lines.append("fatal: 100% failed\r")
    log.write_text("\n".join(lines) + "\n")

    result = _run_reporter(log, github_actions=True)

    assert result.stderr.startswith(
        "::error title=Hermes installer failure details::line 3%0A"
    )
    assert "line 1%0A" not in result.stderr
    assert "line 2%0A" not in result.stderr
    assert result.stderr.endswith("fatal: 100%25 failed%0D\n")
