# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-223 P2.2 (nexus-z0o2p.12): the note writer against the REAL engine.

A note's pieces and its manifest go to the engine in ONE ``write_manifest_many`` request. Each test
here is the evidence that the one request covers a failure the split write (a chunk put, a manifest
write, a compensating delete, a client reap; deleted at nexus-z0o2p.32) was built for:

* a failed request leaves no piece of the note in T3 and the previous manifest intact;
* concurrent puts of the same note converge to one complete manifest with every piece present;
* a failed first write leaves no chunk without a manifest row;
* a supersede that drops pieces has them swept, and a piece another document owns survives;
* Test Plan 8: the client dies after the one request, so no chunk it wrote is without an owner;
* an unknown outcome is reported, never rolled back (``ManifestVerifyUncertainError``).
"""
from __future__ import annotations

import hashlib
import threading

import httpx
import pytest

from nexus.catalog.note_write import NoteWriteError, put_note, write_note
from nexus.catalog.store_hook import ManifestVerifyUncertainError, note_content_hash, note_manifest_metadata
from nexus.errors import CombinedWriteEmbedTimeoutError

_COLLECTION = "knowledge__z0o2p12-note__bge-base-en-v15-768__v1"
_OTHER = "knowledge__z0o2p12-other__bge-base-en-v15-768__v1"


def _chash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _pieces(label: str, n: int) -> list[str]:
    return [f"z0o2p12 {label} piece {i}: distinct text so each piece has its own chash" for i in range(n)]


class ClientDied(BaseException):
    """The simulated death of the client process: not an Exception, so nothing catches it."""


def _embed_timeout() -> CombinedWriteEmbedTimeoutError:
    """What ``write_manifest_many`` raises for a read timeout on the chunk-carrying POST."""
    return CombinedWriteEmbedTimeoutError(collection=_COLLECTION, chunk_count=1, original="ReadTimeout")


def _status_error(code: int, headers: dict | None = None) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://engine.invalid/v1/catalog/manifest/write_many")
    return httpx.HTTPStatusError(
        f"HTTP {code}", request=request, response=httpx.Response(code, request=request, headers=headers or {}))


def _once(make_exc):
    """A ``before`` / ``after`` hook that raises ``make_exc()`` the first time it is called only."""
    fired: list[bool] = []

    def hook(*_a):
        if not fired:
            fired.append(True)
            raise make_exc()

    return hook


@pytest.fixture(autouse=True)
def _no_retry_sleeps(monkeypatch):
    """The manifest-write retry backs off 0.5 + 1 + 2 s and the shared rate brake remembers a trip
    across tests; neither is under test here, so neither may cost real time or leak."""
    from nexus.rate_brake import reset_brake

    monkeypatch.setattr("nexus.retry.time.sleep", lambda seconds: None)
    reset_brake()
    yield
    reset_brake()


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


# ── a failed request leaves nothing ──────────────────────────────────────────


class TestFailedRequestLeavesNothing:
    def test_first_write_failure_leaves_no_chunk_and_no_manifest(self, vec, real_cat):
        """The split write had to delete the pieces it had written when a later piece failed; here there
        is nothing to delete because the engine rolled the whole document back."""
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

    @pytest.mark.parametrize("make_exc", [
        _embed_timeout,
        lambda: _status_error(504),
        lambda: _status_error(500),
    ], ids=["embed-timeout", "gateway-504", "server-500"])
    def test_a_lost_acknowledgement_is_recovered_from_the_manifest(self, vec, real_cat, make_exc):
        """The request committed and only the answer was lost: the manifest read proves it, and the
        note is reported landed, not rolled back."""
        pieces = _pieces("ack-lost", 3)
        doc = _register("z0o2p12-ack-lost", pieces)

        cat = _Recording(real_cat, after=_once(make_exc))
        res = write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces,
                         content_hash=_hash(pieces), cat=cat)
        assert res.recovered is True
        assert cat.calls.count("write_manifest_many") == 2, "the note is resent once, not re-asked"
        assert _present(vec, res.chunk_ids) == set(res.chunk_ids)
        assert _manifest(doc) == [(i, _chash(p)) for i, p in enumerate(pieces)]
        assert res.completed is True and _index_state(doc) == "complete"

    def test_a_lost_acknowledgement_for_a_repeated_piece_is_stamped_complete(self, vec, real_cat):
        """The stamp is re-asked with the manifest ROW count: a repeated piece is one chunk but two
        rows, and the engine verifies rows. Counting distinct chashes refused the stamp and left the
        fence at 'indexing' for a note that had landed."""
        from nexus.doc_indexer import _fence_begin

        p = "z0o2p12 repeated piece under a lost acknowledgement"
        pieces = [p, "z0o2p12 the piece between", p]
        doc = _register("z0o2p12-ack-lost-repeat", pieces)
        _fence_begin(doc, _hash(pieces), _COLLECTION)

        res = write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces,
                         content_hash=_hash(pieces), cat=_Recording(real_cat, after=_once(_embed_timeout)))
        assert res.recovered is True and res.completed is True
        assert _index_state(doc) == "complete"

    def test_an_in_flight_failure_the_manifest_does_not_show_is_unknown_not_failed(self, vec, real_cat):
        """The engine cannot cancel an in-flight embed, so a timeout with no note in the manifest
        may still commit. It is reported as unknown, and the late commit leaves a whole note."""
        pieces = _pieces("late-commit", 2)
        doc = _register("z0o2p12-late-commit", pieces)

        def timeout_before_commit():
            raise _embed_timeout()

        with pytest.raises(ManifestVerifyUncertainError, match="may still commit"):
            write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces,
                       content_hash=_hash(pieces), cat=_Recording(real_cat, before=timeout_before_commit))
        assert _manifest(doc) == [], "nothing had committed when the manifest was read"

        # ... and now the request commits late.
        write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces,
                   content_hash=_hash(pieces), cat=real_cat)
        assert _manifest(doc) == [(i, _chash(p)) for i, p in enumerate(pieces)]
        assert _present(vec, [_chash(p) for p in pieces]) == {_chash(p) for p in pieces}

    def test_a_manifest_with_the_same_chashes_in_other_positions_is_not_the_note(self, vec, real_cat):
        """The comparison is (position, chash), in order: the same chunks in another order are a
        different note, so a failed re-put is not taken for a landed one."""
        a, b = _pieces("order", 2)
        doc = _register("z0o2p12-order", [a, b])
        write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=[a, b], cat=real_cat)

        def dropped():
            raise httpx.RemoteProtocolError("connection dropped")

        with pytest.raises(ManifestVerifyUncertainError):
            write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=[b, a],
                       cat=_Recording(real_cat, before=dropped))

    @pytest.mark.parametrize("code", [400, 409, 422])
    def test_a_definitive_refusal_is_a_failure_even_when_the_manifest_matches(self, vec, real_cat, code):
        """An unchanged re-put refused with new tags must not report 'recovered': the engine
        answered, the metadata was not applied, and the manifest happening to match proves nothing."""
        pieces = _pieces("refused", 2)
        doc = _register("z0o2p12-refused", pieces)
        write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces, cat=real_cat)

        def refuse():
            raise _status_error(code)

        with pytest.raises(NoteWriteError) as ei:
            write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces, tags="new,tags",
                       cat=_Recording(real_cat, before=refuse))
        assert ei.value.manifest_empty is False, "the earlier version is still there"

    def test_manifest_empty_is_true_only_when_the_document_has_no_manifest(self, vec, real_cat):
        pieces = _pieces("empty", 2)
        doc = _register("z0o2p12-empty", pieces)
        with pytest.raises(NoteWriteError) as first:
            write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces,
                       cat=_Recording(real_cat, mutate=_bad_row))
        assert first.value.manifest_empty is True
        write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces, cat=real_cat)
        with pytest.raises(NoteWriteError) as second:
            write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=_pieces("empty-2", 1),
                       cat=_Recording(real_cat, mutate=_bad_row))
        assert second.value.manifest_empty is False

    def test_the_request_is_retried_on_a_connectivity_blip_and_a_rate_limit(self, vec, real_cat):
        """nexus.retry's manifest-write retry (connectivity, 429, 503 with Retry-After) wraps the
        request, as it wraps every other combined write."""
        pieces = _pieces("retry", 2)
        doc = _register("z0o2p12-retry", pieces)
        failures = [httpx.ConnectError("blip"), _status_error(429, {"Retry-After": "0"})]

        def flaky():
            if failures:
                raise failures.pop(0)

        cat = _Recording(real_cat, before=flaky)
        res = write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces, cat=cat)
        assert cat.calls.count("write_manifest_many") == 3
        assert res.chunks_written == 2 and not res.recovered
        assert _manifest(doc) == [(i, _chash(p)) for i, p in enumerate(pieces)]

    def test_an_embed_timeout_is_not_retried(self, vec, real_cat):
        """A retry would start a second uncancelled embed."""
        pieces = _pieces("no-retry", 1)
        doc = _register("z0o2p12-no-retry", pieces)

        def timeout():
            raise _embed_timeout()

        cat = _Recording(real_cat, before=timeout)
        with pytest.raises(ManifestVerifyUncertainError):
            write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces, cat=cat)
        assert cat.calls.count("write_manifest_many") == 1

    def test_the_client_killed_at_the_transport_after_the_post_leaves_no_ownerless_chunk(
        self, vec, monkeypatch,
    ):
        """Test Plan 8, non-vacuous: the process dies at the HTTP layer right after the ONE request has
        gone out (below write_manifest_many, so an implementation that split the write would be
        caught mid-way). Every chunk the note put in T3 has an owner row."""
        import nexus.catalog.http_catalog_client as hcc

        pieces = _pieces("transport-kill", 4)
        doc = _register("z0o2p12-transport-kill", pieces)
        posts: list[str] = []
        real_embed_post = hcc.HttpCatalogClient._post_embedding_write

        def kill_after_post(self, path, *a, **k):
            posts.append(path)
            out = real_embed_post(self, path, *a, **k)
            raise ClientDied()

        monkeypatch.setattr(hcc.HttpCatalogClient, "_post_embedding_write", kill_after_post)
        with pytest.raises(ClientDied):
            write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces,
                       content_hash=_hash(pieces))

        assert posts == ["/manifest/write_many"], posts
        chashes = [_chash(p) for p in pieces]
        in_t3 = _present(vec, chashes)
        owned = {c for _, c in _manifest(doc)}
        assert in_t3 == set(chashes), "control: the one request went out and committed"
        assert in_t3 <= owned, f"chunks without an owner: {sorted(in_t3 - owned)}"

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
            raise _embed_timeout()

        with pytest.raises(ManifestVerifyUncertainError) as ei:
            write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces,
                       cat=_Recording(real_cat, after=lose))
        assert "ReadTimeout" in str(ei.value), "the write's own error rides the message"
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


# ── concurrent puts: no gap between the chunk write and the owner write ──────


class TestConcurrentPuts:
    def test_concurrent_puts_of_the_same_note_converge_to_one_complete_manifest(self, vec):
        """A concurrent rollback could delete pieces between the split write's chunk write and its
        manifest write. There is no such gap now: N writers of one note end with the whole note."""
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

    @pytest.mark.parametrize("round_", range(4))
    def test_concurrent_puts_of_different_content_leave_exactly_one_whole_note(self, vec, round_):
        """Two writers replace the same document with different content at once. Whichever commits
        last owns it, the manifest is never a mix, and the loser's pieces are swept: nothing is left
        in T3 without an owner. That last part needs the engine to read the previous manifest under
        the document's write locks (CatalogRepository.writeManifestMany); read before them, both
        replacers saw the same manifest and neither dropped list named the other's pieces."""
        from nexus.catalog.factory import make_catalog_writer

        x, y = _pieces(f"race-x{round_}", 3), _pieces(f"race-y{round_}", 3)
        # The document's identity stamp (meta.doc_id) names a chunk neither writer writes: a stamp
        # naming x[0] would make the sweep's live-note guard keep that chunk, which is a property of
        # the registration this test made, not of the race.
        doc = _register(f"z0o2p12-race-{round_}", [f"z0o2p12 race identity {round_}"])
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
        assert _present(vec, xs + ys) == set(final), "the loser's pieces must not linger without an owner"


