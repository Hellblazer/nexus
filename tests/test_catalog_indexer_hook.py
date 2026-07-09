# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from pathlib import Path

import pytest

from nexus.catalog.catalog import Catalog


@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@test.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@test.invalid")


def _make_catalog(tmp_path: Path) -> tuple[Path, Catalog]:
    catalog_dir = tmp_path / "catalog"
    cat = Catalog.init(catalog_dir)
    return catalog_dir, cat


class TestCatalogHookSkipped:
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
        rows = cat._db.execute("SELECT count(*) FROM owners").fetchone()
        assert rows[0] == 1


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


class TestCatalogHookBatchedLookups:
    """nexus-dst5h: the pre-index sweep must not do per-file catalog
    round-trips — one owner-scoped list + local join, and no writes for
    unchanged files on a warm re-run."""

    def _index(self, tmp_path, files, head_hash="aaa"):
        from nexus.indexer import _catalog_hook

        return _catalog_hook(
            repo=tmp_path, repo_name="nexus", repo_hash="571b8edd",
            head_hash=head_hash,
            indexed_files=[(f, "code", "code__nexus") for f in files],
        )

    def test_no_per_file_lookups_single_owner_list(self, tmp_path, monkeypatch):
        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
        files = []
        for i in range(3):
            f = tmp_path / f"f{i}.py"
            f.write_text(f"code {i}")
            files.append(f)

        readers, _ = _spy_factories(monkeypatch)
        self._index(tmp_path, files)

        reader = readers[0]
        assert reader.calls.get("by_file_path", 0) == 0
        # Exactly 2: one hook-level join fetch + housekeeping's fresh fetch.
        assert reader.calls.get("by_owner", 0) == 2

    def test_warm_rerun_issues_zero_updates(self, tmp_path, monkeypatch):
        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
        f = tmp_path / "a.py"
        f.write_text("stable")

        self._index(tmp_path, [f], head_hash="same")
        _, writers = _spy_factories(monkeypatch)
        self._index(tmp_path, [f], head_hash="same")

        writer = writers[0]
        assert writer.calls.get("update", 0) == 0
        assert writer.calls.get("register", 0) == 0

    def test_changed_head_hash_still_updates(self, tmp_path, monkeypatch):
        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
        f = tmp_path / "a.py"
        f.write_text("v1")

        self._index(tmp_path, [f], head_hash="aaa")
        _, writers = _spy_factories(monkeypatch)
        self._index(tmp_path, [f], head_hash="bbb")

        assert writers[0].calls.get("update", 0) == 1
        owner = cat.owner_for_repo("571b8edd")
        assert cat.by_file_path(owner, "a.py").head_hash == "bbb"

    def test_changed_content_still_updates(self, tmp_path, monkeypatch):
        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
        f = tmp_path / "a.py"
        f.write_text("v1")

        self._index(tmp_path, [f], head_hash="same")
        f.write_text("v2 changed")
        _, writers = _spy_factories(monkeypatch)
        self._index(tmp_path, [f], head_hash="same")

        assert writers[0].calls.get("update", 0) == 1

    def test_new_file_still_registers_alongside_warm_files(self, tmp_path, monkeypatch):
        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
        old = tmp_path / "old.py"
        old.write_text("old")

        self._index(tmp_path, [old], head_hash="same")
        new = tmp_path / "new.py"
        new.write_text("new")
        _, writers = _spy_factories(monkeypatch)
        result = self._index(tmp_path, [old, new], head_hash="same")

        # nexus-9dvqy: the NEW file is registered via the batched register_many
        # (one page), NOT the per-file register(); the warm file takes no write.
        assert writers[0].calls.get("register_many", 0) == 1
        assert writers[0].calls.get("register", 0) == 0
        assert writers[0].calls.get("update", 0) == 0
        assert set(result) == {old, new}
        owner = cat.owner_for_repo("571b8edd")
        assert cat.by_file_path(owner, "new.py") is not None


    def test_multiple_new_files_registered_in_one_batch(self, tmp_path, monkeypatch):
        # nexus-9dvqy: N new files on a fresh index => ONE register_many call,
        # every file mapped to a doc_id.
        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
        files = []
        for i in range(4):
            f = tmp_path / f"n{i}.py"
            f.write_text(f"code {i}")
            files.append(f)

        _, writers = _spy_factories(monkeypatch)
        result = self._index(tmp_path, files)

        assert writers[0].calls.get("register_many", 0) == 1
        assert writers[0].calls.get("register", 0) == 0
        assert set(result) == set(files)

    def test_fairness_yield_pass1_defers_update_burst(self, tmp_path, monkeypatch):
        # nexus-9dvqy (stacked-review CRITICAL): the RDR-146 per-file yield must
        # still guard the pass-1 inline writer.update() burst. On a warm re-run a
        # HEAD bump flips every existing doc to changed => a large serial update
        # burst; a pending interactive write on the 2nd file must defer the rest.
        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
        monkeypatch.setenv("NX_WRITE_PRIORITY", "batch")
        files = []
        for i in range(3):
            f = tmp_path / f"u{i}.py"
            f.write_text(f"code {i}")
            files.append(f)
        # First index registers all three (head v1).
        self._index(tmp_path, files, head_hash="v1")

        # Re-index with a new HEAD => every doc is "changed" => update() path.
        seen = {"n": 0}

        def fake_await(pending_fn, on_locked):
            seen["n"] += 1
            return "skip" if seen["n"] == 2 else "wait"

        monkeypatch.setattr(
            "nexus.catalog.write_priority.await_fair_window", fake_await
        )
        result = self._index(tmp_path, files, head_hash="v2")

        # Only the first file was resolved before the yield deferred the rest.
        assert len(result) == 1
        assert files[1] not in result and files[2] not in result

    def test_fairness_yield_pass2_defers_between_pages(self, tmp_path, monkeypatch):
        # nexus-9dvqy (audit HIGH): pass 2 adds a SECOND per-page yield for the
        # new-doc batch. Let pass 1 complete (all "wait"), then skip on the 2nd
        # page so page-1 registers and the rest defer. Page shrunk to 2.
        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
        monkeypatch.setenv("NX_WRITE_PRIORITY", "batch")
        monkeypatch.setattr("nexus.indexer._CATALOG_REGISTER_PAGE", 2)

        files = []
        for i in range(3):
            f = tmp_path / f"y{i}.py"
            f.write_text(f"code {i}")
            files.append(f)

        # Calls: pass1 resolves 3 files (n=1,2,3 -> wait), pass2 page@0 (n=4 ->
        # wait, registers files 0,1), page@2 (n=5 -> skip, defers file 2).
        seen = {"n": 0}

        def fake_await(pending_fn, on_locked):
            seen["n"] += 1
            return "skip" if seen["n"] == 5 else "wait"

        monkeypatch.setattr(
            "nexus.catalog.write_priority.await_fair_window", fake_await
        )
        result = self._index(tmp_path, files)

        assert set(result) == {files[0], files[1]}
        assert files[2] not in result

    def test_register_many_failure_falls_back_per_file(self, tmp_path, monkeypatch):
        # nexus-9dvqy: if register_many raises unrecoverably, the hook falls
        # back to per-file register() with ghost-class isolation — every file
        # is still registered.
        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        class _BatchFailsProxy(_SpyProxy):
            def register_many(self, *a, **kw):
                self.calls["register_many"] = self.calls.get("register_many", 0) + 1
                raise RuntimeError("batch endpoint down")

        import nexus.catalog.factory as factory
        real_writer = factory.make_catalog_writer
        writers: list = []

        def spy_writer(**kw):
            spy = _BatchFailsProxy(real_writer(**kw))
            writers.append(spy)
            return spy

        monkeypatch.setattr(factory, "make_catalog_writer", spy_writer)
        # reader stays real
        files = [tmp_path / "a.py", tmp_path / "b.py"]
        for f in files:
            f.write_text(f.name)

        result = self._index(tmp_path, files)

        assert writers[0].calls.get("register_many", 0) == 1
        assert writers[0].calls.get("register", 0) == 2  # per-file fallback
        assert set(result) == set(files)


