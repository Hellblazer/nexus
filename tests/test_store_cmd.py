# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from click.testing import CliRunner

from nexus.cli import main
from nexus.db.http_vector_client import HttpVectorClient


def _seed_for_store_put(content: str, collection: str = "knowledge") -> None:
    """Pre-seed a REAL ``nexus.chunks`` row for what CLI ``nx store put``
    is about to write (nexus-dbzxb, RDR-191 Phase 5 Python collateral).

    ``mock_store`` / the inline ``MagicMock(spec=HttpVectorClient)`` fully
    mock the T3 write here, but the catalog manifest write
    (``store_put_manifest_direct``) always goes through the REAL engine
    catalog (autouse ``_pin_t2_substrate``). ``fk_catalog_chunks_chunk``
    now requires the manifest's chash to have a matching REAL
    ``nexus.chunks`` row. Computes the exact ``(collection, chash)``
    production will use via the same derivation production code uses.
    """
    import hashlib

    from nexus.corpus import t3_collection_name
    from tests._catalog_fixture_ops import seed_manifest_chunks

    col_name = t3_collection_name(collection)
    chash = hashlib.sha256(content.encode()).hexdigest()
    seed_manifest_chunks(col_name, [chash])


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def env_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    # RDR-155 P4b: the chroma-key mode inference is gone — pin cloud mode
    # explicitly so the voyage collection-name promotion under test fires.
    monkeypatch.setenv("NX_LOCAL", "0")
    monkeypatch.setenv("CHROMA_API_KEY", "test-chroma-key")
    monkeypatch.setenv("VOYAGE_API_KEY", "test-voyage-key")
    monkeypatch.setenv("CHROMA_TENANT", "test-tenant")
    monkeypatch.setenv("CHROMA_DATABASE", "test-db")


@pytest.fixture
def mock_store(env_creds):
    # env_creds sets both CHROMA_API_KEY and VOYAGE_API_KEY, so
    # is_local_mode()'s legacy heuristic resolves to cloud/service mode
    # here — the real _t3() would hand back an HttpVectorClient, not a
    # T3Database. spec= it so a method missing from HttpVectorClient
    # (e.g. the gc/_dir class of bug) fails the mocked test too.
    db = MagicMock(spec=HttpVectorClient)
    with patch("nexus.commands.store._t3", return_value=db):
        yield db


@pytest.fixture
def mock_collection(env_creds):
    db = MagicMock(spec=HttpVectorClient)
    with patch("nexus.commands.collection._t3", return_value=db):
        yield db


@pytest.fixture
def mock_search(env_creds):
    db = MagicMock(spec=HttpVectorClient)
    with patch("nexus.commands.search_cmd._t3", return_value=db):
        yield db


def _search_result(content="chunk", **overrides):
    base = {"id": "abc1", "content": content, "distance": 0.1,
            "source_path": "./sec.md", "line_start": 1}
    base.update(overrides)
    return base


def _store_entry(id="aabbccdd1234", title="doc.md", tags="", ttl_days=0,
                 expires_at="", indexed_at="2026-02-22T00:00:00+00:00"):
    return {"id": id, "title": title, "tags": tags, "ttl_days": ttl_days,
            "expires_at": expires_at, "indexed_at": indexed_at}


# ── _t3() factory ───────────────────────────────────────────────────────────
# The credential pre-flight was DELETED (nexus-c7aj3): make_t3() is
# service-backed unconditionally, so no store/search/collection verb needs
# Chroma/Voyage creds. The no-creds victim scenario is pinned end-to-end in
# tests/test_c7aj3_service_mode_cred_gates.py.

def test_store_put_tenant_optional(runner, monkeypatch, tmp_path):
    monkeypatch.setenv("CHROMA_API_KEY", "ck")
    monkeypatch.setenv("VOYAGE_API_KEY", "vk")
    monkeypatch.delenv("CHROMA_TENANT", raising=False)
    monkeypatch.setenv("CHROMA_DATABASE", "mydb")
    src = tmp_path / "f.txt"
    src.write_text("content")
    _seed_for_store_put("content")
    with patch("nexus.commands.store._t3") as mt3:
        db = MagicMock(spec=HttpVectorClient)
        db.__enter__ = MagicMock(return_value=db)
        db.__exit__ = MagicMock(return_value=False)
        db.put.return_value = "doc-id-1"
        mt3.return_value = db
        result = runner.invoke(main, ["store", "put", str(src), "--title", "test"])
    assert result.exit_code == 0, result.output


