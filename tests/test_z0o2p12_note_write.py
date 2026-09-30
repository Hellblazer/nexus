# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-223 P2.2 (nexus-z0o2p.12): the note writer against the REAL engine.

A note's pieces and its manifest go to the engine in ONE ``write_manifest_many`` request. Each test
here is the evidence that the one request covers a failure the split-write machinery (``put_note_pieces``,
``store_put_manifest_direct_with_recovery``, ``rollback_uncataloged_chunk_write``, the bb6n2 client reap)
was built for:

* a failed request leaves no piece of the note in T3 and the previous manifest intact
  (``put_note_pieces``' compensating delete; "a failed re-put leaves the old manifest intact");
* concurrent puts of the same note converge to one complete manifest with every piece present
  (``store_put_manifest_direct_with_recovery``'s FK-race re-put);
* a failed first write leaves no chunk without a manifest row (``rollback_uncataloged_chunk_write``);
* a supersede that drops pieces has them swept, and a piece another document owns survives
  (the bb6n2 client reap);
* Test Plan 8: the client dies after the one request, so no chunk it wrote is without an owner;
* an unknown outcome is reported, never rolled back (``ManifestVerifyUncertainError``).
"""
from __future__ import annotations

import hashlib
import threading

import httpx
import pytest

from nexus.catalog.note_write import NoteWriteError, write_note
from nexus.catalog.store_hook import ManifestVerifyUncertainError, note_content_hash, note_manifest_metadata

_COLLECTION = "knowledge__z0o2p12-note__bge-base-en-v15-768__v1"
_OTHER = "knowledge__z0o2p12-other__bge-base-en-v15-768__v1"


def _chash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _pieces(label: str, n: int) -> list[str]:
    return [f"z0o2p12 {label} piece {i}: distinct text so each piece has its own chash" for i in range(n)]


class ClientDied(BaseException):
    """The simulated death of the client process: not an Exception, so nothing catches it."""


@pytest.fixture
def vec(t2_service_env):
    import nexus.db.http_vector_client as hvc

    return hvc.HttpVectorClient(tenant=t2_service_env)


def _register(title: str, pieces: list[str], collection: str = _COLLECTION) -> str:
    from nexus.catalog.store_hook import catalog_store_hook_tracked

    first, _ = note_manifest_metadata(pieces)
    tumbler, _created = catalog_store_hook_tracked(title=title, doc_id=first, collection_name=collection)
    assert tumbler, "catalog registration must succeed against the real service"
    return tumbler


def _present(vec, chashes: list[str], collection: str = _COLLECTION) -> set[str]:
    from nexus.db.http_vector_client import VectorServiceError

    try:
        return set(vec.existing_ids(collection, chashes))
    except VectorServiceError as exc:
        if "not registered" in str(exc):  # nothing was ever written: the collection does not exist yet
            return set()
        raise


def _manifest(doc: str) -> list[tuple[int, str]]:
    from nexus.catalog.factory import make_catalog_reader

    return [(r.position, r.chash) for r in make_catalog_reader().get_manifest(doc)]


def _index_state(doc: str) -> str | None:
    from nexus.catalog.factory import make_catalog_reader

    return make_catalog_reader().resolve(doc).index_state


def _hash(pieces: list[str]) -> str:
    _first, metas = note_manifest_metadata(pieces)
    return note_content_hash("".join(pieces), metas)


class _Recording:
    """A catalog-writer double that delegates to the real writer and records each call.

    ``mutate(kwargs)`` may edit the call; ``after`` runs after the real call returns and may raise
    (a lost acknowledgement); ``before`` runs first and may raise (a request that never left).
    """

    def __init__(self, real, *, mutate=None, before=None, after=None) -> None:
        self._real = real
        self.calls: list[str] = []
        self._mutate, self._before, self._after = mutate, before, after

    def write_manifest_many(self, docs, *args, **kwargs):
        self.calls.append("write_manifest_many")
        if self._before:
            self._before()
        if self._mutate:
            docs, kwargs = self._mutate(docs, kwargs)
        out = self._real.write_manifest_many(docs, *args, **kwargs)
        if self._after:
            self._after(out)
        return out

    def __getattr__(self, name):
        attr = getattr(self._real, name)
        if callable(attr):
            def _wrapped(*a, **k):
                self.calls.append(name)
                return attr(*a, **k)
            return _wrapped
        return attr


@pytest.fixture
def real_cat():
    from nexus.catalog.factory import make_catalog_writer

    w = make_catalog_writer(priority="interactive")
    yield w
    w.close()


def _bad_row(docs, kwargs):
    """Add a manifest row naming a chash that has no chunk: the engine refuses the document."""
    doc, rows = docs[0]
    return [(doc, [*rows, {"chash": "f" * 64, "position": len(rows)}])], kwargs


# ── one request ───────────────────────────────────────────────────────────────


class TestOneRequest:
    def test_a_note_is_one_request_that_writes_chunks_and_owner_rows(self, vec, real_cat, monkeypatch):
        """No /store-put, no /manifest/write, no separate append: chunks and manifest ride one
        write_manifest_many. The fence stamp rides it too."""
        import nexus.catalog.http_catalog_client as hcc
        import nexus.db.http_vector_client as hvc

        paths: list[str] = []
        real_hcc_post, real_hvc_post = hcc.HttpCatalogClient._post, hvc._post

        def hcc_post(self, path, *a, **k):
            paths.append(path)
            return real_hcc_post(self, path, *a, **k)

        def hvc_post(path, *a, **k):
            paths.append(path)
            return real_hvc_post(path, *a, **k)

        monkeypatch.setattr(hcc.HttpCatalogClient, "_post", hcc_post)
        monkeypatch.setattr(hvc, "_post", hvc_post)

        pieces = _pieces("one-request", 3)
        doc = _register("z0o2p12-one-request", pieces)
        paths.clear()
        res = write_note(
            catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces, content_hash=_hash(pieces),
            title="z0o2p12-one-request", cat=real_cat)

        assert paths.count("/manifest/write_many") == 1, paths
        assert [p for p in paths if p != "/collections/upsert"] == ["/manifest/write_many"], (
            "the collection's first-touch registration aside, the note is that one request: "
            f"no /store-put, no /manifest/write, no append. Got {paths}")
        assert res.chunk_ids == [_chash(p) for p in pieces]
        assert res.chunks_written == 3 and res.completed and not res.recovered
        assert _present(vec, res.chunk_ids) == set(res.chunk_ids)
        assert _manifest(doc) == [(i, _chash(p)) for i, p in enumerate(pieces)]

    def test_the_completion_stamp_rides_the_request(self, vec, real_cat):
        pieces = _pieces("stamp", 2)
        doc = _register("z0o2p12-stamp", pieces)
        from nexus.doc_indexer import _fence_begin

        _fence_begin(doc, _hash(pieces), _COLLECTION)
        assert _index_state(doc) == "indexing"
        write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces,
                   content_hash=_hash(pieces), cat=real_cat)
        assert _index_state(doc) == "complete"

    def test_no_content_hash_stamps_nothing(self, vec, real_cat):
        pieces = _pieces("nostamp", 1)
        doc = _register("z0o2p12-nostamp", pieces)
        res = write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces, cat=real_cat)
        assert res.completed is False
        assert _manifest(doc) == [(0, _chash(pieces[0]))]

    def test_chunk_metadata_matches_what_put_stamped(self, vec, real_cat):
        """A note reads back as one written the old way: title, tags, category, agent and the
        catalog_doc_id cross-reference are on the chunk."""
        pieces = _pieces("meta", 1)
        doc = _register("z0o2p12-meta", pieces)
        write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces, title="z0o2p12-meta",
                   tags="a,b", category="cat", source_agent="agent-x", session_id="sess",
                   ttl_days=30, cat=real_cat)
        got = vec.get_collection(_COLLECTION).get(ids=[_chash(pieces[0])], include=["metadatas"])
        meta = got["metadatas"][0]
        assert meta["title"] == "z0o2p12-meta" and meta["tags"] == "a,b" and meta["category"] == "cat"
        assert meta["source_agent"] == "agent-x" and meta["ttl_days"] == 30
        assert meta["catalog_doc_id"] == doc
        assert meta["embedding_model"] == "bge-base-en-v15-768"

    def test_identical_pieces_collapse_to_one_chunk_with_two_owner_rows(self, vec, real_cat):
        """A note whose text repeats has the same chash at two positions: one chunk, two owner
        rows, and the completion stamp is not refused for the shared chunk."""
        p = "z0o2p12 repeated piece text"
        pieces = [p, "z0o2p12 middle piece", p]
        doc = _register("z0o2p12-repeat", pieces)
        from nexus.doc_indexer import _fence_begin

        _fence_begin(doc, _hash(pieces), _COLLECTION)
        res = write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces,
                         content_hash=_hash(pieces), cat=real_cat)
        assert res.chunks_written == 2 and res.completed
        assert [pos for pos, _ in _manifest(doc)] == [0, 1, 2]
        assert _index_state(doc) == "complete"

    def test_bad_arguments_fail_before_any_request(self, real_cat):
        rec = _Recording(real_cat)
        with pytest.raises(ValueError):
            write_note(catalog_doc_id="", collection=_COLLECTION, pieces=["x"], cat=rec)
        with pytest.raises(ValueError):
            write_note(catalog_doc_id="1.1.1", collection=_COLLECTION, pieces=[], cat=rec)
        with pytest.raises(ValueError, match="ttl_days=0"):
            write_note(catalog_doc_id="1.1.1", collection=_COLLECTION, pieces=["x"], ttl_days=0, cat=rec)
        assert rec.calls == []


# ── replaces put_note_pieces' compensation: a failed request leaves nothing ──


class TestFailedRequestLeavesNothing:
    def test_first_write_failure_leaves_no_chunk_and_no_manifest(self, vec, real_cat):
        """put_note_pieces deleted the pieces it had written when a later piece failed; here there is
        nothing to delete because the engine rolled the whole document back."""
        pieces = _pieces("first-fail", 3)
        doc = _register("z0o2p12-first-fail", pieces)
        with pytest.raises(NoteWriteError):
            write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces,
                       content_hash=_hash(pieces), cat=_Recording(real_cat, mutate=_bad_row))
        assert _present(vec, [_chash(p) for p in pieces]) == set()
        assert _manifest(doc) == []

    def test_a_failed_reput_leaves_the_old_manifest_and_chunks_intact(self, vec, real_cat):
        old = _pieces("reput-old", 2)
        new = _pieces("reput-new", 3)
        doc = _register("z0o2p12-reput", old)
        write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=old,
                   content_hash=_hash(old), cat=real_cat)
        before = _manifest(doc)
        assert before == [(i, _chash(p)) for i, p in enumerate(old)]

        with pytest.raises(NoteWriteError):
            write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=new,
                       content_hash=_hash(new), cat=_Recording(real_cat, mutate=_bad_row))

        assert _manifest(doc) == before, "the previous manifest must be untouched"
        assert _present(vec, [_chash(p) for p in old]) == {_chash(p) for p in old}
        assert _present(vec, [_chash(p) for p in new]) == set(), "no piece of the failed note was written"

    def test_a_request_that_never_left_is_a_confirmed_failure(self, vec, real_cat):
        pieces = _pieces("never-left", 2)
        doc = _register("z0o2p12-never-left", pieces)

        def boom():
            raise httpx.ConnectError("refused")

        with pytest.raises(NoteWriteError) as ei:
            write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces,
                       cat=_Recording(real_cat, before=boom))
        assert "refused" in ei.value.reason
        assert _present(vec, [_chash(p) for p in pieces]) == set()


# ── Test Plan 8 and the lost-acknowledgement outcome ─────────────────────────


class TestClientDeath:
    def test_client_dies_after_the_request_no_chunk_is_without_an_owner(self, vec, real_cat):
        """Test Plan 8. There is one request; the process dies as soon as it has been sent. Every
        chunk of the note that reached T3 has an owner row."""
        pieces = _pieces("dies-after", 4)
        doc = _register("z0o2p12-dies-after", pieces)

        def die(_out):
            raise ClientDied()

        with pytest.raises(ClientDied):
            write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces,
                       content_hash=_hash(pieces), cat=_Recording(real_cat, after=die))

        chashes = [_chash(p) for p in pieces]
        in_t3 = _present(vec, chashes)
        owned = {c for _, c in _manifest(doc)}
        assert in_t3, "control: the request was sent, so the note is in T3"
        assert in_t3 <= owned, f"chunks without an owner: {sorted(in_t3 - owned)}"
        assert in_t3 == set(chashes)

    def test_client_dies_before_the_request_nothing_is_written(self, vec, real_cat):
        pieces = _pieces("dies-before", 4)
        doc = _register("z0o2p12-dies-before", pieces)

        def die():
            raise ClientDied()

        with pytest.raises(ClientDied):
            write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces,
                       cat=_Recording(real_cat, before=die))
        assert _present(vec, [_chash(p) for p in pieces]) == set()
        assert _manifest(doc) == []

    @pytest.mark.parametrize("exc", [
        httpx.ReadTimeout("slow"),
        httpx.RemoteProtocolError("connection dropped before the answer"),
    ], ids=["read-timeout", "connection-dropped"])
    def test_a_lost_acknowledgement_is_recovered_from_the_manifest(self, vec, real_cat, exc):
        """The request committed and only the answer was lost: the manifest read proves it, and the
        note is reported landed, not rolled back."""
        pieces = _pieces("ack-lost", 3)
        doc = _register("z0o2p12-ack-lost", pieces)

        def lose(_out):
            raise exc

        res = write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces,
                         content_hash=_hash(pieces), cat=_Recording(real_cat, after=lose))
        assert res.recovered is True
        assert _present(vec, res.chunk_ids) == set(res.chunk_ids)
        assert _manifest(doc) == [(i, _chash(p)) for i, p in enumerate(pieces)]
        assert res.completed is True and _index_state(doc) == "complete"

    def test_an_unknown_outcome_is_reported_and_nothing_is_rolled_back(self, vec, real_cat, monkeypatch):
        """ManifestVerifyUncertainError survives: an atomic request can still time out with an
        unknown result, and the read that would settle it can fail too."""
        import nexus.catalog.factory as factory
        import nexus.catalog.store_hook as sh

        monkeypatch.setattr(sh, "_manifest_verify_retry_sleep", lambda s: None)
        pieces = _pieces("unknown", 2)
        doc = _register("z0o2p12-unknown", pieces)

        real_reader = factory.make_catalog_reader

        def no_reader():
            raise RuntimeError("catalog reader down")

        def lose(_out):
            # The request has committed; now the read that would settle the outcome is down.
            monkeypatch.setattr(factory, "make_catalog_reader", no_reader)
            raise httpx.ReadTimeout("slow")

        with pytest.raises(ManifestVerifyUncertainError) as ei:
            write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces,
                       cat=_Recording(real_cat, after=lose))
        assert "slow" in str(ei.value), "the write's own error rides the message"
        monkeypatch.setattr(factory, "make_catalog_reader", real_reader)
        assert _present(vec, [_chash(p) for p in pieces]) == {_chash(p) for p in pieces}, (
            "the request had committed: nothing may have been removed")

    def test_a_refused_completion_stamp_is_unknown_not_failed(self, real_cat):
        class Refusing:
            def write_manifest_many(self, docs, **kw):
                return {"failed_doc_ids": [], "chunks_written": 1,
                        "complete_refused": [{"doc_id": docs[0][0], "referenced": 1, "missing": 1,
                                              "chunk_count": 1}]}

        with pytest.raises(ManifestVerifyUncertainError, match="refused to stamp"):
            write_note(catalog_doc_id="1.1.1", collection=_COLLECTION, pieces=["x"],
                       content_hash=_chash("x"), cat=Refusing())


# ── replaces the bb6n2 client reap: the engine's own sweep ──────────────────


class TestSupersedeSweep:
    def test_a_supersede_sweeps_the_dropped_pieces_and_reports_them(self, vec, real_cat):
        a, b, c = _pieces("sweep", 3)
        doc = _register("z0o2p12-sweep", [a, b])
        write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=[a, b], cat=real_cat)
        res = write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=[a, c], cat=real_cat)

        assert res.dropped_chashes == [_chash(b)] and res.swept == 1
        assert _present(vec, [_chash(a), _chash(b), _chash(c)]) == {_chash(a), _chash(c)}
        assert _manifest(doc) == [(0, _chash(a)), (1, _chash(c))]

    def test_a_piece_another_document_owns_survives_the_sweep(self, vec, real_cat):
        a, shared, c = _pieces("shared", 3)
        doc = _register("z0o2p12-shared-a", [a, shared])
        write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=[a, shared], cat=real_cat)
        twin = _register("z0o2p12-shared-twin", [shared])
        write_note(catalog_doc_id=twin, collection=_COLLECTION, pieces=[shared], cat=real_cat)

        res = write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=[a, c], cat=real_cat)
        assert res.dropped_chashes == [_chash(shared)] and res.swept == 0
        assert _chash(shared) in _present(vec, [_chash(shared)])
        assert _manifest(twin) == [(0, _chash(shared))]

    def test_an_unchanged_reput_drops_nothing(self, vec, real_cat):
        pieces = _pieces("same", 2)
        doc = _register("z0o2p12-same", pieces)
        write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces, cat=real_cat)
        res = write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces, cat=real_cat)
        assert res.dropped_chashes == [] and res.swept == 0 and res.embed_embedded == 0


# ── replaces store_put_manifest_direct_with_recovery: no gap left to recover ─


class TestConcurrentPuts:
    def test_concurrent_puts_of_the_same_note_converge_to_one_complete_manifest(self, vec):
        """The FK-race recovery re-put pieces a concurrent rollback deleted between the chunk write
        and the manifest write. There is no such gap: N writers of one note end with the whole note."""
        from nexus.catalog.factory import make_catalog_writer

        pieces = _pieces("concurrent", 4)
        doc = _register("z0o2p12-concurrent", pieces)
        errors: list[BaseException] = []
        barrier = threading.Barrier(4)

        def worker():
            w = make_catalog_writer(priority="interactive")
            try:
                barrier.wait()
                write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces,
                           content_hash=_hash(pieces), cat=w)
            except BaseException as exc:  # noqa: BLE001 — collected and asserted below
                errors.append(exc)
            finally:
                w.close()

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert _manifest(doc) == [(i, _chash(p)) for i, p in enumerate(pieces)]
        assert _present(vec, [_chash(p) for p in pieces]) == {_chash(p) for p in pieces}

    def test_concurrent_puts_of_different_content_leave_one_whole_note(self, vec):
        """Two writers replace the same document with different content. Whichever commits last
        owns it and the manifest is never a mix. RESIDUAL, pinned so it is not mistaken for a
        guarantee: the engine reads the previous manifest before, and sweeps after, the replace
        transaction, so neither writer's drop list names the other's pieces and the loser's pieces
        can stay in T3 without an owner (hidden from reads by live(c), removed by the RDR-192
        reaper). The winner's pieces are always present and owned."""
        from nexus.catalog.factory import make_catalog_writer

        x, y = _pieces("race-x", 3), _pieces("race-y", 3)
        doc = _register("z0o2p12-race", x)
        errors: list[BaseException] = []
        barrier = threading.Barrier(2)

        def worker(pieces):
            w = make_catalog_writer(priority="interactive")
            try:
                barrier.wait()
                write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces, cat=w)
            except BaseException as exc:  # noqa: BLE001 — collected and asserted below
                errors.append(exc)
            finally:
                w.close()

        threads = [threading.Thread(target=worker, args=(p,)) for p in (x, y)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        final = [c for _, c in _manifest(doc)]
        xs, ys = [_chash(p) for p in x], [_chash(p) for p in y]
        assert final in (xs, ys), f"a mixed manifest: {final}"
        loser = set(ys if final == xs else xs)
        in_t3 = _present(vec, xs + ys)
        assert set(final) <= in_t3, "the winner's pieces are all present"
        assert in_t3 - set(final) <= loser, "only the loser's own pieces may linger"


# ── the split-write machinery that stays ─────────────────────────────────────


def test_the_default_writer_is_made_and_closed_by_the_call(vec):
    pieces = _pieces("default-cat", 1)
    doc = _register("z0o2p12-default-cat", pieces)
    res = write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces)
    assert res.chunk_ids == [_chash(pieces[0])]
    assert _manifest(doc) == [(0, _chash(pieces[0]))]
