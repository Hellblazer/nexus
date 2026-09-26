# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-iygza (indexing-brittleness P0.1, client half): drain the chunks a
taxonomized collection holds with no assignment to its own topics.

A taxonomy-assign batch that still fails after the split-in-half retry
(nexus-mg8gx) used to be gone: the tripwire's hook_failures row keeps only
the first chash. Sam's design (2026-09-25) derives the backlog from state
instead of recording it: the engine lists unassigned, manifest-backed
chunks (POST /v1/taxonomy/assignments/unassigned, engine-service-v0.1.132),
and the client feeds each page through the same retrying assign path.

Real engine substrate: the listing is an antijoin the engine computes, and
an assignment has to persist for the next page to see it gone.
"""
from __future__ import annotations

import hashlib
import inspect
import itertools

from unittest.mock import MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner

import nexus.commands.index as index_mod
import nexus.mcp_infra as mcp_infra
from nexus.cli import main
from nexus.commands.index import _drain_repo_collections
from nexus.db import make_t3
from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore
from nexus.mcp_infra import DrainResult, drain_unassigned_chunks
from tests._catalog_fixture_ops import ActiveCatalog

_COLL = "knowledge__iygza-drain__bge-base-en-v15-768__v1"
_BARE = "knowledge__iygza-bare__bge-base-en-v15-768__v1"
#: topics.id is one global sequence in the substrate database, shared by every
#: tenant a test mints, so each seeded topic needs its own id.
_TOPIC_IDS = itertools.count(91001)


def _chash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _seed(collection: str, n: int, *, topic: bool) -> tuple[list[str], int | None]:
    """n manifest-backed chunks in *collection*; one topic whose centroid is
    the first chunk's own vector when *topic*. Returns (chashes, topic_id)."""
    t3 = make_t3()
    docs = [f"iygza drain chunk {i} about lighthouses and tides" for i in range(n)]
    ids = [_chash(d) for d in docs]
    t3.upsert_chunks_with_embeddings(
        collection_name=collection, ids=ids, documents=docs,
        embeddings=[[] for _ in ids], metadatas=[{} for _ in ids],
    )
    cat = ActiveCatalog()
    owner = cat.register_owner("iygza-owner", "curator")
    tumbler = str(cat.register(
        owner, f"{collection}-doc", content_type="knowledge",
        physical_collection=collection, chunk_count=n,
    ))
    cat.write_manifest(
        tumbler, [{"chash": c, "position": i} for i, c in enumerate(ids)],
        collection=collection,
    )
    if not topic:
        return ids, None
    tax = HttpTaxonomyStore()
    topic_id = tax.import_topic(
        src_id=next(_TOPIC_IDS), label="iygza-topic", parent_id=None, collection=collection,
        centroid_hash=None, doc_count=0, created_at="2026-09-25T00:00:00Z",
        review_status="pending", terms=None,
    )
    vec = t3.get_embeddings(collection, ids[:1])[0].tolist()
    tax._centroid.upsert([{
        "collection": collection, "topic_id": topic_id, "embedding": vec,
        "label": "iygza-topic", "doc_count": 0,
    }])
    return ids, topic_id


def test_store_lists_unassigned_with_a_cursor(t2_service_env) -> None:
    ids, _ = _seed(_COLL, 5, topic=True)
    tax = HttpTaxonomyStore()

    first = tax.unassigned_chashes(_COLL, limit=3)
    assert first["has_taxonomy"] is True
    assert len(first["chashes"]) == 3
    assert first["next_after"] == first["chashes"][-1]

    rest = tax.unassigned_chashes(_COLL, limit=3, after=first["next_after"])
    assert len(rest["chashes"]) == 2
    assert rest["next_after"] is None
    assert sorted(first["chashes"] + rest["chashes"]) == sorted(ids)


def test_drain_assigns_every_unassigned_chunk(t2_service_env) -> None:
    ids, topic_id = _seed(_COLL, 7, topic=True)

    result = drain_unassigned_chunks(_COLL, page_size=3)

    assert result.has_taxonomy and result.found == 7 and result.assigned == 7
    assert result.lost == 0 and not result.truncated
    got = HttpTaxonomyStore().get_assignments_for_docs(ids)
    assert all(got[c] == topic_id for c in ids), got
    # Non-vacuity: a second drain finds nothing left.
    again = drain_unassigned_chunks(_COLL, page_size=3)
    assert again.found == 0


def test_drain_stops_at_the_chunk_budget(t2_service_env) -> None:
    _seed(_COLL, 7, topic=True)

    result = drain_unassigned_chunks(_COLL, page_size=3, max_chunks=4)

    assert result.found == 4 and result.truncated
    assert HttpTaxonomyStore().unassigned_chashes(_COLL, limit=10)["chashes"], (
        "chunks past the budget stay for the next run"
    )


def test_a_collection_without_topics_is_skipped(t2_service_env) -> None:
    _seed(_BARE, 3, topic=False)

    result = drain_unassigned_chunks(_BARE)

    assert result.has_taxonomy is False and result.found == 0 and result.skipped_reason == ""


