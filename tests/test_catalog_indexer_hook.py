# SPDX-License-Identifier: AGPL-3.0-or-later
"""``indexer._catalog_hook`` against the real engine.

RDR-193 Step 6 (nexus-u5m5g.7): the hook stages one landing row per file, commits the run as one engine job and
rebuilds what its callers read from the outcome. What this module no longer carries, and what covers it now:

* the nexus-dst5h / nexus-xedhp / nexus-9dvqy batching tests that counted ``by_owner``, ``update``, ``update_many``,
  ``register_many`` and ``register`` calls, drove ``update_many`` paging at the page boundary, and made
  ``register_many`` / ``update_many`` / ``by_owner`` fail to prove the per-file fallbacks (``TestCatalogHookBatchedServiceMode``
  and the counting and fallback cases of ``TestCatalogHookBatchedLookups`` and ``TestCatalogHookOwnerListFailure``):
  the client diff and its fallbacks are deleted. Their contract is now counted on the wire by
  ``tests/test_catalog_hook_reconcile_contract.py`` (one commit per run, ``ceil(N/page)`` stage requests, no single
  update/register/delete/link request on a warm run or a head bump, a failed commit registers nothing and has no
  fallback) and the HTTP client's job contract by ``tests/catalog/test_http_catalog_client_reconcile.py``.
* ``_run_housekeeping`` (deleted): ``TestRunHousekeeping`` keeps every case, driven through ``_catalog_hook`` with
  ``walk="complete"`` and compared on catalog state; the same state machine, row for row against the old path, is
  ``tests/integration/test_rdr193_reconcile_parity.py``, and the engine's own is
  ``service/src/test/java/dev/nexus/service/CatalogReconcileCommitBehaviourTest.java``.
* ``skip_housekeeping=True`` is ``walk="partial"``.
* the cross-owner announcements of nexus-1vc0n: see ``TestCatalogHookReconciledIsNotNew``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._catalog_fixture_ops import (
    ActiveCatalog,
    active_reader,
    count_documents,
    documents_by_file_path,
)


@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@test.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@test.invalid")


@pytest.fixture(autouse=True)
def _point_catalog_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Aim ``catalog_path()`` at the dir ``_make_catalog`` initialises
    (nexus-aqbrk). ActiveCatalog resolves through the same factories
    ``_catalog_hook`` uses, and on the SQLite arm those read
    ``catalog_path()``. Tests that point elsewhere still win — their in-body
    ``setenv`` runs after this."""
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(tmp_path / "catalog"))


def _make_catalog(tmp_path: Path) -> tuple[Path, ActiveCatalog]:
    """Hand back a facade over the LIVE catalog.

    nexus-aqbrk: previously returned the local ``Catalog``, so under the
    engine substrate the assertions read an empty local file while
    ``_catalog_hook`` wrote the service catalog. nexus-i711w terminal
    deletion: the local ``Catalog.init`` seeding is gone entirely.
    """
    catalog_dir = tmp_path / "catalog"
    return catalog_dir, ActiveCatalog()


class TestCatalogHookSkipped:
    @pytest.mark.t2_tenant
    def test_skipped_when_not_initialized(self, tmp_path, monkeypatch):
        from nexus.indexer import _catalog_hook

        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(tmp_path / "no-catalog"))
        # Should not raise
        _catalog_hook(
            repo=tmp_path,
            repo_name="test",
            repo_hash="abcd1234",
            head_hash="abc",
            indexed_files=[],
        )


@pytest.mark.t2_tenant
class TestCatalogHookOwner:
    def test_owner_auto_created(self, tmp_path, monkeypatch):
        from nexus.indexer import _catalog_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        _catalog_hook(
            repo=tmp_path,
            repo_name="nexus",
            repo_hash="571b8edd",
            head_hash="abc123",
            indexed_files=[],
        )
        assert cat.owner_for_repo("571b8edd") is not None

    def test_owner_reused_on_reindex(self, tmp_path, monkeypatch):
        from nexus.indexer import _catalog_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        _catalog_hook(
            repo=tmp_path, repo_name="nexus", repo_hash="571b8edd",
            head_hash="abc", indexed_files=[],
        )
        _catalog_hook(
            repo=tmp_path, repo_name="nexus", repo_hash="571b8edd",
            head_hash="def", indexed_files=[],
        )
        # Should still be the same owner
        rows = (len(cat.list_owners()),)
        assert rows[0] == 1


@pytest.mark.t2_tenant
class TestCatalogHookDocuments:
    def test_document_registered_on_first_index(self, tmp_path, monkeypatch):
        from nexus.indexer import _catalog_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        # Create a file to index
        src = tmp_path / "src" / "main.py"
        src.parent.mkdir(parents=True)
        src.write_text("print('hello')")

        _catalog_hook(
            repo=tmp_path, repo_name="nexus", repo_hash="571b8edd",
            head_hash="abc123",
            indexed_files=[(src, "code", "code__nexus")],
        )
        owner = cat.owner_for_repo("571b8edd")
        entry = cat.by_file_path(owner, "src/main.py")
        assert entry is not None
        assert entry.title == "main.py"
        assert entry.content_type == "code"
        assert entry.physical_collection == "code__nexus"

    def test_document_updated_on_reindex(self, tmp_path, monkeypatch):
        from nexus.indexer import _catalog_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        src = tmp_path / "main.py"
        src.write_text("v1")

        _catalog_hook(
            repo=tmp_path, repo_name="nexus", repo_hash="571b8edd",
            head_hash="aaa",
            indexed_files=[(src, "code", "code__nexus")],
        )
        owner = cat.owner_for_repo("571b8edd")
        entry1 = cat.by_file_path(owner, "main.py")
        tumbler1 = entry1.tumbler

        _catalog_hook(
            repo=tmp_path, repo_name="nexus", repo_hash="571b8edd",
            head_hash="bbb",
            indexed_files=[(src, "code", "code__nexus")],
        )
        entry2 = cat.by_file_path(owner, "main.py")
        assert entry2.tumbler == tumbler1  # same tumbler
        assert entry2.head_hash == "bbb"  # updated hash

    def test_multiple_files(self, tmp_path, monkeypatch):
        from nexus.indexer import _catalog_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        a = tmp_path / "a.py"
        b = tmp_path / "b.md"
        a.write_text("code")
        b.write_text("prose")

        _catalog_hook(
            repo=tmp_path, repo_name="nexus", repo_hash="571b8edd",
            head_hash="abc",
            indexed_files=[(a, "code", "code__nexus"), (b, "prose", "docs__nexus")],
        )
        owner = cat.owner_for_repo("571b8edd")
        entries = cat.by_owner(owner)
        assert len(entries) == 2


@pytest.mark.t2_tenant
class TestFixturePathAutoStamp:
    """nexus-4jj40 (RDR-200 Phase 1c evidence hygiene, review round 3):
    ``nx index repo`` auto-stamps ``non_evidentiary`` in ``Document.meta``
    for files under ``tests/fixtures/`` (fixture DATA), never for a
    sibling test MODULE that exercises real code -- see
    ``nexus.catalog.types.is_fixture_path``'s docstring.
    """

    def test_fixture_path_is_stamped(self, tmp_path, monkeypatch):
        from nexus.indexer import _catalog_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        fixture = tmp_path / "tests" / "fixtures" / "sample.json"
        fixture.parent.mkdir(parents=True)
        fixture.write_text("{}")

        _catalog_hook(
            repo=tmp_path, repo_name="nexus", repo_hash="571b8edd",
            head_hash="abc123",
            indexed_files=[(fixture, "code", "code__nexus")],
        )
        owner = cat.owner_for_repo("571b8edd")
        entry = cat.by_file_path(owner, "tests/fixtures/sample.json")
        assert entry is not None
        assert entry.meta.get("non_evidentiary") is True

    def test_sibling_test_module_is_not_stamped(self, tmp_path, monkeypatch):
        """A test MODULE living beside tests/fixtures/ (same tests/
        directory, but NOT under the fixtures/ subdirectory) is never
        auto-stamped -- the bead's own cited leaks (tests/test_*.py
        files) are marked manually via `nx catalog update --meta`, not
        by this automatic rule."""
        from nexus.indexer import _catalog_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        test_module = tmp_path / "tests" / "test_operator_dispatch.py"
        test_module.parent.mkdir(parents=True)
        test_module.write_text("def test_x(): pass")

        _catalog_hook(
            repo=tmp_path, repo_name="nexus", repo_hash="571b8edd",
            head_hash="abc123",
            indexed_files=[(test_module, "code", "code__nexus")],
        )
        owner = cat.owner_for_repo("571b8edd")
        entry = cat.by_file_path(owner, "tests/test_operator_dispatch.py")
        assert entry is not None
        assert not entry.meta.get("non_evidentiary")