# ── the default writer ───────────────────────────────────────────────────────


def test_the_default_writer_is_made_and_closed_by_the_call(vec):
    pieces = _pieces("default-cat", 1)
    doc = _register("z0o2p12-default-cat", pieces)
    res = write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces)
    assert res.chunk_ids == [_chash(pieces[0])]
    assert _manifest(doc) == [(0, _chash(pieces[0]))]


# ── chunk metadata against the real engine ───────────────────────────────────


class TestChunkMetadata:
    def _meta(self, vec, chash: str, collection: str = _COLLECTION) -> dict:
        got = vec.get_collection(collection).get(ids=[chash], include=["metadatas"])
        return got["metadatas"][0]

    def test_session_content_type_and_indexed_at_reach_the_engine(self, vec, real_cat):
        pieces = _pieces("meta-full", 1)
        doc = _register("z0o2p12-meta-full", pieces)
        write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces, session_id="sess-42",
                   source_agent="agent-x", cat=real_cat)
        meta = self._meta(vec, _chash(pieces[0]))
        assert meta["session_id"] == "sess-42"
        assert meta["content_type"] == "prose"
        assert meta["indexed_at"][:2] == "20", meta["indexed_at"]
        assert meta["source_agent"] == "agent-x"

    def test_content_type_follows_the_collection_prefix_as_put_did(self, vec, real_cat):
        rdr = "rdr__z0o2p12-rdr__bge-base-en-v15-768__v1"
        pieces = _pieces("meta-rdr", 1)
        doc = _register("z0o2p12-meta-rdr", pieces, collection=rdr)
        write_note(catalog_doc_id=doc, collection=rdr, pieces=pieces, cat=real_cat)
        assert self._meta(vec, _chash(pieces[0]), rdr)["content_type"] == "markdown"

    def test_a_same_text_reput_refreshes_the_metadata_without_re_embedding(self, vec, real_cat):
        """Existing chash, no vector: the chunk is not re-embedded and its metadata is refreshed."""
        pieces = _pieces("meta-refresh", 1)
        doc = _register("z0o2p12-meta-refresh", pieces)
        write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces, tags="old", ttl_days=5,
                   cat=real_cat)
        res = write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces, tags="new,tags",
                         ttl_days=30, category="cat2", cat=real_cat)
        meta = self._meta(vec, _chash(pieces[0]))
        assert (meta["tags"], meta["ttl_days"], meta["category"]) == ("new,tags", 30, "cat2")
        assert res.embed_embedded == 0 and res.embed_skipped == 1


    def test_a_reput_keeps_a_key_another_writer_set_on_the_chunk(self, vec, real_cat):
        """M6: the note's metadata is MERGED into the stored chunk's, as the upsert it replaced did.
        ``nx enrich bib`` writes ``bib_*`` onto note chunks; a re-put must refresh what the note sets
        and leave what it does not."""
        pieces = _pieces("meta-merge", 1)
        doc = _register("z0o2p35-meta-merge", pieces)
        write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces, tags="old", cat=real_cat)
        chash = _chash(pieces[0])
        vec.get_collection(_COLLECTION).update(ids=[chash], metadatas=[{"bib_year": 2024, "bib_venue": "VLDB"}])
        assert self._meta(vec, chash)["bib_year"] == 2024, "control: the other writer's key is stored"

        write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces, tags="new", cat=real_cat)
        meta = self._meta(vec, chash)
        assert meta["tags"] == "new", "the key the note sets is refreshed"
        assert (meta["bib_year"], meta["bib_venue"]) == (2024, "VLDB"), "a key another writer set is kept"

    def test_a_permanent_reput_of_a_ttld_note_drops_its_ttl(self, vec, real_cat):
        """nexus-z0o2p.34 (critique 5b): the merge keeps a stored key the incoming metadata omits. A
        re-put that makes a TTL'd note PERMANENT sends no ``ttl_days``, so under a bare merge the old
        TTL would survive and the note would still expire (data loss). The note's own keys must
        therefore be overwritten by a re-put, absent ones included."""
        pieces = _pieces("meta-ttl", 1)
        doc = _register("z0o2p34-meta-ttl", pieces)
        chash = _chash(pieces[0])
        write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces, ttl_days=5, cat=real_cat)
        assert self._meta(vec, chash)["ttl_days"] == 5, "control: the TTL is stored"

        write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces, cat=real_cat)    # permanent
        meta = self._meta(vec, chash)
        assert meta.get("ttl_days") in (None, 0), f"the permanent re-put must drop the stored TTL: {meta}"
        assert meta.get("expires_at") in (None, ""), "and any expiry derived from it"

    def test_the_request_asks_the_engine_to_merge(self, vec, real_cat):
        seen: list[dict] = []

        def spy(docs, kwargs):
            seen.append(dict(kwargs))
            return docs, kwargs

        pieces = _pieces("meta-merge-flag", 1)
        write_note(catalog_doc_id=_register("z0o2p35-meta-merge-flag", pieces), collection=_COLLECTION,
                   pieces=pieces, cat=_Recording(real_cat, mutate=spy))
        assert seen and seen[0].get("metadata_merge") is True and not seen[0].get("metadata_delete_keys")


