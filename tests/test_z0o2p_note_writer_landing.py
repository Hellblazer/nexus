# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-223 Phase 2 landing (nexus-z0o2p.16 / .17 / .18): what the two joint reviews found.

The three note producers (``nx store put``, ``nx memory promote``, the recovery-bundle import) and MCP
``store_put`` share ONE writer, :func:`nexus.catalog.note_write.put_note`. The reviews found that the
code AROUND it had been hand-copied four times and had drifted:

* the post-store chains were fired by four copies of one sequence (``fire_note_chains`` is the one now);
* every outcome was worded four times (``failure_message`` is the one now, so a client-side refusal
  cannot be told to "retry" by one surface and to fix its key by another);
* the three client-side registration refusals were pinned at the CLI for one class only, and the MCP
  writer's own behaviour change had no test at all;
* an outcome status no surface knew fell through to "Stored:" in two of the four.

The ``put_note`` and MCP ``store_put`` tests run against the real engine substrate, because the claim
there is about what happens to the catalog row and the request; the message table and the firing are
pure.
"""
from __future__ import annotations

import ast
import hashlib
import pathlib
from unittest.mock import MagicMock, patch

import httpx
import pytest
from structlog.testing import capture_logs

import nexus.catalog.note_write as nw
from nexus.catalog.note_write import (
    NO_CATALOG,
    NOT_LANDED,
    STORED,
    UNCERTAIN,
    PutNoteOutcome,
    _classify,
    failure_message,
    fire_note_chains,
    put_note,
)
from tests._module_seam import module_time

_COLLECTION = "knowledge__z0o2p-landing__bge-base-en-v15-768__v1"
_SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "nexus"


def _chash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _outcome(status: str, **kw) -> PutNoteOutcome:
    pieces = kw.pop("pieces", ["alpha"])
    return PutNoteOutcome(
        status=status, collection=_COLLECTION, pieces=pieces,
        manifest_metadatas=[{"chunk_text_hash": _chash(p)} for p in pieces],
        chunk_ids=[_chash(p) for p in pieces], catalog_doc_id=kw.pop("catalog_doc_id", "1.2.3"), **kw)


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://engine.invalid/v1/catalog/manifest/write_many")
    return httpx.HTTPStatusError(f"HTTP {code}", request=request, response=httpx.Response(code, request=request))


def _chained(first: Exception, second: Exception) -> Exception:
    """*second* the way the httpx mixin raises it: inside the handler for *first* (its ``__context__``)."""
    try:
        try:
            raise first
        except Exception:
            raise second
    except Exception as caught:
        return caught


def _client_refusals() -> dict[str, Exception]:
    from nexus.collection_errors import SupersededCollectionWriteError
    from nexus.corpus import EmbeddingProfileMismatchError, LocalVoyageCredentialMissingError

    return {
        "profile-mismatch": EmbeddingProfileMismatchError("knowledge", "intent-model-x", "bge-base-en-v15-768"),
        "missing-voyage-key": LocalVoyageCredentialMissingError(
            "local.embed_model names a Voyage model and requires a Voyage API key, but none is configured. "
            "Set one with `nx config set voyage_api_key <key>`, then restart the local service."),
        "retired-collection": SupersededCollectionWriteError("knowledge__old__v1", "knowledge__new__v2"),
    }


@pytest.fixture(autouse=True)
def _no_retry_sleeps(monkeypatch):
    from nexus.rate_brake import reset_brake

    module_time(monkeypatch, "nexus.retry").sleep = lambda seconds: None
    reset_brake()
    yield
    reset_brake()


# ── _classify: the three client-side refusals, every chain shape ─────────────


class TestClassifyClientSideRefusals:
    @pytest.mark.parametrize("name", sorted(_client_refusals()))
    def test_a_client_side_refusal_is_unsent(self, name):
        assert _classify(_client_refusals()[name]) == "unsent"

    @pytest.mark.parametrize("name", sorted(_client_refusals()))
    @pytest.mark.parametrize("in_flight", [
        pytest.param(lambda: httpx.RemoteProtocolError("dropped mid-response"), id="protocol-error"),
        pytest.param(lambda: _status_error(503), id="503"),
        pytest.param(lambda: _status_error(408), id="408"),
    ])
    def test_an_in_flight_node_beats_a_registration_node_in_either_order(self, name, in_flight):
        """A dropped request followed by a refused re-registration (or the reverse) is one request that
        may have reached the engine. The registration node must not turn it into 'unsent'."""
        refusal = _client_refusals()[name]
        assert _classify(_chained(in_flight(), refusal)) == "in-flight"
        assert _classify(_chained(refusal, in_flight())) == "in-flight"

    @pytest.mark.parametrize("name", sorted(_client_refusals()))
    def test_a_4xx_beside_a_registration_node_is_refused_not_unsent(self, name):
        assert _classify(_chained(_status_error(409), _client_refusals()[name])) == "refused"


# ── put_note against the engine: the three refusals are a clean NOT_LANDED ───


@pytest.fixture
def engine(t2_service_env):
    return t2_service_env


class TestPutNoteClientSideRefusals:
    @pytest.mark.parametrize("name", sorted(_client_refusals()))
    def test_put_note_reports_a_client_refusal_as_not_landed_and_removes_the_row_it_minted(
        self, name, engine, monkeypatch,
    ):
        """write_manifest_many registers the collection before it sends, so these refusals arrive
        before any byte left the client: NOT_LANDED (never UNCERTAIN), attributed to the client."""
        refusal = _client_refusals()[name]

        def refuse(*_a, **_k):
            raise refusal

        monkeypatch.setattr("nexus.corpus.ensure_collection_registered", refuse)
        title = f"z0o2p-landing-{name}"

        out = put_note(content=f"z0o2p landing {name} body", collection=_COLLECTION, title=title)

        assert out.status == NOT_LANDED, (out.status, out.reason)
        assert out.refusal == "client"
        assert str(refusal) in out.reason
        assert out.minted is True
        from nexus.catalog.factory import make_catalog_reader

        assert [d for d in make_catalog_reader().all_documents() if d.title == title] == [], (
            "a refused first write must not leave the catalog row it minted")

    def test_an_in_flight_node_beside_a_registration_refusal_is_uncertain_and_keeps_the_row(
        self, engine,
    ):
        refusal = _client_refusals()["profile-mismatch"]
        err = _chained(httpx.RemoteProtocolError("dropped mid-response"), refusal)
        with patch("nexus.catalog.note_write.write_one_request", side_effect=err):
            out = put_note(content="z0o2p landing mixed chain", collection=_COLLECTION,
                           title="z0o2p-landing-mixed")
        assert out.status == UNCERTAIN, (out.status, out.reason)
        assert out.refusal == ""
        from nexus.catalog.factory import make_catalog_reader

        assert [d for d in make_catalog_reader().all_documents() if d.title == "z0o2p-landing-mixed"], (
            "an outcome that may have landed must not remove the row")

    def test_an_engine_4xx_is_attributed_to_the_engine(self, engine):
        with patch("nexus.catalog.note_write.write_one_request", side_effect=_status_error(400)):
            out = put_note(content="z0o2p landing 400 body", collection=_COLLECTION, title="z0o2p-landing-400")
        assert (out.status, out.refusal) == (NOT_LANDED, "engine")

    def test_a_connection_never_made_is_attributed_to_unreachable(self, engine):
        with patch("nexus.catalog.note_write.write_one_request", side_effect=httpx.ConnectError("refused")):
            out = put_note(content="z0o2p landing down body", collection=_COLLECTION, title="z0o2p-landing-down")
        assert (out.status, out.refusal) == (NOT_LANDED, "unreachable")


class TestEmptyContent:
    def test_put_note_refuses_empty_content_before_any_side_effect(self, engine):
        """``write_note`` would raise its ValueError only AFTER a catalog row was minted and the
        fence begun: the writer's own entry point refuses first."""
        with patch("nexus.catalog.store_hook.catalog_store_hook_tracked") as register, \
                patch("nexus.doc_indexer._fence_begin") as begin:
            with pytest.raises(ValueError, match="'content' is required"):
                put_note(content="", collection=_COLLECTION, title="z0o2p-landing-empty")
        register.assert_not_called()
        begin.assert_not_called()


