# SPDX-License-Identifier: AGPL-3.0-or-later
"""Daemon-startup builtin-seed tests -- RDR-112 nexus-me9y.

Covers the ``RegistryStore.seed_from_builtin_dir`` path and its wiring
into ``T2Daemon.start()``:

  (a) Startup seed populates every builtin YAML under
      nx/tuplespace/builtin/ (and its hooks/ + bindings/ subdirs).
  (b) Re-seeding the same on-disk YAML is a no-op (idempotent).
  (c) Bumping a YAML schema (different bytes) updates the stored row.
  (d) Seed bypasses the reserved-prefix gate (builtins legitimately use
      reserved namespaces).
  (e) The daemon ``start()`` call seeds before sockets bind: clients
      observe the seeded names on first handshake.
"""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
import pytest_asyncio

from nexus.daemon.subspace_registry import RegistryStore
from nexus.daemon.t2_daemon import T2Daemon


# ---------------------------------------------------------------------------
# Minimal YAML bodies for synthetic builtin dirs
# ---------------------------------------------------------------------------

# A top-level builtin (reserved prefix: tasks/)
_TASKS_YAML_V1 = """\
name: tasks/<project>
tier: project
content_type: text
embed_from: content
dimensions:
  status:   { type: enum, values: [open, done], required: true }
  priority: { type: enum, values: [P0, P1, P2], required: true }
take:
  enabled: true
  mode: semantic
  floor: 0.50
  margin: 0.05
read:
  default_floor: 0.40
  default_n: 5
tiers: [project]
retention_seconds: 86400
"""

_TASKS_YAML_V2_BUMPED = """\
name: tasks/<project>
tier: project
content_type: text
embed_from: content
dimensions:
  status:   { type: enum, values: [open, done], required: true }
  priority: { type: enum, values: [P0, P1, P2], required: true }
  bumped:   { type: bool, required: false }
take:
  enabled: true
  mode: semantic
  floor: 0.55
  margin: 0.05
read:
  default_floor: 0.40
  default_n: 5
tiers: [project]
retention_seconds: 86400
"""

# A hooks/ subdir builtin
_HOOK_EVENTS_YAML = """\
name: hook_events/tool_call_completed
tier: project
content_type: json
embed_from: match_text
dimensions:
  actor:   { type: string, required: true }
  session: { type: string, required: true }
take:
  enabled: false
  mode: semantic
  floor: 0.50
  margin: 0.05
read:
  default_floor: 0.40
  default_n: 10
tiers: [project]
retention_seconds: 604800
"""

# A bindings/ subdir builtin
_BINDINGS_YAML = """\
name: bindings/<profile>
tier: project
content_type: text
embed_from: content
dimensions:
  profile:    { type: string, required: true }
  enabled:    { type: bool, required: true }
  created_by: { type: string, required: true }
take:
  enabled: false
  mode: semantic
  floor: 0.50
  margin: 0.05
read:
  default_floor: 0.30
  default_n: 50
tiers: [project]
retention_seconds: 0
"""


def _make_builtin_dir(root: Path) -> Path:
    """Build a synthetic nx/tuplespace/builtin/ tree under *root*."""
    builtin = root / "builtin"
    builtin.mkdir(parents=True)
    (builtin / "tasks.yml").write_text(_TASKS_YAML_V1)

    hooks = builtin / "hooks"
    hooks.mkdir()
    (hooks / "hook_events_tool_call_completed.yml").write_text(_HOOK_EVENTS_YAML)

    bindings = builtin / "bindings"
    bindings.mkdir()
    (bindings / "bindings.yml").write_text(_BINDINGS_YAML)
    return builtin


# ---------------------------------------------------------------------------
# (a) seed populates every builtin
# ---------------------------------------------------------------------------


