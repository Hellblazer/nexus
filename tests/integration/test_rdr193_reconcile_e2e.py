# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-193 Step 6 (nexus-u5m5g.7): the reconcile client end to end, through ``index_repository`` (``nx index repo``).

The scenarios of the RDR's Test Plan, run against the real engine with the real catalog pass, real git and the real
staleness decision. Each one counts the requests the run made and reads the rows back:

* a warm full run with nothing changed: one commit, no row rewritten (``indexed_at`` and every column kept);
* a HEAD bump only: every row takes the new head, no content hash changed, no document is fenced for real work;
* a delta run with one file deleted: its document is trashed by the commit, no unvisited row is touched;
* the commit never moves the owner's ``head_hash`` (the run end does, only when nothing was deferred or failed);
* a path another owner holds: the run reports it, that row is left as it is, the file's chunks carry its tumbler;
* hand-set tags and category are kept, ``source_format`` is re-stamped;
* ``--sensitive-glob`` on an existing unflagged document: the commit does not flag it, the client raises it;
* the engine losing the commit job (a restart: the poll answers 404): one resubmit, and the final state is right.

Scenarios the commit's own arms decide on rows an index run cannot produce on demand (a ``refused`` row, the rename and
stamp matrices, the duplicate-path rules) are in ``test_rdr193_reconcile_parity.py`` and the engine's Java tests.
"""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import structlog

from nexus import indexer
from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.db.t2._refreshable_client import RefreshableHttpStoreMixin
from nexus.db.t3 import T3Database
from nexus.repo_identity import _repo_identity
from tests._catalog_fixture_ops import active_reader
from tests.integration.test_a3rl7_since_head_skips_unchanged_delta import _head, _index_delta
from tests.test_cp46b_runfence_repo_staleness import (  # noqa: F401 - fixtures and helpers shared with the fence tests
    _commit,
    _git,
    _index,
    _init_repo,
    _prime_owner,
    _wrap_fence_helpers,
    catalog_env,
    git_identity,
    local_t3,
    mock_voyage_client,
)

pytestmark = [pytest.mark.integration, pytest.mark.t2_tenant]

COMMIT = "/v1/catalog/reconcile/commit"
STAGE = "/v1/catalog/reconcile/stage"
#: The hook's old per-row writes. ``update_many`` is not among them: the run's own frecency stamp (RDR-236,
#: ``indexer._write_frecency_meta``) still sends one per run, and ``_rows`` leaves its two keys out of the comparison.
SINGLE_WRITES = {"/v1/catalog/update", "/v1/catalog/doc/register",
                 "/v1/catalog/doc/register_many", "/v1/catalog/delete", "/v1/catalog/delete_many"}
_FRECENCY_KEYS = ("frecency_score", "frecency_computed_at")


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    sent: list[tuple[str, str]] = []
    real = RefreshableHttpStoreMixin._send

    def spy(self, method, path, **kw):  # noqa: ANN001
        sent.append((method, path))
        return real(self, method, path, **kw)

    monkeypatch.setattr(RefreshableHttpStoreMixin, "_send", spy)
    return sent


def _n(sent: list[tuple[str, str]], path: str) -> int:
    return sum(1 for _m, p in sent if p == path)


def _write_repo(repo: Path) -> None:
    (repo / "src").mkdir(exist_ok=True)
    (repo / "src" / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs" / "guide.md").write_text("# Guide\n\nA guide to the thing.\n", encoding="utf-8")
    (repo / "docs" / "notes.md").write_text("# Notes\n\nSome notes about it.\n", encoding="utf-8")
    (repo / "docs" / "extra.md").write_text("# Extra\n\nOne more document.\n", encoding="utf-8")


def _rows() -> dict[str, dict]:
    """Every live document of the tenant, keyed by file path, as the engine serves it."""
    return {e.file_path: {"tumbler": str(e.tumbler), "head_hash": e.head_hash, "indexed_at": e.indexed_at,
                          "source_mtime": e.source_mtime,
                          "meta": {k: v for k, v in e.meta.items() if k not in _FRECENCY_KEYS}, "physical_collection": e.physical_collection,
                          "index_state": e.index_state, "index_content_hash": e.index_content_hash}
            for e in active_reader().all_documents() if e.file_path}


def _indexed_repo(tmp_path: Path, t3: T3Database, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _prime_owner(repo, t3, monkeypatch)
    _write_repo(repo)
    _commit(repo)
    _index(repo, t3, monkeypatch)
    return repo


def test_a_warm_full_run_is_one_commit_and_rewrites_no_row(
    tmp_path: Path, catalog_env: Path, local_t3: T3Database, monkeypatch: pytest.MonkeyPatch, wire,
) -> None:
    repo = _indexed_repo(tmp_path, local_t3, monkeypatch)
    before = _rows()
    assert len(before) >= 5 and all(r["indexed_at"] for r in before.values()), "non-vacuity: indexed documents to compare"
    wire.clear()
    _index(repo, local_t3, monkeypatch)
    assert _n(wire, COMMIT) == 1 and _n(wire, STAGE) == 1
    sent = {p for _m, p in wire}
    assert not sent & SINGLE_WRITES, f"the warm run sent a single-row write the commit should have made: {sorted(sent & SINGLE_WRITES)}"
    assert _rows() == before, "no row was rewritten: indexed_at, head and every column are as they were"


def test_a_head_bump_alone_updates_every_head_and_fences_no_document(
    tmp_path: Path, catalog_env: Path, local_t3: T3Database, monkeypatch: pytest.MonkeyPatch, wire,
) -> None:
    repo = _indexed_repo(tmp_path, local_t3, monkeypatch)
    before = _rows()
    old_head = _head(repo)
    _git(repo, "commit", "--allow-empty", "-m", "bump")
    new_head = _head(repo)
    assert new_head != old_head
    call_order = _wrap_fence_helpers(monkeypatch)
    wire.clear()
    _index(repo, local_t3, monkeypatch)
    after = _rows()
    assert _n(wire, COMMIT) == 1
    assert set(after) == set(before)
    assert {r["head_hash"] for r in after.values()} == {new_head}, "every row took the new head"
    assert {p: r["meta"].get("content_hash") for p, r in after.items()} == \
           {p: r["meta"].get("content_hash") for p, r in before.items()}, "no content hash changed"
    assert call_order == [], f"a bare head bump begins real work on no document: {call_order}"


def test_a_delta_run_with_a_deleted_file_trashes_it_and_touches_no_unvisited_row(
    tmp_path: Path, catalog_env: Path, local_t3: T3Database, monkeypatch: pytest.MonkeyPatch, wire,
) -> None:
    repo = _indexed_repo(tmp_path, local_t3, monkeypatch)
    base = _head(repo)
    before = _rows()
    (repo / "docs" / "extra.md").unlink()
    (repo / "docs" / "guide.md").write_text("# Guide\n\nA guide, edited.\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "delete and edit")
    wire.clear()
    phases = _index_delta(repo, local_t3, monkeypatch, base=base)
    assert any(p.startswith("--since-head: 1 changed, 1 deleted") for p in phases), phases
    assert _n(wire, COMMIT) == 1
    assert not {p for _m, p in wire} & {"/v1/catalog/delete", "/v1/catalog/delete_many"}, "the commit trashed it"
    after = _rows()
    assert "docs/extra.md" not in after, "the deleted file's document is trashed"
    unvisited = [p for p in before if p not in ("docs/extra.md", "docs/guide.md")]
    assert len(unvisited) >= 3, "non-vacuity: documents the delta did not visit"
    for p in unvisited:
        assert after[p] == before[p], f"{p} was not in the delta and was touched"


def test_the_commit_does_not_move_the_owner_head_hash_but_the_run_end_does(
    tmp_path: Path, catalog_env: Path, local_t3: T3Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _indexed_repo(tmp_path, local_t3, monkeypatch)
    cat = HttpCatalogClient()
    try:
        owner = cat.owner_for_repo(next(iter(
            o for o in [_repo_identity(repo)[1]])))
        assert owner is not None
        base = indexer._get_owner_head_hash(repo)
        _git(repo, "commit", "--allow-empty", "-m", "bump")
        new_head = _head(repo)
        files = [(repo / "src" / "app.py", "code", "code__x")]
        indexer._catalog_hook(repo=repo, repo_name=repo.name, repo_hash=_repo_identity(repo)[1],
                              head_hash=new_head, indexed_files=files, walk="partial")
        assert indexer._get_owner_head_hash(repo) == base, "the commit never moves the owner's head_hash (the run end does)"
        _index(repo, local_t3, monkeypatch)
        assert indexer._get_owner_head_hash(repo) == new_head, "a run with nothing deferred or failed advances it at its end"
    finally:
        cat.close()


def test_a_path_another_owner_holds_is_reported_and_that_row_is_left_as_it_is(
    tmp_path: Path, catalog_env: Path, local_t3: T3Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _prime_owner(repo, local_t3, monkeypatch)
    _write_repo(repo)
    _commit(repo)
    cat = HttpCatalogClient()
    try:
        other = cat.register_owner("e2e_other", "curator")
        held = (repo / "docs" / "notes.md").resolve()
        foreign = str(cat.register(other, "foreign notes", content_type="prose", file_path="docs/notes.md",
                                   physical_collection="docs__foreign", source_uri=f"file://{held}"))
    finally:
        cat.close()
    with structlog.testing.capture_logs() as logs:
        _index(repo, local_t3, monkeypatch)
    assert [e for e in logs if e.get("event") == "catalog_register_reconciled_onto_existing_row"
            and e.get("tumbler") == foreign], "the run reports the row it resolved onto"
    row = HttpCatalogClient()
    try:
        entry = row.resolve(foreign)
        assert entry.physical_collection == "docs__foreign" and entry.title == "foreign notes", "that row is unchanged"
    finally:
        row.close()


def test_hand_set_tags_and_category_are_kept_and_source_format_is_restamped(
    tmp_path: Path, catalog_env: Path, local_t3: T3Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _indexed_repo(tmp_path, local_t3, monkeypatch)
    row = _rows()["docs/guide.md"]
    cat = HttpCatalogClient()
    try:
        cat.update(row["tumbler"], meta={"tags": ["curated"], "category": "curated-cat", "source_format": "bogus"})
    finally:
        cat.close()
    _index(repo, local_t3, monkeypatch)
    meta = _rows()["docs/guide.md"]["meta"]
    assert meta["tags"] == ["curated"] and meta["category"] == "curated-cat", meta
    assert meta["source_format"] != "bogus", "source_format is derived, never curated: it is re-stamped"


def test_a_sensitive_glob_on_an_existing_unflagged_document_is_raised_by_the_client_not_the_commit(
    tmp_path: Path, catalog_env: Path, local_t3: T3Database, monkeypatch: pytest.MonkeyPatch, wire,
) -> None:
    from nexus.indexer import index_repository
    from nexus.registry import RepoRegistry
    from unittest.mock import patch
    from tests.conftest import fake_credentials

    repo = _indexed_repo(tmp_path, local_t3, monkeypatch)
    assert active_reader().by_file_path(active_reader().owner_for_repo(_repo_identity(repo)[1]),
                                        "docs/notes.md").sensitive is False
    wire.clear()
    monkeypatch.setenv("NX_LOCAL", "1")
    reg = RepoRegistry(repo.parent / "repos.json")
    reg.add(repo)
    with patch("nexus.db.make_t3", return_value=local_t3), patch("nexus.config.get_credential", side_effect=fake_credentials()):
        index_repository(repo, reg, force=False, sensitive_globs=("docs/notes.md",))
    assert _n(wire, COMMIT) == 1
    assert _n(wire, "/v1/catalog/sensitivity") >= 1, "the client raised the flag through the audited route"
    entry = active_reader().by_file_path(active_reader().owner_for_repo(_repo_identity(repo)[1]), "docs/notes.md")
    assert entry.sensitive is True
    other = active_reader().by_file_path(active_reader().owner_for_repo(_repo_identity(repo)[1]), "docs/guide.md")
    assert other.sensitive is False, "only the matching document is flagged"


def test_a_commit_job_the_engine_lost_is_resubmitted_once_and_the_state_is_right(
    tmp_path: Path, catalog_env: Path, local_t3: T3Database, monkeypatch: pytest.MonkeyPatch, wire,
) -> None:
    """An engine restart loses the job registry, so the poll of the commit answers 404 although the commit ran. The
    same {owner, run_id} is submitted again and the engine answers the stored outcome of the committed run."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_repo(repo)
    _commit(repo)
    real_get = HttpCatalogClient._get
    lost = {"n": 0}

    def lose_the_first_poll(self, path, **kw):  # noqa: ANN001
        if path.startswith("/reconcile/") and lost["n"] == 0:
            lost["n"] += 1
            real_get(self, path, **kw)  # the poll reaches the engine; its answer is what a restarted engine would not give
            req = httpx.Request("GET", "http://engine/v1/catalog" + path)
            raise httpx.HTTPStatusError("no such reconcile job", request=req, response=httpx.Response(404, request=req))
        return real_get(self, path, **kw)

    monkeypatch.setattr(HttpCatalogClient, "_get", lose_the_first_poll)
    with structlog.testing.capture_logs() as logs:
        _index(repo, local_t3, monkeypatch)
    assert lost["n"] == 1
    assert [e for e in logs if e.get("event") == "engine_job_lost"], "the loss is logged"
    assert _n(wire, COMMIT) >= 2, "the commit was submitted again"
    rows = _rows()
    assert {"src/app.py", "docs/guide.md", "docs/notes.md", "docs/extra.md"} <= set(rows)
    assert all(r["index_state"] == "complete" for p, r in rows.items() if p.endswith((".py", ".md"))), \
        "the run went on with the committed state: every document was indexed"
