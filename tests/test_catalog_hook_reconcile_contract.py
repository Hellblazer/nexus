# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-193 Step 6 (nexus-u5m5g.7): what ``indexer._catalog_hook`` asks of the catalog, with no engine behind it.

The hook used to join the catalog's owner list against the walk in Python and write the difference with
``register_many``, ``update_many``, a per-file fallback and housekeeping (nexus-dst5h counted those). It now stages one
landing row per file, commits the run once and rebuilds what its callers read from the commit's outcome. The cases here
need no engine and mint no tenant: a scripted catalog stands in for the client and records every call the hook makes
to it.

* one stage request per page of at most ``_CATALOG_REGISTER_PAGE`` rows, and the commit sent exactly once per run;
* a commit that fails registers nothing: no file has an id, every one is named unregistered, and the hook calls the
  catalog for nothing but the stage and the commit (no per-file fallback);
* the pure rebuild of the outcome, for the rows an engine cannot be made to produce on demand (a ``refused`` row).

The cases that read the rows back from the real engine (warm runs, head bumps, the structures the hook fills, deleted
and partial walks) are in ``tests/integration/test_rdr193_hook_engine_contract.py``: engine-backed tests live there, and
the python_unit leg does not count them against the tenant ceiling (tests/AGENTS.md).
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from nexus.catalog import reconcile_run
from nexus.catalog.http_catalog_client import CatalogReconcileError
from nexus.indexer import _catalog_hook

REPO_HASH = "6a1c0de1"


class _ScriptedCatalog:
    """The catalog as the hook sees it: an owner, a stage that records its pages, a commit that answers one
    ``unchanged`` row per staged present row (or fails). The per-file writes of the old hook are refused and recorded in
    ``unexpected``."""

    def __init__(self, *, fail: CatalogReconcileError | None = None) -> None:
        self.fail = fail
        self.stages: list[list[dict]] = []
        self.commits: list[str] = []
        self.unexpected: list[str] = []
        self.audited: list[dict] = []  # the failure audit the hook records (it writes a T2 row, which this module has no tenant for)

    def owner_for_repo(self, repo_hash: str) -> str:  # noqa: ARG002
        return "1.7"

    def reconcile_stage(self, owner, run_id, walk, expected_rows, rows) -> int:  # noqa: ANN001, ARG002
        self.stages.append(list(rows))
        return len(rows)

    def reconcile_commit(self, owner, run_id) -> dict:  # noqa: ANN001
        self.commits.append(run_id)
        if self.fail is not None:
            raise self.fail
        present = [r for page in self.stages for r in page if r["kind"] == "present"]
        rows = [{"file_path": r["file_path"], "tumbler": f"1.7.{i}", "action": "unchanged",
                 "content_hash_changed": False, "sensitive": False, "index_state": None, "index_content_hash": ""}
                for i, r in enumerate(present, 1)]
        return {"rows": rows, "trashed": [], "renamed": [], "swept": [], "duplicate_path": [],
                "deleted_skipped_foreign_owner": [], "sensitivity_pending": []}

    def close(self) -> None:
        pass



def _refuse(name: str):
    def call(self: _ScriptedCatalog, *a, **k):  # noqa: ANN002, ANN003, ARG001
        self.unexpected.append(name)
        raise AssertionError(f"the hook called {name}, which a reconcile run never needs")

    return call


# the per-file writes the hook made before the reconcile commit (nexus-dst5h counted them)
for _name in ("register", "register_many", "update", "update_many", "delete", "delete_many", "link", "unlink",
              "register_owner"):
    setattr(_ScriptedCatalog, _name, _refuse(_name))


@pytest.fixture
def scripted(monkeypatch: pytest.MonkeyPatch) -> _ScriptedCatalog:
    cat = _ScriptedCatalog()
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda **kw: cat)
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_writer", lambda **kw: cat)
    monkeypatch.setattr("nexus.indexer.record_catalog_hook_failure", lambda **kw: cat.audited.append(kw))
    return cat


def _write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def _hook(root: Path, files: list[tuple[Path, str, str]], *, head: str = "h1", **kw):
    return _catalog_hook(repo=root, repo_name="hookrepo", repo_hash=REPO_HASH, head_hash=head,
                         indexed_files=files, **kw)


# ── requests counted ─────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("n", "page"), [(7, 3), (6, 3), (1, 3), (0, 3)])
def test_the_run_is_staged_in_pages_and_committed_once(
    tmp_path: Path, scripted: _ScriptedCatalog, monkeypatch: pytest.MonkeyPatch, n: int, page: int,
) -> None:
    monkeypatch.setattr("nexus.indexer._CATALOG_REGISTER_PAGE", page)
    files = [(_write(tmp_path, f"p/f{i}.py", f"x = {i}\n"), "code", "code__hookrepo") for i in range(n)]
    ids = _hook(tmp_path, files)
    assert len(ids) == n
    assert [len(p) for p in scripted.stages] == [min(page, n - s) for s in range(0, max(n, 1), page)], (
        "ceil(N/page) stage requests of at most one page each, one empty page for an empty run")
    assert len(scripted.stages) == max(1, math.ceil(n / page))
    assert len(scripted.commits) == 1, "the commit is sent once per run, however many pages were staged"
    assert scripted.unexpected == []


