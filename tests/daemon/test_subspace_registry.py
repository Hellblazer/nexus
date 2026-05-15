# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for daemon-side subspace registry — RDR-112 P1.5 (nexus-x98k).

Covers:
  (a) Valid YAML registers successfully and emits subspace-added structured log.
  (b) Reserved-prefix collision: names starting with ``tuples/`` or ``daemon/``
      are rejected with ``ReservedPrefixError``.
  (c) Duplicate name rejected with ``DuplicateSubspaceError``.
  (d) ``hello_ack`` ``registry_digest`` field changes after a new subspace is added.
  (e) Fixture covering all seven RDR-111 hook-event subspaces — they must all
      parse and register successfully.

All tests that start a daemon use ``port=0``, ``tmp_path`` config_dir, and a
real SQLite tuples.db (no mocks). The RegistryStore is wired into T2Daemon
and persists to ``tuples.db``.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import structlog.testing

from nexus.daemon.t2_daemon import (
    DAEMON_PROTOCOL_VERSION,
    T2Daemon,
    read_frame,
    write_frame,
)


# ---------------------------------------------------------------------------
# Minimal valid YAML template (reused across tests)
# ---------------------------------------------------------------------------

_VALID_YAML_TEMPLATE = """\
name: {name}
tier: project
content_type: text
embed_from: content
dimensions:
  actor:     {{type: string, required: true}}
  session:   {{type: string, required: true}}
take:
  enabled: true
  mode: semantic
  floor: 0.50
  margin: 0.05
  default_lease_seconds: 300
read:
  default_floor: 0.40
  default_n: 5
tiers: [project]
retention_seconds: 86400
"""

# Seven hook-event subspace names from RDR-111 §Proposed Solution lines ~387-393
_HOOK_EVENT_SUBSPACES: list[str] = [
    "hook_events/tool_call_intent",
    "hook_events/tool_call_completed",
    "hook_events/agent_completed",
    "hook_events/assistant_turn_ended",
    "hook_events/user_prompt",
    "hook_events/session_lifecycle",
    "hook_events/notification",
]

# Minimal valid YAML bodies for each hook-event subspace (dimensions include
# all required fields from RDR-111 §Step 1; the schema check validates shape
# not exact field names, so a minimal conformant YAML is sufficient here).
_HOOK_EVENT_YAML_TEMPLATE = """\
name: {name}
tier: project
content_type: json
embed_from: match_text
dimensions:
  actor:      {{type: string, required: true}}
  session:    {{type: string, required: true}}
  project:    {{type: string, required: true}}
  timestamp:  {{type: string, required: true}}
  tool:       {{type: string, required: false}}
  workflow:   {{type: string, required: false}}
  intent:     {{type: string, required: false}}
  priority:   {{type: string, required: false}}
take:
  enabled: false
  mode: semantic
  floor: 0.50
  margin: 0.05
  default_lease_seconds: 0
read:
  default_floor: 0.40
  default_n: 10
tiers: [project]
retention_seconds: 604800
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _connect_uds(
    uds_path: Path,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_unix_connection(str(uds_path))


async def _handshake(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    version: str = DAEMON_PROTOCOL_VERSION,
) -> dict[str, Any]:
    write_frame(writer, {"op": "hello", "protocol_version": version})
    await writer.drain()
    return await read_frame(reader)


async def _rpc_uds(
    uds_path: Path,
    op: str,
    args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Open a fresh UDS connection, handshake, issue one RPC, return response."""
    reader, writer = await _connect_uds(uds_path)
    try:
        ack = await _handshake(reader, writer)
        assert ack.get("op") == "hello_ack", f"handshake failed: {ack}"
        write_frame(writer, {"op": op, "args": args or {}})
        await writer.drain()
        return await read_frame(reader)
    finally:
        writer.close()
        await writer.wait_closed()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def config_dir(tmp_path: Path) -> Path:
    d = tmp_path / "config" / "nexus"
    d.mkdir(parents=True)
    return d