@pytest.mark.t2_tenant
class TestNeedsFenceOutParam:
    """nexus-hg2dw: ``needs_fence`` names exactly the documents this run's
    registration determined need real indexing work — new registrations,
    and existing documents whose FILE CONTENT genuinely changed — and
    excludes everything else, including a bare head_hash bump that
    touches every tracked document's row without any file's content
    changing at all."""

    def test_new_file_populates_needs_fence(self, tmp_path, monkeypatch):
        from nexus.indexer import _catalog_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        src = tmp_path / "main.py"
        src.write_text("print('hello')")

        needs_fence: dict = {}
        _catalog_hook(
            repo=tmp_path, repo_name="nexus", repo_hash="571b8edd",
            head_hash="abc",
            indexed_files=[(src, "code", "code__nexus")],
            needs_fence=needs_fence,
        )

        owner = cat.owner_for_repo("571b8edd")
        entry = cat.by_file_path(owner, "main.py")
        assert str(entry.tumbler) in needs_fence
        content_hash, collection = needs_fence[str(entry.tumbler)]
        assert content_hash  # a real sha256, non-empty
        assert collection == "code__nexus"

    def test_head_hash_only_bump_does_not_populate_needs_fence(
        self, tmp_path, monkeypatch,
    ):
        """The control case this whole design turns on: a bare git commit
        (HEAD moves) re-sends every tracked document's head_hash without
        touching a single file's content. Fencing these would force
        every unchanged document to 'indexing' on every commit."""
        from nexus.indexer import _catalog_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        src = tmp_path / "main.py"
        src.write_text("print('hello')")

        _catalog_hook(
            repo=tmp_path, repo_name="nexus", repo_hash="571b8edd",
            head_hash="commit-1",
            indexed_files=[(src, "code", "code__nexus")],
            needs_fence={},
        )

        needs_fence: dict = {}
        _catalog_hook(
            repo=tmp_path, repo_name="nexus", repo_hash="571b8edd",
            head_hash="commit-2",  # HEAD moved; file content is IDENTICAL
            indexed_files=[(src, "code", "code__nexus")],
            needs_fence=needs_fence,
        )

        assert needs_fence == {}

    def test_content_change_on_existing_doc_populates_needs_fence(
        self, tmp_path, monkeypatch,
    ):
        from nexus.indexer import _catalog_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        src = tmp_path / "main.py"
        src.write_text("v1")

        _catalog_hook(
            repo=tmp_path, repo_name="nexus", repo_hash="571b8edd",
            head_hash="commit-1",
            indexed_files=[(src, "code", "code__nexus")],
            needs_fence={},
        )
        owner = cat.owner_for_repo("571b8edd")
        tumbler = str(cat.by_file_path(owner, "main.py").tumbler)

        src.write_text("v2 — content actually changed")
        needs_fence: dict = {}
        _catalog_hook(
            repo=tmp_path, repo_name="nexus", repo_hash="571b8edd",
            head_hash="commit-2",
            indexed_files=[(src, "code", "code__nexus")],
            needs_fence=needs_fence,
        )

        assert tumbler in needs_fence

    def test_needs_fence_none_default_is_a_no_op(self, tmp_path, monkeypatch):
        """The out-param is optional — omitting it must not change any
        other behavior (backward compatible with every existing caller
        that doesn't pass it)."""
        from nexus.indexer import _catalog_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        src = tmp_path / "main.py"
        src.write_text("print('hello')")

        # Should not raise despite needs_fence being omitted entirely.
        file_to_doc_id = _catalog_hook(
            repo=tmp_path, repo_name="nexus", repo_hash="571b8edd",
            head_hash="abc",
            indexed_files=[(src, "code", "code__nexus")],
        )
        assert file_to_doc_id


class _SpyProxy:
    """Counting proxy around a real reader/writer (integration over mocks).

    Delegates everything; records call counts for the methods the
    nexus-dst5h batching contract pins.
    """

    def __init__(self, target):
        self._target = target
        self.calls: dict[str, int] = {}

    def __getattr__(self, name):
        attr = getattr(self._target, name)
        if callable(attr):
            def counted(*a, **kw):
                self.calls[name] = self.calls.get(name, 0) + 1
                return attr(*a, **kw)
            return counted
        return attr


def _spy_factories(monkeypatch) -> tuple[list["_SpyProxy"], list["_SpyProxy"]]:
    """Patch the catalog factories to hand out spy-wrapped real objects."""
    import nexus.catalog.factory as factory

    readers: list[_SpyProxy] = []
    writers: list[_SpyProxy] = []
    real_reader, real_writer = factory.make_catalog_reader, factory.make_catalog_writer

    def spy_reader(**kw):
        spy = _SpyProxy(real_reader(**kw))
        readers.append(spy)
        return spy

    def spy_writer(**kw):
        spy = _SpyProxy(real_writer(**kw))
        writers.append(spy)
        return spy

    monkeypatch.setattr(factory, "make_catalog_reader", spy_reader)
    monkeypatch.setattr(factory, "make_catalog_writer", spy_writer)
    return readers, writers