# ── nx store put ─────────────────────────────────────────────────────────────

def test_store_put_stdin_requires_title(runner, mock_store):
    result = runner.invoke(main, ["store", "put", "-"], input="some content")
    assert result.exit_code != 0
    assert "--title" in result.output


def test_store_put_stdin_with_title_succeeds(runner, mock_store):
    mock_store.put.return_value = "doc-id-abc"
    _seed_for_store_put("content here")
    result = runner.invoke(main, ["store", "put", "-", "--title", "my-title.md"], input="content here")
    assert result.exit_code == 0
    assert "doc-id-abc" in result.output
    mock_store.put.assert_called_once()
    kw = mock_store.put.call_args.kwargs
    assert kw["title"] == "my-title.md"
    assert kw["content"] == "content here"


def test_store_put_file_uses_filename_as_title(runner, mock_store, tmp_path):
    src = tmp_path / "analysis.md"
    src.write_text("finding: important")
    mock_store.put.return_value = "doc-id-xyz"
    _seed_for_store_put("finding: important")
    result = runner.invoke(main, ["store", "put", str(src)])
    assert result.exit_code == 0
    assert "doc-id-xyz" in result.output
    assert mock_store.put.call_args.kwargs["title"] == "analysis.md"


def test_store_put_file_not_found(runner, mock_store):
    result = runner.invoke(main, ["store", "put", "/no/such/file.txt"])
    assert result.exit_code != 0
    assert "not found" in result.output.lower() or "File not found" in result.output


def test_store_put_invalid_ttl_shows_error(runner, mock_store, tmp_path):
    src = tmp_path / "f.txt"
    src.write_text("content")
    result = runner.invoke(main, ["store", "put", str(src), "--ttl", "5z"])
    assert result.exit_code != 0
    assert "5z" in result.output


# ── nx store put in-loop heartbeat, always on (nexus-s71lr pass 3) ──────────
#
# Deliverable 3 names `nx store put` literally: a single document is still
# ONE embed call, and a large document's embed can run a minute+ with zero
# progress signal at all. Same `_PhaseHeartbeat` mechanism as
# `nx index rdr` / `nx index pdf --dir` / `nx store import`.


def test_store_put_heartbeat_ticks_during_a_slow_embed(runner, mock_store, tmp_path, monkeypatch):
    import time
    import nexus.commands.index as index_mod

    class _FastPhaseHeartbeat(index_mod._PhaseHeartbeat):
        def __init__(self, *, is_tty, echo, interval=None, prefix="post"):
            super().__init__(is_tty=is_tty, echo=echo, interval=0.02, prefix=prefix)

    monkeypatch.setattr(index_mod, "_PhaseHeartbeat", _FastPhaseHeartbeat)
    # No catalog_doc_id -> the manifest-write path (which needs a live engine,
    # unavailable in this unit-test environment) is skipped entirely; the
    # heartbeat wraps db.put() regardless of whether catalog registration ran.
    monkeypatch.setattr("nexus.commands.store._catalog_store_hook_tracked", lambda **kw: ("", False))

    src = tmp_path / "big.md"
    src.write_text("a large document")

    def _slow_put(**kwargs):
        time.sleep(0.09)  # several 0.02s intervals elapse with nothing done
        return "doc-id-slow"

    mock_store.put.side_effect = _slow_put
    result = runner.invoke(main, ["store", "put", str(src)])

    assert result.exit_code == 0, result.output
    assert "[embed]" in result.output
    assert "still running" in result.output
    assert "elapsed)" in result.output


