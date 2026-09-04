"""Persistent dev-sandbox state must not masquerade as source changes."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
DEV_SANDBOX = REPO_ROOT / "scripts" / "dev-sandbox.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None,
    reason="needs git and bash",
)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _extract_worktree_status() -> str:
    text = DEV_SANDBOX.read_text()
    match = re.search(r"worktree_status\(\) \{.*?\n\}", text, re.DOTALL)
    assert match is not None, "worktree_status() not found in dev-sandbox.sh"
    return match.group(0)


def test_persistent_sandbox_is_excluded_but_source_changes_are_reported(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    (repo / "tracked.txt").write_text("clean\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "initial")

    sandbox_name = ".hermes-sandbox-e2e-installer"
    sandbox = repo / sandbox_name
    sandbox.mkdir()
    (sandbox / "runtime-state").write_text("not source\n")

    script = (
        "set -e\n"
        f'GIT_ROOT="{repo}"\n'
        f'SANDBOX_DIR_NAME="{sandbox_name}"\n'
        'SANDBOX_EXCLUDE_PATHSPEC=":(top,literal,exclude)$SANDBOX_DIR_NAME"\n'
        f"{_extract_worktree_status()}\n"
        "worktree_status\n"
    )
    clean = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=True
    )
    assert clean.stdout == ""

    (repo / "source-change.txt").write_text("real source change\n")
    dirty = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=True
    )
    assert "source-change.txt" in dirty.stdout
    assert sandbox_name not in dirty.stdout


def test_snapshot_git_add_excludes_the_persistent_sandbox(tmp_path: Path) -> None:
    text = DEV_SANDBOX.read_text()
    assert 'git add -A -- . "$SANDBOX_EXCLUDE_PATHSPEC"' in text

    repo = tmp_path / "repo"
    snapshot = tmp_path / "snapshot"
    repo.mkdir()
    snapshot.mkdir()
    _git(repo, "init")
    (repo / "tracked.txt").write_text("clean\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "initial")

    sandbox_name = ".hermes-sandbox-e2e-installer"
    sandbox = repo / sandbox_name
    sandbox.mkdir()
    (sandbox / "runtime-state").write_text("not source\n")
    (repo / "source-change.txt").write_text("real source change\n")

    _git(snapshot, "init")
    _git(snapshot, "fetch", str(repo), "HEAD")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    env = {
        "GIT_DIR": str(snapshot / ".git"),
        "GIT_WORK_TREE": str(repo),
    }
    subprocess.run(["git", "read-tree", head], env=env, check=True)
    subprocess.run(
        [
            "git",
            "add",
            "-A",
            "--",
            ".",
            f":(top,literal,exclude){sandbox_name}",
        ],
        cwd=repo,
        env=env,
        check=True,
    )
    tree = subprocess.run(
        ["git", "write-tree"], env=env, check=True, capture_output=True, text=True
    ).stdout.strip()
    names = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", tree],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()

    assert "source-change.txt" in names
    assert not any(name.startswith(f"{sandbox_name}/") for name in names)
