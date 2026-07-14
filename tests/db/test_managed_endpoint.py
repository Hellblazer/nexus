# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Cloud-mode managed-service endpoint config + capability probe (nexus-vwvv5.12).

RDR-001 consumer requirement. In cloud mode there is NO local Java service and NO
local Postgres: the nx client talks HTTPS to the managed nexus service, which owns
its cloud PG + pgvector entirely server-side. This client deliverable is exactly
two things:

  1. resolve the managed endpoint (default ``https://api.conexus-nexus.com``,
     ``NX_SERVICE_URL`` / ``NX_SERVICE_TOKEN`` env override);
  2. an HTTP reachability + capability/version-compatibility probe of the
     unauthenticated ``/version`` handshake that FAILS LOUD with a remedy on
     unreachable / incompatible.

No pg_provision, no Postgres connection, no Liquibase on the client.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from nexus.db.managed_endpoint import (
    DEFAULT_MANAGED_SERVICE_URL,
    ManagedCapabilities,
    ManagedServiceIncompatible,
    ManagedServiceUnreachable,
    probe_managed_service,
    resolve_managed_endpoint,
)

_ENV_VARS = ("NX_SERVICE_URL", "NX_SERVICE_TOKEN")


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for v in _ENV_VARS:
        monkeypatch.delenv(v, raising=False)


def _resp(status_code: int, body: dict) -> httpx.Response:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = body
    resp.text = str(body)
    return resp


def _version_body(
    app_version: str = "1.0-SNAPSHOT",
    mode: str = "voyage",
    release_version: str | None = "0.1.41",
) -> dict:
    # nexus-x2g1z: the gate pins on release_version. app_version is the frozen
    # 1.0-SNAPSHOT dev coordinate (informational only). The managed public
    # /version was trimmed to {app_version, release_version} (relay [4566]); the
    # embedding_mode / models / schema fields are kept here for the self-hosted
    # path that still reports them (and to assert they stay optional).
    body: dict = {
        "app_version": app_version,
        "embedding_mode": mode,
        "embedding_models": ["voyage-context-3", "voyage-code-3"],
        "schema_latest_id": "vectors-002",
        "schema_changeset_count": 64,
    }
    if release_version is not None:
        body["release_version"] = release_version
    return body


def _trimmed_version_body(release_version: str = "0.1.41") -> dict:
    """The trimmed managed public /version payload (relay [4566])."""
    return {"app_version": "1.0-SNAPSHOT", "release_version": release_version}


# ── endpoint resolution ──────────────────────────────────────────────────────


def test_resolve_defaults_to_managed_url(monkeypatch):
    monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
    base, token = resolve_managed_endpoint()
    assert base == DEFAULT_MANAGED_SERVICE_URL
    assert token == "tok"


def test_resolve_env_override_strips_trailing_slash(monkeypatch):
    monkeypatch.setenv("NX_SERVICE_URL", "https://staging.example.com/")
    monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
    base, token = resolve_managed_endpoint()
    assert base == "https://staging.example.com"
    assert token == "tok"


def test_resolve_config_yml_only_no_env(monkeypatch, tmp_path):
    # nexus-coq1z (critique gap): the Desktop .mcpb is an env-less GUI
    # subprocess — THIS resolver must fall back to config.yml alone. The
    # sibling resolver (service_endpoint) had this test; this one did not,
    # so the desktop-deployment.md claim was inspection-verified only.
    from nexus.config import set_credential

    # Write via the PUBLIC surface (what `nx config set` does) — this is the
    # path the doc instructs Desktop users to take. The autouse fixture
    # already cleared all NX_SERVICE_* env vars; the suite-level config-dir
    # isolation applies (same mechanics as the sibling
    # test_shared_service_endpoint::TestConfigYmlFallback).
    set_credential("service_url", "https://desktop.example.com/")
    set_credential("service_token", "cfg-only-token")
    base, token = resolve_managed_endpoint()
    assert base == "https://desktop.example.com"
    assert token == "cfg-only-token"


def test_resolve_missing_token_fails_loud(monkeypatch):
    with pytest.raises(ManagedServiceIncompatible) as exc:
        resolve_managed_endpoint()
    assert "NX_SERVICE_TOKEN" in str(exc.value)


def test_resolve_token_optional_when_not_required(monkeypatch):
    base, token = resolve_managed_endpoint(require_token=False)
    assert base == DEFAULT_MANAGED_SERVICE_URL
    assert token is None


# ── probe: unreachable ───────────────────────────────────────────────────────


def test_probe_unreachable_connect_error_fails_loud():
    def fake_get(url: str, timeout: float) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(ManagedServiceUnreachable) as exc:
        probe_managed_service(base_url="https://api.conexus-nexus.com", http_get=fake_get)
    msg = str(exc.value)
    assert "api.conexus-nexus.com" in msg
    assert "NX_SERVICE_URL" in msg  # remedy names the override