@pytest_asyncio.fixture()
async def registry_daemon(config_dir: Path) -> Any:
    """Real daemon with RegistryStore wired in (no T2Database domain stores)."""
    from nexus.daemon.subspace_registry import RegistryStore

    tuples_db = config_dir / "tuples.db"
    store = RegistryStore(tuples_db_path=tuples_db)
    daemon = T2Daemon(
        config_dir=config_dir,
        tuples_db_path=tuples_db,
        registry_store=store,
    )
    await daemon.start()
    yield daemon
    await daemon.stop()


# ---------------------------------------------------------------------------
# (a) Valid YAML registers and emits subspace-added event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_yaml_registers_successfully(registry_daemon: T2Daemon) -> None:
    """A well-formed YAML subspace schema registers without error."""
    yaml_str = _VALID_YAML_TEMPLATE.format(name="test_ns/my_subspace")
    resp = await _rpc_uds(
        registry_daemon.uds_path,
        "subspace_add",
        {"yaml": yaml_str},
    )
    assert "error" not in resp, f"Unexpected error: {resp}"
    result = resp.get("result", {})
    assert result.get("name") == "test_ns/my_subspace"
    assert "digest" in result


@pytest.mark.asyncio
async def test_valid_yaml_emits_structured_log(
    config_dir: Path,
) -> None:
    """Successful registration emits a ``daemon/t2/lifecycle`` structured-log event.

    The conftest configures structlog at WARNING level, which means INFO logs
    are suppressed. We patch structlog's bound-logger wrapper at the module
    level to use DEBUG and capture via ``capture_logs()`` so the lifecycle
    event is observable.
    """
    import logging
    import structlog

    from nexus.daemon.subspace_registry import RegistryStore

    yaml_str = _VALID_YAML_TEMPLATE.format(name="log_test/subspace")
    db_path = config_dir / "log_test_registry.db"
    store = RegistryStore(tuples_db_path=db_path)

    # Temporarily configure structlog to DEBUG so INFO events are not filtered
    # before capture_logs() can intercept them.
    original_config = structlog.get_config()
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
    )
    try:
        with structlog.testing.capture_logs() as logs:
            result = store.add(yaml_str)
    finally:
        structlog.configure(**original_config)

    assert result["name"] == "log_test/subspace"

    lifecycle_events = [
        e for e in logs
        if e.get("event") == "daemon/t2/lifecycle"
        and e.get("op") == "subspace-added"
    ]
    assert lifecycle_events, (
        f"Expected 'daemon/t2/lifecycle' with op='subspace-added' in logs. "
        f"Got: {logs}"
    )
    assert lifecycle_events[0].get("name") == "log_test/subspace"


# ---------------------------------------------------------------------------
# (b) Reserved-prefix rejection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reserved_prefix_tuples_rejected(registry_daemon: T2Daemon) -> None:
    """Name starting with ``tuples/`` is rejected with ReservedPrefixError."""
    yaml_str = _VALID_YAML_TEMPLATE.format(name="tuples/my_space")
    resp = await _rpc_uds(
        registry_daemon.uds_path,
        "subspace_add",
        {"yaml": yaml_str},
    )
    assert "error" in resp, f"Expected error for reserved prefix: {resp}"
    err = resp["error"]
    msg = err.get("message", "") if isinstance(err, dict) else str(err)
    assert "tuples/" in msg or "reserved" in msg.lower(), (
        f"Error must mention reserved prefix or 'tuples/': {msg!r}"
    )
    type_name = err.get("type", "") if isinstance(err, dict) else ""
    assert "ReservedPrefixError" in type_name or "reserved" in msg.lower(), (
        f"Expected ReservedPrefixError type: {err}"
    )


