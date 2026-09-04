"""Dev-sandbox must not expose hidden host Node header paths to node-gyp."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
DEV_SANDBOX = REPO_ROOT / "scripts" / "dev-sandbox.sh"
STAGE2 = REPO_ROOT / "scripts" / "sandbox" / "stage2-run.sh"
NIX_SANDBOX = REPO_ROOT / "nix" / "sandbox.nix"


def test_host_node_prefix_is_not_automatically_forwarded() -> None:
    text = DEV_SANDBOX.read_text()

    assert 'NODE_DIR="${DEV_SANDBOX_NODE_DIR:-}"' in text
    assert 'NODE_DIR="$(dirname "$(dirname "$(command -v node)")")"' not in text


def test_explicit_sandbox_visible_node_prefix_is_still_supported() -> None:
    stage2 = STAGE2.read_text()
    nix_sandbox = NIX_SANDBOX.read_text()

    assert 'if [ -n "${DEV_SANDBOX_NODE_DIR:-}" ]; then' in stage2
    assert '--setenv npm_config_nodedir "$DEV_SANDBOX_NODE_DIR"' in stage2
    assert 'export DEV_SANDBOX_NODE_DIR=${nodejs_22}' in nix_sandbox