def test_probe_unreachable_timeout_fails_loud():
    def fake_get(url: str, timeout: float) -> httpx.Response:
        raise httpx.TimeoutException("timed out")

    with pytest.raises(ManagedServiceUnreachable) as exc:
        probe_managed_service(base_url="https://api.conexus-nexus.com", http_get=fake_get)
    msg = str(exc.value)
    assert "api.conexus-nexus.com" in msg
    assert "NX_SERVICE_URL" in msg  # remedy present, not just the type


def test_probe_non_json_body_is_incompatible():
    def fake_get(url: str, timeout: float) -> httpx.Response:
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.json.side_effect = ValueError("not JSON")
        resp.text = "<html>502 Bad Gateway</html>"
        return resp

    with pytest.raises(ManagedServiceIncompatible):
        probe_managed_service(base_url="https://x", http_get=fake_get)


# ── probe: incompatible ──────────────────────────────────────────────────────


def test_probe_non_200_is_incompatible():
    def fake_get(url: str, timeout: float) -> httpx.Response:
        return _resp(503, {"error": "unavailable"})

    with pytest.raises(ManagedServiceIncompatible) as exc:
        probe_managed_service(base_url="https://x", http_get=fake_get)
    assert "503" in str(exc.value)


def test_probe_missing_release_version_is_incompatible():
    # No release_version at all -> fail-closed (a pre-field / dev engine).
    def fake_get(url: str, timeout: float) -> httpx.Response:
        return _resp(200, _version_body(release_version=None))

    with pytest.raises(ManagedServiceIncompatible) as exc:
        probe_managed_service(base_url="https://x", http_get=fake_get)
    assert "release_version" in str(exc.value)


def test_probe_null_release_version_is_incompatible():
    def fake_get(url: str, timeout: float) -> httpx.Response:
        body = _version_body()
        body["release_version"] = None
        return _resp(200, body)

    with pytest.raises(ManagedServiceIncompatible):
        probe_managed_service(base_url="https://x", http_get=fake_get)


def test_probe_snapshot_release_version_fails_closed():
    # A SNAPSHOT/dev release identity is NOT a release — gate refuses.
    def fake_get(url: str, timeout: float) -> httpx.Response:
        return _resp(200, _version_body(release_version="0.1.9-SNAPSHOT"))

    with pytest.raises(ManagedServiceIncompatible):
        probe_managed_service(base_url="https://x", http_get=fake_get)


def test_probe_unparseable_release_version_fails_closed():
    def fake_get(url: str, timeout: float) -> httpx.Response:
        return _resp(200, _version_body(release_version="unknown"))

    with pytest.raises(ManagedServiceIncompatible):
        probe_managed_service(base_url="https://x", http_get=fake_get)


def test_probe_below_release_floor_is_incompatible():
    def fake_get(url: str, timeout: float) -> httpx.Response:
        return _resp(200, _version_body(release_version="0.1.5"))

    with pytest.raises(ManagedServiceIncompatible) as exc:
        probe_managed_service(base_url="https://x", http_get=fake_get)
    # remedy names the offending version and the floor
    assert "0.1.5" in str(exc.value) and "0.1.41" in str(exc.value)


def test_probe_below_release_floor_exception_carries_structured_versions():
    """nexus-b6qlf Fix 2: the below-floor raise must expose the deployed and
    required versions as STRUCTURED attributes, not only baked into the
    message string -- so a caller (e.g. the cloud-mode error wrapper in
    http_vector_client.py) can build its own message without string-parsing
    or re-embedding the underlying remedy clause verbatim (which previously
    told a cloud user to "upgrade/downgrade the nx client", directly
    contradicting the "cannot be fixed locally" framing wrapped around it)."""

    def fake_get(url: str, timeout: float) -> httpx.Response:
        return _resp(200, _version_body(release_version="0.1.5"))

    with pytest.raises(ManagedServiceIncompatible) as exc:
        probe_managed_service(base_url="https://x", http_get=fake_get)
    assert exc.value.deployed_version == "0.1.5"
    assert exc.value.required_version == "0.1.41"


def test_managed_service_incompatible_fields_default_to_none():
    """Every other raise site (no token, non-200, non-JSON, no usable
    release_version) constructs ManagedServiceIncompatible with just a
    message -- the new fields must default to None rather than requiring
    every call site to pass them."""
    exc = ManagedServiceIncompatible("plain message, no structured fields")
    assert exc.deployed_version is None
    assert exc.required_version is None
    assert str(exc) == "plain message, no structured fields"


def test_probe_snapshot_app_version_is_not_gated():
    # app_version=1.0-SNAPSHOT is the frozen dev coordinate and must NOT fail
    # the gate as long as release_version clears the floor (nexus-x2g1z).
    def fake_get(url: str, timeout: float) -> httpx.Response:
        return _resp(200, _version_body(app_version="1.0-SNAPSHOT", release_version="0.1.41"))

    caps = probe_managed_service(base_url="https://x", http_get=fake_get)
    assert caps.app_version == "1.0-SNAPSHOT"
    assert caps.release_version == "0.1.41"