@pytest.mark.t2_tenant
class TestCatalogHookBatchedLookups:
    """nexus-dst5h: the pre-index sweep must not do per-file catalog round-trips, and a warm re-run writes nothing for
    unchanged files. After RDR-193 Step 6 (nexus-u5m5g.7) the hook asks the engine to reconcile the run, so the request
    counts are pinned on the wire by ``tests/test_catalog_hook_reconcile_contract.py``; what stays here is the
    observable catalog state a re-run leaves, and that the hook issues no owner list and no per-file lookup of its own."""

    def _index(self, tmp_path, files, head_hash="aaa"):
        from nexus.indexer import _catalog_hook

        return _catalog_hook(
            repo=tmp_path, repo_name="nexus", repo_hash="571b8edd",
            head_hash=head_hash,
            indexed_files=[(f, "code", "code__nexus") for f in files],
        )

    def test_the_hook_issues_no_owner_list_and_no_per_file_lookup(self, tmp_path, monkeypatch):
        # was: test_no_per_file_lookups_single_owner_list (which counted 2 owner lists, the hook's join and
        # housekeeping's fresh fetch; both moved into the engine's commit, so the count is now 0). GH #1610 added one
        # by_owner read on a complete walk from a git main checkout (the gone-file retirement); tmp_path is not a git
        # repository, so that read is skipped here and this count stays 0. tests/test_indexer_gone_files.py covers it.
        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
        files = []
        for i in range(3):
            f = tmp_path / f"f{i}.py"
            f.write_text(f"code {i}")
            files.append(f)

        readers, _ = _spy_factories(monkeypatch)
        result = self._index(tmp_path, files)

        reader = readers[0]
        assert reader.calls.get("by_file_path", 0) == 0
        assert reader.calls.get("by_owner", 0) == 0
        assert set(result) == set(files)

    def test_changed_head_hash_still_updates(self, tmp_path, monkeypatch):
        # was: counted one update request; the count is the contract test's (a head bump is one commit and no single
        # write), what is pinned here is the row the commit left
        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
        f = tmp_path / "a.py"
        f.write_text("v1")

        self._index(tmp_path, [f], head_hash="aaa")
        owner = cat.owner_for_repo("571b8edd")
        tumbler = cat.by_file_path(owner, "a.py").tumbler
        self._index(tmp_path, [f], head_hash="bbb")

        entry = cat.by_file_path(owner, "a.py")
        assert entry.head_hash == "bbb" and entry.tumbler == tumbler

    def test_changed_content_still_updates(self, tmp_path, monkeypatch):
        # was: counted one update request; now the row's content hash is what shows the update happened
        import hashlib

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
        f = tmp_path / "a.py"
        f.write_text("v1")

        self._index(tmp_path, [f], head_hash="same")
        f.write_text("v2 changed")
        self._index(tmp_path, [f], head_hash="same")

        owner = cat.owner_for_repo("571b8edd")
        assert cat.by_file_path(owner, "a.py").meta["content_hash"] == hashlib.sha256(b"v2 changed").hexdigest()

    def test_new_file_still_registers_alongside_warm_files(self, tmp_path, monkeypatch):
        # was: asserted one register_many and zero register/update requests; the request counts moved to the
        # contract test, the outcome stays
        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
        old = tmp_path / "old.py"
        old.write_text("old")

        first = self._index(tmp_path, [old], head_hash="same")
        new = tmp_path / "new.py"
        new.write_text("new")
        result = self._index(tmp_path, [old, new], head_hash="same")

        assert set(result) == {old, new}
        assert result[old] == first[old], "the warm file keeps its id"
        owner = cat.owner_for_repo("571b8edd")
        assert cat.by_file_path(owner, "new.py") is not None


class TestCatalogHookOwnerListFailure:
    """nexus-dst5h review Critical: a failing owner-list fetch must not silently no-op the work that reads it.

    The hook no longer reads the owner list at all (RDR-193 Step 6): the join against the catalog is the engine's, so
    the old ``test_by_owner_failure_falls_back_per_file`` (a failing ``by_owner`` degrades the hook to per-file lookups)
    has no subject. ``tests/test_catalog_hook_reconcile_contract.py::test_a_failed_commit_leaves_every_file_without_an_id_and_names_it_unregistered``
    pins what replaced the fallback: a failed commit is loud and leaves no file with an id, never a per-file path."""

    def test_frecency_by_owner_failure_returns_empty_map(self, tmp_path, monkeypatch):
        from nexus.indexer import _build_frecency_doc_id_map

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        class _FailingOwnerList(_SpyProxy):
            def by_owner(self, *a, **kw):
                raise ConnectionError("service unreachable")

        import nexus.catalog.factory as factory
        monkeypatch.setattr(
            factory, "make_catalog_reader",
            lambda **kw: _FailingOwnerList(cat),
        )
        # Must not raise; documented contract returns an empty map.
        assert _build_frecency_doc_id_map(tmp_path, [tmp_path / "a.py"]) == {}


class TestFrecencyDocIdMapBatched:
    """nexus-dst5h: _build_frecency_doc_id_map's second per-file
    by_file_path pass must also become one by_owner + local join."""

    @pytest.mark.t2_tenant
    def test_single_by_owner_no_per_file_lookups(self, tmp_path, monkeypatch):
        from nexus.indexer import _build_frecency_doc_id_map, _catalog_hook
        from nexus.repo_identity import _repo_identity

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
        files = []
        for i in range(3):
            f = tmp_path / f"f{i}.py"
            f.write_text(f"code {i}")
            files.append(f)
        _, repo_hash = _repo_identity(tmp_path)
        _catalog_hook(
            repo=tmp_path, repo_name="nexus", repo_hash=repo_hash,
            head_hash="aaa",
            indexed_files=[(f, "code", "code__nexus") for f in files],
        )

        readers, _ = _spy_factories(monkeypatch)
        unknown = tmp_path / "unknown.py"
        mapping = _build_frecency_doc_id_map(tmp_path, [*files, unknown])

        reader = readers[0]
        assert reader.calls.get("by_file_path", 0) == 0
        assert reader.calls.get("by_owner", 0) == 1
        assert set(mapping) == set(files)  # unknown file absent from map


class TestCatalogHookErrorSafe:
    @pytest.mark.t2_tenant
    def test_hook_does_not_propagate_errors(self, tmp_path, monkeypatch):
        from nexus.indexer import _catalog_hook

        catalog_dir, _ = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        # Pass a non-existent file — relative_to will work but that's fine
        # Force an error by passing bad data
        _catalog_hook(
            repo=Path("/nonexistent/repo"),
            repo_name="bad",
            repo_hash="xxx",
            head_hash="abc",
            indexed_files=[(Path("/nonexistent/repo/file.py"), "code", "code__test")],
        )
        # Should not raise — errors are caught internally


