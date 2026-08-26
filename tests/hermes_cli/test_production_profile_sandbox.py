import json

import pytest

from hermes_cli import production_profile_sandbox as sandbox


@pytest.fixture
def production_policy(tmp_path, monkeypatch):
    policy = tmp_path / "profile-access.json"
    policy.write_text(
        json.dumps(
            {
                "profiles": {"personal-assistant": {}},
                "aliases": {"pa": "personal-assistant"},
            }
        ),
        encoding="utf-8",
    )
    cgroup = tmp_path / "cgroup"
    cgroup.write_text("0::/user.slice/test.scope\n", encoding="utf-8")
    monkeypatch.setattr(sandbox, "_POLICY_PATH", policy)
    monkeypatch.setattr(sandbox, "_CGROUP_PATH", cgroup)
    return cgroup


def test_local_profile_keeps_portable_profile_behavior(production_policy):
    assert sandbox.canonical_production_profile("local-coder") is None
    assert sandbox.production_profile_launcher_argv("local-coder") is None
    sandbox.require_production_profile_sandbox("local-coder")


def test_policy_alias_resolves_to_canonical_launcher(production_policy):
    assert sandbox.production_profile_launcher_argv("pa", cwd="/work") == [
        "/usr/local/sbin/hermes-profile-run",
        "--cwd",
        "/work",
        "personal-assistant",
        "--",
    ]


def test_raw_production_profile_is_rejected(production_policy):
    with pytest.raises(sandbox.ProductionProfileSandboxRequired):
        sandbox.require_production_profile_sandbox("personal-assistant")


@pytest.mark.parametrize(
    "marker",
    (
        "hermes-gateway-personal-assistant.service",
        "hermes-profile-personal-assistant-abc123.service",
    ),
)
def test_gateway_or_transient_launcher_cgroup_is_accepted(
    production_policy,
    marker,
):
    production_policy.write_text(f"0::/system.slice/{marker}\n", encoding="utf-8")
    sandbox.require_production_profile_sandbox("personal-assistant")