def test_store_put_heartbeat_silent_on_a_fast_put(runner, mock_store, tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.commands.store._catalog_store_hook_tracked", lambda **kw: ("", False))

    src = tmp_path / "small.md"
    src.write_text("small content")

    mock_store.put.return_value = "doc-id-fast"
    result = runner.invoke(main, ["store", "put", str(src)])

    assert result.exit_code == 0, result.output
    assert "[embed]" not in result.output
    assert "still running" not in result.output


def test_store_put_heartbeat_disarmed_on_exception(runner, mock_store, tmp_path, monkeypatch):
    import threading
    import time
    import nexus.commands.index as index_mod

    class _FastPhaseHeartbeat(index_mod._PhaseHeartbeat):
        def __init__(self, *, is_tty, echo, interval=None, prefix="post"):
            super().__init__(is_tty=is_tty, echo=echo, interval=0.02, prefix=prefix)

    monkeypatch.setattr(index_mod, "_PhaseHeartbeat", _FastPhaseHeartbeat)
    monkeypatch.setattr("nexus.commands.store._catalog_store_hook_tracked", lambda **kw: ("", False))

    src = tmp_path / "boom.md"
    src.write_text("content that triggers a put failure")

    mock_store.put.side_effect = RuntimeError("boom")
    result = runner.invoke(main, ["store", "put", str(src)])

    assert result.exit_code != 0
    time.sleep(0.05)
    assert not any(t.name == "nx-phase-heartbeat" for t in threading.enumerate())


# ── nx store list ────────────────────────────────────────────────────────────

def test_store_list_empty_collection(runner, mock_store):
    mock_store.list_store.return_value = []
    result = runner.invoke(main, ["store", "list"])
    assert result.exit_code == 0
    assert "No entries" in result.output
    mock_store.list_store.assert_called_once()


def test_store_list_shows_entries_and_tags(runner, mock_store):
    mock_store.list_store.return_value = [
        _store_entry(id="abc123def456", title="analysis.md", tags="security,audit"),
        _store_entry(id="fff000aaa111", title="temp-notes.md", ttl_days=30,
                     expires_at="2026-03-24T10:00:00+00:00", indexed_at="2026-02-22T11:00:00+00:00"),
        _store_entry(tags="arch,decision"),
    ]
    result = runner.invoke(main, ["store", "list"])
    assert result.exit_code == 0
    for text in ("abc123def456", "analysis.md", "permanent", "fff000aaa111",
                 "temp-notes.md", "2026-03-24", "security,audit", "arch,decision"):
        assert text in result.output


def test_store_list_custom_collection(runner, mock_store):
    mock_store.list_store.return_value = []
    runner.invoke(main, ["store", "list", "--collection", "knowledge__notes"])
    # RDR-103 Phase 5: t3_collection_name auto-promotes 2-segment.
    assert (
        mock_store.list_store.call_args[0][0]
        == "knowledge__notes__voyage-context-3__v1"
    )


def test_store_list_limit_flag(runner, mock_store):
    mock_store.list_store.return_value = []
    runner.invoke(main, ["store", "list", "--limit", "10"])
    ca = mock_store.list_store.call_args
    assert ca[1].get("limit") == 10 or ca[0][1] == 10


def test_store_list_shows_16char_ids(runner, mock_store):
    mock_store.list_store.return_value = [_store_entry(id="abcdef1234567890ff")]
    result = runner.invoke(main, ["store", "list"])
    assert result.exit_code == 0
    assert "abcdef1234567890" in result.output


def test_store_expire_reports_count(runner, mock_store):
    mock_store.expire.return_value = 3
    result = runner.invoke(main, ["store", "expire"])
    assert result.exit_code == 0
    assert "3" in result.output


# ── nx collection ────────────────────────────────────────────────────────────

def test_collection_list_empty(runner, mock_collection):
    mock_collection.list_collections.return_value = []
    result = runner.invoke(main, ["collection", "list"])
    assert result.exit_code == 0
    assert "No collections" in result.output


def test_collection_list_shows_names_and_counts(runner, mock_collection):
    mock_collection.list_collections.return_value = [
        {"name": "code__myrepo", "count": 42}, {"name": "knowledge__sec", "count": 7}]
    result = runner.invoke(main, ["collection", "list"])
    assert result.exit_code == 0
    for text in ("code__myrepo", "42", "knowledge__sec", "7"):
        assert text in result.output


@pytest.mark.parametrize("subcmd,args", [("info", ["no-such-collection"]), ("verify", ["missing"])])
def test_collection_not_found(runner, mock_collection, subcmd, args):
    mock_collection.list_collections.return_value = []
    result = runner.invoke(main, ["collection", subcmd] + args)
    assert result.exit_code != 0
    assert "not found" in result.output.lower()


def test_collection_delete_without_yes_prompts(runner, mock_collection):
    runner.invoke(main, ["collection", "delete", "knowledge__test"], input="n\n")
    mock_collection.delete_collection.assert_not_called()


# ── nx search ────────────────────────────────────────────────────────────────

def test_search_no_matching_corpus(runner, mock_search):
    mock_search.list_collections.return_value = []
    result = runner.invoke(main, ["search", "my query", "--corpus", "code"])
    assert result.exit_code == 0
    assert "no matching collections" in result.output.lower()


def test_search_no_results(runner, mock_search):
    mock_search.list_collections.return_value = [{"name": "knowledge__sec", "count": 5}]
    mock_search.search.return_value = []
    result = runner.invoke(main, ["search", "my query", "--corpus", "knowledge"])
    assert result.exit_code == 0
    assert "No results" in result.output


def test_search_displays_results(runner, mock_search):
    mock_search.list_collections.return_value = [{"name": "knowledge__sec", "count": 2}]
    mock_search.search.return_value = [
        _search_result(content="security finding here", id="abc12345-0000",
                       distance=0.123, title="sec.md", tags="security")]
    result = runner.invoke(main, ["search", "security", "--corpus", "knowledge"])
    assert result.exit_code == 0
    assert "security finding here" in result.output


@pytest.mark.parametrize("content_flag,content_text,expect_indented", [
    (True, "UNIQUE_CHUNK_BODY", True),
    (False, "Unique chunk text that only appears when content flag is set.", False),
])
def test_search_content_flag_presence(runner, mock_search, content_flag, content_text, expect_indented):
    mock_search.list_collections.return_value = [{"name": "knowledge__sec", "count": 1}]
    mock_search.search.return_value = [_search_result(content=content_text)]
    args = ["search", "security", "--corpus", "knowledge"]
    if content_flag:
        args.append("--content")
    result = runner.invoke(main, args)
    assert result.exit_code == 0
    indented = [ln for ln in result.output.splitlines() if ln.startswith("  ") and content_text in ln]
    assert bool(indented) == expect_indented


@pytest.mark.parametrize("text,expect_ellipsis,max_len", [
    ("A" * 300, True, 210), ("Short enough.", False, None)])
def test_search_content_flag_truncation(runner, mock_search, text, expect_ellipsis, max_len):
    mock_search.list_collections.return_value = [{"name": "knowledge__sec", "count": 1}]
    mock_search.search.return_value = [_search_result(content=text)]
    result = runner.invoke(main, ["search", "query", "--corpus", "knowledge", "--content"])
    assert result.exit_code == 0
    indented = [ln for ln in result.output.splitlines() if ln.startswith("  ")]
    assert indented
    assert indented[0].endswith("...") == expect_ellipsis
    if max_len:
        assert len(indented[0]) <= max_len
    if not expect_ellipsis:
        assert text in indented[0]


# ── [path] positional argument ───────────────────────────────────────────────

def test_search_path_scopes_where_filter(runner, mock_search, tmp_path):
    mock_search.list_collections.return_value = [{"name": "knowledge__sec", "count": 2}]
    mock_search.search.return_value = []
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    result = runner.invoke(main, ["search", "query", str(src_dir), "--corpus", "knowledge"])
    assert result.exit_code == 0
    assert mock_search.search.called
    assert "$startswith" not in str(mock_search.search.call_args.kwargs.get("where"))


def test_search_path_filters_results_by_file_path(runner, mock_search, tmp_path):
    mock_search.list_collections.return_value = [{"name": "knowledge__sec", "count": 2}]
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    other_dir = tmp_path / "other"
    other_dir.mkdir()

    def fake_search(query, collection_names, n_results=10, where=None):
        return [
            _search_result(content="inside src", id="r1",
                           source_path=str(src_dir / "file.py"), file_path=str(src_dir / "file.py")),
            _search_result(content="outside src", id="r2", distance=0.2,
                           source_path=str(other_dir / "file.py"), file_path=str(other_dir / "file.py")),
        ]

    mock_search.search.side_effect = fake_search
    result = runner.invoke(main, ["search", "query", str(src_dir), "--corpus", "knowledge"])
    assert result.exit_code == 0
    assert "inside src" in result.output
    assert "outside src" not in result.output


def test_search_no_path_returns_all(runner, mock_search):
    mock_search.list_collections.return_value = [{"name": "knowledge__sec", "count": 2}]
    mock_search.search.return_value = [_search_result(content="result one")]
    result = runner.invoke(main, ["search", "query", "--corpus", "knowledge"])
    assert result.exit_code == 0
    ca = mock_search.search.call_args
    where_filter = ca.kwargs.get("where") if ca.kwargs else None
    if where_filter is None and ca.args and len(ca.args) > 3:
        where_filter = ca.args[3]
    assert where_filter is None


# ── nx store delete ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("found,exit_ok,expect_text", [(True, True, "Deleted"), (False, False, "not found")])
def test_store_delete_by_id(runner, mock_store, found, exit_ok, expect_text):
    # nexus-c53hy: delete_cmd now does a collection-scoped existence check
    # (get_by_id) BEFORE any catalog reap or delete_by_id call — a bare
    # MagicMock() would otherwise be truthy here and never exercise the
    # "not found" path at all. get_by_id mirrors delete_by_id's found/not
    # found shape so both branches still round-trip through the mock.
    mock_store.get_by_id.return_value = {"id": "abcdef1234567890"} if found else None
    mock_store.delete_by_id.return_value = found
    result = runner.invoke(main, ["store", "delete", "--collection", "knowledge", "--id", "abcdef1234567890"])
    assert (result.exit_code == 0) == exit_ok, result.output
    assert expect_text in result.output


@pytest.mark.parametrize("ids,exit_ok,expect_text", [
    (["id1", "id2"], True, "Deleted 2"), ([], False, "not found")])
def test_store_delete_by_title(runner, mock_store, ids, exit_ok, expect_text):
    mock_store.find_ids_by_title.return_value = ids
    # nexus-o8dil.45: batch_delete now returns the server's ACTUAL deleted
    # count (int), not None -- the CLI echo reads this return value instead
    # of len(ids). A bare MagicMock() return (the pre-fix default) would
    # print "Deleted <MagicMock ...>" here instead of "Deleted 2".
    mock_store.batch_delete.return_value = len(ids)
    result = runner.invoke(main, ["store", "delete", "--collection", "knowledge",
                                  "--title", "doc.md", "--yes"])
    if exit_ok:
        assert result.exit_code == 0, result.output
        mock_store.batch_delete.assert_called_once()
    else:
        assert result.exit_code != 0
    assert expect_text in result.output or "No entries" in result.output


def test_store_delete_by_title_reports_actual_deleted_count_on_partial_anti_join(
    runner, real_http_vector_client, monkeypatch,
):
    """nexus-o8dil.45: RDR-191 F10c's server-side anti-join can legitimately
    delete fewer chunks than requested (a chash another live document's
    manifest still references). The CLI must report the ACTUAL count -- an
    operator-facing message that was previously always wrong (reported
    len(ids) unconditionally) in this scenario."""
    def fake_post(path, body, **kw):
        if path == "/v1/vectors/get":
            return {"ids": ["id1", "id2", "id3"]}
        if path == "/v1/vectors/store-delete":
            return {"deleted": len(body["ids"]) - 1}
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)

    result = runner.invoke(main, [
        "store", "delete",
        "--collection", "knowledge__nexus__voyage-context-3__v1",
        "--title", "doc.md", "--yes",
    ])

    assert result.exit_code == 0, result.output
    assert "Deleted 2" in result.output, (
        f"expected the actual server-reported count (2 of 3 requested), "
        f"got output: {result.output!r}"
    )
    assert "Deleted 3" not in result.output