@pytest.mark.asyncio
async def test_reserved_prefix_daemon_rejected(registry_daemon: T2Daemon) -> None:
    """Name starting with ``daemon/`` is rejected with ReservedPrefixError."""
    yaml_str = _VALID_YAML_TEMPLATE.format(name="daemon/lifecycle_test")
    resp = await _rpc_uds(
        registry_daemon.uds_path,
        "subspace_add",
        {"yaml": yaml_str},
    )
    assert "error" in resp, f"Expected error for reserved prefix: {resp}"
    err = resp["error"]
    msg = err.get("message", "") if isinstance(err, dict) else str(err)
    assert "daemon/" in msg or "reserved" in msg.lower(), (
        f"Error must mention reserved prefix or 'daemon/': {msg!r}"
    )


# ---------------------------------------------------------------------------
# (c) Duplicate name rejected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_name_rejected(registry_daemon: T2Daemon) -> None:
    """Registering the same subspace name twice raises DuplicateSubspaceError."""
    yaml_str = _VALID_YAML_TEMPLATE.format(name="dup_test/space")

    # First add succeeds
    resp1 = await _rpc_uds(
        registry_daemon.uds_path,
        "subspace_add",
        {"yaml": yaml_str},
    )
    assert "error" not in resp1, f"First add should succeed: {resp1}"

    # Second add with same name must fail
    resp2 = await _rpc_uds(
        registry_daemon.uds_path,
        "subspace_add",
        {"yaml": yaml_str},
    )
    assert "error" in resp2, f"Expected error for duplicate: {resp2}"
    err = resp2["error"]
    msg = err.get("message", "") if isinstance(err, dict) else str(err)
    type_name = err.get("type", "") if isinstance(err, dict) else ""
    assert "DuplicateSubspaceError" in type_name or "duplicate" in msg.lower(), (
        f"Expected DuplicateSubspaceError: {err}"
    )


# ---------------------------------------------------------------------------
# (d) hello_ack registry_digest changes after add
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hello_ack_registry_digest_changes_after_add(
    registry_daemon: T2Daemon,
) -> None:
    """The ``registry_digest`` in ``hello_ack`` changes after a subspace is added."""
    # Capture initial digest via a fresh handshake
    reader1, writer1 = await _connect_uds(registry_daemon.uds_path)
    try:
        ack1 = await _handshake(reader1, writer1)
        digest_before = ack1.get("registry_digest")
    finally:
        writer1.close()
        await writer1.wait_closed()

    assert digest_before is not None, (
        f"hello_ack must include registry_digest; got: {ack1}"
    )

    # Add a new subspace
    yaml_str = _VALID_YAML_TEMPLATE.format(name="digest_test/space")
    resp = await _rpc_uds(
        registry_daemon.uds_path,
        "subspace_add",
        {"yaml": yaml_str},
    )
    assert "error" not in resp, f"Add failed: {resp}"

    # Capture new digest via a fresh handshake (new connection = fresh ack)
    reader2, writer2 = await _connect_uds(registry_daemon.uds_path)
    try:
        ack2 = await _handshake(reader2, writer2)
        digest_after = ack2.get("registry_digest")
    finally:
        writer2.close()
        await writer2.wait_closed()

    assert digest_after is not None, (
        f"hello_ack must include registry_digest after add; got: {ack2}"
    )
    assert digest_before != digest_after, (
        f"Digest must change after adding a subspace: before={digest_before!r}, "
        f"after={digest_after!r}"
    )


# ---------------------------------------------------------------------------
# (e) All seven RDR-111 hook-event subspaces parse and register
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_seven_hook_event_subspaces_register(
    registry_daemon: T2Daemon,
) -> None:
    """All seven RDR-111 hook-event subspace names parse and register without error."""
    registered = []
    for name in _HOOK_EVENT_SUBSPACES:
        yaml_str = _HOOK_EVENT_YAML_TEMPLATE.format(name=name)
        resp = await _rpc_uds(
            registry_daemon.uds_path,
            "subspace_add",
            {"yaml": yaml_str},
        )
        assert "error" not in resp, (
            f"hook-event subspace {name!r} failed to register: {resp}"
        )
        result = resp.get("result", {})
        assert result.get("name") == name, (
            f"Expected name={name!r}, got: {result.get('name')!r}"
        )
        registered.append(result.get("name"))

    assert len(registered) == 7, f"Expected 7 registered subspaces, got: {registered}"


