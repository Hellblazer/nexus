# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-223 P2.6 (nexus-z0o2p.16): ``nx store put`` writes a note through the note writer.

The command used to write a note in pieces: one ``/v1/vectors/store-put`` per piece (chunks with no
owner row), then a separate manifest request, then compensation when a later step failed. It now
calls :func:`nexus.catalog.note_write.put_note`, so the pieces and the owner rows go to the engine
as ONE ``write_manifest_many`` request (RDR-223 Technical Design 3, Phase 2 Step 1).

The journey tests run the real ``nx store put`` command against the real engine substrate and watch
the requests it makes at the two transport seams (T3's ``_post`` and the catalog client's ``_post``).
The mapping tests hand the command each :class:`~nexus.catalog.note_write.PutNoteOutcome` and read
its message and exit code.
"""
from __future__ import annotations

import ast
import hashlib
import pathlib
from unittest.mock import patch

import httpx
import pytest
from click.testing import CliRunner

from nexus.catalog.note_write import (
    NO_CATALOG,
    NOT_LANDED,
    STORED,
    UNCERTAIN,
    PutNoteOutcome,
)
from nexus.catalog.store_hook import note_pieces
from nexus.cli import main
from tests._module_seam import module_time

_SUBJECT = "z0o2p16-note"
_FRESH_SUBJECT = "z0o2p16-fresh"

#: Requests that add a chunk to T3 or an owner row to the catalog: the ones a client can die between.
_CHUNK_OR_OWNER_WRITES = frozenset({
    "/v1/vectors/store-put",
    "/v1/vectors/upsert-chunks",
    "/manifest/write_many",
    "/manifest/write",
    "/manifest/append",
    "/manifest/append_many",
})
#: The two that put a chunk into T3 WITHOUT its owner row: a note must never be written with them.
_OWNERLESS_CHUNK_WRITES = frozenset({"/v1/vectors/store-put", "/v1/vectors/upsert-chunks"})


class ClientDied(BaseException):
    """The simulated death of the client process: not an Exception, so nothing catches it."""


def _chash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _note(label: str, sentences: int = 120) -> str:
    """A note long enough to split into several pieces on the bge window."""
    return " ".join(
        f"z0o2p16 {label} sentence {i:03d} carries its own distinct words so every piece hashes apart."
        for i in range(sentences))


@pytest.fixture
def col(t2_service_env) -> str:
    """The subject's collection, already registered in this tenant (what a second write to a
    subject finds)."""
    from nexus.corpus import ensure_collection_registered, t3_collection_name

    name = t3_collection_name(_SUBJECT, for_write=True)
    ensure_collection_registered(name)
    return name


@pytest.fixture
def fresh_col(t2_service_env) -> str:
    """A subject nothing has written to yet: its collection is not registered in this tenant."""
    from nexus.corpus import t3_collection_name

    return t3_collection_name(_FRESH_SUBJECT, for_write=True)


@pytest.fixture
def vec(t2_service_env):
    import nexus.db.http_vector_client as hvc

    return hvc.HttpVectorClient(tenant=t2_service_env)


def _put(title: str, content: str, *, extra: list[str] | None = None, subject: str = _SUBJECT):
    return CliRunner().invoke(
        main, ["store", "put", "-", "--title", title, "-c", subject, *(extra or [])], input=content)


def _doc(title: str, content: str, col: str) -> str:
    """The catalog document of (col, title): registering again reconciles onto the existing row."""
    from nexus.catalog.store_hook import catalog_store_hook_tracked, note_manifest_metadata

    first, _ = note_manifest_metadata(note_pieces(content, col))
    tumbler, _created = catalog_store_hook_tracked(title=title, doc_id=first, collection_name=col)
    assert tumbler
    return tumbler


def _manifest(doc: str) -> list[tuple[int, str]]:
    from nexus.catalog.factory import make_catalog_reader

    return [(r.position, r.chash) for r in make_catalog_reader().get_manifest(doc)]


def _present(vec, chashes: list[str], col: str) -> set[str]:
    from nexus.db.http_vector_client import VectorServiceError

    try:
        return set(vec.existing_ids(col, chashes))
    except VectorServiceError as exc:
        if "not registered" in str(exc):
            return set()
        raise


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://engine.invalid/v1/catalog/manifest/write_many")
    return httpx.HTTPStatusError(f"HTTP {code}", request=request, response=httpx.Response(code, request=request))


class _Wire:
    """Records every request the command makes at both transport seams, and can kill the client or
    refuse a request. ``paths`` is every path in order; ``writes`` only the chunk-or-owner writes."""

    def __init__(self) -> None:
        self.paths: list[str] = []
        self.die_after_first_write = False
        self.refuse_writes_with: int | None = None

    @property
    def writes(self) -> list[str]:
        return [p for p in self.paths if p in _CHUNK_OR_OWNER_WRITES]

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import nexus.catalog.http_catalog_client as hcc
        import nexus.db.http_vector_client as hvc

        wire = self
        real_vec_post, real_cat_post = hvc._post, hcc.HttpCatalogClient._post

        def through(path: str, call):
            wire.paths.append(path)
            is_write = path in _CHUNK_OR_OWNER_WRITES
            if is_write and wire.refuse_writes_with:
                raise _status_error(wire.refuse_writes_with)
            out = call()
            if is_write and wire.die_after_first_write:
                raise ClientDied()
            return out

        def vec_post(path, *a, **k):
            return through(path, lambda: real_vec_post(path, *a, **k))

        def cat_post(self, path, *a, **k):
            return through(path, lambda: real_cat_post(self, path, *a, **k))

        monkeypatch.setattr(hvc, "_post", vec_post)
        monkeypatch.setattr(hcc.HttpCatalogClient, "_post", cat_post)


@pytest.fixture
def wire(monkeypatch) -> _Wire:
    w = _Wire()
    w.install(monkeypatch)
    return w


@pytest.fixture(autouse=True)
def _no_retry_sleeps(monkeypatch):
    from nexus.rate_brake import reset_brake

    module_time(monkeypatch, "nexus.retry").sleep = lambda seconds: None
    reset_brake()
    yield
    reset_brake()


# ── the request shape ────────────────────────────────────────────────────────


class TestOneRequest:
    def test_a_note_is_written_in_one_chunk_plus_owner_request(self, col, vec, wire):
        """The pieces and the owner rows ride one write_manifest_many; nothing adds a chunk to T3
        without its owner row (no /store-put, no /upsert-chunks, no separate manifest request)."""
        content = _note("one-request")
        pieces = note_pieces(content, col)
        assert len(pieces) > 1, "control: the note must split or this proves nothing about pieces"

        result = _put("z0o2p16-one-request", content)

        assert result.exit_code == 0, result.output
        assert "Stored:" in result.output, result.output
        assert wire.writes == ["/manifest/write_many"], wire.paths
        assert not _OWNERLESS_CHUNK_WRITES & set(wire.paths), wire.paths
        doc = _doc("z0o2p16-one-request", content, col)
        chashes = [_chash(p) for p in pieces]
        assert [c for _, c in _manifest(doc)] == chashes
        assert _present(vec, chashes, col) == set(chashes)
        assert f"({len(pieces)} chunks, split to the embedding model's token window)" in result.output

    def test_the_completion_stamp_rides_the_request(self, col, wire):
        from nexus.catalog.factory import make_catalog_reader

        content = _note("stamp", 4)
        result = _put("z0o2p16-stamp", content)
        assert result.exit_code == 0, result.output
        doc = _doc("z0o2p16-stamp", content, col)
        assert make_catalog_reader().resolve(doc).index_state == "complete"
        assert wire.writes == ["/manifest/write_many"], wire.paths


class TestFirstWriteToANewSubject:
    def test_a_first_put_registers_the_collection_and_lands_the_note_in_one_request(
        self, fresh_col, vec, wire,
    ):
        """The collection registration rides the note's own request (write_manifest_many registers
        before it sends), so a subject nothing has written to yet needs no separate T3 write first."""
        content = _note("fresh", 6)

        result = _put("z0o2p16-fresh", content, subject=_FRESH_SUBJECT)

        assert result.exit_code == 0, result.output
        assert "Stored:" in result.output
        assert wire.writes == ["/manifest/write_many"], wire.paths
        chashes = [_chash(p) for p in note_pieces(content, fresh_col)]
        assert _present(vec, chashes, fresh_col) == set(chashes)


# ── Test Plan 8 and the failure case ─────────────────────────────────────────


class TestClientDeath:
    def test_the_client_dying_after_the_first_request_leaves_no_ownerless_chunk(self, col, vec, wire):
        """Test Plan 8. The process dies as soon as the FIRST chunk-or-owner request has gone out.
        Every chunk of the note that reached T3 has an owner row (and the first request alone
        landed the whole note: the split write would have left piece 0 in T3 with no manifest)."""
        content = _note("dies-after")
        pieces = note_pieces(content, col)
        assert len(pieces) > 1
        wire.die_after_first_write = True

        with pytest.raises(ClientDied):
            _put("z0o2p16-dies-after", content)

        doc = _doc("z0o2p16-dies-after", content, col)
        chashes = [_chash(p) for p in pieces]
        in_t3 = _present(vec, chashes, col)
        owned = {c for _, c in _manifest(doc)}
        assert in_t3, "control: the first request was sent, so the note is in T3"
        assert in_t3 <= owned, f"chunks without an owner: {sorted(in_t3 - owned)}"
        assert in_t3 == set(chashes)
        assert wire.writes == ["/manifest/write_many"], wire.paths

    def test_a_failed_put_leaves_the_old_manifest_intact(self, col, vec, wire):
        """A re-put whose request the engine refuses changes nothing: the old manifest and its chunks
        stay, none of the new note's chunks is added, and the command says the note was not stored."""
        title = "z0o2p16-reput"
        old, new = _note("reput-old"), _note("reput-new")
        first = _put(title, old)
        assert first.exit_code == 0, first.output
        doc = _doc(title, old, col)
        old_manifest = _manifest(doc)
        old_chashes = [_chash(p) for p in note_pieces(old, col)]
        new_chashes = [_chash(p) for p in note_pieces(new, col)]
        assert old_manifest and not set(old_chashes) & set(new_chashes)

        wire.refuse_writes_with = 400
        second = _put(title, new)

        assert second.exit_code != 0, second.output
        assert "Stored:" not in second.output
        assert "was not stored" in second.output, second.output
        assert _manifest(doc) == old_manifest
        assert _present(vec, old_chashes, col) == set(old_chashes)
        assert _present(vec, new_chashes, col) == set()

    def test_a_refused_first_put_leaves_no_chunk_and_no_catalog_row(self, col, vec, wire):
        title, content = "z0o2p16-refused-first", _note("refused-first", 6)
        wire.refuse_writes_with = 400

        result = _put(title, content)

        assert result.exit_code != 0 and "Stored:" not in result.output, result.output
        chashes = [_chash(p) for p in note_pieces(content, col)]
        assert _present(vec, chashes, col) == set()
        from nexus.catalog.store_hook import catalog_store_hook_tracked, note_manifest_metadata

        first, _ = note_manifest_metadata(note_pieces(content, col))
        _tumbler, minted_now = catalog_store_hook_tracked(title=title, doc_id=first, collection_name=col)
        assert minted_now, "the refused put must not leave the catalog row it minted"

    def test_a_client_side_registration_refusal_is_a_clean_failure_not_an_unknown(self, col, vec, wire, monkeypatch):
        """The collection registration runs inside the request's client call, before anything is
        sent. A profile or credential refusal there is 'nothing was written', not 'may already have
        succeeded'. (The old command turned these into a clean exit from db.put; the note writer
        must not read them as in flight.)"""
        from nexus.corpus import EmbeddingProfileMismatchError

        def refuse(*_a, **_k):
            raise EmbeddingProfileMismatchError("knowledge", "intent-model-x", "bge-base-en-v15-768")

        monkeypatch.setattr("nexus.corpus.ensure_collection_registered", refuse)
        content = _note("profile", 6)

        result = _put("z0o2p16-profile", content)

        assert result.exit_code != 0, result.output
        assert "Stored:" not in result.output
        assert "may already have succeeded" not in result.output, result.output
        # A client-side refusal leads with its own remedy and says the note was not written. It is not "retry
        # is safe" (a retry fails the same way until the operator acts) and not "could not catalog".
        assert "The note was not written and its chunks and manifest are unchanged" in result.output, result.output
        for wrong in ("retry is safe", "no chunk was left behind", "could not catalog"):
            assert wrong not in result.output, (wrong, result.output)
        chashes = [_chash(p) for p in note_pieces(content, col)]
        assert _present(vec, chashes, col) == set()