# ── nx store delete --title, REAL HttpVectorClient (nexus-umvh2 regression) ──
#
# mock_store above is a bare MagicMock() (no spec=): it silently answers
# ``.find_ids_by_title`` / ``.batch_delete`` even when the real production
# HttpVectorClient lacks those methods. That gap is exactly why the
# nexus-umvh2 AttributeError shipped unnoticed. These tests exercise a REAL
# HttpVectorClient (fake HTTP transport only) end-to-end through the CLI so a
# missing method surfaces as a real AttributeError/test failure.

@pytest.fixture
def real_http_vector_client(monkeypatch):
    from nexus.db.http_vector_client import (
        HttpVectorClient,
        reset_http_vector_client_for_tests,
    )
    reset_http_vector_client_for_tests()
    client = HttpVectorClient()
    monkeypatch.setattr("nexus.commands.store._t3", lambda: client)
    yield client
    reset_http_vector_client_for_tests()


def test_store_delete_by_title_service_mode_real_client(runner, real_http_vector_client, monkeypatch):
    """End-to-end: title resolves to 2 chunk ids and both get deleted,
    routed entirely through the real HttpVectorClient (no mocked T3)."""
    calls = []

    def fake_post(path, body, **kw):
        calls.append((path, body))
        if path == "/v1/vectors/get":
            return {"ids": ["id1", "id2"]}
        if path == "/v1/vectors/store-delete":
            return {"deleted": len(body["ids"])}
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)

    result = runner.invoke(main, [
        "store", "delete",
        "--collection", "knowledge__nexus__voyage-context-3__v1",
        "--title", "doc.md", "--yes",
    ])
    assert result.exit_code == 0, result.output
    assert "Deleted 2" in result.output
    delete_calls = [c for c in calls if c[0] == "/v1/vectors/store-delete"]
    assert len(delete_calls) == 1
    assert delete_calls[0][1]["ids"] == ["id1", "id2"]