# ---------------------------------------------------------------------------
# RegistryStore unit tests (no daemon)
# ---------------------------------------------------------------------------


def _make_store(tmp_path: Path):
    from nexus.daemon.subspace_registry import RegistryStore
    db = tmp_path / "tuples.db"
    return RegistryStore(tuples_db_path=db)


def test_registry_store_add_and_digest(tmp_path: Path) -> None:
    """RegistryStore.add() returns name + digest; digest() changes after each add."""
    store = _make_store(tmp_path)
    d0 = store.digest()

    yaml_str = _VALID_YAML_TEMPLATE.format(name="unit/test_a")
    result = store.add(yaml_str)
    assert result["name"] == "unit/test_a"
    assert len(result["digest"]) == 64  # sha256 hex

    d1 = store.digest()
    assert d0 != d1, "digest must change after first add"

    yaml_str2 = _VALID_YAML_TEMPLATE.format(name="unit/test_b")
    store.add(yaml_str2)
    d2 = store.digest()
    assert d1 != d2, "digest must change after second add"


def test_registry_store_reserved_prefix_tuples(tmp_path: Path) -> None:
    from nexus.daemon.subspace_registry import ReservedPrefixError
    store = _make_store(tmp_path)
    yaml_str = _VALID_YAML_TEMPLATE.format(name="tuples/forbidden")
    with pytest.raises(ReservedPrefixError):
        store.add(yaml_str)


def test_registry_store_reserved_prefix_daemon(tmp_path: Path) -> None:
    from nexus.daemon.subspace_registry import ReservedPrefixError
    store = _make_store(tmp_path)
    yaml_str = _VALID_YAML_TEMPLATE.format(name="daemon/forbidden")
    with pytest.raises(ReservedPrefixError):
        store.add(yaml_str)


def test_registry_store_duplicate_rejected(tmp_path: Path) -> None:
    from nexus.daemon.subspace_registry import DuplicateSubspaceError
    store = _make_store(tmp_path)
    yaml_str = _VALID_YAML_TEMPLATE.format(name="dup/space")
    store.add(yaml_str)
    with pytest.raises(DuplicateSubspaceError):
        store.add(yaml_str)


def test_registry_store_persists_to_sqlite(tmp_path: Path) -> None:
    """After add(), the subspace is stored in the SQLite table."""
    store = _make_store(tmp_path)
    yaml_str = _VALID_YAML_TEMPLATE.format(name="persist/check")
    store.add(yaml_str)

    db_path = tmp_path / "tuples.db"
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT name, yaml FROM subspace_registry WHERE name = ?",
            ("persist/check",),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None, "Row must be persisted to SQLite"
    assert row[0] == "persist/check"
    assert "persist/check" in row[1]


def test_registry_store_invalid_yaml_rejected(tmp_path: Path) -> None:
    """Malformed YAML raises SubspaceValidationError."""
    from nexus.daemon.subspace_registry import SubspaceValidationError
    store = _make_store(tmp_path)
    with pytest.raises(SubspaceValidationError):
        store.add("::this is: not: valid: yaml: content: [unclosed")


def test_registry_store_invalid_schema_rejected(tmp_path: Path) -> None:
    """YAML that fails the SubspaceSchema JSON Schema raises SubspaceValidationError."""
    from nexus.daemon.subspace_registry import SubspaceValidationError
    store = _make_store(tmp_path)
    # Missing required fields (name present but nothing else)
    with pytest.raises(SubspaceValidationError):
        store.add("name: incomplete/schema\ntier: project\n")
