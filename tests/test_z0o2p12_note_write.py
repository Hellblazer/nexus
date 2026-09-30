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

    @pytest.mark.parametrize("make_exc", [
        _embed_timeout,
        lambda: httpx.RemoteProtocolError("connection dropped before the answer"),
        lambda: _status_error(504),
    ], ids=["embed-timeout", "connection-dropped", "gateway-504"])
    def test_a_lost_acknowledgement_is_recovered_from_the_manifest(self, vec, real_cat, make_exc):
        """The request committed and only the answer was lost: the manifest read proves it, and the
        note is reported landed, not rolled back."""
        pieces = _pieces("ack-lost", 3)
        doc = _register("z0o2p12-ack-lost", pieces)

        def lose(_out):
            raise make_exc()

        res = write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces,
                         content_hash=_hash(pieces), cat=_Recording(real_cat, after=lose))
        assert res.recovered is True
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

        def lose(_out):
            raise _embed_timeout()

        res = write_note(catalog_doc_id=doc, collection=_COLLECTION, pieces=pieces,
                         content_hash=_hash(pieces), cat=_Recording(real_cat, after=lose))
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

    @pytest.mark.parametrize("code", [400, 409, 422, 500])
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


# ── the split-write machinery that stays ─────────────────────────────────────


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


# ── put_note: the caller protocol ────────────────────────────────────────────


class TestPutNote:
    def test_put_note_begins_the_fence_before_the_write_and_stamps_it_complete(self, vec):
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
        assert out.status == nw.STORED and out.write.completed
        assert out.chunk_ids == [_chash("z0o2p12 put_note fence order")] and out.doc_id == out.chunk_ids[0]
        assert _index_state(out.catalog_doc_id) == "complete"

    def test_no_document_means_nothing_is_written_and_no_fence_begins(self, vec):
        import nexus.catalog.note_write as nw
        from unittest.mock import patch

        with patch("nexus.catalog.store_hook.catalog_store_hook_tracked", return_value=("", False)), \
             patch("nexus.doc_indexer._fence_begin") as begin, \
             patch("nexus.catalog.note_write.write_note") as write:
            out = put_note(content="z0o2p12 no document", collection=_COLLECTION, title="z0o2p12-nodoc")
        assert out.status == nw.NO_CATALOG and out.reason == "catalog registration failed"
        begin.assert_not_called()
        write.assert_not_called()

    def test_a_landed_note_the_fence_was_not_told_about_is_unknown(self, vec):
        import nexus.catalog.note_write as nw
        from unittest.mock import patch

        unstamped = nw.NoteWriteResult(catalog_doc_id="x", collection=_COLLECTION, completed=False)
        with patch("nexus.catalog.note_write.write_note", return_value=unstamped), \
             patch("nexus.doc_indexer._fence_fail") as fail, \
             patch("nexus.catalog.store_hook.rollback_minted_catalog_entry") as rollback:
            out = put_note(content="z0o2p12 unstamped", collection=_COLLECTION, title="z0o2p12-unstamped")
        assert out.status == nw.UNCERTAIN
        assert fail.call_count == 1
        rollback.assert_not_called()

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
        assert fail.call_count == 1
        assert rollback.call_count == (1 if manifest_empty else 0)

    def test_a_bad_ttl_and_an_oversized_note_fail_before_any_row_is_minted(self, vec):
        from nexus.errors import PutOversizedError
        from unittest.mock import patch

        with patch("nexus.catalog.store_hook.catalog_store_hook_tracked") as register:
            with pytest.raises(ValueError, match="ttl_days=0"):
                put_note(content="z0o2p12 ttl", collection=_COLLECTION, ttl_days=0)
            with pytest.raises(PutOversizedError):
                put_note(content="x" * 20000, collection=_COLLECTION, title="z0o2p12-huge")
        register.assert_not_called()