def test_store_delete_by_title_not_found_service_mode_clean_error(runner, real_http_vector_client, monkeypatch):
    """Title-not-found must be a clean ClickException, never a traceback."""
    monkeypatch.setattr(
        "nexus.db.http_vector_client._post",
        lambda path, body, **kw: {"ids": []},
    )

    result = runner.invoke(main, [
        "store", "delete",
        "--collection", "knowledge__nexus__voyage-context-3__v1",
        "--title", "missing.md", "--yes",
    ])
    assert result.exit_code != 0
    assert "No entries" in result.output
    assert "AttributeError" not in result.output
    assert "Traceback" not in result.output


def test_store_delete_missing_collection_rejected(runner, env_creds):
    result = runner.invoke(main, ["store", "delete", "--id", "abc"])
    assert result.exit_code != 0


# ── nx store get ─────────────────────────────────────────────────────────────

def test_store_get_happy(runner, mock_store):
    mock_store.get_by_id.return_value = {
        "id": "abcdef1234567890", "content": "Important knowledge content here",
        "title": "finding.md", "tags": "arch,review", "indexed_at": "2026-03-09T10:00:00+00:00"}
    result = runner.invoke(main, ["store", "get", "abcdef1234567890"])
    assert result.exit_code == 0, result.output
    for text in ("abcdef1234567890", "finding.md", "arch,review", "Important knowledge content here"):
        assert text in result.output


