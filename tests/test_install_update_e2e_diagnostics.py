"""CI diagnostics for the real install/update E2E."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
E2E_SCRIPT = REPO_ROOT / "tests" / "install" / "install-update-e2e.sh"
E2E_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "install-e2e.yml"
E2E_REUSABLE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "install-e2e-run.yml"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None,
    reason="needs bash",
)


def _extract_reporter() -> str:
    text = E2E_SCRIPT.read_text()
    match = re.search(r"report_ci_log_tail\(\) \{.*?\n\}", text, re.DOTALL)
    assert match is not None, "report_ci_log_tail() not found"
    return match.group(0)


def _extract_transient_classifier() -> str:
    text = E2E_SCRIPT.read_text()
    match = re.search(r"is_transient_install_failure\(\) \{.*?\n\}", text, re.DOTALL)
    assert match is not None, "is_transient_install_failure() not found"
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


def _classifies_as_transient(tmp_path: Path, message: str) -> bool:
    log = tmp_path / "install.log"
    log.write_text(message)
    script = f'{_extract_transient_classifier()}\nis_transient_install_failure "$1"\n'
    result = subprocess.run(
        ["bash", "-c", script, "bash", str(log)],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


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


@pytest.mark.parametrize(
    "message",
    [
        "remote returned HTTP 429\n",
        "curl: (52) Empty reply from server\n",
        "npm error code ETIMEDOUT\n",
        "npm error code ECONNRESET\n",
        "request failed with HTTP 503 Service Unavailable\n",
    ],
)
def test_transient_classifier_accepts_network_failures(
    tmp_path: Path, message: str
) -> None:
    assert _classifies_as_transient(tmp_path, message)


@pytest.mark.parametrize(
    "message",
    [
        "npm ERR! code ERESOLVE\n",
        "The lockfile at uv.lock needs to be updated\n",
        "No matching distribution found for missing-package\n",
        "npm install failed or timed out\n",
        "Resolved 500 packages successfully\n",
    ],
)
def test_transient_classifier_rejects_deterministic_or_ambiguous_failures(
    tmp_path: Path, message: str
) -> None:
    assert not _classifies_as_transient(tmp_path, message)


def test_workflow_reuses_full_tagged_checkout_and_bounds_download_parallelism() -> None:
    caller = E2E_WORKFLOW.read_text()
    reusable = E2E_REUSABLE_WORKFLOW.read_text()

    assert caller.count("max-parallel: 2") == 2
    assert "fetch-depth: 0" in reusable
    assert "fetch-tags: true" in reusable
    assert "HERMES_DEV_SANDBOX_UPSTREAM: ${{ github.workspace }}" in reusable
