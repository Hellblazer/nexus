# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-aginu: ``_endpoint_resolve.py`` is the ONE shared stdlib-only
endpoint/credential resolver for every hook script under
``conexus/hooks/scripts/`` that needs to talk to the nexus-service engine
without importing ``nexus``. Before this module, three independent
hand-rolled mirrors of ``nexus.db.service_endpoint.resolve_service_endpoint``'s
precedence existed (``t2_prefix_scan.py``, ``routing/_lib.py``,
``tuple_ledger_project.py``) and one of them (the third, and the one that
fell behind) is exactly how nexus-0zsmg happened.

This suite pins :func:`_endpoint_resolve.resolve_base_url`'s precedence
against the REAL client's ``nexus.db.service_endpoint.resolve_service_endpoint``
-- the cases below are built from that module's own pinning suite,
``tests/db/test_shared_service_endpoint.py`` -- plus the primitive readers
(``read_config_yml_credentials``, ``read_data_token_lease``,
``read_storage_service_lease``) and the two nexus-aginu-specific fixes:
inline-comment/quoted-value handling in the config.yml scanner, and the
host/port-env leg's host now merging from a live local lease instead of
always defaulting to 127.0.0.1 (critique observation 1).

The suite-wide autouse ``_isolate_config_dir`` fixture (``tests/conftest.py``)
redirects ``NEXUS_CONFIG_DIR`` to a fresh ``tmp_path`` per test, so both the
real client (imported normally here -- this test file, unlike the hook
scripts themselves, has no stdlib-only constraint) and the loaded
``_endpoint_resolve`` module see the same isolated on-disk state.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import time
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "scripts"
MODULE_PATH = SCRIPTS_DIR / "_endpoint_resolve.py"
T2_PREFIX_SCAN_PATH = SCRIPTS_DIR / "t2_prefix_scan.py"
ROUTING_LIB_PATH = SCRIPTS_DIR / "routing" / "_lib.py"
TUPLE_LEDGER_PATH = SCRIPTS_DIR / "tuple_ledger_project.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def ep():
    return _load(MODULE_PATH, "nx_endpoint_resolve")


def _config_dir() -> Path:
    d = Path(os.environ["NEXUS_CONFIG_DIR"])
    d.mkdir(parents=True, exist_ok=True)
    return d


def _publish_lease(*, host: str = "127.0.0.1", port: int, token: str) -> None:
    from nexus.daemon.service_registry import ServiceRegistry

    reg = ServiceRegistry(dir=_config_dir(), tier="storage_service")
    reg.publish(
        str(os.getuid()),
        endpoint={"host": host, "port": port, "token": token},
        version="test",
        owner_token="aginu-test-owner",
    )


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("NX_SERVICE_HOST", "NX_SERVICE_PORT", "NX_SERVICE_TOKEN", "NX_SERVICE_URL"):
        monkeypatch.delenv(k, raising=False)
    yield


# ── Module identity / no-nexus-import invariant ─────────────────────────────


def test_module_exists_and_never_imports_nexus() -> None:
    assert MODULE_PATH.exists()
    src = MODULE_PATH.read_text()
    assert "import nexus" not in src
    assert "from nexus" not in src


@pytest.mark.parametrize(
    "consumer_path",
    [T2_PREFIX_SCAN_PATH, ROUTING_LIB_PATH, TUPLE_LEDGER_PATH],
    ids=["t2_prefix_scan.py", "routing/_lib.py", "tuple_ledger_project.py"],
)
def test_every_consumer_imports_the_shared_module(consumer_path: Path) -> None:
    """The bead's actual ask: no consumer keeps a private copy of the
    precedence any more -- each one imports the shared module by name."""
    src = consumer_path.read_text()
    assert "import _endpoint_resolve" in src, (
        f"{consumer_path.name}: does not import the shared _endpoint_resolve module"
    )


