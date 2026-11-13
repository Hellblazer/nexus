# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx backup`` in cloud mode against the REAL engine (nexus-csayy.3, RDR-248): a minted tenant token, a seeded
tenant, the export routes of the session engine (``tests/_engine_substrate.py``), the file checked offline.

``tests/test_nx_backup_cloud.py`` and ``tests/test_cloud_export.py`` cover every failure scenario against a stand-in
for the routes; this file is the one place the client talks to the engine that serves them, so a drift between the
two (a header renamed, a key encoding changed, a column type the file cannot hold) turns red here. The engine
gates the routes on a tenant or data token: the substrate's own bootstrap token is the root token and is refused,
which one case pins.

ONE test mints a tenant (``t2_tenant``) and asserts its cases in turn, on one seeded tenant and one export: the
ceiling on tenant-minting tests (``tests/storage_touch_ceiling.toml``) allows no new one, and every case needs the
same real engine and the same seeded data. Each case is a helper below, called in order; a failure names the case.

A test-side cluster is not the cloud: this proves the wire contract and the file against a real PostgreSQL with
row-level security and pgvector, at a few rows. Paging at 500,000 chunks is nexus-csayy.4's.
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from nexus.backup import cloud_export as ce
from nexus.backup.pg_backup import BackupRefused
from nexus.backup.tenant_file import iter_copy_tuples, verify_file
from nexus.cli import main
from tests._engine_substrate import ensure_engine, minted_test_tenant

_NOTES = {
    "roundtrip-note-one": "HNSW indexes approximate nearest neighbour search with a layered proximity graph.",
    "roundtrip-note-two": "Row level security shows a session only the rows of its own tenant.",
    "roundtrip-note-three": "A binary COPY page carries its rows, its last key and a SHA-256.",
}


def _seed(runner: CliRunner) -> None:
    for title, text in _NOTES.items():
        put = runner.invoke(main, ["store", "put", "-", "--title", title, "-c", "fixture-subject"], input=text + "\n")
        assert put.exit_code == 0, put.output
    mem = runner.invoke(main, ["memory", "put", "round trip memory body", "--project", "rt", "--title", "rt-memory"])
    assert mem.exit_code == 0, mem.output


def _rows(report_path: Path, table: str) -> list[list[bytes | None]]:
    """The raw fields of every row of *table*, read back out of the finished file's own page frames."""
    import json  # noqa: PLC0415
    import struct  # noqa: PLC0415

    raw = report_path.read_bytes()
    pos = 10
    rows: list[list[bytes | None]] = []
    while pos < len(raw):
        kind, length = struct.unpack_from(">cQ", raw, pos)
        payload = raw[pos + 9: pos + 9 + length]
        pos += 9 + length
        if kind == b"P":
            (mlen,) = struct.unpack_from(">I", payload, 0)
            if json.loads(payload[4: 4 + mlen])["table"] == table:
                rows += [[bytes(f) if f is not None else None for f in t] for t in iter_copy_tuples(payload[4 + mlen:])]
    return rows


def _page_metas(path: Path) -> list[dict]:
    import json  # noqa: PLC0415
    import struct  # noqa: PLC0415

    raw = path.read_bytes()
    pos, metas = 10, []
    while pos < len(raw):
        kind, length = struct.unpack_from(">cQ", raw, pos)
        payload = raw[pos + 9: pos + 9 + length]
        pos += 9 + length
        if kind == b"P":
            (mlen,) = struct.unpack_from(">I", payload, 0)
            metas.append(json.loads(payload[4: 4 + mlen]))
    return metas


def _case_the_export_verifies_and_holds_the_seeded_rows(dest: Path, tenant: str) -> None:
    report = verify_file(dest)
    assert report.header["tenant_id"] == tenant, "the export is of the token's own tenant"
    assert isinstance(report.header["schema_changeset_count"], int) and report.header["schema_changeset_count"] > 0
    assert report.header["format_version"] == 1 and report.header["engine_version"]
    by_name = {t.name: t for t in report.tables}
    assert {"memory", "chunks", "catalog_documents", "catalog_document_chunks", "catalog_collections"} <= set(by_name)
    assert len(by_name) >= 20, "every data and derived table of the registry is in the file, empty ones included"
    assert all(t.pages >= 1 for t in report.tables)

    columns = {t["name"]: [c["name"] for c in t["columns"]] for t in report.header["tables"]}
    types = {t["name"]: {c["name"]: c["type"] for c in t["columns"]} for t in report.header["tables"]}

    memory = _rows(dest, "memory")
    assert by_name["memory"].rows == len(memory) >= 1
    title_at = columns["memory"].index("title")
    assert b"rt-memory" in [r[title_at] for r in memory], "the note put through the CLI is in the file"

    docs = _rows(dest, "catalog_documents")
    titles = {d[columns["catalog_documents"].index("title")] for d in docs}
    assert {t.encode() for t in _NOTES} <= titles

    chunks = _rows(dest, "chunks")
    assert by_name["chunks"].rows == len(chunks) >= len(_NOTES)
    # the unified chunks table has one nullable vector column per dimension; each row fills exactly one
    vec_cols = {c: int(re.search(r"\((\d+)\)", ty).group(1)) for c, ty in types["chunks"].items() if "vector(" in ty}  # type: ignore[union-attr]
    assert vec_cols, types["chunks"]
    for r in chunks:
        filled = [(c, r[columns["chunks"].index(c)]) for c in vec_cols if r[columns["chunks"].index(c)] is not None]
        assert len(filled) == 1, "exactly one embedding per chunk"
        col, raw = filled[0]
        assert len(raw) == 4 + 4 * vec_cols[col], "the embedding travelled whole"
    tenant_at = columns["chunks"].index("tenant_id")
    assert {r[tenant_at] for r in chunks} == {tenant.encode()}, "no other tenant's chunk is in the file"