def test_an_engine_without_the_route_is_a_named_skip(monkeypatch) -> None:
    def _404(*a, **kw):
        req = httpx.Request("POST", "http://engine/v1/taxonomy/assignments/unassigned")
        raise httpx.HTTPStatusError("404", request=req, response=httpx.Response(404, request=req))

    monkeypatch.setattr(HttpTaxonomyStore, "unassigned_chashes", _404)

    result = drain_unassigned_chunks(_COLL)

    assert result.found == 0 and "engine" in result.skipped_reason


def test_cli_drain_reports_and_assigns(t2_service_env) -> None:
    ids, _ = _seed(_COLL, 4, topic=True)

    out = CliRunner().invoke(main, ["taxonomy", "drain", "-c", _COLL])

    assert out.exit_code == 0, out.output
    assert "4 unassigned" in out.output and "4 assigned" in out.output, out.output
    assert not HttpTaxonomyStore().unassigned_chashes(_COLL)["chashes"]


def test_cli_drain_exits_nonzero_when_chunks_are_lost(monkeypatch, t2_service_env) -> None:

    _seed(_COLL, 2, topic=True)
    monkeypatch.setattr(
        mcp_infra, "_assign_from_chashes_with_retry",
        lambda collection, doc_ids, **kw: ({"assigned": 0}, list(doc_ids), ["HTTP 500"]),
    )

    out = CliRunner().invoke(main, ["taxonomy", "drain", "-c", _COLL])

    assert out.exit_code == 1, out.output
    assert "2 lost" in out.output, out.output


@pytest.mark.parametrize("flag", [[], ["-c", "x", "--all"]])
def test_cli_drain_requires_exactly_one_scope(flag) -> None:
    out = CliRunner().invoke(main, ["taxonomy", "drain", *flag])
    assert out.exit_code == 2, out.output


def test_index_drain_is_quiet_when_nothing_is_unassigned(monkeypatch, capsys) -> None:

    monkeypatch.setattr(mcp_infra, "drain_unassigned_chunks", lambda name, **kw: DrainResult(name, True))
    _drain_repo_collections(["code__iygza-quiet"])
    assert capsys.readouterr().out == ""


def test_index_drain_reports_work_and_survives_a_failure(monkeypatch, capsys) -> None:

    def _drain(name, **kw):
        if name == "bad":
            raise RuntimeError("engine 503")
        return DrainResult(name, True, found=76, assigned=76, truncated=False)

    monkeypatch.setattr(mcp_infra, "drain_unassigned_chunks", _drain)
    _drain_repo_collections(["bad", "docs__1-1"])
    out = capsys.readouterr().out
    assert "bad failed (RuntimeError); next run retries" in out
    assert "docs__1-1: 76 of 76 unassigned chunk(s) assigned, 0 lost" in out


def test_index_repo_calls_the_drain() -> None:
    assert "_drain_repo_collections(collections, client=_t2_client)" in inspect.getsource(index_mod)


def test_cli_drain_all_isolates_a_failing_collection(monkeypatch) -> None:
    def _drain(name, **kw):
        if name == "a-bad":
            raise httpx.ConnectError("engine unreachable")
        return DrainResult(name, True, found=1, assigned=1)

    monkeypatch.setattr(mcp_infra, "drain_unassigned_chunks", _drain)
    monkeypatch.setattr(mcp_infra, "get_live_collection_names", lambda: ["a-bad", "b-good"])
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)

    out = CliRunner().invoke(main, ["taxonomy", "drain", "--all"])

    assert out.exit_code == 1, out.output
    assert "a-bad: failed (ConnectError" in out.output
    assert "b-good: 1 unassigned, 1 assigned, 0 lost" in out.output


def test_cli_drain_all_honours_the_local_exclusion(monkeypatch) -> None:
    seen: list[str] = []

    def _drain(name, **kw):
        seen.append(name)
        return DrainResult(name, True)

    monkeypatch.setattr(mcp_infra, "drain_unassigned_chunks", _drain)
    monkeypatch.setattr(mcp_infra, "get_live_collection_names", lambda: ["code__r", "docs__r"])
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
    monkeypatch.setattr(
        "nexus.config.load_config",
        lambda: {"taxonomy": {"local_exclude_collections": ["code__*"]}},
    )

    out = CliRunner().invoke(main, ["taxonomy", "drain", "--all"])

    assert out.exit_code == 0, out.output
    assert seen == ["docs__r"]