# ── the log lines: no traceback, the cause chain instead, both outcomes alike ─


class TestOutcomeLogLines:
    """``put_note``'s NOT_LANDED and UNCERTAIN warnings are printed to the operator's terminal by the
    CLI. Neither carries a traceback (a definitive refusal is a normal outcome and an unknown one is
    worded by the caller); both carry the exception-class chain, so a log reader keeps what the
    traceback gave."""

    def _events(self, logs, name):
        return [e for e in logs if e.get("event") == name]

    def test_a_not_landed_warning_has_no_traceback_and_names_the_cause_chain(self, engine):
        err = _chained(httpx.ConnectError("refused"), _client_refusals()["profile-mismatch"])
        with patch("nexus.catalog.note_write.write_one_request", side_effect=err), capture_logs() as logs:
            out = put_note(content="z0o2p landing log nl", collection=_COLLECTION, title="z0o2p-landing-log-nl")
        assert out.status == NOT_LANDED
        (event,) = self._events(logs, "store_put_note_write_failed")
        assert "exc_info" not in event
        assert "EmbeddingProfileMismatchError" in event["cause_chain"]
        assert "ConnectError" in event["cause_chain"]

    def test_an_uncertain_warning_has_no_traceback_and_names_the_cause_chain(self, engine):
        err = _chained(httpx.ConnectError("refused"), httpx.RemoteProtocolError("dropped mid-response"))
        with patch("nexus.catalog.note_write.write_one_request", side_effect=err), capture_logs() as logs:
            out = put_note(content="z0o2p landing log unc", collection=_COLLECTION,
                           title="z0o2p-landing-log-unc")
        assert out.status == UNCERTAIN
        (event,) = self._events(logs, "store_put_manifest_verify_uncertain")
        assert "exc_info" not in event
        assert "RemoteProtocolError" in event["cause_chain"]
        assert "ConnectError" in event["cause_chain"]

    def test_an_uncertain_outcome_from_an_unexpected_exception_keeps_its_stack(self, engine):
        """A TypeError from signature drift settles as 'in flight' like any unrecognised exception.
        Its warning must stay diagnosable in production (MCP store_put), so it keeps the traceback
        while the same outcome from a transport error does not."""
        with patch("nexus.catalog.note_write.write_one_request", side_effect=TypeError("unexpected kwarg")), \
                capture_logs() as logs:
            out = put_note(content="z0o2p landing log bug", collection=_COLLECTION, title="z0o2p-landing-log-bug")
        assert out.status == UNCERTAIN
        (event,) = self._events(logs, "store_put_manifest_verify_uncertain")
        assert event["exc_info"] is True
        assert "TypeError" in event["cause_chain"]

    @pytest.mark.parametrize("exc,expect_stack", [
        pytest.param(lambda: RuntimeError("catalog bug"), True, id="unexpected"),
        pytest.param(lambda: httpx.ConnectError("refused"), False, id="transport"),
        pytest.param(lambda: _status_error(503), False, id="engine-5xx"),
    ])
    def test_a_registration_failure_keeps_its_stack_only_when_unexpected(self, exc, expect_stack, engine):
        """The NO_CATALOG path logs from catalog_store_hook_tracked, which swallows the exception."""
        with patch("nexus.catalog.factory.make_catalog_reader", side_effect=exc()), capture_logs() as logs:
            out = put_note(content="z0o2p landing log reg", collection=_COLLECTION, title="z0o2p-landing-log-reg")
        assert out.status == NO_CATALOG
        (event,) = self._events(logs, "catalog_store_hook_failed")
        assert bool(event.get("exc_info")) is expect_stack

    @pytest.mark.parametrize("exc,expect_stack", [
        pytest.param(lambda: TypeError("drift"), True, id="unexpected"),
        pytest.param(lambda: httpx.ConnectError("refused"), False, id="transport"),
    ])
    def test_a_registration_that_raises_out_of_the_hook_keeps_its_stack_only_when_unexpected(
        self, exc, expect_stack, engine,
    ):
        with patch("nexus.catalog.store_hook.catalog_store_hook_tracked", side_effect=exc()), \
                capture_logs() as logs:
            out = put_note(content="z0o2p landing log raise", collection=_COLLECTION, title="z0o2p-landing-log-raise")
        assert out.status == NO_CATALOG
        (event,) = self._events(logs, "catalog_store_hook_failed")
        assert bool(event.get("exc_info")) is expect_stack

    def test_a_stamp_refusal_warning_has_no_traceback_either(self, engine):
        with patch("nexus.catalog.note_write.write_note", side_effect=nw.StampRefusedError("refused", detail="409")), \
                capture_logs() as logs:
            out = put_note(content="z0o2p landing log stamp", collection=_COLLECTION,
                           title="z0o2p-landing-log-stamp")
        assert out.stamp_refused
        (event,) = self._events(logs, "store_put_stamp_refused")
        assert "exc_info" not in event