def _case_a_table_spanning_pages_is_chained_by_the_engines_own_last_keys(dest: Path) -> None:
    """The substrate engine cuts pages at 1,024 bytes (``tests/_engine_substrate.py``), so each chunk (an embedding
    alone is over 1.5 KB) is a page of its own. The client sends every page's real ``X-Nexus-Export-Last-Key`` back as
    ``after``; the file verifies only when every row arrived exactly once and in the engine's count."""
    metas = _page_metas(dest)
    chunk_pages = [m for m in metas if m["table"] == "chunks"]
    assert len(chunk_pages) >= len(_NOTES), f"chunks fit in {len(chunk_pages)} page(s); the page cut is not 1,024 bytes"
    assert [m["done"] for m in chunk_pages] == [False] * (len(chunk_pages) - 1) + [True]
    keys = [m["last_key"] for m in chunk_pages if m["rows"]]  # a table's final page may be empty, with no key
    assert all(keys) and len(set(keys)) == len(keys) >= len(_NOTES), "each page ends at a new key the next page started after"
    report = verify_file(dest)
    assert {t.name: t for t in report.tables}["chunks"].rows == sum(m["rows"] for m in chunk_pages) >= len(_NOTES)
    chunks = _rows(dest, "chunks")
    cols = [c["name"] for t in report.header["tables"] if t["name"] == "chunks" for c in t["columns"]]
    chashes = {r[cols.index("chash")] for r in chunks}
    assert len(chashes) == len(chunks), "no chunk twice"


def _case_a_sensitive_document_is_in_the_file_and_the_verb_says_so(dest: Path, output: str) -> None:
    assert "sensitive document" in " ".join(output.split())
    report = verify_file(dest)
    columns = [c["name"] for t in report.header["tables"] if t["name"] == "catalog_documents" for c in t["columns"]]
    docs = _rows(dest, "catalog_documents")
    assert any(d[columns.index("sensitive")] == b"\x01" for d in docs)


def _case_the_session_was_closed_so_a_second_export_succeeds(runner: CliRunner, dest: Path) -> None:
    # the same tenant opens another session at once, which the engine allows one of at a time
    second = dest.parent / "second.nxtenant"
    again = runner.invoke(main, ["backup", "--to", str(second)])
    assert again.exit_code == 0, again.output
    assert [t.rows for t in verify_file(second).tables] == [t.rows for t in verify_file(dest).tables]
    assert {p.name for p in dest.parent.iterdir()} == {"tenant.nxtenant", "second.nxtenant"}


def _case_an_interrupted_export_frees_the_engines_session_slot(tenant: str, tmp_path: Path) -> None:
    client = ce.connect()

    def stop(*_: object) -> None:
        raise KeyboardInterrupt

    dest = tmp_path / "interrupted" / "x.nxtenant"
    with pytest.raises(KeyboardInterrupt):
        ce.run_export(client, dest, on_progress=stop)
    assert not dest.exists() and list(dest.parent.iterdir()) == [], "no partial file"
    # one session per tenant: if the DELETE had not gone out this open would answer 409 export_session_exists
    session = client.open_session()
    assert session.header["tenant_id"] == tenant
    client.close(session)
    client.close_http()


#: Tables whose row count may differ between a tenant and its restore, with why. Everything else must be equal.
_RESTORE_COUNT_EXCEPTIONS = {
    "catalog_meta": "the restore writes its own marker row (rdr248_restore), and the tenant's first request seeds another",
}