# ── each outcome's message and exit code ─────────────────────────────────────


def _outcome(status: str, **kw) -> PutNoteOutcome:
    pieces = kw.pop("pieces", ["alpha"])
    return PutNoteOutcome(
        status=status, collection="knowledge__z0o2p16__bge-base-en-v15-768__v1", pieces=pieces,
        manifest_metadatas=[{"chunk_text_hash": _chash(p)} for p in pieces],
        chunk_ids=[_chash(p) for p in pieces], catalog_doc_id=kw.pop("catalog_doc_id", "1.2.3"), **kw)


class TestOutcomeMessages:
    @pytest.mark.parametrize("outcome,needles,absent", [
        pytest.param(
            _outcome(STORED), ["Stored: " + _chash("alpha")], [], id="stored"),
        pytest.param(
            _outcome(STORED, pieces=["alpha", "beta"]),
            ["Stored: " + _chash("alpha"), "(2 chunks, split to the embedding model's token window)"], [],
            id="stored-split"),
        pytest.param(
            _outcome(NO_CATALOG, catalog_doc_id="", reason="catalog registration failed: RuntimeError: down"),
            ["could not catalog 'z0o2p16-mapping'", "RuntimeError: down", "Nothing was written"], ["Stored:"],
            id="no-catalog"),
        pytest.param(
            _outcome(NOT_LANDED, reason="engine said no", refusal="engine"),
            ["could not store 'z0o2p16-mapping'", "engine said no", "The note was not stored", "retry is safe",
             "metadata refreshed"], ["Stored:", "could not catalog"],
            id="not-landed-engine"),
        pytest.param(
            _outcome(NOT_LANDED, reason="Set a key with `nx config set voyage_api_key`.", refusal="client"),
            ["Set a key with `nx config set voyage_api_key`.", "The note was not written and its chunks and manifest are unchanged"],
            ["Stored:", "retry is safe", "no chunk was left behind", "could not catalog", "metadata refreshed"],
            id="not-landed-client"),
        pytest.param(
            _outcome(UNCERTAIN, reason="timed out"),
            ["could not confirm that 'z0o2p16-mapping' landed", "timed out", "Nothing was rolled back",
             "nx store list"], ["Stored:", "catalog manifest landed"],
            id="uncertain"),
        pytest.param(
            _outcome(UNCERTAIN, reason="refused", stamp_refused=True, stamp_detail="409 stale hash"),
            ["accepted the write", "refused to stamp the document complete", "409 stale hash",
             "stays 'indexing'", "Nothing was rolled back"], ["Stored:", "may already have succeeded"],
            id="stamp-refused"),
        pytest.param(
            _outcome(UNCERTAIN, reason="note 1.2.3 landed but was not stamped complete", unstamped=True),
            ["was not stamped complete", "Nothing was rolled back"],
            ["Stored:", "could not confirm", "catalog manifest landed"],
            id="landed-but-unstamped"),
        pytest.param(
            _outcome("bogus"),
            ["unrecognised state ('bogus')", "nothing was confirmed stored"], ["Stored:"],
            id="unknown-status"),
    ])
    def test_each_outcome_maps_to_its_message(self, outcome, needles, absent, col):
        with patch("nexus.catalog.note_write.put_note", return_value=outcome), \
             patch("nexus.hook_registry.HookRegistry.fire_single"), \
             patch("nexus.hook_registry.HookRegistry.fire_batch"), \
             patch("nexus.hook_registry.HookRegistry.fire_document"):
            result = _put("z0o2p16-mapping", "alpha")
        for needle in needles:
            assert needle in result.output, (needle, result.output)
        for bad in absent:
            assert bad not in result.output, (bad, result.output)
        assert result.exit_code == (0 if outcome.status == STORED else 1), result.output

    @pytest.mark.parametrize("status", [NO_CATALOG, NOT_LANDED, UNCERTAIN, "bogus"])
    def test_a_failed_outcome_fires_no_post_store_hook(self, status, col):
        outcome = _outcome(status, reason="x")
        with patch("nexus.catalog.note_write.put_note", return_value=outcome), \
             patch("nexus.hook_registry.HookRegistry.fire_single") as single, \
             patch("nexus.hook_registry.HookRegistry.fire_batch") as batch, \
             patch("nexus.hook_registry.HookRegistry.fire_document") as document:
            result = _put("z0o2p16-no-hooks", "alpha")
        assert result.exit_code == 1
        single.assert_not_called(), batch.assert_not_called(), document.assert_not_called()

    def test_a_stored_note_fires_the_chains_without_the_manifest_leg(self, col):
        """The manifest and the stamp were written by the request, so the batch chain skips the
        manifest hook and carries no manifest_complete."""
        from nexus.mcp_infra import manifest_write_batch_hook

        outcome = _outcome(STORED, pieces=["alpha", "beta"])
        with patch("nexus.catalog.note_write.put_note", return_value=outcome), \
             patch("nexus.hook_registry.HookRegistry.fire_single") as single, \
             patch("nexus.hook_registry.HookRegistry.fire_batch") as batch, \
             patch("nexus.hook_registry.HookRegistry.fire_document") as document:
            result = _put("z0o2p16-hooks", "alphabeta")
        assert result.exit_code == 0, result.output
        assert [c.args[0] for c in single.call_args_list] == [_chash("alpha"), _chash("beta")]
        (batch_call,) = batch.call_args_list
        assert batch_call.kwargs["skip_hooks"] == {manifest_write_batch_hook}
        assert "manifest_complete" not in batch_call.kwargs
        assert batch_call.kwargs["catalog_doc_id"] == "1.2.3"
        (doc_call,) = document.call_args_list
        assert doc_call.args[2] == "alphabeta" and doc_call.kwargs["doc_id"] == "1.2.3"

    def test_an_oversized_note_is_a_clean_refusal_before_any_row_is_minted(self, col, wire):
        result = _put("z0o2p16-huge", "x" * 20000)
        assert result.exit_code == 1 and "Stored:" not in result.output, result.output
        assert wire.writes == [], wire.paths

    def test_the_fence_and_catalog_row_belong_to_put_note(self, col):
        """The command hands put_note the whole call; it registers nothing and fences nothing itself."""
        seen: dict = {}

        def fake(**kw):
            seen.update(kw)
            return _outcome(STORED)

        with patch("nexus.catalog.note_write.put_note", side_effect=fake), \
             patch("nexus.catalog.store_hook.catalog_store_hook_tracked") as register, \
             patch("nexus.doc_indexer._fence_begin") as begin, \
             patch("nexus.hook_registry.HookRegistry.fire_single"), \
             patch("nexus.hook_registry.HookRegistry.fire_batch"), \
             patch("nexus.hook_registry.HookRegistry.fire_document"):
            result = _put("z0o2p16-args", "alpha", extra=["--tags", "a,b", "--category", "c", "--ttl", "30d",
                                                          "--agent", "dev", "--session-id", "s1"])
        assert result.exit_code == 0, result.output
        register.assert_not_called()
        begin.assert_not_called()
        assert seen["content"] == "alpha" and seen["title"] == "z0o2p16-args"
        assert seen["tags"] == "a,b" and seen["category"] == "c" and seen["ttl_days"] == 30
        assert seen["source_agent"] == "dev" and seen["session_id"] == "s1"
        assert seen["collection"].startswith("knowledge__z0o2p16-note__")