@pytest.mark.t2_tenant
class TestRunHousekeeping:
    """Orphan detection with miss_count tracking and rename detection, driven through ``_catalog_hook`` with
    ``walk="complete"``.

    RDR-193 Step 6 (nexus-u5m5g.7) moved this state machine into the engine's reconcile commit
    (``nexus.catalog_reconcile_commit``), so these tests no longer call ``_run_housekeeping`` (deleted) with a
    hand-set ``indexed_set``: each seeds the catalog by indexing real files, removes or renames files on disk, and
    runs the hook again, then reads the catalog. They are the observable-state bar of parity with the old client path;
    the same cases, over a much larger fixture and compared row for row against the old path's golden file, are in
    ``tests/integration/test_rdr193_reconcile_parity.py``, and the engine's own behaviour is pinned by
    ``service/src/test/java/dev/nexus/service/CatalogReconcileCommitBehaviourTest.java``.
    """

    HASH = "ho05e001"

    @staticmethod
    def _ctype(rel: str) -> tuple[str, str]:
        return ("prose", "docs__nexus") if rel.endswith(".md") else ("code", "code__nexus")

    def _write(self, root: Path, rel: str, text: str) -> Path:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def _run(self, root: Path, rels: list[str], *, head: str = "h1") -> None:
        from nexus.indexer import _catalog_hook

        files = [(root / r, *self._ctype(r)) for r in rels]
        _catalog_hook(
            repo=root, repo_name="nexus", repo_hash=self.HASH, head_hash=head,
            indexed_files=files, walk="complete",
        )

    def _setup(self, tmp_path, monkeypatch):
        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
        return cat

    def _seed(self, cat, root: Path, files: dict[str, str]) -> dict[str, object]:
        """Index *files* (rel -> text, in order: the order is the mint order) and return rel -> tumbler."""
        for rel, text in files.items():
            self._write(root, rel, text)
        self._run(root, list(files))
        owner = cat.owner_for_repo(self.HASH)
        return {rel: cat.by_file_path(owner, rel).tumbler for rel in files}

    def _t(self, cat, rel: str):
        return cat.by_file_path(cat.owner_for_repo(self.HASH), rel).tumbler

    def test_miss_count_incremented_for_missing_file(self, tmp_path, monkeypatch):
        """A catalog entry whose file is not in the walk → miss_count goes from 0 to 1, not deleted."""
        cat = self._setup(tmp_path, monkeypatch)
        t = self._seed(cat, tmp_path, {"src/missing.py": "m = 1\n"})["src/missing.py"]
        (tmp_path / "src/missing.py").unlink()

        self._run(tmp_path, [])

        entry = cat.resolve(t)
        assert entry is not None  # not deleted yet
        assert entry.meta.get("miss_count") == 1

    def test_miss_count_reset_when_file_seen(self, tmp_path, monkeypatch):
        """An entry in the walk with miss_count=1 → reset to 0."""
        cat = self._setup(tmp_path, monkeypatch)
        t = self._seed(cat, tmp_path, {"src/present.py": "p = 1\n"})["src/present.py"]
        cat.update(t, meta={"miss_count": 1})  # simulate a prior miss

        self._run(tmp_path, ["src/present.py"])

        entry = cat.resolve(t)
        assert entry is not None
        assert entry.meta.get("miss_count", 0) == 0

    def test_orphan_deleted_at_threshold(self, tmp_path, monkeypatch):
        """An entry with miss_count=1 whose file is not in the walk → increments to 2 → deleted."""
        cat = self._setup(tmp_path, monkeypatch)
        t = self._seed(cat, tmp_path, {"src/stale.py": "s = 1\n"})["src/stale.py"]
        cat.update(t, meta={"miss_count": 1})
        (tmp_path / "src/stale.py").unlink()

        self._run(tmp_path, [])

        assert cat.resolve(t) is None  # deleted after reaching the threshold of 2

    def test_already_at_threshold_gets_deleted(self, tmp_path, monkeypatch):
        """An entry with miss_count already >= 2 and not in the walk is deleted."""
        cat = self._setup(tmp_path, monkeypatch)
        t = self._seed(cat, tmp_path, {"src/dead.py": "d = 1\n"})["src/dead.py"]
        cat.update(t, meta={"miss_count": 2})
        (tmp_path / "src/dead.py").unlink()

        self._run(tmp_path, [])

        assert cat.resolve(t) is None

    def test_present_files_not_affected(self, tmp_path, monkeypatch):
        """Files in the walk are never aged (miss_count stays at 0 if already 0)."""
        cat = self._setup(tmp_path, monkeypatch)
        t = self._seed(cat, tmp_path, {"src/ok.py": "ok = 1\n"})["src/ok.py"]

        self._run(tmp_path, ["src/ok.py"])

        entry = cat.resolve(t)
        assert entry is not None
        assert entry.meta.get("miss_count", 0) == 0

    def test_rename_detected_by_content_hash(self, tmp_path, monkeypatch):
        """File renamed: the orphan with a matching content_hash is retired and its links move to the new entry."""
        cat = self._setup(tmp_path, monkeypatch)
        ts = self._seed(cat, tmp_path, {"src/old_name.py": "same body\n", "docs/rdr/rdr-1.md": "# rdr one\n"})
        old_t, other_t = ts["src/old_name.py"], ts["docs/rdr/rdr-1.md"]
        cat.link(old_t, other_t, "implements", created_by="test")
        (tmp_path / "src/old_name.py").unlink()
        self._write(tmp_path, "src/new_name.py", "same body\n")

        self._run(tmp_path, ["src/new_name.py", "docs/rdr/rdr-1.md"])

        new_t = self._t(cat, "src/new_name.py")
        assert cat.resolve(old_t) is None  # a rename transfers links, then deletes the old entry
        assert any(str(l.to_tumbler) == str(other_t) for l in cat.links_from(new_t))

    def test_rename_does_not_turn_a_link_between_the_two_entries_into_a_self_link(self, tmp_path, monkeypatch):
        """nexus-x3bxj: a retired entry whose text matches a live one (a symlink ghost) can link to it, or be linked
        from it. Moving those links onto the live entry would make it link to itself, so they are dropped; every
        other link still moves."""
        cat = self._setup(tmp_path, monkeypatch)
        ts = self._seed(cat, tmp_path, {"docs/ghost.md": "abad1dea\n", "docs/target.md": "abad1dea\n",
                                        "docs/rdr/rdr-2.md": "# rdr two\n"})
        ghost_t, target_t, other_t = ts["docs/ghost.md"], ts["docs/target.md"], ts["docs/rdr/rdr-2.md"]
        cat.link(ghost_t, target_t, "cites", created_by="test")
        cat.link(target_t, ghost_t, "relates", created_by="test")
        cat.link(ghost_t, other_t, "implements", created_by="test")
        cat.link(other_t, ghost_t, "quotes", created_by="test")
        cat.link(ghost_t, ghost_t, "supersedes", created_by="test")  # the ghost's own self-link
        (tmp_path / "docs/ghost.md").unlink()

        self._run(tmp_path, ["docs/target.md", "docs/rdr/rdr-2.md"])

        assert cat.resolve(ghost_t) is None
        out = cat.links_from(target_t)
        inc = cat.links_to(target_t)
        assert not [l for l in out if str(l.to_tumbler) == str(target_t)], out
        assert not [l for l in inc if str(l.from_tumbler) == str(target_t)], inc
        # the ghost's self-link is not copied into a target-to-ghost or ghost-to-target link to the deleted ghost
        # (the target's own earlier link to the ghost, "relates", stays: a delete is a tombstone)
        assert not [l for l in out + inc if l.link_type == "supersedes"], out + inc
        # every other link moves, outgoing and incoming
        assert any(str(l.to_tumbler) == str(other_t) and l.link_type == "implements" for l in out)
        assert any(str(l.from_tumbler) == str(other_t) and l.link_type == "quotes" for l in inc)

    def test_rename_keeps_the_spans_of_a_moved_link(self, tmp_path, monkeypatch):
        """nexus-12ml0: the renamed entry's text is the same (same content_hash), so a span into it still
        points at the same text; a moved link keeps from_span and to_span, outgoing and incoming."""
        cat = self._setup(tmp_path, monkeypatch)
        ts = self._seed(cat, tmp_path, {"docs/old.md": "5ca1ab1e\n", "docs/rdr/rdr-3.md": "# rdr three\n"})
        old_t, other_t = ts["docs/old.md"], ts["docs/rdr/rdr-3.md"]
        cat.link(old_t, other_t, "cites", created_by="test", from_span="3-7", to_span="12-14")
        cat.link(other_t, old_t, "quotes", created_by="test", from_span="1-2", to_span="20-25")
        (tmp_path / "docs/old.md").unlink()
        self._write(tmp_path, "docs/new.md", "5ca1ab1e\n")

        self._run(tmp_path, ["docs/new.md", "docs/rdr/rdr-3.md"])

        new_t = self._t(cat, "docs/new.md")
        out = [l for l in cat.links_from(new_t) if l.link_type == "cites"]
        inc = [l for l in cat.links_to(new_t) if l.link_type == "quotes"]
        assert [(l.from_span, l.to_span) for l in out] == [("3-7", "12-14")], out
        assert [(l.from_span, l.to_span) for l in inc] == [("1-2", "20-25")], inc

    def test_rename_of_two_entries_in_one_run_redirects_a_link_between_them(self, tmp_path, monkeypatch):
        """nexus-12ml0: when A and B are both renamed in one run and A links to B, the moved link joins the two
        new entries. Copying it as A'->B would leave A' linked to B, which the same run deletes."""
        cat = self._setup(tmp_path, monkeypatch)
        ts = self._seed(cat, tmp_path, {"docs/a.md": "aaaa0001\n", "docs/b.md": "bbbb0002\n"})
        a_t, b_t = ts["docs/a.md"], ts["docs/b.md"]
        cat.link(a_t, b_t, "cites", created_by="test")
        cat.link(b_t, a_t, "quotes", created_by="test")
        (tmp_path / "docs/a.md").unlink()
        (tmp_path / "docs/b.md").unlink()
        self._write(tmp_path, "docs/a2.md", "aaaa0001\n")
        self._write(tmp_path, "docs/b2.md", "bbbb0002\n")

        self._run(tmp_path, ["docs/a2.md", "docs/b2.md"])

        a2_t, b2_t = self._t(cat, "docs/a2.md"), self._t(cat, "docs/b2.md")
        assert cat.resolve(a_t) is None and cat.resolve(b_t) is None
        gone = {str(a_t), str(b_t)}
        for t in (a2_t, b2_t):
            touching = cat.links_from(t) + cat.links_to(t)
            assert not [l for l in touching if {str(l.from_tumbler), str(l.to_tumbler)} & gone], touching
        assert [l.link_type for l in cat.links_from(a2_t) if str(l.to_tumbler) == str(b2_t)] == ["cites"]
        assert [l.link_type for l in cat.links_from(b2_t) if str(l.to_tumbler) == str(a2_t)] == ["quotes"]

    def test_a_second_rename_does_not_move_a_link_from_an_entry_deleted_in_an_earlier_run(
        self, tmp_path, monkeypatch,
    ):
        """nexus-12ml0: a delete is a tombstone, so after run 1 renames A to A2 the row A->X is still read when
        run 2 renames X to X2. Moving it as A->X2 hit the engine's dangling-endpoint refusal, which aborted
        the run and every later one; it is not moved, and X is retired."""
        cat = self._setup(tmp_path, monkeypatch)
        ts = self._seed(cat, tmp_path, {"docs/a.md": "aaaa1201\n", "docs/x.md": "eeee1202\n"})
        a_t, x_t = ts["docs/a.md"], ts["docs/x.md"]
        cat.link(a_t, x_t, "cites", created_by="test")
        (tmp_path / "docs/a.md").unlink()
        self._write(tmp_path, "docs/a2.md", "aaaa1201\n")
        self._run(tmp_path, ["docs/a2.md", "docs/x.md"])
        assert cat.resolve(a_t) is None

        (tmp_path / "docs/x.md").unlink()
        self._write(tmp_path, "docs/x2.md", "eeee1202\n")
        self._run(tmp_path, ["docs/a2.md", "docs/x2.md"])

        a2_t, x2_t = self._t(cat, "docs/a2.md"), self._t(cat, "docs/x2.md")
        assert cat.resolve(x_t) is None
        assert not [l for l in cat.links_to(x2_t) if str(l.from_tumbler) == str(a_t)]
        assert [l.link_type for l in cat.links_from(a2_t) if str(l.to_tumbler) == str(x2_t)] == ["cites"]

    def test_a_second_rename_does_not_move_a_link_to_an_entry_deleted_in_an_earlier_run(
        self, tmp_path, monkeypatch,
    ):
        """nexus-12ml0, the outgoing mirror: run 1 renames T to T2 and leaves the row X->T; run 2 renames X to X2.
        X->T is not moved as X2->T, and X->T2 moves as X2->T2."""
        cat = self._setup(tmp_path, monkeypatch)
        ts = self._seed(cat, tmp_path, {"docs/x.md": "eeee1222\n", "docs/t.md": "ffff1221\n"})
        x_t, t_t = ts["docs/x.md"], ts["docs/t.md"]
        cat.link(x_t, t_t, "cites", created_by="test")
        (tmp_path / "docs/t.md").unlink()
        self._write(tmp_path, "docs/t2.md", "ffff1221\n")
        self._run(tmp_path, ["docs/x.md", "docs/t2.md"])
        assert cat.resolve(t_t) is None

        (tmp_path / "docs/x.md").unlink()
        self._write(tmp_path, "docs/x2.md", "eeee1222\n")
        self._run(tmp_path, ["docs/x2.md", "docs/t2.md"])

        x2_t, t2_t = self._t(cat, "docs/x2.md"), self._t(cat, "docs/t2.md")
        assert cat.resolve(x_t) is None
        assert not [l for l in cat.links_from(x2_t) if str(l.to_tumbler) == str(t_t)]
        assert [l.link_type for l in cat.links_from(x2_t) if str(l.to_tumbler) == str(t2_t)] == ["cites"]

    @pytest.mark.parametrize("orphan_first", [True, False])
    def test_rename_does_not_move_a_link_to_an_orphan_the_same_run_deletes(self, tmp_path, monkeypatch, orphan_first):
        """nexus-12ml0: an orphan on its second miss is deleted in the same run that renames an entry it links
        with. Its link is not moved onto the new entry, whichever is processed first: before, the move hit the
        engine's dangling-endpoint refusal; after, it would point at the deleted orphan."""
        cat = self._setup(tmp_path, monkeypatch)
        order = {"docs/gone.md": "gone body\n", "docs/old.md": "c0ffee12\n"}
        if not orphan_first:
            order = dict(reversed(list(order.items())))
        ts = self._seed(cat, tmp_path, order)
        orphan_t, old_t = ts["docs/gone.md"], ts["docs/old.md"]
        cat.update(orphan_t, meta={"miss_count": 1})
        cat.link(orphan_t, old_t, "cites", created_by="test")
        cat.link(old_t, orphan_t, "quotes", created_by="test")
        (tmp_path / "docs/gone.md").unlink()
        (tmp_path / "docs/old.md").unlink()
        self._write(tmp_path, "docs/new.md", "c0ffee12\n")

        self._run(tmp_path, ["docs/new.md"])

        new_t = self._t(cat, "docs/new.md")
        assert cat.resolve(orphan_t) is None and cat.resolve(old_t) is None
        touching = cat.links_from(new_t) + cat.links_to(new_t)
        assert not [l for l in touching if str(orphan_t) in (str(l.from_tumbler), str(l.to_tumbler))], touching

    def test_rename_carries_non_evidentiary_stamp_to_new_entry(self, tmp_path, monkeypatch):
        """nexus-4jj40 (RDR-200 Phase 1c evidence hygiene, review round 4): a manually-stamped document survives a
        rename. Without this, the old entry's non_evidentiary meta key is discarded with the old entry, so the very
        next `nx index repo` after a `git mv` silently un-stamps the document."""
        import hashlib

        cat = self._setup(tmp_path, monkeypatch)
        old_t = self._seed(cat, tmp_path, {"src/old_name.py": "deadbeef5678\n"})["src/old_name.py"]
        # stamped by an operator, NOT under tests/fixtures/, so nothing auto-derives this stamp from its path
        cat.update(old_t, meta={"non_evidentiary": True})
        (tmp_path / "src/old_name.py").unlink()
        self._write(tmp_path, "src/new_name.py", "deadbeef5678\n")

        self._run(tmp_path, ["src/new_name.py"])

        assert cat.resolve(old_t) is None  # the old entry is still deleted
        new_entry = cat.by_file_path(cat.owner_for_repo(self.HASH), "src/new_name.py")
        assert new_entry is not None
        assert new_entry.meta.get("non_evidentiary") is True
        # the new entry's OWN content_hash is authoritative and untouched
        assert new_entry.meta.get("content_hash") == hashlib.sha256(b"deadbeef5678\n").hexdigest()

    def test_rename_does_not_stamp_new_entry_when_old_entry_unstamped(self, tmp_path, monkeypatch):
        """The transfer is conditional: an ordinary (unstamped) rename must not acquire a stamp from nowhere."""
        cat = self._setup(tmp_path, monkeypatch)
        old_t = self._seed(cat, tmp_path, {"src/old_name.py": "cafef00d\n"})["src/old_name.py"]
        (tmp_path / "src/old_name.py").unlink()
        self._write(tmp_path, "src/new_name.py", "cafef00d\n")

        self._run(tmp_path, ["src/new_name.py"])

        assert cat.resolve(old_t) is None
        new_entry = cat.by_file_path(cat.owner_for_repo(self.HASH), "src/new_name.py")
        assert new_entry is not None
        assert not new_entry.meta.get("non_evidentiary")

    def test_rename_not_triggered_without_content_hash(self, tmp_path, monkeypatch):
        """An orphan without a content_hash follows the normal miss_count path, not rename (even when a present
        file has the very text it had)."""
        cat = self._setup(tmp_path, monkeypatch)
        t = self._seed(cat, tmp_path, {"src/no_hash.py": "same text\n"})["src/no_hash.py"]
        cat.update(t, meta={"content_hash": ""})
        (tmp_path / "src/no_hash.py").unlink()
        self._write(tmp_path, "src/elsewhere.py", "same text\n")

        self._run(tmp_path, ["src/elsewhere.py"])

        entry = cat.resolve(t)
        assert entry is not None  # not renamed, just incremented
        assert entry.meta.get("miss_count") == 1