# ── resolve_base_url precedence, pinned against the real client ────────────
# Cases mirror tests/db/test_shared_service_endpoint.py's
# TestResolveServiceEndpoint / TestSchemeAwareEndpoint / TestConfigYmlFallback.


class TestResolveBaseUrlPrecedence:
    def test_env_host_port_resolves(self, ep, monkeypatch) -> None:
        monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", "8123")
        base_url, is_local = ep.resolve_base_url(_config_dir())
        assert base_url == "http://127.0.0.1:8123"
        assert is_local is False

    def test_base_url_from_lease(self, ep) -> None:
        _publish_lease(port=5555, token="lease-tok")
        base_url, is_local = ep.resolve_base_url(_config_dir())
        assert base_url == "http://127.0.0.1:5555"
        assert is_local is True

    def test_service_url_https_used_verbatim(self, ep, monkeypatch) -> None:
        monkeypatch.setenv("NX_SERVICE_URL", "https://api.conexus-nexus.com:443")
        base_url, is_local = ep.resolve_base_url(_config_dir())
        assert base_url == "https://api.conexus-nexus.com:443"
        assert is_local is False

    def test_service_url_trailing_slash_stripped(self, ep, monkeypatch) -> None:
        monkeypatch.setenv("NX_SERVICE_URL", "https://api.conexus-nexus.com:443/")
        base_url, _ = ep.resolve_base_url(_config_dir())
        assert base_url == "https://api.conexus-nexus.com:443"

    def test_service_url_wins_over_host_port(self, ep, monkeypatch) -> None:
        monkeypatch.setenv("NX_SERVICE_URL", "https://api.conexus-nexus.com:443")
        monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", "8123")
        base_url, _ = ep.resolve_base_url(_config_dir())
        assert base_url == "https://api.conexus-nexus.com:443"

    def test_config_yml_service_url_resolves_when_env_absent(self, ep, monkeypatch) -> None:
        from nexus.config import set_credential

        set_credential("service_url", "https://api.conexus-nexus.com")
        base_url, is_local = ep.resolve_base_url(_config_dir())
        assert base_url == "https://api.conexus-nexus.com"
        assert is_local is False

    def test_env_service_url_wins_over_config_yml(self, ep, monkeypatch) -> None:
        from nexus.config import set_credential

        set_credential("service_url", "https://config.example:443")
        monkeypatch.setenv("NX_SERVICE_URL", "https://env.example:443")
        base_url, _ = ep.resolve_base_url(_config_dir())
        assert base_url == "https://env.example:443"

    def test_fail_loud_when_nothing_resolvable(self, ep) -> None:
        with pytest.raises(ep.EndpointUnresolvable, match="no service endpoint resolvable"):
            ep.resolve_base_url(_config_dir())

    def test_non_integer_port_raises(self, ep, monkeypatch) -> None:
        monkeypatch.setenv("NX_SERVICE_PORT", "not-a-number")
        with pytest.raises(ep.EndpointUnresolvable, match="not an integer"):
            ep.resolve_base_url(_config_dir())

    def test_expired_lease_is_absent(self, ep) -> None:
        from nexus.daemon.service_registry import ServiceRegistry

        reg = ServiceRegistry(
            dir=_config_dir(), tier="storage_service", ttl=10.0, clock=lambda: 100.0,
        )
        reg.publish(
            str(os.getuid()),
            endpoint={"host": "127.0.0.1", "port": 4242, "token": "stale"},
            version="test",
            owner_token="aginu-test-owner",
        )
        with pytest.raises(ep.EndpointUnresolvable):
            ep.resolve_base_url(_config_dir())

    def test_host_port_env_leg_fills_host_from_live_lease(self, ep, monkeypatch) -> None:
        """nexus-aginu (critique observation 1): the real client's
        resolve_service_config merges a missing HOST from a live local
        lease before defaulting to 127.0.0.1. The pre-consolidation
        tuple_ledger_project.py mirror always defaulted to 127.0.0.1 here
        without ever consulting the lease -- this pins the fix."""
        _publish_lease(host="10.0.0.9", port=4242, token="lease-tok")
        monkeypatch.setenv("NX_SERVICE_PORT", "9999")  # a DIFFERENT port than the lease
        base_url, is_local = ep.resolve_base_url(_config_dir())
        assert base_url == "http://10.0.0.9:9999"
        assert is_local is False  # still not the bare-lease leg -- PORT was explicit env

    def test_host_port_env_leg_defaults_host_when_no_lease(self, ep, monkeypatch) -> None:
        monkeypatch.setenv("NX_SERVICE_PORT", "9999")
        base_url, is_local = ep.resolve_base_url(_config_dir())
        assert base_url == "http://127.0.0.1:9999"
        assert is_local is False

    def test_host_port_env_leg_explicit_host_wins_over_lease(self, ep, monkeypatch) -> None:
        _publish_lease(host="10.0.0.9", port=4242, token="lease-tok")
        monkeypatch.setenv("NX_SERVICE_HOST", "192.168.1.1")
        monkeypatch.setenv("NX_SERVICE_PORT", "9999")
        base_url, _ = ep.resolve_base_url(_config_dir())
        assert base_url == "http://192.168.1.1:9999"