class TestEmptyContent:
    """MCP store_put answers empty content with "content is required". The CLI refuses it the same
    way, up front: an empty stdin or file must not reach the writer, whose ValueError would surface
    as a raw traceback after a catalog row was minted."""

    def test_empty_stdin_is_a_clean_refusal_with_nothing_written(self, col, wire):
        result = _put("z0o2p16-empty", "")
        assert result.exit_code == 1, result.output
        assert result.exc_info[0] is SystemExit, result.exc_info
        assert "Traceback" not in result.output and "ValueError" not in result.output, result.output
        assert "nothing to store" in result.output and "stdin" in result.output
        assert wire.paths == [], "nothing may reach the engine for an empty note"

    def test_an_empty_file_is_a_clean_refusal_naming_the_file(self, col, wire, tmp_path):
        empty = tmp_path / "empty.md"
        empty.write_text("")
        result = CliRunner().invoke(main, ["store", "put", str(empty), "-c", _SUBJECT])
        assert result.exit_code == 1, result.output
        assert result.exc_info[0] is SystemExit, result.exc_info
        assert "nothing to store" in result.output and "empty.md" in result.output
        assert wire.paths == []


class TestARealEngineRefusalLeavesTheOldManifest:
    """The two injected-400 tests above refuse in the transport wrapper BEFORE the call, so their
    "old manifest intact" assertions could be satisfied by a request that never left the client. This
    refusal is the engine's: the request carries a manifest row naming a chunk that is not in it, so
    the engine's per-document transaction rolls back (``failed_doc_ids``)."""

    @pytest.fixture
    def engine_refuses(self, monkeypatch):
        from nexus.catalog.http_catalog_client import HttpCatalogClient

        real = HttpCatalogClient.write_manifest_many

        def refused(self, docs, *a, **k):
            doc, rows = docs[0]
            return real(self, [(doc, [*rows, {"chash": "f" * 64, "position": len(rows)}])], *a, **k)

        return type("Refuse", (), {
            "arm": staticmethod(lambda: monkeypatch.setattr(HttpCatalogClient, "write_manifest_many", refused)),
            "disarm": staticmethod(lambda: monkeypatch.setattr(HttpCatalogClient, "write_manifest_many", real)),
        })

    def test_a_reput_the_engine_refuses_changes_nothing_and_says_the_engine_refused(self, col, vec, wire, engine_refuses):
        title = "z0o2p16-real-refusal"
        old, new = _note("real-old"), _note("real-new")
        assert _put(title, old).exit_code == 0
        doc = _doc(title, old, col)
        old_manifest = _manifest(doc)
        old_chashes = [_chash(p) for p in note_pieces(old, col)]
        new_chashes = [_chash(p) for p in note_pieces(new, col)]
        assert old_manifest and not set(old_chashes) & set(new_chashes)

        engine_refuses.arm()
        second = _put(title, new)
        engine_refuses.disarm()

        assert second.exit_code != 0 and "Stored:" not in second.output, second.output
        assert "could not store" in second.output and "failed_doc_ids" in second.output, second.output
        assert "The note was not stored" in second.output
        assert "metadata refreshed" in second.output, "the engine received the request, so the qualifier can be true"
        assert "The note was not written and its chunks and manifest are unchanged" not in second.output
        assert _manifest(doc) == old_manifest, "the engine's refusal must leave the old manifest as it was"
        assert _present(vec, old_chashes, col) == set(old_chashes)
        assert _present(vec, new_chashes, col) == set()
        from nexus.catalog.factory import make_catalog_reader

        assert make_catalog_reader().resolve(doc) is not None, "a row this call did not mint stays"
        # and the next put, with the engine well again, replaces the note whole
        third = _put(title, new)
        assert third.exit_code == 0, third.output
        assert [c for _, c in _manifest(doc)] == new_chashes

    def test_a_refused_first_put_through_the_engine_leaves_no_chunk_and_no_row(self, col, vec, wire, engine_refuses):
        title, content = "z0o2p16-real-first", _note("real-first", 6)
        engine_refuses.arm()
        result = _put(title, content)
        engine_refuses.disarm()
        assert result.exit_code != 0 and "Stored:" not in result.output, result.output
        assert _present(vec, [_chash(p) for p in note_pieces(content, col)], col) == set()
        from nexus.catalog.factory import make_catalog_reader

        assert [d for d in make_catalog_reader().all_documents() if d.title == title] == []


# ── the command writes through put_note and registers nothing itself ─────────

_SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "nexus" / "commands" / "store.py"
_SPLIT_WRITE_NAMES = frozenset({
    "_catalog_store_hook_tracked", "catalog_store_hook_tracked", "put", "existing_ids",
})


def test_put_cmd_calls_put_note_and_does_not_register_the_catalog_row_itself() -> None:
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    put_cmd = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "put_cmd")
    names = {n.id for n in ast.walk(put_cmd) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(put_cmd) if isinstance(n, ast.Attribute)}
    assert "put_note" in names, "non-vacuity: put_cmd must write its note through put_note"
    assert not (names | attrs) & _SPLIT_WRITE_NAMES, (names | attrs) & _SPLIT_WRITE_NAMES