# ── put_note: the caller protocol ────────────────────────────────────────────


class TestPutNote:
    def test_put_note_begins_the_fence_before_the_write_and_leaves_the_stamp_to_stamp_note(self, vec):
        import nexus.catalog.note_write as nw
        import nexus.doc_indexer as di
        from unittest.mock import patch

        order: list[str] = []
        real_begin, real_write = di._fence_begin, nw.write_note

        def spy_begin(*a, **k):
            order.append("fence-begin")
            return real_begin(*a, **k)

        def spy_write(**kw):
            order.append("write")
            return real_write(**kw)

        with patch("nexus.doc_indexer._fence_begin", side_effect=spy_begin), \
             patch("nexus.catalog.note_write.write_note", side_effect=spy_write):
            out = put_note(content="z0o2p12 put_note fence order", collection=_COLLECTION,
                           title="z0o2p12-putnote-fence")
        assert order == ["fence-begin", "write"]
        assert out.status == nw.STORED and out.chunk_ids == [_chash("z0o2p12 put_note fence order")]
        assert out.doc_id == out.chunk_ids[0]
        # RDR-223 (nexus-z0o2p.34): the write carries no stamp; the producer stamps after its chains.
        assert out.stamp_pending and not out.write.completed
        assert _index_state(out.catalog_doc_id) == "indexing"
        stamped = nw.stamp_note(out)
        assert stamped is out and out.status == nw.STORED and out.write.completed and not out.stamp_pending
        assert _index_state(out.catalog_doc_id) == "complete"
        assert nw.stamp_note(out) is out, "a second call has nothing to send"

    def test_no_document_means_nothing_is_written_and_no_fence_begins(self, vec):
        import nexus.catalog.note_write as nw
        from unittest.mock import patch

        with patch("nexus.catalog.store_hook.catalog_store_hook_tracked", return_value=("", False)), \
             patch("nexus.doc_indexer._fence_begin") as begin, \
             patch("nexus.catalog.note_write.write_note") as write:
            out = put_note(content="z0o2p12 no document", collection=_COLLECTION, title="z0o2p12-nodoc")
        assert out.status == nw.NO_CATALOG and out.reason.startswith("catalog registration failed")
        begin.assert_not_called()
        write.assert_not_called()

    def test_a_stamp_that_fails_after_the_chains_is_unknown_and_leaves_the_fence_indexing(self, vec):
        """nexus-z0o2p.34: the stamp is sent after the post-store chains. One that fails (a transport
        error, an engine with no fence route) reports the note as uncertain with ``unstamped`` and
        leaves the document ``indexing``: nothing fails the fence, nothing is rolled back."""
        import httpx
        import nexus.catalog.note_write as nw
        from unittest.mock import patch

        out = put_note(content="z0o2p12 unstamped", collection=_COLLECTION, title="z0o2p12-unstamped")
        assert out.status == nw.STORED and out.stamp_pending
        with patch("nexus.catalog.note_write.complete_document",
                   side_effect=httpx.ConnectError("refused")), \
             patch("nexus.doc_indexer._fence_fail") as fail, \
             patch("nexus.catalog.store_hook.rollback_minted_catalog_entry") as rollback:
            stamped = nw.stamp_note(out)
        assert stamped is out and out.status == nw.UNCERTAIN and out.unstamped and not out.stamp_refused
        assert "stamp failed" in out.reason
        assert "stays 'indexing'" in (nw.failure_message(out, subject="x") or "")
        fail.assert_not_called()
        rollback.assert_not_called()
        assert _index_state(out.catalog_doc_id) == "indexing"

    def test_a_stamp_the_engine_refuses_after_the_chains_is_the_stamp_refused_outcome(self, vec):
        import nexus.catalog.note_write as nw
        from nexus.errors import IndexRunVerifyRefused
        from unittest.mock import patch

        out = put_note(content="z0o2p12 refused stamp", collection=_COLLECTION, title="z0o2p12-refused-stamp")
        refusal = IndexRunVerifyRefused(
            doc_id=out.catalog_doc_id, referenced=1, present=0, missing=1, chunk_count=1)
        with patch("nexus.catalog.note_write.complete_document", side_effect=refusal), \
             patch("nexus.doc_indexer._fence_fail") as fail:
            nw.stamp_note(out)
        assert out.status == nw.UNCERTAIN and out.stamp_refused and out.stamp_detail
        assert "refused to stamp" in (nw.failure_message(out, subject="x") or "")
        fail.assert_not_called()
        assert _index_state(out.catalog_doc_id) == "indexing"

    def test_stamp_note_does_nothing_for_a_note_that_did_not_store(self, vec):
        import nexus.catalog.note_write as nw
        from unittest.mock import patch

        out = nw.PutNoteOutcome(status=nw.NOT_LANDED, collection=_COLLECTION, stamp_pending=True)
        with patch("nexus.catalog.note_write.complete_document") as stamp:
            assert nw.stamp_note(out) is out
        stamp.assert_not_called()
        assert out.status == nw.NOT_LANDED

    @pytest.mark.parametrize("manifest_empty", [True, False])
    def test_a_minted_row_is_removed_only_when_its_manifest_is_empty(self, vec, manifest_empty):
        """A different concurrent version means the row is no longer this call's to delete."""
        import nexus.catalog.note_write as nw
        from unittest.mock import patch

        refusal = NoteWriteError(catalog_doc_id="x", collection=_COLLECTION, reason="refused",
                                 manifest_empty=manifest_empty)
        with patch("nexus.catalog.note_write.write_note", side_effect=refusal), \
             patch("nexus.doc_indexer._fence_fail") as fail, \
             patch("nexus.catalog.store_hook.rollback_minted_catalog_entry") as rollback:
            out = put_note(content=f"z0o2p12 minted {manifest_empty}", collection=_COLLECTION,
                           title=f"z0o2p12-minted-{manifest_empty}")
        assert out.status == nw.NOT_LANDED and out.minted is True
        assert rollback.call_count == (1 if manifest_empty else 0)
        assert fail.call_count == 1                      # both still fire (Decision 5)

    def test_a_minted_row_is_removed_before_the_fence_is_failed(self, vec):
        """M1: the read-then-delete window in ``rollback_minted_catalog_entry`` must not be widened by
        the fence write, so the removal runs FIRST and the fence is failed after (Decision 5 keeps
        both); a row that could not be removed has its stamp cleared in between."""
        import nexus.catalog.note_write as nw
        from unittest.mock import patch

        refusal = NoteWriteError(catalog_doc_id="x", collection=_COLLECTION, reason="refused",
                                 manifest_empty=True)
        for removed in (True, False):
            order: list[str] = []

            def rollback(*a, _removed=removed, **k):
                order.append("rollback")
                return _removed

            with patch("nexus.catalog.note_write.write_note", side_effect=refusal), \
                 patch("nexus.doc_indexer._fence_fail", side_effect=lambda *a, **k: order.append("fence-fail")), \
                 patch("nexus.catalog.store_hook.rollback_minted_catalog_entry", side_effect=rollback), \
                 patch("nexus.catalog.store_hook.restore_pre_call_stamp",
                       side_effect=lambda *a, **k: order.append("restore-stamp")):
                out = put_note(content=f"z0o2p35 minted order {removed}", collection=_COLLECTION,
                               title=f"z0o2p35-minted-order-{removed}")
            assert out.status == nw.NOT_LANDED
            assert order == (["rollback", "fence-fail"] if removed
                             else ["rollback", "restore-stamp", "fence-fail"]), order

    def test_a_landed_note_whose_resend_failed_does_not_fail_the_fence(self, vec):
        """M2: the manifest showed the note, so attempt 1 committed (without a stamp: the write
        carries none, nexus-z0o2p.34). A resend that then fails is UNCERTAIN, and ``_fence_fail`` must
        not run: the note is whole and its fence already reads ``indexing``."""
        import nexus.catalog.note_write as nw
        from unittest.mock import patch

        landed = nw.LandedUnconfirmedError("note x is in the manifest, but resending failed")
        with patch("nexus.catalog.note_write.write_note", side_effect=landed), \
             patch("nexus.doc_indexer._fence_fail") as fail, \
             patch("nexus.catalog.store_hook.rollback_minted_catalog_entry") as rollback:
            out = put_note(content="z0o2p35 landed unconfirmed", collection=_COLLECTION,
                           title="z0o2p35-landed-unconfirmed")
        assert out.status == nw.UNCERTAIN and "resending" in out.reason
        fail.assert_not_called()
        rollback.assert_not_called()

    def test_an_in_flight_request_the_manifest_does_not_show_still_fails_the_fence(self, vec):
        """Control for M2: nothing landed that we can see, the request may still commit, and the
        existing rule (fail the fence, keep the row) stands."""
        import nexus.catalog.note_write as nw
        from unittest.mock import patch

        with patch("nexus.catalog.note_write.write_note",
                   side_effect=ManifestVerifyUncertainError("in flight, not visible")), \
             patch("nexus.doc_indexer._fence_fail") as fail:
            out = put_note(content="z0o2p35 in flight", collection=_COLLECTION, title="z0o2p35-in-flight")
        assert out.status == nw.UNCERTAIN
        assert fail.call_count == 1

    def test_a_minted_row_whose_own_removal_fails_keeps_no_stamp_for_a_chunk_never_written(self, vec):
        """Case E: the write is refused with an empty manifest and the row this call minted cannot be
        deleted (the same outage). The surviving row must not keep ``meta.doc_id`` naming the first chash
        of a chunk that does not exist, or a later note that owns and drops that chash has it kept by
        ``live_note_chashes`` as if it were a manifest-less note."""
        import nexus.catalog.note_write as nw
        from nexus.catalog.factory import make_catalog_reader
        from unittest.mock import patch

        content = "z0o2p12 case E survivor"
        refusal = NoteWriteError(catalog_doc_id="x", collection=_COLLECTION, reason="refused",
                                 manifest_empty=True)
        with patch("nexus.catalog.note_write.write_note", side_effect=refusal), \
             patch("nexus.catalog.store_hook.rollback_minted_catalog_entry", return_value=False) as rollback:
            out = put_note(content=content, collection=_COLLECTION, title="z0o2p12-case-e")
        assert out.status == nw.NOT_LANDED and out.minted is True
        assert rollback.call_count == 1
        entry = make_catalog_reader().resolve(out.catalog_doc_id)
        assert entry is not None, "control: the row survived its failed removal"
        assert (entry.meta or {}).get("doc_id", "") == "", (
            f"the row still names chunk {(entry.meta or {}).get('doc_id')!r}, which was never written")
        assert out.doc_id == _chash(content), "control: the stamp catalog_store_hook_tracked wrote was that chash"

    def test_an_unexpected_failure_after_registration_restores_a_reconciled_rows_stamp(self, vec):
        """The generic-exception branch re-raises, but a row the call reconciled onto (not minted) gets
        the identity stamp it changed put back, exactly as the refused-write branch does."""
        from nexus.catalog.factory import make_catalog_reader
        from unittest.mock import patch

        old = "z0o2p12 reconciled row original"
        new = "z0o2p12 reconciled row replacement"
        title = "z0o2p12-reconciled-odd"
        first = put_note(content=old, collection=_COLLECTION, title=title)
        assert first.status == "stored"
        with patch("nexus.catalog.note_write.write_note", side_effect=RuntimeError("odd")), \
             patch("nexus.doc_indexer._fence_fail"):
            with pytest.raises(RuntimeError, match="odd"):
                put_note(content=new, collection=_COLLECTION, title=title)
        entry = make_catalog_reader().resolve(first.catalog_doc_id)
        assert (entry.meta or {}).get("doc_id") == _chash(old), "the stamp must be the pre-call one"

    def test_a_skipped_engine_sweep_is_warned_about_with_the_counts(self, vec):
        """The engine sweeps what a supersede dropped after the commit and reports ``sweep_skipped`` when
        that sweep errored (the old chunk then stays, hidden by live(c)). Nothing else tells the operator."""
        from structlog.testing import capture_logs

        cat = _SweepCat(swept=0, sweep_skipped=1, dropped=["a" * 64, "b" * 64])
        with capture_logs() as logs:
            out = put_note(content="z0o2p12 sweep skipped", collection=_COLLECTION,
                           title="z0o2p12-sweep-skipped", cat=cat)
        assert out.status == "stored"
        events = [e for e in logs if e["event"] == "note_sweep_skipped"]
        assert len(events) == 1, logs
        ev = events[0]
        assert ev["log_level"] == "warning"
        assert (ev["doc_id"], ev["collection"]) == (out.doc_id, _COLLECTION)
        assert (ev["swept"], ev["sweep_skipped"], ev["dropped"]) == (0, 1, 2)

    def test_a_shared_piece_the_sweep_legitimately_keeps_does_not_warn(self, vec):
        """swept < dropped with no error is a chunk another document owns: kept by design."""
        from structlog.testing import capture_logs

        cat = _SweepCat(swept=1, sweep_skipped=0, dropped=["a" * 64, "b" * 64])
        with capture_logs() as logs:
            out = put_note(content="z0o2p12 sweep kept", collection=_COLLECTION,
                           title="z0o2p12-sweep-kept", cat=cat)
        assert out.status == "stored"
        assert [e for e in logs if e["event"] == "note_sweep_skipped"] == []

    def test_a_bad_ttl_and_an_oversized_note_fail_before_any_row_is_minted(self, vec):
        from nexus.errors import PutOversizedError
        from unittest.mock import patch

        with patch("nexus.catalog.store_hook.catalog_store_hook_tracked") as register:
            with pytest.raises(ValueError, match="ttl_days=0"):
                put_note(content="z0o2p12 ttl", collection=_COLLECTION, ttl_days=0)
            with pytest.raises(PutOversizedError):
                put_note(content="x" * 20000, collection=_COLLECTION, title="z0o2p12-huge")
        register.assert_not_called()


