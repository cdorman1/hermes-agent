"""Dev-sandbox clients must trust the CA presented by its MITM proxy."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
STAGE2 = REPO_ROOT / "scripts" / "sandbox" / "stage2-run.sh"


def test_all_sandbox_clients_trust_the_proxy_signing_ca() -> None:
    text = STAGE2.read_text()

    for variable in (
        "CURL_CA_BUNDLE",
        "SSL_CERT_FILE",
        "GIT_SSL_CAINFO",
        "NODE_EXTRA_CA_CERTS",
    ):
        assert f"--setenv {variable} /work/certs/ca.pem" in text

    assert "--setenv NODE_EXTRA_CA_CERTS /work/certs/real-ca.pem" not in text


def test_proxy_still_uses_the_real_ca_for_its_outbound_tls() -> None:
    text = STAGE2.read_text()
    assert (
        "python3 /work/proxy.py /work/http /work/certs "
        "/work/certs/real-ca.pem"
    ) in text