def _index_repo(tmp_path, monkeypatch):
    """A real `nx index repo` invocation with the indexer internals mocked
    (the tests/test_index_cmd_shared_client_fanout.py convention); the
    taxonomy step, drain and exit-code check run for real."""
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "myrepo"
    repo.mkdir()
    (repo / ".git").mkdir()
    reg = MagicMock()
    # docs_collection too: local mode excludes code__* from taxonomy work.
    reg.get.return_value = {"collection": "code__myrepo", "docs_collection": "docs__myrepo"}
    mcp_infra.reset_taxonomy_assign_run_stats()
    # No uptime evidence, so nexus-tawfg does not defer: the substrate
    # engine really did start seconds ago, which would defer the drain
    # these tests exercise. (Deferral itself: tests/test_tawfg_*.)
    monkeypatch.setattr(mcp_infra, "engine_process_uptime_seconds", lambda: None)
    with patch("nexus.commands.index._registry", return_value=reg), \
            patch("nexus.indexer.index_repository", return_value={"files_changed": 0}):
        return CliRunner().invoke(main, ["index", "repo", str(repo)])


def test_index_repo_fails_when_the_drain_loses_a_chunk(tmp_path, monkeypatch, t2_service_env) -> None:
    """Both reviewers' Critical: the exit-code check read a stats snapshot
    taken before the drain, so a drain loss exited 0."""
    def _lossy(name, **kw):
        mcp_infra._record_taxonomy_assign_attempt()
        mcp_infra._record_taxonomy_assign_batch_failure(3)
        return DrainResult(name, True, found=3, assigned=0, lost=3)

    monkeypatch.setattr(mcp_infra, "drain_unassigned_chunks", _lossy)

    out = _index_repo(tmp_path, monkeypatch)

    assert out.exit_code != 0, out.output
    assert "3 chunk(s) affected" in out.output and "(nexus-7lw6a)" in out.output, out.output


def test_index_repo_passes_when_the_drain_assigns_everything(tmp_path, monkeypatch, t2_service_env) -> None:
    def _clean(name, **kw):
        mcp_infra._record_taxonomy_assign_attempt()
        return DrainResult(name, True, found=2, assigned=2)

    monkeypatch.setattr(mcp_infra, "drain_unassigned_chunks", _clean)

    out = _index_repo(tmp_path, monkeypatch)

    assert out.exit_code == 0, out.output
    assert "2 of 2 unassigned chunk(s) assigned, 0 lost" in out.output


def test_index_drain_says_once_that_the_engine_has_no_route(monkeypatch, capsys) -> None:
    calls: list[str] = []

    def _old_engine(name, **kw):
        calls.append(name)
        return DrainResult(name, skipped_reason="engine has no /v1/taxonomy/assignments/unassigned")

    monkeypatch.setattr(mcp_infra, "drain_unassigned_chunks", _old_engine)
    _drain_repo_collections(["code__a", "docs__a", "rdr__a"])

    out = capsys.readouterr().out
    assert out.count("Taxonomy drain: skipped") == 1, out
    assert calls == ["code__a"]


def test_drain_of_an_unregistered_collection_is_a_quiet_no_op(t2_service_env) -> None:
    result = drain_unassigned_chunks("knowledge__iygza-never-registered__bge-base-en-v15-768__v1")
    assert result.found == 0 and result.skipped_reason == "" and not result.has_taxonomy


def test_an_acknowledged_stuck_chunk_is_skipped_and_counted(t2_service_env) -> None:
    """nexus-j7ae6: an acknowledged chunk is not retried and not a loss,
    but the drain reports it every run; the rest still assign."""
    ids, topic_id = _seed(_COLL, 4, topic=True)
    stuck = ids[0]

    out = CliRunner().invoke(
        main, ["taxonomy", "acknowledge", stuck, "-c", _COLL, "--note", "engine refuses it"],
    )
    assert out.exit_code == 0, out.output

    first = drain_unassigned_chunks(_COLL)
    assert (first.found, first.assigned, first.lost, first.acknowledged) == (4, 3, 0, 1)
    got = HttpTaxonomyStore().get_assignments_for_docs(ids)
    assert stuck not in got and all(got[c] == topic_id for c in ids[1:])

    again = drain_unassigned_chunks(_COLL)
    assert (again.found, again.assigned, again.acknowledged) == (1, 0, 1), "reported every run"

    listed = CliRunner().invoke(main, ["taxonomy", "acknowledge", "--list"])
    assert stuck in listed.output and "engine refuses it" in listed.output

    CliRunner().invoke(main, ["taxonomy", "acknowledge", stuck, "--remove"])
    after = drain_unassigned_chunks(_COLL)
    assert after.acknowledged == 0 and after.assigned == 1


def test_acknowledge_rejects_a_non_chash() -> None:
    out = CliRunner().invoke(main, ["taxonomy", "acknowledge", "not-a-chash"])
    assert out.exit_code == 2 and "not a 64-hex chunk chash" in out.output


def test_the_drain_line_names_acknowledged_skips(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        mcp_infra, "drain_unassigned_chunks",
        lambda name, **kw: DrainResult(name, True, found=3, assigned=2, acknowledged=1),
    )
    _drain_repo_collections(["docs__a"])
    assert "2 of 3 unassigned chunk(s) assigned, 0 lost, 1 acknowledged stuck (skipped)" in capsys.readouterr().out