# ── NO_CATALOG carries the cause ─────────────────────────────────────────────


class TestNoCatalogCarriesItsCause:
    def test_a_registration_that_raises_names_what_it_raised(self, engine):
        """catalog_store_hook_tracked swallows every exception into ('', False); the cause is handed
        back through its out-parameter, never reduced to 'catalog registration failed'."""
        with patch("nexus.catalog.factory.make_catalog_reader", side_effect=RuntimeError("catalog service is down")):
            out = put_note(content="z0o2p landing no catalog", collection=_COLLECTION, title="z0o2p-landing-nocat")
        assert out.status == NO_CATALOG
        assert "catalog service is down" in out.reason
        assert "RuntimeError" in out.reason

    def test_a_registration_with_no_catalog_at_all_says_so(self, engine):
        with patch("nexus.catalog.factory.make_catalog_reader", return_value=None):
            out = put_note(content="z0o2p landing no reader", collection=_COLLECTION, title="z0o2p-landing-noreader")
        assert out.status == NO_CATALOG
        assert "no catalog" in out.reason.lower()

    def test_the_bare_phrase_is_gone_when_a_cause_exists(self, engine):
        with patch("nexus.catalog.factory.make_catalog_reader", side_effect=RuntimeError("boom")):
            out = put_note(content="z0o2p landing boom", collection=_COLLECTION, title="z0o2p-landing-boom")
        assert out.reason != "catalog registration failed"


