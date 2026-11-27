# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ec37o: a stored credential goes only to the origin it was issued for.

The pure functions (origin normaliser, equality, loopback test, candidate choice)
and the IO wrapper ``select_service_token`` / ``bound_mint_credential`` in
``nexus.db.service_endpoint``. The plugin's stdlib mirror is held to the same
table in ``tests/hooks/test_origin_binding_parity.py``.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from nexus.db import service_endpoint as se

MANAGED = "https://api.conexus-nexus.com"
DECOY = "http://127.0.0.1:41999"
LEASE_URL = "http://127.0.0.1:40123"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in (
        "NX_SERVICE_URL", "NX_SERVICE_HOST", "NX_SERVICE_PORT", "NX_SERVICE_TOKEN",
        "NX_MINT_TOKEN",
    ):
        monkeypatch.delenv(k, raising=False)


def _save(credentials: dict[str, str]) -> None:
    d = Path(os.environ["NEXUS_CONFIG_DIR"])
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.yml").write_text(yaml.safe_dump({"credentials": credentials}), encoding="utf-8")


def _no_lease() -> tuple[None, None]:
    return None, None


# ── the origin normaliser ────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://API.Example.com", ("https", "api.example.com", 443)),
        ("https://api.example.com:443/", ("https", "api.example.com", 443)),
        ("http://api.example.com:80/x?y#z", ("http", "api.example.com", 80)),
        ("https://api.example.com./", ("https", "api.example.com", 443)),
        ("http://[::1]:8080", ("http", "::1", 8080)),
        ("HTTP://[::1]", ("http", "::1", 80)),
        ("http://localhost:9", ("http", "localhost", 9)),
        ("  https://h  ", ("https", "h", 443)),
        # refused: two HTTP stacks may read these as different hosts
        ("http://evil.com\\@good.com", None),
        ("http://user:pw@host", None),
        ("http://ho st", None),
        ("http://host:", None),
        ("http://host:0", None),
        ("http://host:65536", None),
        ("http://host:١٢", None),
        ("http://h%6fst", None),
        ("http://hôst", None),
        ("http://[::1", None),
        ("http://[abc]:1", None),
        ("http://a:b:c", None),
        ("ftp://host", None),
        ("host:80", None),
        ("", None),
        ("https://", None),
    ],
)
def test_normalize_origin(url, expected):
    assert se.normalize_origin(url) == expected


def test_localhost_is_not_127_0_0_1():
    assert not se.origins_equal("http://localhost:8080", "http://127.0.0.1:8080")
    assert se.origins_equal("http://127.0.0.1:8080", "http://127.0.0.1:8080/")


def test_scheme_is_part_of_the_origin():
    assert not se.origins_equal("http://h", "https://h")
    assert not se.origins_equal("http://h:443", "https://h:443")


@pytest.mark.parametrize(
    ("host", "loopback"),
    [("localhost", True), ("127.0.0.1", True), ("127.5.5.5", True), ("::1", True),
     ("api.example.com", False), ("10.0.0.5", False), ("evil.localhost", False), ("", False)],
)
def test_is_loopback_host(host, loopback):
    assert se.is_loopback_host(host) is loopback


# ── select_service_token ─────────────────────────────────────────────────────

def test_saved_token_with_saved_url_goes_to_that_url():
    _save({"service_url": "https://staging.example.com", "service_token": "saved"})
    assert se.select_service_token("https://staging.example.com", lease=_no_lease) == (
        "saved", "the saved service_token",
    )


@pytest.mark.expects_unresolvable_endpoint  # the refusal is the point
def test_saved_token_with_no_saved_url_binds_to_the_default_managed_endpoint():
    _save({"service_token": "saved"})
    assert se.select_service_token(MANAGED + "/", lease=_no_lease) == ("saved", "the saved service_token")
    with pytest.raises(se.CredentialOriginMismatchError):
        se.select_service_token("https://other.example.com", lease=_no_lease)


@pytest.mark.expects_unresolvable_endpoint  # the refusal is the point
def test_saved_token_is_refused_for_an_env_url_naming_another_host(monkeypatch):
    _save({"service_url": MANAGED, "service_token": "saved"})
    monkeypatch.setenv("NX_SERVICE_URL", DECOY)
    with pytest.raises(se.CredentialOriginMismatchError) as err:
        se.select_service_token(DECOY, lease=_no_lease)
    text = str(err.value)
    assert "NX_SERVICE_URL" in text and "saved service_token" in text and "nx config set service_url" in text
    assert "41999" not in text and "127.0.0.1" not in text  # never the URL


def test_mismatch_error_is_a_token_unresolvable_error():
    assert issubclass(se.CredentialOriginMismatchError, se.ServiceTokenUnresolvableError)
    assert issubclass(se.InsecureServiceTransportError, se.ServiceTokenUnresolvableError)