def _case_a_restore_into_a_second_tenant_round_trips_with_equal_counts(
    runner: CliRunner, dest: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole verb against the real engine: the file the export wrote restores into a fresh tenant, and an
    export of that tenant holds the same rows (counts per table, and the content columns that survive id
    reassignment)."""
    state = ensure_engine()
    original = verify_file(dest)
    with minted_test_tenant(state) as (second, token):
        monkeypatch.setenv("NX_SERVICE_TOKEN", token)
        restored = runner.invoke(main, ["backup", "restore", str(dest)])
        assert restored.exit_code == 0, restored.output
        out = " ".join(restored.output.split())
        assert f"Restore committed: {dest}" in out and "data:" in out and "rows loaded" in out, out
        # a second restore into the now non-empty tenant is refused, and says this file is the one that is in it
        again = runner.invoke(main, ["backup", "restore", str(dest)])
        assert again.exit_code != 0 and "already restored" in " ".join(again.output.split()), again.output

        second_file = tmp_path / "restored.nxtenant"
        exported = runner.invoke(main, ["backup", "--to", str(second_file)])
        assert exported.exit_code == 0, exported.output
        copy = verify_file(second_file)
        assert copy.header["tenant_id"] == second != original.header["tenant_id"]
        before, after = {t.name: t.rows for t in original.tables}, {t.name: t.rows for t in copy.tables}
        assert set(before) == set(after)
        differing = {n: (before[n], after[n]) for n in before if before[n] != after[n] and n not in _RESTORE_COUNT_EXCEPTIONS}
        assert not differing, f"row counts differ after a restore: {differing}"
        assert after["memory"] >= 1 and after["chunks"] >= len(_NOTES) and after["catalog_documents"] >= len(_NOTES)
        columns = {t["name"]: [c["name"] for c in t["columns"]] for t in original.header["tables"]}

        def column(path: Path, table: str, name: str) -> list[bytes | None]:
            return sorted((r[columns[table].index(name)] for r in _rows(path, table)), key=lambda v: v or b"")

        # ids are reassigned, so the check is on what the rows say, not on what they are numbered
        for table, name in (("memory", "title"), ("catalog_documents", "title"), ("chunks", "chash")):
            assert column(dest, table, name) == column(second_file, table, name), f"{table}.{name} differs after the restore"
        marker = httpx.get(f"{state['base_url']}/v1/tenant/import-marker", headers={"Authorization": f"Bearer {token}"}, timeout=10).json()
        assert marker["present"] is True and marker["marker"]["header_sha256"], marker


def _case_the_engines_root_token_is_refused_saying_which_token_to_use(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state = ensure_engine()
    monkeypatch.setenv("NX_SERVICE_TOKEN", state["bearer"])
    client = ce.connect()
    refused = tmp_path / "refused"
    refused.mkdir()
    with pytest.raises(BackupRefused, match="tenant or data token"):
        ce.run_export(client, refused / "x.nxtenant")
    client.close_http()
    assert list(refused.iterdir()) == []


def _case_the_engine_advertises_the_routes_the_client_gates_on() -> None:
    state = ensure_engine()
    body = httpx.get(f"{state['base_url']}/version", timeout=10).json()
    assert body["tenant_import_supported"] is True
    assert body["tenant_export_supported"] is True
    assert body["tenant_export_format_version"] in ce.ENGINE_FORMAT_VERSIONS


def test_cloud_backup_against_the_real_engine(t2_tenant: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """One seeded tenant, one export through ``nx backup``, then the cases in turn (see the module docstring)."""
    runner = CliRunner()
    _seed(runner)
    put = runner.invoke(main, ["store", "put", "-", "--title", "rt-secret", "-c", "fixture-subject", "--sensitive"],
                        input="a document the owner marked sensitive\n")
    assert put.exit_code == 0, put.output
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)
    dest = tmp_path / "out" / "tenant.nxtenant"

    result = runner.invoke(main, ["backup", "--to", str(dest)])
    assert result.exit_code == 0, result.output
    assert f"Backup written: {dest}" in result.output

    _case_the_export_verifies_and_holds_the_seeded_rows(dest, t2_tenant)
    _case_a_table_spanning_pages_is_chained_by_the_engines_own_last_keys(dest)
    _case_a_sensitive_document_is_in_the_file_and_the_verb_says_so(dest, result.output)
    _case_the_session_was_closed_so_a_second_export_succeeds(runner, dest)
    _case_an_interrupted_export_frees_the_engines_session_slot(t2_tenant, tmp_path)
    _case_the_engine_advertises_the_routes_the_client_gates_on()
    _case_a_restore_into_a_second_tenant_round_trips_with_equal_counts(runner, dest, tmp_path, monkeypatch)
    _case_the_engines_root_token_is_refused_saying_which_token_to_use(monkeypatch, tmp_path)  # last: it swaps the token
