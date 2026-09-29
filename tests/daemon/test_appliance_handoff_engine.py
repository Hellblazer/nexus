# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ijue9.29 against the REAL engine: the appliance handoff never carries
the root bearer, and what it does carry works exactly as a Windows client needs.

Record: T2 nexus/rdr-218-appliance-endpoint-handoff-decision section 3,
amended for Sam's O1 (the root bearer never leaves the distro). The supervisor
issues a mint-locked credential once, with the root bearer, and projects it.
Proven here, over HTTP against the session engine substrate:
  - the file's mint_token is not the root bearer and is issued as mint-locked;
  - it is refused on a data route and on the token-admin surface;
  - the Windows side's path (DataTokenManager with mint_token/mint_tenant)
    turns it into data tokens that work on a data route;
  - a second publish reuses the persisted credential (no second issue).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest
import structlog

from nexus.daemon.appliance_handoff import (
    HANDOFF_FILE_ENV,
    MINT_CREDENTIAL_FILENAME,
    MINT_CREDENTIAL_LABEL,
    ROOT_TENANT,
)
from nexus.daemon.storage_service_daemon import StorageServiceSupervisor

pytestmark = [pytest.mark.integration]

_DATA_ROUTE = "/v1/catalog/collections/list"


@pytest.fixture
def engine() -> dict:
    from tests._engine_substrate import ensure_engine  # noqa: PLC0415 — boots the engine only when requested

    return ensure_engine()


def _supervisor(config_dir: Path, root_bearer: str) -> StorageServiceSupervisor:
    return StorageServiceSupervisor(
        config_dir=config_dir,
        binary_path=Path("/fake/nexus-service"),
        pg_port=15432,
        service_port=0,
        creds={"NX_SERVICE_TOKEN": root_bearer},
        engine_liveness_scan=lambda _c, _b: [],
    )


def _get(base_url: str, path: str, bearer: str) -> httpx.Response:
    return httpx.get(base_url + path, headers={"Authorization": f"Bearer {bearer}"}, timeout=10)


def _labelled(base_url: str, root: str) -> list[dict]:
    rows = httpx.post(
        base_url + "/v1/service-tokens/list", json={"tenant": ROOT_TENANT},
        headers={"Authorization": f"Bearer {root}"}, timeout=10,
    ).json().get("tokens", [])
    return [r for r in rows if r.get("label") == MINT_CREDENTIAL_LABEL]


def test_the_handoff_carries_a_working_mint_locked_credential_never_the_root(
    engine, tmp_path, monkeypatch,
) -> None:
    base_url, root = engine["base_url"], engine["bearer"]
    port = urlparse(base_url).port
    handoff = tmp_path / "appliance" / "endpoint.json"
    handoff.parent.mkdir()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv(HANDOFF_FILE_ENV, str(handoff))
    monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)

    before = len(_labelled(base_url, root))
    sup = _supervisor(config_dir, root)
    sup._project_appliance_handoff(port)

    obj = json.loads(handoff.read_text())
    assert obj["schema"] == 1 and obj["host"] == "127.0.0.1" and obj["port"] == port
    mint_token, mint_tenant = obj["mint_token"], obj["mint_tenant"]
    assert mint_tenant == ROOT_TENANT
    assert mint_token != root and root not in handoff.read_text(), "the root bearer never leaves the distro"
    assert "token" not in obj

    issued = [r for r in _labelled(base_url, root)
              if r.get("token_hash") == hashlib.sha256(mint_token.encode()).hexdigest()]
    assert len(issued) == 1 and issued[0].get("scope") == "mint-locked", issued

    # Refused on a data route and on the token-admin surface.
    assert _get(base_url, _DATA_ROUTE, mint_token).status_code in (401, 403)
    admin = httpx.post(
        base_url + "/v1/service-tokens/list", json={"tenant": ROOT_TENANT},
        headers={"Authorization": f"Bearer {mint_token}"}, timeout=10,
    )
    assert admin.status_code in (401, 403)

    # The Windows side's path: DataTokenManager mints a data token that works.
    from nexus.db.data_token import DataTokenManager  # noqa: PLC0415 — the Windows side, imported where it acts

    manager = DataTokenManager(
        mint_credential=lambda: mint_token, mint_tenant=lambda: mint_tenant,
        config_dir=tmp_path / "windows-client",
    )
    (tmp_path / "windows-client").mkdir()
    data_token = manager.bearer_for(base_url, mint_tenant)
    assert data_token and data_token not in (mint_token, root)
    assert _get(base_url, _DATA_ROUTE, data_token).status_code == 200

    # A second publish reuses the persisted credential: no second issue, same bytes.
    first = handoff.read_bytes()
    sup2 = _supervisor(config_dir, root)
    sup2._project_appliance_handoff(port)
    assert handoff.read_bytes() == first
    assert len(_labelled(base_url, root)) == before + 1, "issued exactly once across both publishes"
    assert (config_dir / MINT_CREDENTIAL_FILENAME).exists()


def test_unset_env_issues_and_writes_nothing(engine, tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(HANDOFF_FILE_ENV, raising=False)
    base_url, root = engine["base_url"], engine["bearer"]
    before = len(_labelled(base_url, root))
    _supervisor(tmp_path, root)._project_appliance_handoff(urlparse(base_url).port)
    assert list(tmp_path.iterdir()) == []
    assert len(_labelled(base_url, root)) == before


def test_an_unwritable_handoff_path_is_logged_not_fatal(engine, tmp_path, monkeypatch) -> None:
    base_url, root = engine["base_url"], engine["bearer"]
    monkeypatch.setenv(HANDOFF_FILE_ENV, str(tmp_path / "missing-dir" / "endpoint.json"))
    with structlog.testing.capture_logs() as logs:
        _supervisor(tmp_path, root)._project_appliance_handoff(urlparse(base_url).port)
    warned = [e for e in logs if e.get("event") == "appliance_handoff_not_written"]
    assert warned and "missing-dir" in warned[0]["path"]


def test_a_revoked_credential_takes_the_handoff_down_and_is_not_reissued(
    engine, tmp_path, monkeypatch,
) -> None:
    base_url, root = engine["base_url"], engine["bearer"]
    port = urlparse(base_url).port
    handoff = tmp_path / "appliance" / "endpoint.json"
    handoff.parent.mkdir()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv(HANDOFF_FILE_ENV, str(handoff))
    monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)

    _supervisor(config_dir, root)._project_appliance_handoff(port)
    mint_token = json.loads(handoff.read_text())["mint_token"]
    before = len(_labelled(base_url, root))
    revoked = httpx.post(
        base_url + "/v1/service-tokens/revoke",
        json={"selector": hashlib.sha256(mint_token.encode()).hexdigest()},
        headers={"Authorization": f"Bearer {root}"}, timeout=10,
    )
    assert revoked.status_code == 200, revoked.text

    with structlog.testing.capture_logs() as logs:
        _supervisor(config_dir, root)._project_appliance_handoff(port)   # a new lifetime checks on start
    assert not handoff.exists(), "a dead credential takes the handoff down"
    assert any(e.get("event") == "appliance_mint_credential_dead" and e.get("state") == "revoked" for e in logs)
    assert len(_labelled(base_url, root)) == before, "revocation stays revoked: nothing re-issued"
