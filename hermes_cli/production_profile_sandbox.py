"""VPS production-profile launcher enforcement.

Hermes' portable profile feature normally switches ``HERMES_HOME`` inside the
current process.  The Sentinel Forge VPS adds an operating-system isolation
layer for the named profiles.  Profiles present in the authoritative access
policy must therefore run through the systemd sandbox launcher or their
dedicated gateway service; ordinary local Hermes profiles remain unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path


_POLICY_PATH = Path("/etc/hermes/profile-access.json")
_CGROUP_PATH = Path("/proc/self/cgroup")
_LAUNCHER = "/usr/local/sbin/hermes-profile-run"


class ProductionProfileSandboxRequired(RuntimeError):
    """Raised when a named production profile is started outside its sandbox."""


def _production_policy() -> dict:
    try:
        data = json.loads(_POLICY_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def canonical_production_profile(profile_name: str) -> str | None:
    """Return the canonical policy name, or ``None`` for a local profile."""
    policy = _production_policy()
    profiles = policy.get("profiles")
    aliases = policy.get("aliases")
    if not isinstance(profiles, dict):
        return None
    name = str(profile_name or "").strip()
    if isinstance(aliases, dict):
        alias_target = aliases.get(name)
        if isinstance(alias_target, str) and alias_target:
            name = alias_target
    return name if name in profiles else None


def production_profile_launcher_argv(
    profile_name: str,
    *,
    cwd: str | None = None,
) -> list[str] | None:
    """Return the sandbox-launcher prefix for a policy-managed profile."""
    canonical = canonical_production_profile(profile_name)
    if canonical is None:
        return None
    argv = [_LAUNCHER]
    if cwd:
        argv.extend(["--cwd", cwd])
    argv.extend([canonical, "--"])
    return argv


def require_production_profile_sandbox(profile_name: str) -> None:
    """Reject raw execution of an operating-system-isolated profile."""
    canonical = canonical_production_profile(profile_name)
    if canonical is None:
        return
    try:
        cgroup = _CGROUP_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        cgroup = ""
    approved_markers = (
        f"hermes-gateway-{canonical}.service",
        f"hermes-profile-{canonical}-",
    )
    if any(marker in cgroup for marker in approved_markers):
        return
    raise ProductionProfileSandboxRequired(
        "raw production profile execution is disabled; use: "
        f"hermes-profile-run {canonical} -- <hermes arguments>"
    )