# ── probe: compatible ────────────────────────────────────────────────────────


def test_probe_compatible_returns_capabilities():
    seen = {}

    def fake_get(url: str, timeout: float) -> httpx.Response:
        seen["url"] = url
        return _resp(200, _version_body())

    caps = probe_managed_service(base_url="https://api.conexus-nexus.com", http_get=fake_get)
    assert isinstance(caps, ManagedCapabilities)
    # probes the unauthenticated /version handshake
    assert seen["url"] == "https://api.conexus-nexus.com/version"
    assert caps.app_version == "1.0-SNAPSHOT"
    assert caps.release_version == "0.1.41"
    assert caps.embedding_mode == "voyage"
    assert caps.embedding_models == ["voyage-context-3", "voyage-code-3"]
    assert caps.schema_latest_id == "vectors-002"
    assert caps.schema_changeset_count == 64
    assert caps.base_url == "https://api.conexus-nexus.com"


def test_probe_at_release_floor_passes():
    def fake_get(url: str, timeout: float) -> httpx.Response:
        return _resp(200, _version_body(release_version="0.1.41"))

    caps = probe_managed_service(base_url="https://x", http_get=fake_get)
    assert caps.release_version == "0.1.41"


def test_probe_above_release_floor_passes():
    def fake_get(url: str, timeout: float) -> httpx.Response:
        return _resp(200, _version_body(release_version="0.2.0"))

    caps = probe_managed_service(base_url="https://x", http_get=fake_get)
    assert caps.release_version == "0.2.0"


def test_probe_v_prefixed_release_version_parses():
    # A self-hosted service tagging "v0.2.0" must not be misread as below-floor.
    def fake_get(url: str, timeout: float) -> httpx.Response:
        return _resp(200, _version_body(release_version="v0.2.0"))

    caps = probe_managed_service(base_url="https://x", http_get=fake_get)
    assert caps.release_version == "v0.2.0"


def test_probe_trimmed_payload_passes_and_defaults_optional_fields():
    # The managed public /version (relay [4566]) returns only
    # {app_version, release_version}; the optional embedding/schema fields are
    # absent and must default gracefully — not fail the probe.
    def fake_get(url: str, timeout: float) -> httpx.Response:
        return _resp(200, _trimmed_version_body(release_version="0.1.41"))

    caps = probe_managed_service(base_url="https://x", http_get=fake_get)
    assert caps.release_version == "0.1.41"
    assert caps.app_version == "1.0-SNAPSHOT"
    assert caps.embedding_mode == "unknown"
    assert caps.embedding_models == []
    assert caps.schema_latest_id is None
    assert caps.schema_changeset_count is None


def test_probe_defaults_base_url_to_managed(monkeypatch):
    monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
    seen = {}

    def fake_get(url: str, timeout: float) -> httpx.Response:
        seen["url"] = url
        return _resp(200, _version_body())

    probe_managed_service(http_get=fake_get)
    assert seen["url"] == f"{DEFAULT_MANAGED_SERVICE_URL}/version"


# ── fail-closed parser: unified single source of truth + type-confusion ─────


def test_probe_uses_the_canonical_engine_version_floor():
    """managed_endpoint no longer owns a local parser/floor (nexus-b6qlf
    unification) — it imports nexus.engine_version.REQUIRED_ENGINE_VERSION /
    parse_engine_version directly, so drift between the managed-cloud gate and
    the native/local gate (guided_upgrade.verify_service_version) is no longer
    possible by construction (previously a hand-maintained local copy, caught
    diverging in nexus-x2g1z review). Confirm the module actually imports the
    canonical names rather than redefining its own."""
    from nexus.db import managed_endpoint
    from nexus.engine_version import REQUIRED_ENGINE_VERSION, parse_engine_version

    assert managed_endpoint.REQUIRED_ENGINE_VERSION is REQUIRED_ENGINE_VERSION
    assert managed_endpoint.parse_engine_version is parse_engine_version
    assert not hasattr(managed_endpoint, "MIN_MANAGED_RELEASE_VERSION")
    assert not hasattr(managed_endpoint, "_parse_release_version")


@pytest.mark.parametrize("bad", [True, False, 1, 0, 1.5, [], {}, None])
def test_probe_non_string_release_version_fails_closed(bad):
    """A non-string release_version (malformed/JSON-confused service) must
    fail closed — the isinstance(str) guard collapses it to "" -> refuse."""
    def fake_get(url: str, timeout: float) -> httpx.Response:
        body = _version_body()
        body["release_version"] = bad
        return _resp(200, body)

    with pytest.raises(ManagedServiceIncompatible):
        probe_managed_service(base_url="https://x", http_get=fake_get)