def test_seed_populates_top_level_and_subdirs(tmp_path: Path) -> None:
    """Seed picks up YAML at the top level, plus hooks/ and bindings/ subdirs."""
    builtin = _make_builtin_dir(tmp_path)
    store = RegistryStore(tuples_db_path=tmp_path / "tuples.db")

    written = store.seed_from_builtin_dir(builtin)
    assert written == 3, (
        f"Expected 3 rows written on first seed, got {written}"
    )

    conn = sqlite3.connect(str(tmp_path / "tuples.db"))
    try:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM subspace_registry ORDER BY name"
            ).fetchall()
        }
    finally:
        conn.close()

    assert names == {
        "tasks/<project>",
        "hook_events/tool_call_completed",
        "bindings/<profile>",
    }, f"Unexpected seeded names: {names}"


def test_seed_bypasses_reserved_prefix_gate(tmp_path: Path) -> None:
    """Builtin schemas use reserved prefixes (tasks/, hook_events/,
    bindings/) by design; the seed path must accept them. The same names
    submitted via ``add`` would raise ReservedPrefixError."""
    builtin = _make_builtin_dir(tmp_path)
    store = RegistryStore(tuples_db_path=tmp_path / "tuples.db")

    written = store.seed_from_builtin_dir(builtin)
    assert written == 3, "Seed must persist all three reserved-prefix builtins"

    # Sanity: the third-party path rejects the same names.
    from nexus.daemon.subspace_registry import ReservedPrefixError
    with pytest.raises(ReservedPrefixError):
        store.add(_TASKS_YAML_V1)


# ---------------------------------------------------------------------------
# (b) Re-seed is a no-op
# ---------------------------------------------------------------------------


def test_seed_is_idempotent_on_unchanged_yaml(tmp_path: Path) -> None:
    """Calling seed_from_builtin_dir twice with no YAML changes writes 0
    rows on the second call and leaves all schema_digests unchanged."""
    builtin = _make_builtin_dir(tmp_path)
    store = RegistryStore(tuples_db_path=tmp_path / "tuples.db")

    first = store.seed_from_builtin_dir(builtin)
    assert first == 3

    db_path = tmp_path / "tuples.db"
    conn = sqlite3.connect(str(db_path))
    try:
        digests_before = dict(
            conn.execute(
                "SELECT name, schema_digest FROM subspace_registry"
            ).fetchall()
        )
    finally:
        conn.close()

    second = store.seed_from_builtin_dir(builtin)
    assert second == 0, (
        f"Second seed of unchanged YAML must be a no-op, got {second} writes"
    )

    conn = sqlite3.connect(str(db_path))
    try:
        digests_after = dict(
            conn.execute(
                "SELECT name, schema_digest FROM subspace_registry"
            ).fetchall()
        )
    finally:
        conn.close()

    assert digests_before == digests_after


# ---------------------------------------------------------------------------
# (c) Bumped YAML triggers an update
# ---------------------------------------------------------------------------


def test_seed_updates_row_when_yaml_changes(tmp_path: Path) -> None:
    """Bumping a builtin YAML on disk causes seed to UPDATE the stored row
    (digest changes, only that one name is counted as written)."""
    builtin = _make_builtin_dir(tmp_path)
    store = RegistryStore(tuples_db_path=tmp_path / "tuples.db")

    first = store.seed_from_builtin_dir(builtin)
    assert first == 3

    db_path = tmp_path / "tuples.db"
    conn = sqlite3.connect(str(db_path))
    try:
        digest_before = conn.execute(
            "SELECT schema_digest FROM subspace_registry WHERE name = ?",
            ("tasks/<project>",),
        ).fetchone()[0]
    finally:
        conn.close()

    # Bump the on-disk YAML
    (builtin / "tasks.yml").write_text(_TASKS_YAML_V2_BUMPED)

    second = store.seed_from_builtin_dir(builtin)
    assert second == 1, (
        f"Only the bumped row should be rewritten, got {second}"
    )

    conn = sqlite3.connect(str(db_path))
    try:
        digest_after = conn.execute(
            "SELECT schema_digest FROM subspace_registry WHERE name = ?",
            ("tasks/<project>",),
        ).fetchone()[0]
    finally:
        conn.close()

    assert digest_before != digest_after, (
        "Bumping the YAML must change the persisted schema_digest"
    )