class TestCatalogHookForeignCwd:
    """nexus-3e4s critique-followup S2.

    The original contamination scenario was: ``nx index repo <REPO>``
    invoked from a CWD outside ``<REPO>``. The catalog hook computes
    ``rel_path = abs_path.relative_to(repo)`` and passes the relative
    path to ``Catalog.register()``. Pre-fix, ``_normalize_source_uri``
    applied ``os.path.abspath()`` against the process CWD instead of
    the owner's ``repo_root``, producing ``source_uri`` rows pointing
    at the foreign CWD's tree but attributed to ``<REPO>``'s owner.

    This integration test drives the actual catalog hook (not just
    ``Catalog.register()``) from a foreign CWD and asserts the
    persisted ``source_uri`` is anchored on the indexed repo.
    """

    @pytest.mark.t2_tenant
    def test_hook_writes_correctly_attributed_uris_from_foreign_cwd(
        self, tmp_path, monkeypatch,
    ):
        from nexus.indexer import _catalog_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        repo = tmp_path / "indexed_repo"
        repo.mkdir()
        src = repo / "src" / "main.py"
        src.parent.mkdir(parents=True)
        src.write_text("print('hello')")

        # Move CWD to a totally unrelated directory — the smoking gun
        # for the contamination class. Pre-fix this would write a
        # source_uri pointing at the foreign tree.
        foreign_cwd = tmp_path / "elsewhere"
        foreign_cwd.mkdir()
        monkeypatch.chdir(foreign_cwd)

        _catalog_hook(
            repo=repo, repo_name="indexed_repo", repo_hash="repo7777",
            head_hash="aaa",
            indexed_files=[(src, "code", "code__indexed_repo")],
        )

        owner = cat.owner_for_repo("repo7777")
        entry = cat.by_file_path(owner, "src/main.py")
        assert entry is not None
        # source_uri must point inside the indexed repo, NOT inside
        # the foreign CWD.
        assert str(repo) in entry.source_uri
        assert str(foreign_cwd) not in entry.source_uri