# ── the one message table ────────────────────────────────────────────────────


_ENGINE_REFUSED = dict(reason="the engine reported the document in failed_doc_ids", refusal="engine")

#: (id, outcome, required substrings, forbidden substrings)
_TABLE = [
    pytest.param(
        _outcome(NO_CATALOG, catalog_doc_id="", reason="catalog registration failed: RuntimeError: down"),
        ["could not catalog notes.md in " + _COLLECTION, "RuntimeError: down", "Nothing was written",
         "never without one"],
        ["Stored", "retry is safe", "catalog manifest landed"],
        id="no-catalog"),
    pytest.param(
        _outcome(NOT_LANDED, **_ENGINE_REFUSED),
        ["could not store notes.md in " + _COLLECTION, "failed_doc_ids", "The note was not stored",
         "no chunk was left behind", "any earlier version of the note is unchanged",
         "chunks whose text was already stored may have had their metadata refreshed", "retry is safe"],
        ["could not catalog", "not written and its chunks and manifest are unchanged", "index state"],
        id="not-landed-engine"),
    pytest.param(
        _outcome(NOT_LANDED, reason="connection refused", refusal="unreachable"),
        ["could not store notes.md in " + _COLLECTION, "connection refused", "could not be reached",
         "the note was not written and its chunks and manifest are unchanged", "retry once it is running",
         "its index state may read 'failed' until a write succeeds"],
        ["metadata refreshed", "no chunk was left behind", "could not catalog"],
        id="not-landed-unreachable"),
    pytest.param(
        _outcome(NOT_LANDED, reason="collection 'knowledge__old__v1' was superseded by 'knowledge__new__v2', so "
                                    "a write to it is refused. Write to 'knowledge__new__v2' instead.",
                 refusal="client"),
        ["Write to 'knowledge__new__v2' instead.",
         "The note was not written and its chunks and manifest are unchanged",
         "run the command again once that is fixed", "its index state may read 'failed' until a write succeeds"],
        ["retry is safe", "no chunk was left behind", "could not catalog", "metadata refreshed",
         "could not store"],
        id="not-landed-client-refusal"),
    pytest.param(
        _outcome(UNCERTAIN, reason="the write request failed in flight (timed out)"),
        ["could not confirm that notes.md landed in " + _COLLECTION, "timed out", "Nothing was rolled back",
         "may already have succeeded", "nx store list", "idempotent re-write"],
        ["Stored", "catalog manifest landed"],
        id="uncertain-in-flight"),
    pytest.param(
        _outcome(UNCERTAIN, reason="refused", stamp_refused=True, stamp_detail="409 stale hash"),
        ["accepted the write", "refused to stamp the document complete", "409 stale hash",
         "stays 'indexing'", "Nothing was rolled back"],
        ["Stored", "may already have succeeded", "could not confirm"],
        id="uncertain-stamp-refused"),
    pytest.param(
        _outcome(UNCERTAIN, reason="note 1.2.3 in c landed but was not stamped complete", unstamped=True),
        ["wrote " + _chash("alpha"), "Nothing was rolled back", "idempotent re-write"],
        ["Stored", "could not confirm", "catalog manifest landed", "may already have succeeded"],
        id="uncertain-landed-but-unstamped"),
]