# ── the shared one-request primitive ─────────────────────────────────────────


class _CannedCat:
    """A catalog writer whose write_manifest_many answers with a canned response."""

    def __init__(self, response: dict) -> None:
        self._response = response
        self.stamps: list[tuple] = []

    def write_manifest_many(self, docs, **kwargs):
        return dict(self._response)

    def begin_index_run(self, doc_id, content_hash, run_id, collection, **kw):
        return {"prior_chashes": [], "prior_count": 0}

    def complete_index_run(self, doc_id, content_hash, count):
        self.stamps.append((doc_id, content_hash, count))
        return {}

    def fail_index_run(self, doc_id, error):
        return {}


class _SweepCat(_CannedCat):
    """A catalog writer whose ``write_manifest_many`` answers for whatever document it is given, with
    the engine's sweep counts and drop list as given."""

    def __init__(self, *, swept: int, sweep_skipped: int, dropped: list[str]) -> None:
        super().__init__({})
        self._swept, self._sweep_skipped, self._dropped = swept, sweep_skipped, dropped

    def write_manifest_many(self, docs, **kwargs):
        doc = docs[0][0]
        return {"failed_doc_ids": [], "chunks_written": 1, "swept": self._swept,
                "sweep_skipped": self._sweep_skipped,
                "dropped_chashes": {doc: list(self._dropped)}, "dropped_count": {doc: len(self._dropped)}}