def test_store_get_not_found(runner, mock_store):
    mock_store.get_by_id.return_value = None
    result = runner.invoke(main, ["store", "get", "nonexistent12345"])
    assert result.exit_code != 0
    assert "not found" in result.output.lower()


def test_store_get_json_output(runner, mock_store):
    mock_store.get_by_id.return_value = {
        "id": "abcdef1234567890", "content": "test content",
        "title": "doc.md", "tags": "test", "indexed_at": "2026-03-09"}
    result = runner.invoke(main, ["store", "get", "abcdef1234567890", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["id"] == "abcdef1234567890"
    assert data["content"] == "test content"


def test_store_get_custom_collection(runner, mock_store):
    mock_store.get_by_id.return_value = {"id": "abc123", "content": "x", "title": "t"}
    runner.invoke(main, ["store", "get", "abc123", "-c", "code__myrepo"])
    # RDR-103 Phase 5: t3_collection_name auto-promotes 2-segment.
    mock_store.get_by_id.assert_called_once_with(
        "code__myrepo__voyage-code-3__v1", "abc123",
    )


# ── nexus-s71lr: `nx store import` in-loop heartbeat, always on ────────────
#
# import_collection is one opaque call with no per-record progress callback
# at all -- worse than the per-file loops (not even a start/end line per
# record). Reuses the same `_PhaseHeartbeat` mechanism as `nx index rdr` /
# `nx index pdf --dir`: ticks every 5s for as long as the call is in flight.


def test_store_import_heartbeat_ticks_during_a_slow_import(runner, tmp_path, monkeypatch):
    import time
    import nexus.commands.index as index_mod

    class _FastPhaseHeartbeat(index_mod._PhaseHeartbeat):
        def __init__(self, *, is_tty, echo, interval=None, prefix="post"):
            super().__init__(is_tty=is_tty, echo=echo, interval=0.02, prefix=prefix)

    monkeypatch.setattr(index_mod, "_PhaseHeartbeat", _FastPhaseHeartbeat)

    dummy = tmp_path / "dummy.nxexp"
    dummy.write_bytes(b"not a real file -- import_collection is fully mocked below")

    def _slow_import_collection(**kwargs):
        time.sleep(0.09)  # several 0.02s intervals elapse with nothing done
        return {"imported_count": 5, "collection_name": "knowledge__x__voyage-context-3__v1",
                "elapsed_seconds": 0.09}

    with patch("nexus.commands.store._t3", return_value=MagicMock()), \
         patch("nexus.exporter.import_collection", side_effect=_slow_import_collection):
        result = runner.invoke(main, ["store", "import", str(dummy)])

    assert result.exit_code == 0, result.output
    assert "[embed]" in result.output
    assert "still running" in result.output
    assert "elapsed)" in result.output


def test_store_import_heartbeat_silent_on_a_fast_import(runner, tmp_path):
    dummy = tmp_path / "dummy.nxexp"
    dummy.write_bytes(b"not a real file -- import_collection is fully mocked below")

    fake_result = {"imported_count": 5, "collection_name": "knowledge__x__voyage-context-3__v1",
                   "elapsed_seconds": 0.01}
    with patch("nexus.commands.store._t3", return_value=MagicMock()), \
         patch("nexus.exporter.import_collection", return_value=fake_result):
        result = runner.invoke(main, ["store", "import", str(dummy)])

    assert result.exit_code == 0, result.output
    assert "[embed]" not in result.output
    assert "still running" not in result.output


def test_store_import_heartbeat_disarmed_on_exception(runner, tmp_path, monkeypatch):
    import threading
    import time
    import nexus.commands.index as index_mod

    class _FastPhaseHeartbeat(index_mod._PhaseHeartbeat):
        def __init__(self, *, is_tty, echo, interval=None, prefix="post"):
            super().__init__(is_tty=is_tty, echo=echo, interval=0.02, prefix=prefix)

    monkeypatch.setattr(index_mod, "_PhaseHeartbeat", _FastPhaseHeartbeat)

    dummy = tmp_path / "dummy.nxexp"
    dummy.write_bytes(b"not a real file -- import_collection is fully mocked below")

    with patch("nexus.commands.store._t3", return_value=MagicMock()), \
         patch("nexus.exporter.import_collection",
               side_effect=RuntimeError("boom")):
        result = runner.invoke(main, ["store", "import", str(dummy)])

    assert result.exit_code != 0
    time.sleep(0.05)
    assert not any(t.name == "nx-phase-heartbeat" for t in threading.enumerate())
