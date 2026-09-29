# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ijue9.29 (RDR-218 P2.1a): the supervisor honours an appliance-only
fixed port, NX_SERVICE_FIXED_PORT.

Decision of record: T2 nexus/rdr-218-appliance-endpoint-handoff-decision
[27808] section 2. Unset, the supervisor allocates an ephemeral port exactly as
before (local Linux and macOS never set it). Set, it uses that port or fails
loudly: a collision is a StorageServiceStartError naming the port and the
remedy, never a silent fall back to an ephemeral port (that is Gap 1 again).

Real sockets throughout: a free port is taken by binding port 0, a collision
by holding a listening socket on 127.0.0.1.
"""
from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

import pytest

import nexus.daemon.storage_service_daemon as ssd
from nexus.daemon.storage_service_daemon import (
    APPLIANCE_DEFAULT_PORT,
    FIXED_PORT_ENV,
    StorageServiceStartError,
    StorageServiceSupervisor,
)

_CREDS = {
    "NX_DB_URL": "jdbc:...", "NX_DB_USER": "svc", "NX_DB_PASS": "pass",
    "NX_DB_ADMIN_URL": "jdbc:...", "NX_DB_ADMIN_USER": "admin",
    "NX_DB_ADMIN_PASS": "adminpass", "PG_PORT": "15432", "PG_DATA": "/tmp/pgdata",
    "NX_SERVICE_TOKEN": "root-token-from-creds-deadbeef",
}


def _supervisor(tmp_path: Path) -> StorageServiceSupervisor:
    return StorageServiceSupervisor(
        config_dir=tmp_path,
        binary_path=Path("/fake/nexus-service"),
        pg_port=15432,
        service_port=0,
        creds=dict(_CREDS),
        engine_liveness_scan=lambda _c, _b: [],
    )


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Spawns:
    """Records what reached the process boundary and whether an ephemeral port was asked for."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.envs: list[dict[str, str]] = []
        self.ephemeral_calls = 0
        real_allocate = ssd._allocate_free_port

        def _popen(cmd: Any, env: dict[str, str] | None = None, **kw: Any) -> Any:
            self.envs.append(dict(env or {}))

            class _P:
                pid = 43210
            return _P()

        def _allocate(*a: Any, **kw: Any) -> int:
            self.ephemeral_calls += 1
            return real_allocate(*a, **kw)

        monkeypatch.setattr(ssd, "_popen", _popen)
        monkeypatch.setattr(ssd, "_allocate_free_port", _allocate)


def test_the_appliance_default_port_is_the_recorded_one() -> None:
    # ijue9.5's env-file test asserts equality with this constant, never a retyped literal.
    assert APPLIANCE_DEFAULT_PORT == 29517
    assert FIXED_PORT_ENV == "NX_SERVICE_FIXED_PORT"


@pytest.mark.parametrize("unset", ["absent", "empty"])
def test_unset_keeps_the_ephemeral_allocation(tmp_path, monkeypatch, unset) -> None:
    if unset == "absent":
        monkeypatch.delenv(FIXED_PORT_ENV, raising=False)
    else:
        monkeypatch.setenv(FIXED_PORT_ENV, "")
    spawns = _Spawns(monkeypatch)
    _proc, port = _supervisor(tmp_path)._spawn_service()
    assert spawns.ephemeral_calls == 1, "local Linux/macOS behaviour must be unchanged"
    assert spawns.envs[0]["NX_SERVICE_PORT"] == str(port)


def test_a_free_fixed_port_reaches_the_engine(tmp_path, monkeypatch) -> None:
    fixed = _free_port()
    monkeypatch.setenv(FIXED_PORT_ENV, str(fixed))
    spawns = _Spawns(monkeypatch)
    _proc, port = _supervisor(tmp_path)._spawn_service()
    assert port == fixed
    assert spawns.envs[0]["NX_SERVICE_PORT"] == str(fixed)
    assert spawns.ephemeral_calls == 0


def test_a_held_fixed_port_fails_loud_and_spawns_nothing(tmp_path, monkeypatch) -> None:
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    held = holder.getsockname()[1]
    try:
        monkeypatch.setenv(FIXED_PORT_ENV, str(held))
        spawns = _Spawns(monkeypatch)
        sup = _supervisor(tmp_path)
        with pytest.raises(StorageServiceStartError) as exc:
            sup._spawn_service()
        msg = str(exc.value)
        assert str(held) in msg
        assert FIXED_PORT_ENV in msg
        assert "/etc/nexus/appliance.env" in msg, "the error names the remedy"
        assert spawns.envs == [], "no process may be spawned on a collision"
        assert spawns.ephemeral_calls == 0, "never fall back to an ephemeral port"
    finally:
        holder.close()


@pytest.mark.parametrize("bad", ["abc", "0", "1023", "65536", "-1", "29517.0", " 29517x"])
def test_an_invalid_fixed_port_is_refused_at_construction(tmp_path, monkeypatch, bad) -> None:
    monkeypatch.setenv(FIXED_PORT_ENV, bad)
    with pytest.raises(StorageServiceStartError) as exc:
        _supervisor(tmp_path)
    assert FIXED_PORT_ENV in str(exc.value)
    assert repr(bad) in str(exc.value) or bad.strip() in str(exc.value)


@pytest.mark.parametrize("edge", ["1024", "65535", " 29517 "])
def test_the_range_edges_and_whitespace_are_accepted(tmp_path, monkeypatch, edge) -> None:
    monkeypatch.setenv(FIXED_PORT_ENV, edge)
    _supervisor(tmp_path)  # must not raise