# ── config.yml credential parsing: quoted values + inline comments ─────────


class TestConfigYmlCredentials:
    def _write(self, lines: list[str]) -> None:
        (_config_dir() / "config.yml").write_text("\n".join(lines) + "\n")

    def test_plain_value(self, ep) -> None:
        self._write(["credentials:", "  service_url: https://api.example.test"])
        assert ep.read_config_yml_credentials(_config_dir()) == {
            "service_url": "https://api.example.test"
        }

    def test_quoted_value(self, ep) -> None:
        self._write(["credentials:", '  service_url: "https://api.example.test"'])
        assert ep.read_config_yml_credentials(_config_dir())["service_url"] == (
            "https://api.example.test"
        )

    def test_inline_comment_stripped(self, ep) -> None:
        """nexus-aginu: real YAML/PyYAML strips a trailing ` # comment` --
        the three pre-consolidation mirrors did not, so a hand-edited
        config.yml fed the comment text into the resolved credential."""
        self._write([
            "credentials:",
            "  service_url: https://api.example.test  # set by hand",
        ])
        assert ep.read_config_yml_credentials(_config_dir())["service_url"] == (
            "https://api.example.test"
        )

    def test_hash_inside_quotes_is_not_a_comment(self, ep) -> None:
        self._write(["credentials:", "  service_token: 'tok#withhash'"])
        assert ep.read_config_yml_credentials(_config_dir())["service_token"] == "tok#withhash"

    def test_both_keys_with_inline_comments(self, ep) -> None:
        self._write([
            "credentials:",
            "  service_url: https://api.example.test # url comment",
            "  service_token: abc123  # token comment",
        ])
        creds = ep.read_config_yml_credentials(_config_dir())
        assert creds == {
            "service_url": "https://api.example.test",
            "service_token": "abc123",
        }

    def test_absent_file(self, ep) -> None:
        assert ep.read_config_yml_credentials(_config_dir()) == {}

    def test_no_credentials_block(self, ep) -> None:
        self._write(["install:", "  mode: managed"])
        assert ep.read_config_yml_credentials(_config_dir()) == {}


# ── data-token lease: tenant scoping + near-expiry margin ──────────────────


def _write_data_token_lease(
    config_dir: Path, *, base_url: str, token: str, tenant: str = "default",
    ttl_seconds: float = 3600.0, remaining_s: float = 3600.0,
) -> None:
    from urllib.parse import urlsplit

    host = urlsplit(base_url).netloc or base_url
    digest = hashlib.sha256(f"{host}\x00{tenant}".encode("utf-8")).hexdigest()
    record = {
        "format_version": 1,
        "token": token,
        "tenant": tenant,
        "base_url_digest": digest,
        "expires_at": time.time() + remaining_s,
        "ttl_seconds": ttl_seconds,
        "minted_by_pid": os.getpid(),
    }
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / f"data_token_lease.{digest}").write_text(json.dumps(record))