_DOC = "1.9.9"
_OK_DROPS = {"dropped_chashes": {_DOC: []}, "dropped_count": {_DOC: 0}}


class TestOneRequestPrimitive:
    """write_note and the multi-batch writer's single-request path judge the engine's answer with
    ONE body of code (multi_batch_write.write_one_request), so a bad answer is a bad answer to both."""

    def _writer_single_request(self, cat, content_hash="h"):
        from nexus.catalog.multi_batch_write import MultiBatchDocumentWriter

        rows = [{"chash": _chash("x"), "position": 0}]
        chunks = [{"chash": _chash("x"), "text": "x", "metadata": {}}]
        with MultiBatchDocumentWriter(cat, doc_id=_DOC, collection=_COLLECTION, content_hash=content_hash) as w:
            w.add_batch(rows, chunks)
            return w.finish()

    def _note(self, cat, content_hash="h"):
        return write_note(catalog_doc_id=_DOC, collection=_COLLECTION, pieces=["x"],
                          content_hash=content_hash, cat=cat)

    def test_a_failed_document_is_rejected_by_both(self):
        from nexus.errors import BatchWriteFailedError

        cat = _CannedCat({"failed_doc_ids": [_DOC], **_OK_DROPS})
        with pytest.raises(BatchWriteFailedError, match="failed_doc_ids"):
            self._writer_single_request(cat)
        with pytest.raises(NoteWriteError, match="failed_doc_ids"):
            self._note(cat)

    def test_chunks_the_engine_dropped_as_unreferenced_are_rejected_by_both(self):
        from nexus.errors import BatchWriteFailedError

        cat = _CannedCat({"failed_doc_ids": [], "chunks_written": 1, "chunks_unreferenced": 2, **_OK_DROPS})
        with pytest.raises(BatchWriteFailedError, match="referenced by no row"):
            self._writer_single_request(cat)
        with pytest.raises(ManifestVerifyUncertainError, match="cannot be trusted"):
            self._note(cat)

    def test_a_refused_stamp_is_rejected_by_both(self):
        from nexus.errors import IndexRunVerifyRefused

        cat = _CannedCat({
            "failed_doc_ids": [], "chunks_written": 1, **_OK_DROPS,
            "complete_refused": [{"doc_id": _DOC, "referenced": 2, "missing": 1, "chunk_count": 2}]})
        with pytest.raises(IndexRunVerifyRefused):
            self._writer_single_request(cat)
        with pytest.raises(ManifestVerifyUncertainError, match="refused to stamp"):
            self._note(cat)

    def test_a_good_answer_is_taken_the_same_way_by_both(self):
        cat = _CannedCat({"failed_doc_ids": [], "chunks_written": 1, "swept": 2, "sweep_skipped": 1,
                          "dropped_chashes": {_DOC: ["a" * 64, "b" * 64]}, "dropped_count": {_DOC: 2}})
        written = self._writer_single_request(cat)
        noted = self._note(cat)
        assert (written.chunks_written, written.swept, written.sweep_skipped) == (1, 2, 1)
        assert (noted.chunks_written, noted.swept, noted.sweep_skipped) == (1, 2, 1)
        assert written.completed and noted.completed
        assert noted.dropped_chashes == ["a" * 64, "b" * 64]

    def test_the_stamp_is_the_primitives_stamp_and_retries_a_blip(self):
        """complete_document is what the writer's multi-request path uses; write_note's lost-ack
        recovery uses the same one, so it retries connectivity errors."""
        from nexus.catalog.multi_batch_write import complete_document

        cat = _CannedCat({})
        flaky = [httpx.ConnectError("blip")]
        real = cat.complete_index_run

        def complete(doc_id, content_hash, count):
            if flaky:
                raise flaky.pop()
            return real(doc_id, content_hash, count)

        cat.complete_index_run = complete
        complete_document(cat, doc_id=_DOC, content_hash="h", manifest_rows=3)
        assert cat.stamps == [(_DOC, "h", 3)]