@pytest.mark.t2_tenant
class TestCatalogHookEphemeralPathGuard:
    """nexus-u8n4r: ``_catalog_hook``'s per-file loop refuses registration
    when the REGISTERED identity (owner's main-repo root + rel_path)
    sits under an agent worktree marker or system temp dir, unless the
    owner's own repo_root is itself rooted there. See
    ``nexus.repo_identity.should_skip_ephemeral_registration``.
    """

    def test_worktree_marker_file_swept_by_main_repo_run_is_not_registered(
        self, tmp_path, monkeypatch,
    ):
        """A Claude Code worktree lives INSIDE the primary repo's tree at
        ``.claude/worktrees/<agent>/`` — an unfiltered walk over the
        primary repo sweeps the agent's ephemeral checkout in too. That
        file must be refused; a sibling clean file in the same run must
        still register (nexus-u8n4r's 4,002-doc production evidence)."""
        from nexus.indexer import _catalog_hook
        from nexus.repo_identity import is_worktree_or_tempdir_path

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        # Linux/CI-vs-macOS divergence (nexus-u8n4r CI red, 2026-08-03): on
        # Linux, pytest's ``tmp_path`` lives under ``/tmp/``, which itself
        # matches ``_TEMP_DIR_PREFIXES`` — so this test's OWN owner root
        # (derived from ``tmp_path``) would look ephemeral too, the
        # owner-root exception would exempt the registration, and the
        # guard would never fire (false green for the wrong reason). On
        # macOS ``tmp_path`` is under ``/private/var/folders/...``,
        # deliberately excluded from the predicate, so the same test
        # passes there for a DIFFERENT reason. Force a non-tmp-shaped
        # prefix set so the owner root reads as clean on BOTH platforms —
        # this test is about the WORKTREE MARKER, not the temp-prefix
        # rule. Do not strip this patch; see nexus-u8n4r CI run 30850463195.
        monkeypatch.setattr(
            "nexus.repo_identity._TEMP_DIR_PREFIXES", ("/nonexistent-tmp-prefix/",),
        )
        main_repo = tmp_path / "primary"
        # Non-vacuity: prove the premise (clean owner root) instead of
        # inheriting it from whichever platform happens to be running.
        assert not is_worktree_or_tempdir_path(str(main_repo))

        polluted = main_repo / ".claude" / "worktrees" / "agent-x" / "docs" / "foo.md"
        polluted.parent.mkdir(parents=True)
        polluted.write_text("# ephemeral")
        good = main_repo / "src" / "good.py"
        good.parent.mkdir(parents=True)
        good.write_text("print('good')")

        import structlog.testing
        with structlog.testing.capture_logs() as logs:
            _catalog_hook(
                repo=main_repo, repo_name="primary", repo_hash="u8n4r0001",
                head_hash="h1",
                indexed_files=[
                    (polluted, "rdr", "rdr__test-u8n4r-1"),
                    (good, "code", "code__test-u8n4r-1"),
                ],
                walk="partial",
            )

        assert count_documents() == 1
        assert documents_by_file_path("src/good.py")
        assert not documents_by_file_path(
            ".claude/worktrees/agent-x/docs/foo.md"
        )
        skipped_events = [
            log_entry for log_entry in logs
            if log_entry.get("event") == "ephemeral_path_registration_skipped"
        ]
        assert len(skipped_events) == 1
        assert "agent-x" in skipped_events[0]["path"]

    def test_indexing_from_worktree_checkout_still_registers_clean_paths(
        self, tmp_path, monkeypatch,
    ):
        """nexus-zr2ie precedent (must stay green): ``nx index repo``
        invoked FROM a worktree checkout resolves the SAME main-repo
        owner (git-worktree-stable identity) and registers a clean,
        main-repo-anchored relative path — this is legal and must NOT be
        refused by the nexus-u8n4r guard.

        Review fix FIX3 (both reviewers, 2026-08-03): the pre-fix version
        of this test never created ``main_repo/src/main.py`` — it only
        created the WORKTREE copy — so it exercised the FALSE-clean shape
        FIX2 closes (a worktree-unique file whose reconstructed main-repo
        path doesn't exist) and asserted that shape was legal. The
        legitimate zr2ie case is a file that IS mirrored at the same
        relative path in the main repo (e.g. checked out from a shared
        commit); this test now creates that mirror so it actually pins
        the case its docstring claims. The false-clean shape has its own
        dedicated test below
        (``test_worktree_unique_file_with_no_main_mirror_is_skipped``).
        """
        from nexus.indexer import _catalog_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        main_repo = tmp_path / "primary"
        main_repo.mkdir()
        worktree = main_repo / ".claude" / "worktrees" / "agent-y"
        worktree.mkdir(parents=True)
        src = worktree / "src" / "main.py"
        src.parent.mkdir(parents=True)
        src.write_text("print('hi')")
        # The mirror: same relative path, present in the MAIN repo too
        # (as it would be for a file checked out from a shared commit).
        mirror = main_repo / "src" / "main.py"
        mirror.parent.mkdir(parents=True, exist_ok=True)
        mirror.write_text("print('hi')")

        # Simulate the real `git rev-parse --git-common-dir` worktree-to-
        # main resolution: indexing FROM the worktree path resolves to
        # the SAME main-repo owner as the primary checkout.
        monkeypatch.setattr(
            "nexus.repo_identity._repo_identity_with_main",
            lambda r: ("primary", "u8n4r0002", main_repo),
        )

        _catalog_hook(
            repo=worktree, repo_name="primary", repo_hash="u8n4r0002",
            head_hash="h1",
            indexed_files=[(src, "code", "code__test-u8n4r-2")],
            walk="partial",
        )

        assert count_documents() == 1
        assert documents_by_file_path("src/main.py")

    def test_worktree_unique_file_with_no_main_mirror_is_skipped(
        self, tmp_path, monkeypatch,
    ):
        """nexus-u8n4r FIX2 (substantive-critic Critical, empirically
        reproduced): indexing FROM a worktree checkout over a file that
        is WORKTREE-UNIQUE — no mirror at the same relative path in the
        main repo, e.g. a new/uncommitted session draft — reconstructs a
        clean-shaped main-repo-anchored path the marker predicate never
        flags. The existence check (only consulted when ``repo !=
        main_repo``) is the second signal that catches it."""
        from nexus.indexer import _catalog_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        main_repo = tmp_path / "primary"
        main_repo.mkdir()
        worktree = main_repo / ".claude" / "worktrees" / "agent-y"
        worktree.mkdir(parents=True)
        # Worktree-unique: exists ONLY in the worktree, no mirror at
        # main_repo/docs/rdr/rdr-999-draft.md.
        draft = worktree / "docs" / "rdr" / "rdr-999-draft.md"
        draft.parent.mkdir(parents=True)
        draft.write_text("# uncommitted agent draft\n")

        monkeypatch.setattr(
            "nexus.repo_identity._repo_identity_with_main",
            lambda r: ("primary", "u8n4r0004", main_repo),
        )

        import structlog.testing
        with structlog.testing.capture_logs() as logs:
            _catalog_hook(
                repo=worktree, repo_name="primary", repo_hash="u8n4r0004",
                head_hash="h1",
                indexed_files=[(draft, "rdr", "rdr__test-u8n4r-4")],
                walk="partial",
            )

        assert count_documents() == 0
        assert not documents_by_file_path("docs/rdr/rdr-999-draft.md")
        skipped_events = [
            log_entry for log_entry in logs
            if log_entry.get("event") == "ephemeral_path_registration_skipped"
        ]
        assert len(skipped_events) == 1
        assert skipped_events[0]["reason"] == "worktree_unique_no_main_mirror"

    def test_worktree_file_whose_reconstructed_main_path_is_a_directory_is_skipped(
        self, tmp_path, monkeypatch,
    ):
        """nexus-u8n4r review fix Minor #2: ``Path.exists()`` is true for
        a DIRECTORY too — a same-named directory in the main repo (e.g.
        ``docs/rdr/`` existing as a dir while the worktree has a FILE at
        that exact path, an unlikely but possible shape) must not count
        as a mirror. ``.is_file()`` closes this."""
        from nexus.indexer import _catalog_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        main_repo = tmp_path / "primary"
        main_repo.mkdir()
        worktree = main_repo / ".claude" / "worktrees" / "agent-y"
        worktree.mkdir(parents=True)

        # The reconstructed main-repo path is a DIRECTORY, not a file.
        (main_repo / "docs" / "rdr" / "rdr-999-draft.md").mkdir(parents=True)

        draft = worktree / "docs" / "rdr" / "rdr-999-draft.md"
        draft.parent.mkdir(parents=True)
        draft.write_text("# uncommitted agent draft\n")

        monkeypatch.setattr(
            "nexus.repo_identity._repo_identity_with_main",
            lambda r: ("primary", "u8n4r0006", main_repo),
        )

        import structlog.testing
        with structlog.testing.capture_logs() as logs:
            _catalog_hook(
                repo=worktree, repo_name="primary", repo_hash="u8n4r0006",
                head_hash="h1",
                indexed_files=[(draft, "rdr", "rdr__test-u8n4r-6")],
                walk="partial",
            )

        assert count_documents() == 0
        assert not documents_by_file_path("docs/rdr/rdr-999-draft.md")
        skipped_events = [
            log_entry for log_entry in logs
            if log_entry.get("event") == "ephemeral_path_registration_skipped"
        ]
        assert len(skipped_events) == 1
        assert skipped_events[0]["reason"] == "worktree_unique_no_main_mirror"

    def test_owner_rooted_under_tmp_registers_tmp_paths(
        self, tmp_path, monkeypatch,
    ):
        """Population (b): a throwaway owner whose repo_root is ITSELF a
        temp-dir path (e.g. the pytest tmp-dir suite, or a gate sandbox
        with its own config dir) must stay fully registrable — the
        owner-root exception."""
        from nexus.indexer import _catalog_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        sandbox_root = Path("/tmp/nexus-u8n4r-sandbox-test")
        monkeypatch.setattr(
            "nexus.repo_identity._repo_identity_with_main",
            lambda r: ("sandbox", "u8n4r0003", sandbox_root),
        )
        # Need not physically exist: rel_path derivation and the OSError-
        # tolerant mtime/hash reads both degrade gracefully.
        fake_file = sandbox_root / "src" / "foo.py"

        _catalog_hook(
            repo=sandbox_root, repo_name="sandbox", repo_hash="u8n4r0003",
            head_hash="h1",
            indexed_files=[(fake_file, "code", "code__test-u8n4r-3")],
            walk="partial",
        )

        assert count_documents() == 1
        assert documents_by_file_path("src/foo.py")

    def test_real_git_worktree_discovery_mirrored_registers_unique_skipped(
        self, tmp_path, monkeypatch,
    ):
        """FIX3 (both reviewers): the hand-built ``indexed_files`` lists
        in the tests above prove the guard's own logic, but the critic's
        probe showed real ``git`` discovery (``_git_ls_files`` over an
        actual ``git worktree add`` checkout) can differ from a
        synthetic list. This drives the REAL discovery helper over a
        REAL git worktree — no ``_repo_identity_with_main`` mock — and
        asserts the two-file split end to end: the file mirrored in the
        main repo registers; the worktree-unique file is skipped.
        """
        import subprocess

        from nexus.indexer import _catalog_hook, _git_ls_files

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        main_repo = tmp_path / "primary"
        main_repo.mkdir()
        subprocess.run(
            ["git", "init", "--quiet"], cwd=main_repo, check=True,
            capture_output=True,
        )
        mirrored = main_repo / "src" / "main.py"
        mirrored.parent.mkdir(parents=True)
        mirrored.write_text("print('hi')\n")
        subprocess.run(["git", "add", "-A"], cwd=main_repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "--quiet", "-m", "initial"], cwd=main_repo,
            check=True, capture_output=True,
        )

        worktree = main_repo / ".claude" / "worktrees" / "agent-real"
        worktree.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "worktree", "add", "--quiet", str(worktree), "-b", "agent-real-branch"],
            cwd=main_repo, check=True, capture_output=True,
        )
        # Worktree-unique untracked draft — no mirror in main_repo.
        draft = worktree / "docs" / "rdr" / "rdr-real-draft.md"
        draft.parent.mkdir(parents=True)
        draft.write_text("# real git worktree-unique draft\n")

        # Real discovery — respects git, not a Python-literal list.
        discovered = _git_ls_files(worktree, include_untracked=True)
        discovered_rel = {str(p.relative_to(worktree)) for p in discovered}
        assert discovered_rel == {"src/main.py", "docs/rdr/rdr-real-draft.md"}

        indexed_files = [
            (p, "code" if p.suffix == ".py" else "rdr", "code__test-u8n4r-real")
            for p in discovered
        ]

        import structlog.testing
        with structlog.testing.capture_logs() as logs:
            _catalog_hook(
                repo=worktree, repo_name="primary", repo_hash="u8n4r-real-0005",
                head_hash="h1",
                indexed_files=indexed_files,
                walk="partial",
            )

        assert count_documents() == 1
        assert documents_by_file_path("src/main.py")
        assert not documents_by_file_path("docs/rdr/rdr-real-draft.md")
        skipped_events = [
            log_entry for log_entry in logs
            if log_entry.get("event") == "ephemeral_path_registration_skipped"
        ]
        assert len(skipped_events) == 1
        assert skipped_events[0]["reason"] == "worktree_unique_no_main_mirror"
        assert "rdr-real-draft.md" in skipped_events[0]["path"]