class TestReadDataTokenLease:
    def test_matching_lease_returns_token(self, ep) -> None:
        cd = _config_dir()
        _write_data_token_lease(cd, base_url="http://127.0.0.1:4242", token="tok")
        assert ep.read_data_token_lease(cd, "http://127.0.0.1:4242") == "tok"

    def test_wrong_tenant_is_ignored_even_with_matching_digest(self, ep) -> None:
        """The digest is self-consistent (recomputed from the SAME
        lease's own tenant field), so a tenant filter is the only thing
        that catches a foreign-tenant lease on the same host."""
        cd = _config_dir()
        _write_data_token_lease(cd, base_url="http://127.0.0.1:4242", token="other-tok", tenant="other")
        assert ep.read_data_token_lease(cd, "http://127.0.0.1:4242", tenant="default") is None

    def test_near_expiry_threshold_treats_close_lease_as_absent(self, ep) -> None:
        cd = _config_dir()
        _write_data_token_lease(
            cd, base_url="http://127.0.0.1:4242", token="soon",
            ttl_seconds=3600.0, remaining_s=100.0,
        )
        assert ep.read_data_token_lease(
            cd, "http://127.0.0.1:4242", near_expiry_threshold=0.20,
        ) is None
        # ...but the SAME lease is fine at threshold=0.0 (t2_prefix_scan.py /
        # routing/_lib.py's historical, unchanged behaviour).
        assert ep.read_data_token_lease(
            cd, "http://127.0.0.1:4242", near_expiry_threshold=0.0,
        ) == "soon"

    def test_missing_returns_none(self, ep) -> None:
        assert ep.read_data_token_lease(_config_dir(), "http://127.0.0.1:4242") is None


# ── resolve_endpoint_and_token: nexus-g2lln policy ──────────────────────────


class TestResolveEndpointAndToken:
    def test_managed_endpoint_never_falls_back_to_local_lease_token(self, ep, monkeypatch) -> None:
        cd = _config_dir()
        _publish_lease(port=4242, token="local-static")
        os_chmod_owner_only = cd / f"{ep.STORAGE_SERVICE_TIER}_addr.{os.getuid()}"
        os_chmod_owner_only.chmod(0o600)
        monkeypatch.setenv("NX_SERVICE_URL", "https://managed.example:443")
        with pytest.raises(ep.EndpointUnresolvable, match="no fresh data-token lease"):
            ep.resolve_endpoint_and_token(cd)

    def test_local_supervisor_falls_back_to_its_own_token(self, ep) -> None:
        cd = _config_dir()
        _publish_lease(port=4242, token="local-static")
        (cd / f"{ep.STORAGE_SERVICE_TIER}_addr.{os.getuid()}").chmod(0o600)
        base_url, token, is_local = ep.resolve_endpoint_and_token(cd)
        assert base_url == "http://127.0.0.1:4242"
        assert token == "local-static"
        assert is_local is True

    def test_local_supervisor_token_refused_when_world_readable(self, ep) -> None:
        cd = _config_dir()
        _publish_lease(port=4242, token="local-static")
        (cd / f"{ep.STORAGE_SERVICE_TIER}_addr.{os.getuid()}").chmod(0o644)
        with pytest.raises(ep.EndpointUnresolvable, match="group/other-accessible"):
            ep.resolve_endpoint_and_token(cd)

    def test_data_token_lease_wins_over_local_supervisor_token(self, ep) -> None:
        cd = _config_dir()
        _publish_lease(port=4242, token="local-static")
        (cd / f"{ep.STORAGE_SERVICE_TIER}_addr.{os.getuid()}").chmod(0o600)
        _write_data_token_lease(cd, base_url="http://127.0.0.1:4242", token="fresh-data-token")
        base_url, token, is_local = ep.resolve_endpoint_and_token(cd)
        assert token == "fresh-data-token"
        assert is_local is True