# ── every attempt counts; only 4xx, unsent and failed_doc_ids are definitive ─


class TestSettlingFromEveryAttempt:
    def test_a_dropped_connection_followed_by_refused_reconnects_is_not_unsent(self, vec, real_cat):
        """The retry wrapper re-raises only the LAST error. The first attempt died in flight, so the
        request may have reached the engine: settled from the manifest (unknown), never 'nothing written'."""
        pieces = _pieces("attempts", 2)
        doc = _register("z0o2p12-attempts", pieces)
        errors = [httpx.RemoteProtocolError("dropped mid-flight")]

        def flaky():
            raise errors.pop(0) if errors else httpx.ConnectError("refused")

        with pytest.raises(ManifestVerifyUncertainError):
            write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces,
                       cat=_Recording(real_cat, before=flaky))

    def test_only_connections_never_made_are_unsent(self, vec, real_cat):
        pieces = _pieces("unsent", 2)
        doc = _register("z0o2p12-unsent", pieces)

        def refuse():
            raise httpx.ConnectError("refused")

        with pytest.raises(NoteWriteError):
            write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces,
                       cat=_Recording(real_cat, before=refuse))

    def test_a_500_that_did_not_commit_is_unknown_not_failed(self, vec, real_cat):
        pieces = _pieces("500-none", 2)
        doc = _register("z0o2p12-500-none", pieces)

        def boom():
            raise _status_error(500)

        with pytest.raises(ManifestVerifyUncertainError):
            write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces,
                       cat=_Recording(real_cat, before=boom))

    def test_a_500_after_the_commit_is_recovered_by_resending(self, vec, real_cat):
        """CombinedWriteService records mismatches after the commit, so a 500 can follow it."""
        pieces = _pieces("500-after", 2)
        doc = _register("z0o2p12-500-after", pieces)
        cat = _Recording(real_cat, after=_once(lambda: _status_error(500)))
        res = write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces,
                         content_hash=_hash(pieces), cat=cat)
        assert res.recovered and res.completed
        assert _manifest(doc) == [(i, _chash(p)) for i, p in enumerate(pieces)]

    def test_an_unexpected_exception_is_settled_from_the_manifest(self, vec, real_cat):
        pieces = _pieces("odd", 1)
        doc = _register("z0o2p12-odd", pieces)

        def odd():
            raise ValueError("malformed body")

        with pytest.raises(ManifestVerifyUncertainError):
            write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces,
                       cat=_Recording(real_cat, before=odd))

    def test_a_manifest_that_already_matches_is_resent_so_new_metadata_is_applied(self, vec, real_cat):
        """The request died in flight before it committed; the manifest equals the note only because
        the content is unchanged. Changed tags, ttl and category must still be applied."""
        pieces = _pieces("resend", 1)
        doc = _register("z0o2p12-resend", pieces)
        write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces, tags="old", ttl_days=5,
                   cat=real_cat)
        cat = _Recording(real_cat, before=_once(_embed_timeout))
        res = write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces, tags="new",
                         ttl_days=30, category="c2", cat=cat)
        assert res.recovered is True and cat.calls.count("write_manifest_many") == 2
        got = vec.get_collection(_COLLECTION).get(ids=[_chash(pieces[0])], include=["metadatas"])
        meta = got["metadatas"][0]
        assert (meta["tags"], meta["ttl_days"], meta["category"]) == ("new", 30, "c2")

    def test_a_resend_that_fails_is_unknown(self, vec, real_cat):
        pieces = _pieces("resend-fails", 1)
        doc = _register("z0o2p12-resend-fails", pieces)
        write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces, cat=real_cat)

        def dropped():
            raise httpx.RemoteProtocolError("dropped")

        from nexus.catalog.note_write import LandedUnconfirmedError

        # The manifest shows the note, so the failure is "landed, unconfirmed", the type put_note
        # answers without failing the fence (nexus-z0o2p.35, M2).
        with pytest.raises(LandedUnconfirmedError, match="resending"):
            write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces, tags="x",
                       cat=_Recording(real_cat, before=dropped))

    def test_a_document_failed_after_an_in_flight_attempt_is_unknown_not_failed(self, vec, real_cat):
        """The first attempt died in flight (the wrapper retried it); the retry got a definitive
        failed_doc_ids. The first attempt may have committed, so 'nothing was written' is not known:
        the write is settled from the manifest. Deleting the ``recorder.any_in_flight()`` branch in
        write_note turns this into a NoteWriteError."""
        pieces = _pieces("failed-after-flight", 2)
        doc = _register("z0o2p12-failed-after-flight", pieces)
        cat = _Recording(
            real_cat, mutate=_bad_row, before=_once(lambda: httpx.RemoteProtocolError("dropped")))
        with pytest.raises(ManifestVerifyUncertainError):
            write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces, cat=cat)
        assert cat.calls.count("write_manifest_many") == 2, "the wrapper retried the dropped connection"

    def test_a_refused_stamp_on_the_resend_is_stamp_refused_and_recorded(self, vec, real_cat):
        """The manifest already matches, so the request is resent; the engine refuses the stamp on the
        resend. That is the writer's rule (recorded, fence left indexing), not a generic failed resend.
        Deleting the ``except IndexRunVerifyRefused`` arm of the resend turns this into a plain
        ManifestVerifyUncertainError."""
        from unittest.mock import patch

        from nexus.catalog.note_write import StampRefusedError

        pieces = _pieces("resend-stamp", 1)
        doc = _register("z0o2p12-resend-stamp", pieces)
        write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces, cat=real_cat)

        class TimeoutThenRefusedStamp:
            def __init__(self) -> None:
                self.calls = 0

            def write_manifest_many(self, docs, **kw):
                self.calls += 1
                if self.calls == 1:
                    raise _embed_timeout()
                return {"failed_doc_ids": [], "chunks_written": 0,
                        "dropped_chashes": {doc: []}, "dropped_count": {doc: 0},
                        "complete_refused": [{"doc_id": doc, "referenced": 1, "missing": 1, "chunk_count": 1}]}

        cat = TimeoutThenRefusedStamp()
        with patch("nexus.mcp_infra._record_complete_refusal") as record:
            with pytest.raises(StampRefusedError):
                write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces,
                           content_hash=_hash(pieces), cat=cat)
        assert cat.calls == 2, "the request was resent once"
        record.assert_called_once_with(doc)

    def test_a_408_is_a_timeout_not_a_refusal(self, vec, real_cat):
        """A 408 is 4xx but the server gave up waiting: the request may have been processed. Removing
        the ``status != 408`` exclusion from _classify makes it a NoteWriteError ('nothing written')."""
        pieces = _pieces("408", 2)
        doc = _register("z0o2p12-408", pieces)

        def timed_out():
            raise _status_error(408)

        with pytest.raises(ManifestVerifyUncertainError):
            write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces,
                       cat=_Recording(real_cat, before=timed_out))