# ── no fallback: a failed commit registers nothing ──────────────────────────────────────────────────────────────

def test_a_failed_commit_leaves_every_file_without_an_id_and_names_it_unregistered(
    tmp_path: Path, scripted: _ScriptedCatalog,
) -> None:
    """The hook's own half of "a failed commit registers nothing". What the RUN then does about it (no chunk written,
    a non-zero exit, the since-head base held) is ``tests/test_rdr193_failed_commit_run.py``: the hook never writes
    chunks or manifests, so nothing here could say so. That the engine applies nothing of a run it could not commit is
    the engine's and is read back in ``tests/integration/test_rdr193_hook_engine_contract.py``."""
    cat = scripted
    cat.fail = CatalogReconcileError("transient_db_error", "the engine said no", http_status=503)
    a = _write(tmp_path, "a.py", "a\n")
    b = _write(tmp_path, "b.py", "b\n")
    unregistered: dict[Path, str] = {}
    failures: list[str] = []
    ids = _hook(tmp_path, [(a, "code", "code__hookrepo"), (b, "code", "code__hookrepo")], unregistered=unregistered,
                failures=failures)
    assert ids == {}
    assert unregistered == {a: "catalog_hook_failed", b: "catalog_hook_failed"}
    assert len(failures) == 1 and "transient_db_error" in failures[0]
    assert len(cat.commits) == 1, "the commit was tried once and not retried by the hook"
    assert [a["hook_name"] for a in cat.audited] == ["catalog_index_hook"], "the failure is audited once"
    assert "transient_db_error" in cat.audited[0]["error"]
    assert cat.unexpected == [], f"no per-file fallback after a failed commit, the hook also called {cat.unexpected}"


# ── the pure rebuild, for the rows an engine cannot be made to produce on demand ────────────────────────────────

def _outcome(*rows: dict, pending: tuple[str, ...] = ()) -> dict:
    return {"rows": list(rows), "trashed": [], "renamed": [], "swept": [], "duplicate_path": [],
            "deleted_skipped_foreign_owner": [], "sensitivity_pending": list(pending)}


def _row(path: str, action: str, tumbler: str | None, **kw) -> dict:
    return {"file_path": path, "tumbler": tumbler, "action": action, "content_hash_changed": False, "sensitive": False,
            "index_state": None, "index_content_hash": "", **kw}


def test_a_refused_row_has_no_id_and_is_named_with_the_engines_reason(tmp_path: Path) -> None:
    bad, ok = tmp_path / "bad.py", tmp_path / "ok.py"
    staged = {"bad.py": {"content_type": "code", "content_hash": "h", "physical_collection": "c"},
              "ok.py": {"content_type": "code", "content_hash": "h", "physical_collection": "c"}}
    applied = reconcile_run.apply_outcome(
        _outcome(_row("bad.py", "refused", None, reason="derived source_uri lies outside the owner repo_root"),
                 _row("ok.py", "unchanged", "1.2.3")),
        rel_to_abs={"bad.py": bad, "ok.py": ok}, staged=staged)
    assert applied.file_to_doc_id == {ok: "1.2.3"}
    assert applied.refused == {bad: "derived source_uri lies outside the owner repo_root"}


def test_keep_and_deleted_rows_belong_to_no_file_of_the_run(tmp_path: Path) -> None:
    ok = tmp_path / "ok.py"
    applied = reconcile_run.apply_outcome(
        _outcome(_row("ok.py", "created", "1.2.1"), _row("held.pdf", "keep", "1.2.2"), _row("gone.py", "deleted", "1.2.3")),
        rel_to_abs={"ok.py": ok},
        staged={"ok.py": {"content_type": "code", "content_hash": "h", "physical_collection": "c"}})
    assert applied.file_to_doc_id == {ok: "1.2.1"}
    assert [str(t) for t in applied.new_tumblers] == ["1.2.1"]


def test_the_stage_and_commit_of_an_empty_run_sends_one_empty_page() -> None:
    class W:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        def reconcile_stage(self, *a):  # noqa: ANN002
            self.calls.append(("stage", a))
            return 0

        def reconcile_commit(self, *a):  # noqa: ANN002
            self.calls.append(("commit", a))
            return {"rows": []}

    w = W()
    assert reconcile_run.stage_and_commit(w, "1.2", [], walk="complete") == {"rows": []}
    assert [c[0] for c in w.calls] == ["stage", "commit"]
    assert w.calls[0][1][3] == 0 and w.calls[0][1][4] == []
