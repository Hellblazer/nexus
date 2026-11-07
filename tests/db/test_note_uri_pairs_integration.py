# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-bivz3.28: the RDR-231 pair query against a real migrated schema, through a real psql.

The unit file ``tests/test_service_note_uri_pairs.py`` pins the role derivation, the password handling and the
messages with a recorder in place of psql. This file pins that ``PAIR_SQL`` finds exactly the pairs the changeset's
guard would halt on, by seeding the substrate's catalog (superuser, so row-level security does not hide the rows)
and running the real verb against the substrate's cluster. Controls that must NOT be listed are seeded beside the
pairs: a chroma:// row with no twin, a pair whose chroma side is tombstoned, a pair in a file-routed collection.
"""
from __future__ import annotations

import socket
import subprocess
import uuid
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.db import note_uri_pairs
from nexus.db.note_uri_pairs import NoteUriPairsError, list_note_uri_pairs
from nexus.db.pg_provision import PgBinaries

pytestmark = [pytest.mark.integration]


@pytest.fixture(scope="module")
def state() -> dict:
    from tests._engine_substrate import ensure_engine  # noqa: PLC0415 — substrate boots lazily

    return ensure_engine()


def _psql(state: dict, sql: str) -> None:
    proc = subprocess.run(
        [str(Path(state["pg_bin"]) / "psql"), "-X", "-h", "127.0.0.1", "-p", str(state["pg_port"]),
         "-U", state["pg_user"], "-d", state["pg_dbname"], "-v", "ON_ERROR_STOP=1", "-c", sql],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"psql failed: {proc.stderr}\nSQL: {sql}"


@pytest.fixture
def cfg(state, tmp_path, monkeypatch) -> Path:
    """A config dir whose pg_credentials names the substrate's cluster. The substrate is trust-auth, so no password."""
    (tmp_path / "pg_credentials").write_text(f"PG_PORT={state['pg_port']}\n", encoding="utf-8")
    monkeypatch.setenv("USER", state["pg_user"])  # the POSIX superuser derivation (bootstrap_superuser)
    return tmp_path


@pytest.fixture
def seeded(state):
    """Seed pairs and controls under one throwaway tenant; remove them afterwards. Yields the tenant and tumblers."""
    tenant = f"pairs-it-{uuid.uuid4().hex[:10]}"
    rows = {
        # name: (tumbler, source_uri, deleted)
        "pair_chroma": ("t.1", "chroma://knowledge__pairs-it/note-a", False),
        "pair_nx": ("t.2", "nx-note://knowledge__pairs-it/note-a", False),
        "export_chroma": ("t.3", "nxexp://tgt/chroma://knowledge__pairs-it/note-b", False),
        "export_nx": ("t.4", "nxexp://tgt/nx-note://knowledge__pairs-it/note-b", False),
        "lone_chroma": ("t.5", "chroma://knowledge__pairs-it/note-c", False),
        "dead_chroma": ("t.6", "chroma://knowledge__pairs-it/note-d", True),
        "dead_twin": ("t.7", "nx-note://knowledge__pairs-it/note-d", False),
        "file_chroma": ("t.8", "chroma://docs__pairs-it/note-e", False),
        "file_twin": ("t.9", "nx-note://docs__pairs-it/note-e", False),
    }
    values = ", ".join(
        f"('{tenant}', '{tumbler}', 'pairs-it {name}', '{uri}', {'now()' if dead else 'NULL'})"
        for name, (tumbler, uri, dead) in rows.items()
    )
    _psql(state, f"INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, source_uri, deleted_at) VALUES {values}")
    try:
        yield tenant, {name: tumbler for name, (tumbler, _u, _d) in rows.items()}
    finally:
        _psql(state, f"DELETE FROM nexus.catalog_documents WHERE tenant_id = '{tenant}'")


def test_the_verb_lists_exactly_the_live_pairs_the_guard_would_halt_on(state, cfg, seeded):
    tenant, t = seeded
    report = list_note_uri_pairs(cfg, platform="linux", bins=PgBinaries.from_dir(Path(state["pg_bin"])), dbname=state["pg_dbname"])
    mine = {(p.chroma_row, p.nx_note_row) for p in report.pairs if p.tenant_id == tenant}
    assert mine == {(t["pair_chroma"], t["pair_nx"]), (t["export_chroma"], t["export_nx"])}
    listed = {p.chroma_row: p for p in report.pairs if p.tenant_id == tenant}
    assert listed[t["pair_chroma"]].source_uri == "chroma://knowledge__pairs-it/note-a"
    assert listed[t["export_chroma"]].source_uri == "nxexp://tgt/chroma://knowledge__pairs-it/note-b"
    assert report.role == state["pg_user"]
    # the engine ran its migrations on this database, so the backfill changeset has a recorded state
    assert report.changeset == "EXECUTED", report.changeset


def test_after_a_merge_the_query_returns_no_row_for_the_tenant(state, cfg, seeded):
    """The runbook's step 2 ends with 'run the step 1 query again: it must return no rows'."""
    tenant, t = seeded
    run = lambda: list_note_uri_pairs(cfg, platform="linux", bins=PgBinaries.from_dir(Path(state["pg_bin"])), dbname=state["pg_dbname"])  # noqa: E731
    assert any(p.tenant_id == tenant for p in run().pairs)
    _psql(state, f"UPDATE nexus.catalog_documents SET deleted_at = now() WHERE tenant_id = '{tenant}' AND tumbler IN "
                 f"('{t['pair_chroma']}', '{t['export_chroma']}')")
    assert not any(p.tenant_id == tenant for p in run().pairs)


def test_cli_end_to_end_through_the_default_runner(state, cfg, seeded, monkeypatch):
    """The real verb: discover_pg_binaries via NEXUS_PG_BIN, the real psql, the printed table."""
    tenant, t = seeded
    monkeypatch.setenv("NEXUS_PG_BIN", str(state["pg_bin"]))
    # The CLI has no database seam (a real cluster's database is `nexus`); the substrate's is another name.
    real = note_uri_pairs.list_note_uri_pairs
    monkeypatch.setattr(note_uri_pairs, "list_note_uri_pairs", lambda d, **kw: real(d, dbname=state["pg_dbname"], **kw))
    result = CliRunner().invoke(main, ["daemon", "service", "note-uri-pairs", "--config-dir", str(cfg)])
    assert result.exit_code == 0, result.output
    assert f"{tenant}\t{t['pair_chroma']}\t{t['pair_nx']}\tchroma://knowledge__pairs-it/note-a" in result.output
    assert t["lone_chroma"] not in result.output and t["dead_chroma"] not in result.output
    assert t["file_chroma"] not in result.output
    assert "Changeset note-uri-001-1: EXECUTED" in result.output


def test_a_cluster_that_is_not_listening_is_a_message(state, tmp_path, monkeypatch):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead_port = s.getsockname()[1]
    (tmp_path / "pg_credentials").write_text(f"PG_PORT={dead_port}\n", encoding="utf-8")
    monkeypatch.setenv("USER", state["pg_user"])
    with pytest.raises(NoteUriPairsError, match="not accepting connections"):
        list_note_uri_pairs(
            tmp_path, platform="linux", bins=PgBinaries.from_dir(Path(state["pg_bin"])), dbname=state["pg_dbname"]
        )
