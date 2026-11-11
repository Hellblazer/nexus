# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-193 Step 6 (nexus-u5m5g.7): what ``indexer._catalog_hook`` asks of the engine, counted on the wire and read back.

The hook stages one landing row per file, commits the run once and rebuilds what its callers read from the commit's
outcome. This module pins that contract against the real engine (``t2_tenant``: the service substrate), counting every
HTTP request the catalog clients send and reading the rows back:

* a warm run (nothing changed) sends one commit and no single update, register, delete or link request;
* each structure the hook fills is rebuilt from the outcome, with no extra request to read it;
* a commit the engine cannot find fails the hook and registers nothing;
* a delta (``walk="partial"``) run touches no row it did not visit and trashes what ``deleted_relpaths`` names.

The cases that need no engine (the stage paging, a failed commit's hook-side half, the pure rebuild of the outcome) are
in ``tests/test_catalog_hook_reconcile_contract.py`` and mint no tenant. These live in ``tests/integration`` because an
engine-backed test is not counted by the python_unit tenant ceiling there (tests/AGENTS.md); CI runs them when the hook
or this file changes (``integration (affected)``) and the nightly gate runs the whole family.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import structlog.testing

from nexus.catalog import link_generator
from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.db.t2._refreshable_client import RefreshableHttpStoreMixin
from tests.test_catalog_hook_reconcile_contract import REPO_HASH, _hook, _write

FORBIDDEN_SINGLE_WRITES = {"/v1/catalog/update", "/v1/catalog/update_many", "/v1/catalog/doc/register",
                           "/v1/catalog/doc/register_many", "/v1/catalog/delete", "/v1/catalog/delete_many",
                           "/v1/catalog/link", "/v1/catalog/unlink"}

pytestmark = [pytest.mark.integration, pytest.mark.t2_tenant]


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in (("GIT_AUTHOR_NAME", "Test"), ("GIT_AUTHOR_EMAIL", "t@t.invalid"),
                 ("GIT_COMMITTER_NAME", "Test"), ("GIT_COMMITTER_EMAIL", "t@t.invalid")):
        monkeypatch.setenv(k, v)


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Every (method, path) any client instance sends, in order, with the real transport underneath."""
    sent: list[tuple[str, str]] = []
    real = RefreshableHttpStoreMixin._send

    def spy(self, method, path, **kw):  # noqa: ANN001
        sent.append((method, path))
        return real(self, method, path, **kw)

    monkeypatch.setattr(RefreshableHttpStoreMixin, "_send", spy)
    return sent


def _count(sent: list[tuple[str, str]], path: str) -> int:
    return sum(1 for _m, p in sent if p == path)


def _commits(sent: list[tuple[str, str]]) -> int:
    return _count(sent, "/v1/catalog/reconcile/commit")


def _stages(sent: list[tuple[str, str]]) -> int:
    return _count(sent, "/v1/catalog/reconcile/stage")


def _doc(root: Path, rel: str):  # noqa: ARG001
    cat = HttpCatalogClient()
    try:
        return cat.by_file_path(cat.owner_for_repo(REPO_HASH), rel)
    finally:
        cat.close()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ── requests counted ─────────────────────────────────────────────────────────────────────────────────────────────

def test_a_cold_run_stages_one_page_and_commits_once(tmp_path: Path, wire) -> None:
    a = _write(tmp_path, "a.py", "a = 1\n")
    b = _write(tmp_path, "b.md", "# b\nbody\n")
    ids = _hook(tmp_path, [(a, "code", "code__hookrepo"), (b, "prose", "docs__hookrepo")])
    assert set(ids) == {a, b}
    assert _stages(wire) == 1 and _commits(wire) == 1


def test_a_warm_run_commits_once_and_sends_no_single_write(tmp_path: Path, wire) -> None:
    files = [(_write(tmp_path, f"src/f{i}.py", f"x = {i}\n"), "code", "code__hookrepo") for i in range(5)]
    first = _hook(tmp_path, files)
    wire.clear()
    again = _hook(tmp_path, files)
    assert again == first, "the same files resolve to the same ids"
    assert _commits(wire) == 1 and _stages(wire) == 1
    assert not {p for _m, p in wire} & FORBIDDEN_SINGLE_WRITES, sorted({p for _m, p in wire})


def test_a_head_bump_is_one_commit_and_still_no_single_write(tmp_path: Path, wire) -> None:
    files = [(_write(tmp_path, f"f{i}.py", f"x = {i}\n"), "code", "code__hookrepo") for i in range(4)]
    _hook(tmp_path, files, head="h1")
    wire.clear()
    _hook(tmp_path, files, head="h2")
    assert _commits(wire) == 1
    assert not {p for _m, p in wire} & FORBIDDEN_SINGLE_WRITES
    assert _doc(tmp_path, "f0.py").head_hash == "h2", "the commit moved the head of the row"


# ── the structures the hook fills, rebuilt from the outcome ─────────────────────────────────────────────────────

def test_new_files_fill_new_tumblers_content_types_and_needs_fence(tmp_path: Path) -> None:
    a = _write(tmp_path, "a.py", "a = 1\n")
    r = _write(tmp_path, "docs/rdr/rdr-001-x.md", "---\ntitle: t\nid: RDR-001\n---\n# x\nbody\n")
    needs_fence: dict = {}
    ids = _hook(tmp_path, [(a, "code", "code__hookrepo"), (r, "rdr", "rdr__hookrepo")], needs_fence=needs_fence)
    assert needs_fence == {ids[a]: (_sha("a = 1\n"), "code__hookrepo"),
                           ids[r]: (hashlib.sha256(r.read_bytes()).hexdigest(), "rdr__hookrepo")}
    assert _doc(tmp_path, "a.py").title == "a.py"


def test_a_changed_file_fills_needs_fence_and_an_unchanged_one_does_not(tmp_path: Path) -> None:
    a = _write(tmp_path, "a.py", "v1\n")
    b = _write(tmp_path, "b.py", "same\n")
    files = [(a, "code", "code__hookrepo"), (b, "code", "code__hookrepo")]
    ids = _hook(tmp_path, files)
    a.write_text("v2\n")
    needs_fence: dict = {}
    _hook(tmp_path, files, head="h2", needs_fence=needs_fence)
    assert needs_fence == {ids[a]: (_sha("v2\n"), "code__hookrepo")}


def test_a_head_bump_alone_fills_no_needs_fence_and_relinks_no_rdr(tmp_path: Path, monkeypatch) -> None:
    r = _write(tmp_path, "docs/rdr/rdr-001-x.md", "---\ntitle: t\nid: RDR-001\n---\n# x\n")
    files = [(r, "rdr", "rdr__hookrepo")]
    _hook(tmp_path, files, head="h1")
    seen: list[list[str]] = []

    def bound(cat, repo):  # noqa: ANN001
        def gen(cat, *, writer, new_tumblers, new_content_types, inputs=None):  # noqa: ANN001, ARG001
            seen.append([str(t) for t in new_tumblers])
            return 0
        return "rdr-dependency", gen

    monkeypatch.setattr(link_generator, "bind_rdr_dependency_generator", bound)
    needs_fence: dict = {}
    _hook(tmp_path, files, head="h2", needs_fence=needs_fence)
    assert needs_fence == {} and seen == [[]], "a bare HEAD bump re-feeds no RDR to the dependency generator"


def test_a_changed_rdr_is_handed_to_the_dependency_generator_as_if_new(tmp_path: Path, monkeypatch) -> None:
    r = _write(tmp_path, "docs/rdr/rdr-001-x.md", "---\ntitle: t\nid: RDR-001\n---\n# x\n")
    files = [(r, "rdr", "rdr__hookrepo")]
    ids = _hook(tmp_path, files)
    seen: list[list[str]] = []

    def bound(cat, repo):  # noqa: ANN001
        def gen(cat, *, writer, new_tumblers, new_content_types, inputs=None):  # noqa: ANN001, ARG001
            seen.append([str(t) for t in new_tumblers])
            return 0
        return "rdr-dependency", gen

    monkeypatch.setattr(link_generator, "bind_rdr_dependency_generator", bound)
    r.write_text("---\ntitle: t\nid: RDR-001\nsupersedes: RDR-000\n---\n# x\nmore\n")
    _hook(tmp_path, files, head="h2")
    assert seen == [[ids[r]]]


def test_a_stranded_fence_fills_stale_fence_doc_ids_and_a_complete_one_fills_complete_doc_hashes(tmp_path: Path) -> None:
    a = _write(tmp_path, "a.py", "a\n")
    b = _write(tmp_path, "b.py", "b\n")
    c = _write(tmp_path, "c.py", "c\n")
    files = [(p, "code", "code__hookrepo") for p in (a, b, c)]
    ids = _hook(tmp_path, files)
    cat = HttpCatalogClient()
    try:
        cat.begin_index_run(ids[a], _sha("a\n"), "run-a", "code__hookrepo")                  # stranded: indexing
        cat.begin_index_run(ids[b], _sha("b\n"), "run-b", "code__hookrepo")
        cat.fail_index_run(ids[b], "boom")                                                   # failed
        cat.begin_index_run(ids[c], _sha("c\n"), "run-c", "code__hookrepo")
        cat.complete_index_run(ids[c], _sha("c\n"), 0)                                       # complete
    finally:
        cat.close()
    stale: set[str] = set()
    complete: dict[str, str] = {}
    _hook(tmp_path, files, head="h2", stale_fence_doc_ids=stale, complete_doc_hashes=complete)
    assert stale == {ids[a], ids[b]}
    assert complete == {ids[c]: _sha("c\n")}


def test_a_path_another_owner_holds_in_this_runs_collection_resolves_to_that_row_and_is_reported_not_called_new(
    tmp_path: Path, capsys,
) -> None:
    """The nexus-53cae shape: the other owner's row holds the collection the run writes the file to, so the file keeps
    its tumbler (the chunks land where the row says), the row is not touched, and the run does not call it new."""
    cat = HttpCatalogClient()
    try:
        other = cat.register_owner("hook_other", "curator")
        held = _write(tmp_path, "held.txt", "held\n")
        foreign = str(cat.register(other, "foreign", content_type="prose", file_path="held.txt",
                                   physical_collection="docs__hookrepo", source_uri=f"file://{held.resolve()}"))
    finally:
        cat.close()
    with structlog.testing.capture_logs() as logs:
        ids = _hook(tmp_path, [(held, "prose", "docs__hookrepo")])
    assert ids[held] == foreign, "the chunks of the file carry the tumbler of the row that holds it"
    assert [e for e in logs if e.get("event") == "catalog_register_reconciled_onto_existing_row"]
    assert not [e for e in logs if e.get("event") == "catalog_register_skipped_foreign_owner"]
    row = HttpCatalogClient()
    try:
        assert row.resolve(foreign).physical_collection == "docs__hookrepo", "the other owner's row is unchanged"
    finally:
        row.close()
    assert "1 new" not in capsys.readouterr().err


def test_a_path_another_owner_holds_in_another_collection_gets_no_document_id_and_that_row_is_unchanged(
    tmp_path: Path, capsys,
) -> None:
    """GH #1611, the engine's own ``foreign_owner`` row and the hook's one batched read of the row's collection: the file
    is the other owner's document, so it has no id here (none of its chunks are written under that tumbler), it is named
    as a deliberate skip, and the other owner's row is as it was."""
    cat = HttpCatalogClient()
    try:
        other = cat.register_owner("hook_other", "curator")
        held = _write(tmp_path, "held.txt", "held\n")
        ok = _write(tmp_path, "ok.txt", "ok\n")
        foreign = str(cat.register(other, "foreign", content_type="prose", file_path="held.txt",
                                   physical_collection="docs__foreign", source_uri=f"file://{held.resolve()}"))
    finally:
        cat.close()
    unregistered: dict[Path, str] = {}
    with structlog.testing.capture_logs() as logs:
        ids = _hook(tmp_path, [(held, "prose", "docs__hookrepo"), (ok, "prose", "docs__hookrepo")],
                    unregistered=unregistered)
    assert ok in ids and held not in ids, "non-vacuity: the ordinary file has its id, the held one has none"
    assert unregistered == {held: f"foreign_owner:{foreign}"}
    ev = [e for e in logs if e.get("event") == "catalog_register_skipped_foreign_owner"]
    assert len(ev) == 1 and ev[0]["held_in"] == "docs__foreign" and ev[0]["written_to"] == "docs__hookrepo", logs
    row = HttpCatalogClient()
    try:
        entry = row.resolve(foreign)
        assert entry.physical_collection == "docs__foreign" and entry.title == "foreign", "the other owner's row is unchanged"
    finally:
        row.close()
    assert "held by another owner's document in another collection, not written: held.txt" in capsys.readouterr().err


def test_a_file_matching_a_sensitive_glob_is_born_flagged_and_confirmed(tmp_path: Path) -> None:
    s = _write(tmp_path, "secret/a.txt", "secret\n")
    wanted: set[Path] = set()
    confirmed: set[Path] = set()
    _hook(tmp_path, [(s, "prose", "docs__hookrepo")], sensitive_globs=("secret/*",),
          sensitive_wanted=wanted, sensitive_confirmed=confirmed)
    assert wanted == confirmed == {s}
    assert _doc(tmp_path, "secret/a.txt").sensitive is True


def test_an_existing_unflagged_document_is_raised_through_the_audited_route_not_by_the_commit(tmp_path: Path, wire) -> None:
    s = _write(tmp_path, "secret/a.txt", "secret\n")
    files = [(s, "prose", "docs__hookrepo")]
    _hook(tmp_path, files)
    assert _doc(tmp_path, "secret/a.txt").sensitive is False
    wire.clear()
    confirmed: set[Path] = set()
    _hook(tmp_path, files, sensitive_globs=("secret/*",), sensitive_confirmed=confirmed)
    assert _count(wire, "/v1/catalog/sensitivity") == 1, "one audited request raises it"
    assert confirmed == {s}
    assert _doc(tmp_path, "secret/a.txt").sensitive is True


# ── no fallback: a failed commit registers nothing ──────────────────────────────────────────────────────────────

def test_a_commit_the_engine_cannot_find_fails_the_hook_the_same_way(tmp_path: Path, monkeypatch) -> None:
    """The real engine's own failure, not a stand-in: a run whose stage never reached the engine has no header to
    commit, and the engine says so (``run_not_found``). Nothing is registered, and the failure is reported."""
    monkeypatch.setattr(HttpCatalogClient, "reconcile_stage", lambda self, *a, **k: 0)
    a = _write(tmp_path, "a.py", "a\n")
    unregistered: dict[Path, str] = {}
    failures: list[str] = []
    ids = _hook(tmp_path, [(a, "code", "code__hookrepo")], unregistered=unregistered, failures=failures)
    assert ids == {} and unregistered == {a: "catalog_hook_failed"}
    assert len(failures) == 1, failures
    assert _doc(tmp_path, "a.py") is None


# ── a file the walk saw but staged no present row for counts as present ─────────────────────────────────────────

def test_a_row_under_an_ephemeral_path_is_not_aged_toward_the_sweep_by_complete_walks(tmp_path: Path) -> None:
    """The old housekeeping built its present set from every file the walk saw, the ephemeral-skipped included, so a
    pre-guard row at such a path was never counted missing. The staged ``keep`` row keeps that: two complete walks that
    skip the file leave its row alive and uncounted."""
    ok = _write(tmp_path, "ok.py", "ok\n")
    eph = _write(tmp_path, ".claude/worktrees/agent/x.py", "e\n")
    files = [(ok, "code", "code__hookrepo"), (eph, "code", "code__hookrepo")]
    _hook(tmp_path, files[:1])
    cat = HttpCatalogClient()
    try:
        cat.register(cat.owner_for_repo(REPO_HASH), "x.py", content_type="code",
                     file_path=".claude/worktrees/agent/x.py", physical_collection="code__hookrepo")
    finally:
        cat.close()
    for head in ("h2", "h3", "h4"):
        _hook(tmp_path, files, head=head)
    row = _doc(tmp_path, ".claude/worktrees/agent/x.py")
    assert row is not None, "the row was swept"
    assert not row.meta.get("miss_count"), "the row was counted missing"


# ── deleted rows and a partial walk ─────────────────────────────────────────────────────────────────────────────

def test_deleted_relpaths_are_trashed_by_the_commit_with_no_single_delete(tmp_path: Path, wire) -> None:
    keep = _write(tmp_path, "keep.py", "k\n")
    gone = _write(tmp_path, "gone.py", "g\n")
    _hook(tmp_path, [(keep, "code", "code__hookrepo"), (gone, "code", "code__hookrepo")])
    gone.unlink()
    wire.clear()
    _hook(tmp_path, [(keep, "code", "code__hookrepo")], walk="partial", deleted_relpaths=["gone.py"])
    assert _doc(tmp_path, "gone.py") is None and _doc(tmp_path, "keep.py") is not None
    assert not {p for _m, p in wire} & FORBIDDEN_SINGLE_WRITES and _commits(wire) == 1


def test_a_partial_walk_touches_no_row_it_did_not_visit(tmp_path: Path) -> None:
    visited = _write(tmp_path, "visited.py", "v\n")
    unvisited = _write(tmp_path, "unvisited.py", "u\n")
    files = [(visited, "code", "code__hookrepo"), (unvisited, "code", "code__hookrepo")]
    _hook(tmp_path, files, head="h1")
    for _ in range(3):  # a complete walk that missed it would count and delete it in two runs
        _hook(tmp_path, [(visited, "code", "code__hookrepo")], head="h2", walk="partial")
    row = _doc(tmp_path, "unvisited.py")
    assert row is not None and row.head_hash == "h1" and not row.meta.get("miss_count")


def test_a_complete_walk_counts_a_missing_file_and_a_kept_one_is_not_counted(tmp_path: Path) -> None:
    here = _write(tmp_path, "here.py", "h\n")
    missing = _write(tmp_path, "missing.py", "m\n")
    held = _write(tmp_path, "held.pdf", "pdf\n")
    files = [(here, "code", "code__hookrepo"), (missing, "code", "code__hookrepo"), (held, "code", "code__hookrepo")]
    _hook(tmp_path, files)
    _hook(tmp_path, [(here, "code", "code__hookrepo")], head="h2", keep_paths=[held])
    assert int(_doc(tmp_path, "missing.py").meta.get("miss_count", 0)) == 1
    assert not _doc(tmp_path, "held.pdf").meta.get("miss_count"), "a held file counts as present"