class TestFailureMessageTable:
    @pytest.mark.parametrize("outcome,needles,absent", _TABLE)
    def test_each_outcome_reads_as_its_row(self, outcome, needles, absent):
        msg = failure_message(outcome, subject="notes.md", check="nx store list")
        assert msg is not None
        for needle in needles:
            assert needle in msg, (needle, msg)
        for bad in absent:
            assert bad not in msg, (bad, msg)

    def test_a_stored_outcome_has_no_failure_message(self):
        assert failure_message(_outcome(STORED), subject="notes.md") is None

    @pytest.mark.parametrize("status", ["", "bogus", "stored ", "STORED"])
    def test_an_unknown_status_is_a_failure_never_a_store(self, status):
        msg = failure_message(_outcome(status), subject="notes.md")
        assert msg is not None and "unrecognised" in msg and repr(status) in msg

    def test_the_client_refusal_leads_with_its_reason(self):
        reason = "local.embed_model names a Voyage model and requires a Voyage API key. Set one with `nx config set`."
        msg = failure_message(_outcome(NOT_LANDED, reason=reason, refusal="client"), subject="notes.md")
        assert msg.startswith(reason)

    def test_a_reason_that_ends_without_a_full_stop_still_reads_as_a_sentence(self):
        msg = failure_message(_outcome(NOT_LANDED, reason="no route", refusal="unreachable"), subject="s")
        assert "no route." in msg

    def test_the_metadata_qualifier_appears_only_where_it_can_be_true(self):
        """A metadata refresh needs a request the engine received: an engine refusal only."""
        qualifier = "metadata refreshed"
        for refusal, expected in (("engine", True), ("client", False), ("unreachable", False)):
            msg = failure_message(_outcome(NOT_LANDED, reason="r", refusal=refusal), subject="s")
            assert (qualifier in msg) is expected, (refusal, msg)

    def test_an_uncertain_message_without_a_check_hint_still_names_something_to_do(self):
        msg = failure_message(_outcome(UNCERTAIN, reason="r"), subject="s")
        assert "look for the note in the store before retrying" in msg
        assert "check before" not in msg

    def test_the_unstamped_message_says_it_once(self):
        msg = failure_message(
            _outcome(UNCERTAIN, reason="note 1.2.3 in c landed but was not stamped complete", unstamped=True),
            subject="s")
        assert msg.count("stamped complete") == 1, msg

    def test_the_client_and_unreachable_rows_claim_only_what_is_true(self):
        """put_note has already registered the catalog document and begun, then failed, the index-run
        fence before it reports the refusal, and a re-put of a complete note leaves it reading
        'failed'. Neither row may say that nothing was sent or that nothing changed."""
        for refusal in ("client", "unreachable"):
            msg = failure_message(_outcome(NOT_LANDED, reason="r", refusal=refusal), subject="s")
            assert "chunks and manifest are unchanged" in msg, msg
            assert "index state may read 'failed'" in msg, msg
            for false_claim in ("nothing was sent", "Nothing was sent", "nothing changed"):
                assert false_claim not in msg, (refusal, msg)

    def test_the_check_hint_is_the_surfaces_own(self):
        msg = failure_message(_outcome(UNCERTAIN, reason="r"), subject="s", check="store_get")
        assert "check with store_get before retrying" in msg


# ── one firing of the post-store chains ──────────────────────────────────────


class _Registry:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def fire_single(self, *a, **k):
        self.calls.append(("single", a, k))

    def fire_batch(self, *a, **k):
        self.calls.append(("batch", a, k))

    def fire_document(self, *a, **k):
        self.calls.append(("document", a, k))


class TestFireNoteChains:
    def test_the_three_chains_fire_in_store_put_shape(self):
        from nexus.mcp_infra import manifest_write_batch_hook

        outcome = _outcome(STORED, pieces=["alpha", "beta"])
        hooks = _Registry()

        fire_note_chains(outcome, "alphabeta", hooks=hooks)

        kinds = [c[0] for c in hooks.calls]
        assert kinds == ["single", "single", "batch", "document"]
        assert [c[1][0] for c in hooks.calls[:2]] == [_chash("alpha"), _chash("beta")]
        assert [c[1][2] for c in hooks.calls[:2]] == ["alpha", "beta"]
        _kind, batch_args, batch_kw = hooks.calls[2]
        assert list(batch_args[0]) == [_chash("alpha"), _chash("beta")] and batch_args[1] == _COLLECTION
        assert list(batch_args[2]) == ["alpha", "beta"]
        assert batch_kw["catalog_doc_id"] == "1.2.3" and batch_kw["skip_hooks"] == {manifest_write_batch_hook}
        assert "manifest_complete" not in batch_kw
        _kind, doc_args, doc_kw = hooks.calls[3]
        assert doc_args == (_chash("alpha"), _COLLECTION, "alphabeta") and doc_kw == {"doc_id": "1.2.3"}

    @pytest.mark.parametrize("status", [NO_CATALOG, NOT_LANDED, UNCERTAIN, "bogus", ""])
    def test_a_note_that_is_not_stored_fires_nothing(self, status):
        hooks = _Registry()
        with pytest.raises(ValueError, match="only a stored note"):
            fire_note_chains(_outcome(status), "alpha", hooks=hooks)
        assert hooks.calls == []

    def test_without_a_registry_it_builds_the_default_one(self):
        with patch("nexus.hook_registry.HookRegistry.fire_single") as single, \
                patch("nexus.hook_registry.HookRegistry.fire_batch") as batch, \
                patch("nexus.hook_registry.HookRegistry.fire_document") as document:
            fire_note_chains(_outcome(STORED), "alpha")
        assert single.call_count == batch.call_count == document.call_count == 1