# ── the exception CHAIN is judged whole (I1) ─────────────────────────────────


def _chained(first: Exception, second: Exception) -> Exception:
    """*second* the way the httpx mixin raises it: inside the ``except`` handler for *first*, so
    *first* is its ``__context__``. The mixin retries once inside its own handler
    (``_refreshable_client._request``), and the attempt recorder sees only the final exception."""
    try:
        try:
            raise first
        except Exception:
            raise second
    except Exception as caught:
        return caught


def _connect() -> Exception:
    return httpx.ConnectError("connection refused")


#: (outer attempt-1 error, attempt-2 error): the exception attempt 2 raises carries attempt 1's as context.
_IN_FLIGHT_CHAINS = {
    "connect-then-protocol-error": (_connect, lambda: httpx.RemoteProtocolError("dropped mid-response")),
    "protocol-error-then-connect": (lambda: httpx.RemoteProtocolError("dropped mid-response"), _connect),
    "connect-then-read-error": (_connect, lambda: httpx.ReadError("reset while reading")),
    "write-error-then-connect": (lambda: httpx.WriteError("reset while writing"), _connect),
    "connect-then-500": (_connect, lambda: _status_error(500)),
    "500-then-connect": (lambda: _status_error(500), _connect),
    "connect-then-408": (_connect, lambda: _status_error(408)),
    "408-then-connect": (lambda: _status_error(408), _connect),
    "connect-then-embed-timeout": (_connect, _embed_timeout),
    "embed-timeout-then-connect": (_embed_timeout, _connect),
}


class TestChainIsJudgedWhole:
    @pytest.mark.parametrize("name", sorted(_IN_FLIGHT_CHAINS))
    def test_any_in_flight_node_of_the_chain_makes_the_attempt_in_flight(self, name):
        from nexus.catalog.note_write import _classify

        first, second = _IN_FLIGHT_CHAINS[name]
        assert _classify(_chained(first(), second())) == "in-flight"

    @pytest.mark.parametrize("first,second,expected", [
        pytest.param(_connect, _connect, "unsent", id="connect-then-connect"),
        pytest.param(_connect, lambda: httpx.ConnectTimeout("slow"), "unsent", id="connect-then-connect-timeout"),
        pytest.param(_connect, lambda: _status_error(409), "refused", id="connect-then-409"),
        pytest.param(lambda: _status_error(409), _connect, "refused", id="409-then-connect"),
    ])
    def test_definitive_chains_stay_definitive(self, first, second, expected):
        from nexus.catalog.note_write import _classify

        assert _classify(_chained(first(), second())) == expected

    @pytest.mark.parametrize("name", sorted(_IN_FLIGHT_CHAINS))
    def test_a_chained_in_flight_error_is_unknown_never_nothing_written(self, vec, real_cat, name):
        """The consequence: a request that reached the engine must not read as 'nothing was written'
        (NoteWriteError -> fence failed, minted row removed)."""
        first, second = _IN_FLIGHT_CHAINS[name]
        pieces = _pieces(f"chain-{name}", 2)
        doc = _register(f"z0o2p12-chain-{name}", pieces)

        def chained():
            raise _chained(first(), second())

        with pytest.raises(ManifestVerifyUncertainError):
            write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces,
                       cat=_Recording(real_cat, before=chained))