def test_indexed_relpaths_tolerates_symlinked_repo(tmp_path: Path) -> None:
    """nexus-f3tyz: a symlink mismatch (file under the real dir, repo passed as
    a symlink to it) must not abort the indexed-path set. The pre-fix
    comprehension raised ValueError on the first such path, which the caller's
    except swallowed, silently skipping _run_housekeeping (orphan eviction)."""
    import os

    from nexus.indexer import _indexed_relpaths

    real = tmp_path / "real"
    real.mkdir()
    (real / "a.py").write_text("x")
    link = tmp_path / "link"
    os.symlink(real, link)

    abs_path = real / "a.py"
    # abs_path.relative_to(link) raises ValueError; the resolve-fallback recovers.
    assert _indexed_relpaths([(abs_path, None, None)], link) == {"a.py"}


def test_indexed_relpaths_skips_outside_repo_without_aborting(tmp_path: Path) -> None:
    """A genuinely-outside-repo path is skipped, not raised — the rest of the
    set (and thus housekeeping) still runs."""
    from nexus.indexer import _indexed_relpaths

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "in.py").write_text("x")
    outside = tmp_path / "elsewhere" / "out.py"

    result = _indexed_relpaths(
        [(repo / "in.py", None, None), (outside, None, None)], repo,
    )
    assert result == {"in.py"}