def test_seed_missing_dir_raises(tmp_path: Path) -> None:
    """A non-existent builtin_dir is a programmer error, not a silent
    skip. seed_from_builtin_dir surfaces FileNotFoundError."""
    store = RegistryStore(tuples_db_path=tmp_path / "tuples.db")
    with pytest.raises(FileNotFoundError):
        store.seed_from_builtin_dir(tmp_path / "does-not-exist")


# ---------------------------------------------------------------------------
# (e) Daemon start() seeds before sockets bind
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def seeded_daemon(tmp_path: Path):
    """Daemon constructed with a synthetic builtin_dir; start() must seed
    before binding sockets."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    builtin = _make_builtin_dir(tmp_path)

    tuples_db = config_dir / "tuples.db"
    store = RegistryStore(tuples_db_path=tuples_db)
    daemon = T2Daemon(
        config_dir=config_dir,
        tuples_db_path=tuples_db,
        registry_store=store,
        builtin_dir=builtin,
    )
    await daemon.start()
    try:
        yield daemon, store, tuples_db
    finally:
        await daemon.stop()


@pytest.mark.asyncio
async def test_daemon_start_seeds_builtin_before_bind(seeded_daemon) -> None:
    """After T2Daemon.start() returns, the registry contains every builtin
    schema (seed ran before bind, so any client that handshakes sees the
    seeded state on first contact)."""
    _daemon, _store, tuples_db = seeded_daemon

    conn = sqlite3.connect(str(tuples_db))
    try:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM subspace_registry ORDER BY name"
            ).fetchall()
        }
    finally:
        conn.close()

    assert "tasks/<project>" in names
    assert "hook_events/tool_call_completed" in names
    assert "bindings/<profile>" in names


@pytest.mark.asyncio
async def test_daemon_restart_seed_is_idempotent(tmp_path: Path) -> None:
    """Starting, stopping, and re-starting a daemon over the same tuples.db
    leaves the registry unchanged (no duplicate rows, digests stable)."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    builtin = _make_builtin_dir(tmp_path)
    tuples_db = config_dir / "tuples.db"

    # First boot
    store1 = RegistryStore(tuples_db_path=tuples_db)
    daemon1 = T2Daemon(
        config_dir=config_dir,
        tuples_db_path=tuples_db,
        registry_store=store1,
        builtin_dir=builtin,
    )
    await daemon1.start()
    await daemon1.stop()

    conn = sqlite3.connect(str(tuples_db))
    try:
        digests_first = dict(
            conn.execute(
                "SELECT name, schema_digest FROM subspace_registry"
            ).fetchall()
        )
        count_first = conn.execute(
            "SELECT COUNT(*) FROM subspace_registry"
        ).fetchone()[0]
    finally:
        conn.close()

    # Second boot (simulated restart -- same on-disk YAML)
    store2 = RegistryStore(tuples_db_path=tuples_db)
    daemon2 = T2Daemon(
        config_dir=config_dir,
        tuples_db_path=tuples_db,
        registry_store=store2,
        builtin_dir=builtin,
    )
    await daemon2.start()
    await daemon2.stop()

    conn = sqlite3.connect(str(tuples_db))
    try:
        digests_second = dict(
            conn.execute(
                "SELECT name, schema_digest FROM subspace_registry"
            ).fetchall()
        )
        count_second = conn.execute(
            "SELECT COUNT(*) FROM subspace_registry"
        ).fetchone()[0]
    finally:
        conn.close()

    assert count_first == count_second == 3
    assert digests_first == digests_second
