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


NASTY = "knowledge__pairs-it/tab\there\nnewline\u2028ls\x85nel"


@pytest.fixture
def seeded(state):
    """Seed pairs and controls under two throwaway tenants; remove them afterwards. Yields (tenant, other, tumblers).

    One row per clause of ``PAIR_SQL`` that must discriminate (each is mutation-checked): the live-twin test, the live
    original, the tenant join, and each of the three file-routed prefixes, direct and export form.
    """
    tenant = f"pairs-it-{uuid.uuid4().hex[:10]}"
    other = tenant + "-b"
    rows = {
        # name: (tenant, tumbler, source_uri, deleted)
        "pair_chroma": (tenant, "t.1", "chroma://knowledge__pairs-it/note-a", False),
        "pair_nx": (tenant, "t.2", "nx-note://knowledge__pairs-it/note-a", False),
        "export_chroma": (tenant, "t.3", "nxexp://tgt/chroma://knowledge__pairs-it/note-b", False),
        "export_nx": (tenant, "t.4", "nxexp://tgt/nx-note://knowledge__pairs-it/note-b", False),
        "nasty_chroma": (tenant, "t.20", "chroma://" + NASTY, False),
        "nasty_nx": (tenant, "t.21", "nx-note://" + NASTY, False),
        # controls: none of these is a pair the guard halts on
        "lone_chroma": (tenant, "t.5", "chroma://knowledge__pairs-it/note-c", False),
        "dead_chroma": (tenant, "t.6", "chroma://knowledge__pairs-it/note-d", True),
        "dead_chroma_live_twin": (tenant, "t.7", "nx-note://knowledge__pairs-it/note-d", False),
        "live_chroma": (tenant, "t.10", "chroma://knowledge__pairs-it/note-f", False),
        "live_chroma_dead_twin": (tenant, "t.11", "nx-note://knowledge__pairs-it/note-f", True),
        "tenant_a_chroma": (tenant, "t.12", "chroma://knowledge__pairs-it/note-g", False),
        "tenant_b_twin": (other, "t.12", "nx-note://knowledge__pairs-it/note-g", False),
        "docs_chroma": (tenant, "t.8", "chroma://docs__pairs-it/note-e", False),
        "docs_twin": (tenant, "t.9", "nx-note://docs__pairs-it/note-e", False),
        "rdr_chroma": (tenant, "t.14", "chroma://rdr__pairs-it/note-e", False),
        "rdr_twin": (tenant, "t.15", "nx-note://rdr__pairs-it/note-e", False),
        "code_chroma": (tenant, "t.16", "chroma://code__pairs-it/note-e", False),
        "code_twin": (tenant, "t.17", "nx-note://code__pairs-it/note-e", False),
        "routed_export_chroma": (tenant, "t.18", "nxexp://tgt/chroma://docs__pairs-it/note-h", False),
        "routed_export_nx": (tenant, "t.19", "nxexp://tgt/nx-note://docs__pairs-it/note-h", False),
    }
    values = ", ".join(
        f"('{ten}', '{tumbler}', 'pairs-it {name}', $q${uri}$q$, {'now()' if dead else 'NULL'})"
        for name, (ten, tumbler, uri, dead) in rows.items()
    )
    _psql(state, f"INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, source_uri, deleted_at) VALUES {values}")
    try:
        yield tenant, other, {name: tumbler for name, (_t, tumbler, _u, _d) in rows.items()}
    finally:
        _psql(state, f"DELETE FROM nexus.catalog_documents WHERE tenant_id IN ('{tenant}', '{other}')")


def _list(state, cfg):
    return list_note_uri_pairs(cfg, platform="linux", bins=PgBinaries.from_dir(Path(state["pg_bin"])), dbname=state["pg_dbname"])


def test_the_verb_lists_exactly_the_live_pairs_the_guard_would_halt_on(state, cfg, seeded):
    tenant, other, t = seeded
    report = _list(state, cfg)
    mine = {(p.tenant_id, p.chroma_row, p.nx_note_row) for p in report.pairs if p.tenant_id in (tenant, other)}
    assert mine == {
        (tenant, t["pair_chroma"], t["pair_nx"]),
        (tenant, t["export_chroma"], t["export_nx"]),
        (tenant, t["nasty_chroma"], t["nasty_nx"]),
    }, "a control was listed or a pair was missed"
    listed = {p.chroma_row: p for p in report.pairs if p.tenant_id == tenant}
    assert listed[t["pair_chroma"]].source_uri == "chroma://knowledge__pairs-it/note-a"
    assert listed[t["export_chroma"]].source_uri == "nxexp://tgt/chroma://knowledge__pairs-it/note-b"
    assert listed[t["nasty_chroma"]].source_uri == "chroma://" + NASTY, "a tab, newline or U+2028 moved a column boundary"
    assert report.role == state["pg_user"]
    # the engine ran its migrations on this database, so the backfill changeset has a recorded state
    assert report.changeset == "EXECUTED", report.changeset


def test_after_a_merge_the_query_returns_no_row_for_the_tenant(state, cfg, seeded):
    """The runbook's step 2 ends with 'run the step 1 query again: it must return no rows'.

    A real merge (CatalogRepository.mergeDocuments) clears the duplicate's source_uri; it does not tombstone it."""
    tenant, other, t = seeded
    assert any(p.tenant_id == tenant for p in _list(state, cfg).pairs)
    dupes = ", ".join(f"'{t[k]}'" for k in ("pair_chroma", "export_chroma", "nasty_chroma"))
    _psql(state, f"UPDATE nexus.catalog_documents SET source_uri = '' WHERE tenant_id = '{tenant}' AND tumbler IN ({dupes})")
    assert not any(p.tenant_id in (tenant, other) for p in _list(state, cfg).pairs)


def test_cli_end_to_end_through_the_default_runner(state, cfg, seeded, monkeypatch):
    """The real verb: discover_pg_binaries via NEXUS_PG_BIN, the real psql, the printed table."""
    tenant, _other, t = seeded
    monkeypatch.setenv("NEXUS_PG_BIN", str(state["pg_bin"]))
    # The CLI has no database seam (a real cluster's database is `nexus`); the substrate's is another name.
    real = note_uri_pairs.list_note_uri_pairs
    monkeypatch.setattr(note_uri_pairs, "list_note_uri_pairs", lambda d, **kw: real(d, dbname=state["pg_dbname"], **kw))
    result = CliRunner().invoke(main, ["daemon", "service", "note-uri-pairs", "--config-dir", str(cfg), "--check"])
    assert result.exit_code == 3, result.output  # pairs exist
    assert f"{tenant}\t{t['pair_chroma']}\t{t['pair_nx']}\tchroma://knowledge__pairs-it/note-a" in result.output
    assert t["lone_chroma"] not in result.output and t["dead_chroma"] not in result.output
    assert t["docs_chroma"] not in result.output
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