@pytest.mark.t2_tenant
class TestCatalogHookReconciledIsNotNew:
    """nexus-53cae (2026-09-04): 198 docs/rdr files were absent from owner 1.1's snapshot, the register reconciled
    each onto a live curator-owner row by source_uri, and the hook counted the returned tumblers as new on every run
    ("Catalog: 199 new", 113 runs). The count follows the commit's ``created`` action, not the number of rows.

    RDR-193 Step 6 (nexus-u5m5g.7): the engine's commit now resolves a path by source_uri and reports
    ``same_owner_by_uri`` / ``foreign_owner`` rows, so these run against the real engine instead of a stub writer.

    Not carried over (the subject left the client; no test replaces them, the parity bar is catalog state and not
    log events): ``test_a_batched_mint_over_an_existing_path_is_announced``,
    ``test_a_conflict_minted_between_pages_is_seen_by_the_later_page`` and
    ``test_a_batched_reconcile_onto_another_owner_is_announced`` (nexus-1vc0n's ``catalog_mint_over_existing_file_path``
    and ``catalog_mint_resolved_existing_document`` events came from one bulk ``find_all_by_file_paths`` read per
    register page, which the hook no longer makes), and the three ``test_the_per_file_fallback*`` tests (register_many
    and its per-file fallback are gone; a failed commit has no fallback, pinned by
    ``tests/test_catalog_hook_reconcile_contract.py::test_a_failed_commit_leaves_every_file_without_an_id_and_names_it_unregistered``).
    The foreign-owner and same-owner physical_collection cases live on below and in
    ``tests/integration/test_rdr193_reconcile_parity.py`` (``id_foreign`` / ``id_same_owner``).
    """

    REPO_HASH = "53cae001"

    def _hook(self, root, files, head="h1"):
        from nexus.indexer import _catalog_hook

        return _catalog_hook(
            repo=root, repo_name="nexus", repo_hash=self.REPO_HASH, head_hash=head,
            indexed_files=[(f, "code", "code__nexus") for f in files],
        )

    def _foreign_row(self, cat, path, *, collection="code__OLD") -> str:
        other = cat.register_owner("recon_other", "curator")
        return str(cat.register(other, "foreign", content_type="code", file_path=path.name,
                                physical_collection=collection, source_uri=f"file://{path.resolve()}"))

    def test_a_reconciled_row_is_counted_and_named_not_called_new(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        import structlog.testing

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
        a, b = tmp_path / "a.py", tmp_path / "b.py"
        a.write_text("a = 1\n")
        b.write_text("b = 2\n")
        foreign = self._foreign_row(cat, b)

        with structlog.testing.capture_logs() as logs:
            mapping = self._hook(tmp_path, [a, b])

        err = capsys.readouterr().err
        assert "Catalog: 1 new, 0 updated, 1 reconciled onto rows outside this owner" in err, err
        assert "done (1 new," in err
        assert mapping[b] == foreign, "the reconciled path still maps to its live row"
        events = [e for e in logs if e["event"] == "catalog_register_reconciled_onto_existing_row"]
        assert len(events) == 1 and events[0]["tumbler"] == foreign and events[0]["rel_path"] == "b.py"

    def test_a_foreign_owners_reconciled_row_is_never_repointed(self, tmp_path, monkeypatch) -> None:
        """nexus-r1tnx round 4 (fix-check CRITICAL): b.py resolves onto a row of ANOTHER owner. Repointing its
        physical_collection to THIS run's target would reassign that owner's document on this owner's intent. The
        commit reports it ``foreign_owner`` and leaves the row as it is."""
        from nexus.mcp_infra import (
            get_reconciled_collections_count,
            reset_reconciled_collections_count,
        )
        reset_reconciled_collections_count()

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
        a, b = tmp_path / "a.py", tmp_path / "b.py"
        a.write_text("a = 1\n")
        b.write_text("b = 2\n")
        foreign = self._foreign_row(cat, b, collection="code__OLD")

        self._hook(tmp_path, [a, b])

        assert cat.resolve(foreign).physical_collection == "code__OLD", "a foreign owner's document must not be repointed"
        assert get_reconciled_collections_count() == 0

    def test_a_same_owners_reconciled_rows_stale_physical_collection_is_repointed(
        self, tmp_path, monkeypatch,
    ) -> None:
        """The genuine nexus-2t63u case: b.py matches a document under THIS owner by source_uri although the
        owner-scoped path lookup missed it (the row carries another file_path). The commit resolves it
        (``same_owner_by_uri``) and repoints its stale ``physical_collection``."""
        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
        a, b = tmp_path / "a.py", tmp_path / "b.py"
        a.write_text("a = 1\n")
        b.write_text("b = 2\n")
        self._hook(tmp_path, [a])  # creates the owner
        owner = cat.owner_for_repo(self.REPO_HASH)
        stale = str(cat.register(owner, "b.py", content_type="code", file_path="old-b.py",
                                 physical_collection="code__OLD", source_uri=f"file://{b.resolve()}"))

        mapping = self._hook(tmp_path, [a, b])

        assert mapping[b] == stale
        assert cat.resolve(stale).physical_collection == "code__nexus", "the stale physical_collection was never repointed"

    def test_all_created_keeps_the_plain_line(self, tmp_path, monkeypatch, capsys) -> None:
        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
        a = tmp_path / "a.py"
        a.write_text("a = 1\n")
        self._hook(tmp_path, [a])
        err = capsys.readouterr().err
        assert "Catalog: 1 new, 0 updated\n" in err, err