# ── every note producer goes through both, in order ──────────────────────────

#: (file, function) of every code path that writes a note through put_note.
_NOTE_PRODUCERS = (
    ("mcp/core.py", "store_put"),
    ("commands/store.py", "put_cmd"),
    ("commands/memory.py", "promote_cmd"),
    ("catalog/recovery_bundle.py", "_default_import_doc"),
)


def _calls(fn: ast.AST) -> list[tuple[int, str]]:
    return sorted(
        (c.lineno, c.func.id if isinstance(c.func, ast.Name) else c.func.attr)
        for c in ast.walk(fn)
        if isinstance(c, ast.Call) and isinstance(c.func, (ast.Name, ast.Attribute)))


def _function(rel: str, name: str) -> ast.AST:
    tree = ast.parse((_SRC / rel).read_text())
    return next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)


class TestEveryProducerUsesTheSharedHelpers:
    @pytest.mark.parametrize("rel,name", _NOTE_PRODUCERS)
    def test_it_writes_then_words_then_fires_through_the_helpers(self, rel, name):
        calls = _calls(_function(rel, name))
        names = [n for _l, n in calls]
        for needed in ("put_note", "failure_message", "fire_note_chains"):
            assert needed in names, f"{rel}::{name} must call {needed}: {names}"
        assert names.index("put_note") < names.index("failure_message") < names.index("fire_note_chains"), (
            f"{rel}::{name} must write, then judge the outcome, then fire: {calls}")
        for hand_copied in ("fire_single", "fire_batch", "fire_document", "fire_store_chains"):
            assert hand_copied not in names, f"{rel}::{name} fires {hand_copied} itself instead of fire_note_chains"

    def test_the_recovery_import_fires_from_the_same_function_that_writes(self):
        """The import's former helper (_fire_post_store_hooks) is gone: the write and the firing of
        its chains are one function, so a caller cannot fire for a write it did not make."""
        tree = ast.parse((_SRC / "catalog" / "recovery_bundle.py").read_text())
        assert "_fire_post_store_hooks" not in {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}

    def test_no_other_source_function_calls_put_note(self):
        """The four producers are the whole list: a fifth is a new writer and must be added above, with
        the vw594 fence gate and the drift guard told about it."""
        found: set[tuple[str, str]] = set()
        for path in _SRC.rglob("*.py"):
            rel = path.relative_to(_SRC).as_posix()
            if rel == "catalog/note_write.py":
                continue
            tree = ast.parse(path.read_text())
            for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
                if any(n == "put_note" for _l, n in _calls(fn)):
                    found.add((rel, fn.name))
        assert found == set(_NOTE_PRODUCERS), found ^ set(_NOTE_PRODUCERS)

    def test_every_function_that_puts_or_fires_a_note_also_stamps_it_after_the_chains(self):
        """nexus-z0o2p.34: ``put_note`` writes with no completion stamp, so a caller that never calls
        ``stamp_note`` leaves its notes ``indexing`` forever. A census over the whole source, not the
        four known producers: any function that calls ``put_note`` or ``fire_note_chains`` (outside
        note_write.py, which defines them) calls ``stamp_note`` and does so after the chains fired.
        Non-vacuous: it finds the four producers."""
        offenders: list[str] = []
        seen: set[tuple[str, str]] = set()
        for path in _SRC.rglob("*.py"):
            rel = path.relative_to(_SRC).as_posix()
            if rel == "catalog/note_write.py":
                continue
            s, o = _note_stamp_census(ast.parse(path.read_text()), rel)
            seen |= s
            offenders += o
        assert seen >= set(_NOTE_PRODUCERS), seen ^ set(_NOTE_PRODUCERS)
        assert not offenders, offenders

    def test_the_census_is_red_on_a_caller_that_skips_or_reorders_the_stamp(self):
        good = "def f():\n    o = put_note(c)\n    fire_note_chains(o, c)\n    o = stamp_note(o)\n"
        skipped = "def f():\n    o = put_note(c)\n    fire_note_chains(o, c)\n"
        early = "def f():\n    o = put_note(c)\n    o = stamp_note(o)\n    fire_note_chains(o, c)\n"
        assert _note_stamp_census(ast.parse(good), "x.py")[1] == []
        assert _note_stamp_census(ast.parse(skipped), "x.py")[1]
        assert _note_stamp_census(ast.parse(early), "x.py")[1]