class TestCatalogHookOwnerListFailure:
    """nexus-dst5h review Critical: a failing owner-list fetch must not
    silently no-op the whole hook — it falls back to per-file lookups."""

    def test_by_owner_failure_falls_back_per_file(self, tmp_path, monkeypatch):
        from nexus.indexer import _catalog_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
        f = tmp_path / "a.py"
        f.write_text("code")

        class _FailingOwnerList(_SpyProxy):
            def by_owner(self, *a, **kw):
                raise ConnectionError("service unreachable")

        # Failing variant: housekeeping's by_owner also fails; the hook
        # must still register the file via the per-file fallback.
        import nexus.catalog.factory as factory
        monkeypatch.setattr(
            factory, "make_catalog_reader",
            lambda **kw: _FailingOwnerList(cat),
        )

        result = _catalog_hook(
            repo=tmp_path, repo_name="nexus", repo_hash="571b8edd",
            head_hash="aaa",
            indexed_files=[(f, "code", "code__nexus")],
        )

        assert set(result) == {f}
        owner = cat.owner_for_repo("571b8edd")
        assert cat.by_file_path(owner, "a.py") is not None

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


class TestCatalogHookBatchedServiceMode:
    """nexus-dst5h critic Critical: pin the batching + changed-predicate
    contract against the REAL HttpCatalogClient, with entries passing
    through ``_to_entry``'s JSON None→\"\"/0/{} coercion — the boundary the
    warm-run tax actually lives on. The HTTP transport is mocked
    (httpx.MockTransport); the client, URL routing, and JSON round-trip
    are real."""

    class _StubWriter:
        """Write-side stub with the surface _catalog_hook touches."""

        priority = "interactive"

        def __init__(self, *, update_many_raises: bool = False):
            self.register_calls: list[dict] = []
            self.update_calls: list[dict] = []
            self.update_many_calls: list[list[dict]] = []
            self._update_many_raises = update_many_raises

        def register(self, *args, **kw):
            from nexus.catalog.tumbler import Tumbler
            # Positional owner/title (per-file fallback path) or all-kw.
            self.register_calls.append(kw)
            return Tumbler.parse("1.1.99")

        def register_many(self, owner, docs):
            from nexus.catalog.tumbler import Tumbler
            out = []
            for d in docs:
                self.register_calls.append(d)
                out.append(Tumbler.parse("1.1.99"))
            return out

        def update(self, tumbler, **fields):
            self.update_calls.append({"tumbler": str(tumbler), **fields})

        def update_many(self, docs: list[dict]) -> list[int]:
            # nexus-xedhp / substantive-critic finding: without this method,
            # _catalog_hook's Pass 1b capability check (`getattr(writer,
            # "update_many", None)`) always falls back to the per-file
            # `update()` loop, and the batched branch is never exercised by
            # any test. Defining it here is what makes
            # TestCatalogHookBatchedServiceMode a genuine service-mode test.
            self.update_many_calls.append(docs)
            if self._update_many_raises:
                raise RuntimeError("simulated update_many transport failure")
            return [1 for _ in docs]

        def is_interactive_write_pending(self) -> bool:
            # Property-style counter: Pass 1b's per-page fairness yield
            # (await_fair_window) probes this before each update_many page.
            self.fairness_probes = getattr(self, "fairness_probes", 0) + 1
            return False

        def close(self) -> None:
            pass

    def _http_client_and_log(self, monkeypatch, docs: list[dict]):
        """Real HttpCatalogClient over a MockTransport serving *docs*."""
        import httpx

        from nexus.catalog.http_catalog_client import HttpCatalogClient

        requests: list[tuple[str, dict]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            requests.append((request.url.path, params))
            if request.url.path == "/v1/catalog/owners/by_repo":
                return httpx.Response(200, json={"tumbler_prefix": "1.1"})
            if request.url.path == "/v1/catalog/list":
                return httpx.Response(200, json={"documents": docs})
            return httpx.Response(200, json={})

        monkeypatch.setenv("NX_SERVICE_TOKEN", "test-token")
        client = HttpCatalogClient(base_url="http://mock.test")
        client._client = httpx.Client(
            base_url="http://mock.test",
            transport=httpx.MockTransport(handler),
        )
        return client, requests

    def _run_hook(self, tmp_path, monkeypatch, docs, head_hash, *, writer=None, files=None):
        from nexus.indexer import _catalog_hook

        monkeypatch.setenv("NX_STORAGE_BACKEND_CATALOG", "service")
        client, requests = self._http_client_and_log(monkeypatch, docs)
        writer = writer if writer is not None else self._StubWriter()

        import nexus.catalog.factory as factory
        monkeypatch.setattr(factory, "make_catalog_reader", lambda **kw: client)
        monkeypatch.setattr(factory, "make_catalog_writer", lambda **kw: writer)

        if files is None:
            files = [tmp_path / "a.py"]
        result = _catalog_hook(
            repo=tmp_path, repo_name="nexus", repo_hash="571b8edd",
            head_hash=head_hash,
            indexed_files=[(f, "code", "code__nexus") for f in files],
        )
        return result, writer, requests

    def _doc_for(self, f, *, head_hash, tumbler: str = "1.1.1") -> dict:
        import hashlib

        return {
            "tumbler": tumbler,
            "title": f.name,
            "content_type": "code",
            "file_path": f.name,
            "physical_collection": "code__nexus",
            "head_hash": head_hash,
            "source_mtime": f.stat().st_mtime,
            "meta": {"content_hash": hashlib.sha256(f.read_bytes()).hexdigest()},
            # Java payloads carry explicit nulls; _to_entry must coerce.
            "author": None,
            "year": None,
            "corpus": None,
            "source_uri": None,
        }

    def test_warm_rerun_no_updates_no_per_file_lookups(self, tmp_path, monkeypatch):
        f = tmp_path / "a.py"
        f.write_text("stable")
        doc = self._doc_for(f, head_hash="same")

        result, writer, requests = self._run_hook(
            tmp_path, monkeypatch, [doc], head_hash="same",
        )

        assert writer.update_calls == []
        assert writer.register_calls == []
        assert set(result) == {f}
        # No per-file lookups: zero /list calls carrying file_path.
        assert [p for path, p in requests
                if path == "/v1/catalog/list" and "file_path" in p] == []
        # Exactly 2 owner-list fetches: hook join + housekeeping.
        assert len([p for path, p in requests
                    if path == "/v1/catalog/list" and "owner" in p]) == 2

    def test_changed_head_hash_updates_through_json_boundary(
        self, tmp_path, monkeypatch,
    ):
        # nexus-xedhp / substantive-critic Critical: with a service-mode
        # writer that actually exposes update_many, Pass 1b must route
        # through the BATCHED path, not the per-file update() fallback —
        # this is the branch that was never exercised before _StubWriter
        # gained update_many.
        f = tmp_path / "a.py"
        f.write_text("stable")
        doc = self._doc_for(f, head_hash="old")

        _, writer, _ = self._run_hook(
            tmp_path, monkeypatch, [doc], head_hash="new",
        )

        assert len(writer.update_many_calls) == 1, "must route through the batched update_many path"
        assert writer.update_many_calls[0][0]["head_hash"] == "new"
        assert writer.update_many_calls[0][0]["tumbler"] == "1.1.1"
        assert writer.update_calls == [], "per-file update() must NOT fire when update_many succeeds"

    def test_update_many_pages_at_boundary_with_fairness_per_page(
        self, tmp_path, monkeypatch,
    ):
        # nexus-vxgnh critique: Pass 1b genuinely pages changed_batch at
        # _CATALOG_REGISTER_PAGE and consults the fairness window PER PAGE
        # (indexer.py Pass 1b loop) — load-bearing behavior that no test
        # drove past a single page. Shrink the page size and prove both.
        monkeypatch.setattr("nexus.indexer._CATALOG_REGISTER_PAGE", 2)
        writer = self._StubWriter()
        # The per-page fairness yield only arms for BATCH-priority writers
        # (interactive writers ARE the interactive party — indexer.py
        # `_batch_producer = writer.priority == "batch"`).
        writer.priority = "batch"
        files, docs = [], []
        for i in range(5):
            f = tmp_path / f"f{i}.py"
            f.write_text("stable")
            files.append(f)
            docs.append(self._doc_for(f, head_hash="old", tumbler=f"1.1.{i + 1}"))

        _, writer, _ = self._run_hook(
            tmp_path, monkeypatch, docs, head_hash="new", files=files,
            writer=writer,
        )

        assert [len(c) for c in writer.update_many_calls] == [2, 2, 1], \
            "5 changed docs at page=2 must batch as 2+2+1"
        assert writer.update_calls == []
        # await_fair_window probes is_interactive_write_pending at least
        # once per page (3 pages) — the RDR-146 fairness contract.
        assert getattr(writer, "fairness_probes", 0) >= 3, \
            f"fairness window must be consulted per page, got {getattr(writer, 'fairness_probes', 0)}"

    def test_update_many_failure_falls_back_to_per_file_update(
        self, tmp_path, monkeypatch,
    ):
        # nexus-xedhp: a whole-batch update_many failure must not sink the
        # run — Pass 1b falls back to the per-file update() loop, mirroring
        # register_many's established per-file failure isolation.
        f = tmp_path / "a.py"
        f.write_text("stable")
        doc = self._doc_for(f, head_hash="old")

        writer = self._StubWriter(update_many_raises=True)
        result, writer, _ = self._run_hook(
            tmp_path, monkeypatch, [doc], head_hash="new", writer=writer,
        )

        assert len(writer.update_many_calls) == 1, "must attempt the batched path first"
        assert len(writer.update_calls) == 1, "must fall back to per-file update() on batch failure"
        assert writer.update_calls[0]["head_hash"] == "new"
        assert set(result) == {f}
        assert writer.update_calls[0]["head_hash"] == "new"


class TestCatalogHookErrorSafe:
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


class TestRunHousekeeping:
    """Tests for _run_housekeeping() — orphan detection with miss_count tracking."""

    def _make_cat(self, tmp_path: Path) -> tuple[Path, "Catalog"]:
        return _make_catalog(tmp_path)

    def test_miss_count_incremented_for_missing_file(self, tmp_path, monkeypatch):
        """Entry not in indexed_set → miss_count goes from 0 to 1, not deleted."""
        from nexus.indexer import _run_housekeeping
        from nexus.catalog.tumbler import Tumbler

        catalog_dir, cat = self._make_cat(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        owner = cat.register_owner("nexus", "repo", repo_hash="aaa111")
        owner_t = cat.owner_for_repo("aaa111")
        t = cat.register(owner_t, "missing.py", content_type="code", file_path="src/missing.py")

        _run_housekeeping(cat, owner_t, indexed_set=set())

        entry = cat.resolve(t)
        assert entry is not None  # not deleted yet
        assert entry.meta.get("miss_count") == 1

    def test_miss_count_reset_when_file_seen(self, tmp_path, monkeypatch):
        """Entry in indexed_set with miss_count=1 → reset to 0."""
        from nexus.indexer import _run_housekeeping
        from nexus.catalog.tumbler import Tumbler

        catalog_dir, cat = self._make_cat(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        owner = cat.register_owner("nexus", "repo", repo_hash="bbb222")
        owner_t = cat.owner_for_repo("bbb222")
        t = cat.register(owner_t, "present.py", content_type="code", file_path="src/present.py")
        # Simulate a prior miss
        cat.update(t, meta={"miss_count": 1})

        _run_housekeeping(cat, owner_t, indexed_set={"src/present.py"})

        entry = cat.resolve(t)
        assert entry is not None
        assert entry.meta.get("miss_count", 0) == 0

    def test_orphan_deleted_at_threshold(self, tmp_path, monkeypatch):
        """Entry with miss_count=1, not in indexed_set → increments to 2 → deleted."""
        from nexus.indexer import _run_housekeeping

        catalog_dir, cat = self._make_cat(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        owner = cat.register_owner("nexus", "repo", repo_hash="ccc333")
        owner_t = cat.owner_for_repo("ccc333")
        t = cat.register(owner_t, "stale.py", content_type="code", file_path="src/stale.py")
        cat.update(t, meta={"miss_count": 1})

        _run_housekeeping(cat, owner_t, indexed_set=set())

        # Should be deleted after reaching threshold of 2
        assert cat.resolve(t) is None

    def test_already_at_threshold_gets_deleted(self, tmp_path, monkeypatch):
        """Entry with miss_count already >= 2 and not in indexed_set is deleted."""
        from nexus.indexer import _run_housekeeping

        catalog_dir, cat = self._make_cat(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        owner = cat.register_owner("nexus", "repo", repo_hash="ddd444")
        owner_t = cat.owner_for_repo("ddd444")
        t = cat.register(owner_t, "dead.py", content_type="code", file_path="src/dead.py")
        cat.update(t, meta={"miss_count": 2})

        _run_housekeeping(cat, owner_t, indexed_set=set())

        assert cat.resolve(t) is None

    def test_present_files_not_affected(self, tmp_path, monkeypatch):
        """Files in indexed_set are never modified (miss_count stays at 0 if already 0)."""
        from nexus.indexer import _run_housekeeping

        catalog_dir, cat = self._make_cat(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        owner = cat.register_owner("nexus", "repo", repo_hash="eee555")
        owner_t = cat.owner_for_repo("eee555")
        t = cat.register(owner_t, "ok.py", content_type="code", file_path="src/ok.py")

        _run_housekeeping(cat, owner_t, indexed_set={"src/ok.py"})

        entry = cat.resolve(t)
        assert entry is not None
        assert entry.meta.get("miss_count", 0) == 0

    def test_rename_detected_by_content_hash(self, tmp_path, monkeypatch):
        """File renamed: orphan with matching content_hash gets file_path updated, links preserved."""
        from nexus.indexer import _run_housekeeping

        catalog_dir, cat = self._make_cat(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        owner = cat.register_owner("nexus", "repo", repo_hash="fff666")
        owner_t = cat.owner_for_repo("fff666")
        # Old entry at old path with a content hash
        old_t = cat.register(owner_t, "old_name.py", content_type="code", file_path="src/old_name.py",
                             meta={"content_hash": "deadbeef1234"})
        # Create a link from old entry to another doc
        other_t = cat.register(owner_t, "rdr-1", content_type="rdr", file_path="docs/rdr/rdr-1.md")
        cat.link(old_t, other_t, "implements", created_by="test")

        # New entry already registered at new path with same content hash
        new_t = cat.register(owner_t, "new_name.py", content_type="code", file_path="src/new_name.py",
                             meta={"content_hash": "deadbeef1234"})

        # indexed_set has new path but not old
        _run_housekeeping(cat, owner_t, indexed_set={"src/new_name.py", "docs/rdr/rdr-1.md"})

        # Old entry should be deleted (rename transfers links, then deletes)
        assert cat.resolve(old_t) is None
        # Links should be transferred to the new entry
        links = cat.links_from(new_t)
        assert any(str(l.to_tumbler) == str(other_t) for l in links)

    def test_rename_not_triggered_without_content_hash(self, tmp_path, monkeypatch):
        """Orphan without content_hash follows normal miss_count path, not rename."""
        from nexus.indexer import _run_housekeeping

        catalog_dir, cat = self._make_cat(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        owner = cat.register_owner("nexus", "repo", repo_hash="ggg777")
        owner_t = cat.owner_for_repo("ggg777")
        # No content_hash in meta
        t = cat.register(owner_t, "no_hash.py", content_type="code", file_path="src/no_hash.py")

        _run_housekeeping(cat, owner_t, indexed_set=set())

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