def test_env_token_is_unbound(monkeypatch):
    _save({"service_url": MANAGED, "service_token": "saved"})
    monkeypatch.setenv("NX_SERVICE_TOKEN", "from-env")
    assert se.select_service_token(DECOY, lease=_no_lease) == ("from-env", "NX_SERVICE_TOKEN")


@pytest.mark.expects_unresolvable_endpoint  # the refusal is the point
def test_lease_token_goes_only_to_the_lease_origin():
    lease = lambda: (LEASE_URL, "lease-tok")  # noqa: E731
    assert se.select_service_token(LEASE_URL, lease=lease) == ("lease-tok", "the supervisor lease token")
    with pytest.raises(se.CredentialOriginMismatchError) as err:
        se.select_service_token("http://127.0.0.1:40124", lease=lease)  # a stale or second port
    assert "supervisor lease token" in str(err.value)
    with pytest.raises(se.CredentialOriginMismatchError):
        se.select_service_token("http://localhost:40123", lease=lease)  # localhost is not 127.0.0.1


def test_a_refused_saved_token_falls_through_to_a_matching_lease_token():
    _save({"service_url": MANAGED, "service_token": "saved"})
    got = se.select_service_token(LEASE_URL, lease=lambda: (LEASE_URL, "lease-tok"))
    assert got == ("lease-tok", "the supervisor lease token")


def test_no_credential_at_all_returns_none():
    assert se.select_service_token(DECOY, lease=_no_lease) is None


@pytest.mark.expects_unresolvable_endpoint  # the refusal is the point
def test_mint_credential_applies_only_to_its_origin():
    _save({"service_url": MANAGED, "mint_token": "mint-secret"})
    assert se.select_service_token(MANAGED, lease=_no_lease) == ("", "mint_token")
    with pytest.raises(se.CredentialOriginMismatchError) as err:
        se.select_service_token(DECOY, lease=_no_lease)
    assert "mint_token" in str(err.value)
    assert se.select_service_token(DECOY, lease=_no_lease, allow_mint=False) is None


def test_bound_mint_credential(monkeypatch):
    _save({"service_url": MANAGED, "mint_token": "mint-secret"})
    assert se.bound_mint_credential(MANAGED) == "mint-secret"
    assert se.bound_mint_credential(DECOY) is None
    monkeypatch.setenv("NX_MINT_TOKEN", "env-mint")
    assert se.bound_mint_credential(DECOY) == "env-mint"  # an env credential is unbound


@pytest.mark.expects_unresolvable_endpoint  # the refusal is the point
def test_a_bearer_never_goes_over_cleartext_http_off_this_machine(monkeypatch):
    monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
    with pytest.raises(se.InsecureServiceTransportError) as err:
        se.select_service_token("http://10.0.0.5:8080", lease=_no_lease)
    assert "10.0.0.5" not in str(err.value)
    for ok in ("https://10.0.0.5", "http://127.0.0.1:1", "http://localhost:1", "http://[::1]:1"):
        assert se.select_service_token(ok, lease=_no_lease) == ("tok", "NX_SERVICE_TOKEN")
    with pytest.raises(se.InsecureServiceTransportError):
        se.select_service_token("not a url", lease=_no_lease)


@pytest.mark.expects_unresolvable_endpoint  # the refusal is the point
def test_cleartext_rule_covers_the_mint_credential(monkeypatch):
    monkeypatch.setenv("NX_MINT_TOKEN", "m")
    with pytest.raises(se.InsecureServiceTransportError):
        se.bound_mint_credential("http://10.0.0.5:8080")


def test_default_managed_url_is_the_real_cloud_endpoint():
    # tests/conftest.py repoints managed_endpoint.DEFAULT_MANAGED_SERVICE_URL at a dead port
    # for isolation; the binding default is the real one and is not patched.
    assert se.DEFAULT_MANAGED_SERVICE_URL == "https://api.conexus-nexus.com"


def test_a_mint_credential_issued_for_another_origin_warns_once_per_process(monkeypatch):
    """bearer_for asks on every request: the refusal is logged the first time, not each time."""
    from structlog.testing import capture_logs  # noqa: PLC0415 -- this test only

    se._WARNED_ONCE.clear()
    _save({"service_url": MANAGED, "mint_token": "mint-secret"})
    with capture_logs() as logs:
        for _ in range(5):
            assert se.bound_mint_credential(DECOY) is None
    warnings = [e for e in logs if e["event"] == "mint_credential_origin_mismatch"]
    assert len(warnings) == 1, logs
    assert "mint-secret" not in str(logs)
    se._WARNED_ONCE.clear()