# ── a refused stamp follows the writer's rule ────────────────────────────────


class TestRefusedStamp:
    def _refusing_cat(self):
        return _CannedCat({
            "failed_doc_ids": [], "chunks_written": 1, **_OK_DROPS,
            "complete_refused": [{"doc_id": _DOC, "referenced": 2, "missing": 1, "chunk_count": 2}]})

    def test_write_note_records_the_refusal_and_raises_stamp_refused(self):
        from unittest.mock import patch

        from nexus.catalog.note_write import StampRefusedError

        with patch("nexus.mcp_infra._record_complete_refusal") as record:
            with pytest.raises(StampRefusedError):
                write_note(catalog_doc_id=_DOC, collection=_COLLECTION, pieces=["x"],
                           content_hash="h", cat=self._refusing_cat())
        record.assert_called_once_with(_DOC)

    def test_put_note_leaves_the_fence_indexing_and_reports_unknown(self, vec):
        """No _fence_fail (so no failed-document heal), nothing rolled back, status unknown."""
        import nexus.catalog.note_write as nw
        from unittest.mock import patch

        with patch("nexus.catalog.note_write.write_note", side_effect=nw.StampRefusedError("refused")), \
             patch("nexus.doc_indexer._fence_fail") as fail, \
             patch("nexus.catalog.store_hook.rollback_minted_catalog_entry") as rollback:
            out = put_note(content="z0o2p12 refused stamp", collection=_COLLECTION,
                           title="z0o2p12-refused-stamp")
        assert out.status == nw.UNCERTAIN
        fail.assert_not_called()
        rollback.assert_not_called()
        assert _index_state(out.catalog_doc_id) == "indexing"


# ── one prefix -> content_type map ───────────────────────────────────────────


class TestContentTypeMap:
    """The map lives in ``metadata_schema``. The real-engine test
    ``TestChunkMetadata.test_content_type_follows_the_collection_prefix_as_put_did`` is the pin that a
    note written through ``write_note`` reads back with the prefix's content type; the tests here
    catch the two ways that could silently stop being true."""

    @pytest.mark.parametrize("collection,expected", [
        ("code__x__bge-base-en-v15-768__v1", "code"),
        ("docs__x__bge-base-en-v15-768__v1", "prose"),
        ("rdr__x__bge-base-en-v15-768__v1", "markdown"),
        ("knowledge__x__bge-base-en-v15-768__v1", "prose"),
        ("taxonomy__x__bge-base-en-v15-768__v1", "prose"),
    ])
    def test_the_helper_maps_each_prefix(self, collection, expected):
        from nexus.metadata_schema import chunk_content_type_for_collection

        assert chunk_content_type_for_collection(collection) == expected

    @staticmethod
    def _sent_chunks(monkeypatch, **kw):
        import nexus.metadata_schema as ms

        monkeypatch.setattr(ms, "chunk_content_type_for_collection", lambda collection: "pdf")
        sent: list[list[dict]] = []

        class Capturing:
            def write_manifest_many(self, docs, **kwargs):
                sent.append(kwargs["chunks"])
                return {"failed_doc_ids": [], "chunks_written": 1, **_OK_DROPS}

        write_note(catalog_doc_id=_DOC, pieces=["content type"], cat=Capturing(), **kw)
        return [c["metadata"]["content_type"] for c in sent[0]]

    def test_write_note_takes_the_content_type_from_the_helper(self, monkeypatch):
        """Drives write_note itself with a helper that answers 'pdf', a valid type no prefix in the
        map yields: a write_note that stopped calling the helper (and used a table of its own)
        would send the prefix's real type instead."""
        got = self._sent_chunks(monkeypatch, collection="rdr__x__bge-base-en-v15-768__v1")
        assert got == ["pdf"]

    def test_an_explicit_content_type_wins_over_the_helper(self, monkeypatch):
        got = self._sent_chunks(monkeypatch, collection=_COLLECTION, content_type="code")
        assert got == ["code"]

    @pytest.mark.lint
    def test_there_is_one_copy_of_the_map(self):
        """put, T3Database.put and the note writer share metadata_schema's map; a second copy would
        drift. AST-based, so another spelling (a dict, a list, a prefix test that returns the type) is
        found too. The detector is proven against each spelling first, so a clean scan means it looked."""
        import ast
        import pathlib

        def is_rdr(n):
            return isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value in {"rdr", "rdr__"}

        def is_md(n):
            return isinstance(n, ast.Constant) and n.value == "markdown"

        def second_copies(source: str) -> list[int]:
            """Lines of a literal or branch that pairs an ``rdr`` prefix with 'markdown'."""
            hits = []
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, ast.Dict):
                    if any(is_rdr(k) and is_md(v) for k, v in zip(node.keys, node.values)):
                        hits.append(node.lineno)
                elif isinstance(node, (ast.Tuple, ast.List)):
                    if any(is_rdr(e) for e in node.elts) and any(is_md(e) for e in node.elts):
                        hits.append(node.lineno)
                elif isinstance(node, ast.If):
                    tests_rdr = any(is_rdr(n) for n in ast.walk(node.test))
                    returns_md = any(is_md(n) for b in node.body for n in ast.walk(b))
                    if tests_rdr and returns_md:
                        hits.append(node.lineno)
            return hits

        spellings = {
            "tuple": 'M = (("rdr__", "markdown"),)',
            "dict": 'M = {"rdr__": "markdown"}',
            "bare-dict": 'M = {"rdr": "markdown"}',
            "list": 'M = [["rdr", "markdown"]]',
            "branch": 'def f(c):\n    if c.startswith("rdr__"):\n        return "markdown"\n',
        }
        for name, source in sorted(spellings.items()):
            assert second_copies(source), f"the detector is blind to the {name} spelling"
        assert not second_copies('M = {"docs__": "prose"}\nx = "markdown"\n'), "control: no false positive"

        src = pathlib.Path(__file__).parent.parent / "src" / "nexus"
        holders = sorted(
            str(p.relative_to(src)) for p in src.rglob("*.py") if second_copies(p.read_text()))
        assert holders == ["metadata_schema.py"], holders