def _note_stamp_census(tree: ast.AST, rel: str) -> tuple[set[tuple[str, str]], list[str]]:
    seen: set[tuple[str, str]] = set()
    offenders: list[str] = []
    for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
        names = [n for _l, n in _calls(fn)]
        if "put_note" not in names and "fire_note_chains" not in names:
            continue
        seen.add((rel, fn.name))
        if "stamp_note" not in names:
            offenders.append(f"{rel}::{fn.name} never calls stamp_note")
        elif "fire_note_chains" in names and names.index("stamp_note") < names.index("fire_note_chains"):
            offenders.append(f"{rel}::{fn.name} stamps before it fires the chains")
    return seen, offenders


# ── MCP store_put: the writer's behaviour change, at the tool level ──────────


class TestMcpStorePut:
    """``_classify`` moved three refusals from in flight to unsent, which changes what MCP ``store_put``
    does too (it used to say 'may already have succeeded', keep a minted row and leave a failed fence).
    The pin sits here, at the tool, and not only at the CLI."""

    @pytest.mark.parametrize("name", sorted(_client_refusals()))
    def test_a_client_side_refusal_is_an_error_that_leads_with_its_remedy_and_leaves_no_row(
        self, name, engine, monkeypatch,
    ):
        from nexus.catalog.factory import make_catalog_reader
        from nexus.mcp.core import store_put

        refusal = _client_refusals()[name]

        def refuse(*_a, **_k):
            raise refusal

        monkeypatch.setattr("nexus.corpus.ensure_collection_registered", refuse)
        title = f"z0o2p-landing-mcp-{name}"

        result = store_put(content=f"z0o2p landing mcp {name}", collection="z0o2p-landing", title=title)

        assert result.startswith("Error: store_put: "), result
        assert str(refusal) in result
        assert "The note was not written and its chunks and manifest are unchanged" in result
        for wrong in ("may already have succeeded", "retry is safe", "no chunk was left behind", "could not catalog"):
            assert wrong not in result, (wrong, result)
        assert "Stored" not in result
        assert [d for d in make_catalog_reader().all_documents() if d.title == title] == []

    @pytest.mark.parametrize("outcome,check", [
        pytest.param(_outcome(NO_CATALOG, catalog_doc_id="", reason="catalog registration failed: X"), "", id="no-catalog"),
        pytest.param(_outcome(NOT_LANDED, **_ENGINE_REFUSED), "", id="not-landed"),
        pytest.param(_outcome(UNCERTAIN, reason="timed out"), "store_get", id="uncertain"),
        pytest.param(_outcome(UNCERTAIN, reason="r", stamp_refused=True, stamp_detail="409"), "", id="stamp-refused"),
        pytest.param(_outcome(UNCERTAIN, reason="unstamped", unstamped=True), "", id="unstamped"),
        pytest.param(_outcome("bogus"), "", id="unknown-status"),
    ])
    def test_every_non_stored_outcome_is_the_tables_message_and_fires_nothing(self, outcome, check, engine):
        from nexus.mcp.core import store_put

        with patch("nexus.catalog.note_write.put_note", return_value=outcome), \
                patch("nexus.hook_registry.HookRegistry.fire_single") as single, \
                patch("nexus.hook_registry.HookRegistry.fire_batch") as batch, \
                patch("nexus.hook_registry.HookRegistry.fire_document") as document:
            result = store_put(content="z0o2p landing mcp table", collection="z0o2p-landing", title="t")

        assert result == "Error: store_put: " + failure_message(outcome, subject="content", check="store_get")
        assert "Stored" not in result
        single.assert_not_called(), batch.assert_not_called(), document.assert_not_called()

    @pytest.mark.parametrize("outcome,invalidated", [
        pytest.param(_outcome(UNCERTAIN, reason="timed out"), True, id="uncertain"),
        pytest.param(_outcome(UNCERTAIN, reason="r", stamp_refused=True, stamp_detail="409"), True, id="stamp-refused"),
        pytest.param(_outcome(UNCERTAIN, reason="unstamped", unstamped=True), True, id="unstamped"),
        pytest.param(_outcome(NOT_LANDED, **_ENGINE_REFUSED), False, id="not-landed"),
        pytest.param(_outcome(NO_CATALOG, catalog_doc_id="", reason="catalog registration failed: X"), False,
                     id="no-catalog"),
    ])
    def test_an_uncertain_outcome_drops_the_caches_before_the_error_returns(self, outcome, invalidated, engine):
        """M5: an UNCERTAIN note may have landed (or still land), so a cached page burst or collection
        list built before it is stale for as long as the note exists, error or not. A note that
        definitely did not land changes nothing, so it keeps the caches."""
        from nexus.mcp.core import store_put

        with patch("nexus.catalog.note_write.put_note", return_value=outcome), \
                patch("nexus.mcp.core._page_cache_invalidate") as page, \
                patch("nexus.mcp.core._invalidate_collections_cache") as collections:
            result = store_put(content="z0o2p35 invalidate", collection="z0o2p-landing", title="t")
        assert result.startswith("Error: store_put: ")
        assert page.called is invalidated and collections.called is invalidated

    def test_a_stamp_that_fails_still_records_the_write_and_returns_the_tables_error(self, engine):
        """nexus-z0o2p.34: the note is stored and its chains fired when the stamp fails, so the tier
        write is recorded and the relevance log runs (they are facts about the write), and the caller
        is still told the note could not be confirmed complete."""
        from nexus.mcp.core import store_put

        stored = _outcome(STORED, stamp_pending=True, content_hash="h")

        def failing_stamp(outcome, **_k):
            outcome.status, outcome.unstamped = UNCERTAIN, True
            outcome.reason = "the completion stamp failed: boom"
            return outcome

        with patch("nexus.catalog.note_write.put_note", return_value=stored), \
                patch("nexus.catalog.note_write.fire_note_chains"), \
                patch("nexus.catalog.note_write.stamp_note", side_effect=failing_stamp), \
                patch("nexus.mcp.core._record_tier_write") as record, \
                patch("nexus.mcp.core._get_recent_search_traces", return_value=[]) as traces:
            result = store_put(content="z0o2p34 stamp failed", collection="z0o2p-landing", title="t")
        assert result.startswith("Error: store_put: "), result
        assert "not stamped complete" in result and "stays 'indexing'" in result
        record.assert_called_once()
        traces.assert_called()

    def test_a_stored_outcome_fires_the_chains_through_the_shared_helper(self, engine):
        from nexus.mcp.core import store_put

        with patch("nexus.catalog.note_write.put_note", return_value=_outcome(STORED)), \
                patch("nexus.catalog.note_write.fire_note_chains") as fire:
            result = store_put(content="z0o2p landing mcp stored", collection="z0o2p-landing", title="t")
        assert result.startswith("Stored: " + _chash("alpha")), result
        fire.assert_called_once()
        assert fire.call_args.args[1] == "z0o2p landing mcp stored"


# ── the recovery import: an unknown status is a failure, never an import ─────


class TestRecoveryImportUnknownStatus:
    def test_an_unknown_status_is_counted_failed_and_fires_nothing(self, engine, tmp_path):
        from nexus.catalog.recovery_bundle import ExportSummary, import_bundle, write_bundle
        from nexus.db import make_t3

        rec = {"record": "knowledge_doc", "source_uri": "", "collection": _COLLECTION, "title": "z0o2p-landing-bogus",
               "tags": "", "category": "", "content": "z0o2p landing bogus body"}
        path = tmp_path / "bundle.jsonl"
        write_bundle(path, [rec], [], ExportSummary())
        with patch("nexus.catalog.note_write.put_note", return_value=_outcome("bogus")), \
                patch("nexus.hook_registry.HookRegistry.fire_single") as single, \
                patch("nexus.hook_registry.HookRegistry.fire_batch") as batch, \
                patch("nexus.hook_registry.HookRegistry.fire_document") as document:
            summary = import_bundle(None, None, make_t3(), path)
        assert (summary.docs_imported, summary.docs_failed) == (0, 1)
        assert "unrecognised" in summary.doc_failures[0]["error"]
        single.assert_not_called(), batch.assert_not_called(), document.assert_not_called()
