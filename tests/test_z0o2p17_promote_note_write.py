# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-223 P2.7 (nexus-z0o2p.17): ``nx memory promote`` writes the promoted note in ONE request.

The promoted entry's chunk and its manifest go to the engine as one ``write_manifest_many`` request
through ``note_write.put_note`` (the writer MCP ``store_put`` uses). Promote therefore makes no
ownerless chunk write (``/store-put`` or ``/upsert-chunks``), and what it does to the T2 entry
follows from the outcome of that one request:

========================  =============================  ====================================
put_note outcome          CLI message / exit code        T2 entry
========================  =============================  ====================================
STORED                    "Promoted: ..." / 0            kept; deleted only with ``--remove``
NOT_LANDED (engine)       error "retry is safe" / 1      untouched, ``--remove`` ignored
UNCERTAIN                 error "could not confirm" / 1  untouched, ``--remove`` ignored
NOT_LANDED (client)       error leads with the remedy / 1  untouched, ``--remove`` ignored
UNCERTAIN, stamp refused  error "refused to stamp" / 1   untouched, ``--remove`` ignored
NO_CATALOG                error "could not catalog" / 1  untouched, ``--remove`` ignored
put_note raises           error / non-zero               untouched, ``--remove`` ignored
========================  =============================  ====================================

Real engine substrate throughout (the autouse ``_pin_t2_substrate``); the only doubles are the
failure-injection points and a local in-memory T3 that resolves the collection name (promote's
collection probe) and nothing else.
"""
from __future__ import annotations

import ast
import hashlib
import pathlib
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.db.minilm_direct import MiniLMDirectEmbeddingFunction as DefaultEmbeddingFunction
from nexus.db.t2 import T2Database
from nexus.db.t3 import T3Database
from tests._catalog_fixture_ops import documents_by_title
from tests.conftest import make_vector_test_client

_COLLECTION = "knowledge__fixture-subject__bge-base-en-v15-768__v1"
_SRC = pathlib.Path(__file__).parent.parent / "src" / "nexus"


def _chash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ClientDied(BaseException):
    """The simulated death of the client process: not an Exception, so nothing catches it."""


@pytest.fixture(autouse=True)
def _no_retry_sleeps(monkeypatch):
    """The manifest-write retry backs off in real seconds and the shared rate brake remembers a
    trip across tests; neither is under test here."""
    from nexus.rate_brake import reset_brake

    monkeypatch.setattr("nexus.retry.time.sleep", lambda seconds: None)
    reset_brake()
    yield
    reset_brake()


@pytest.fixture
def local_t3() -> T3Database:
    db = T3Database(_client=make_vector_test_client(), _ef_override=DefaultEmbeddingFunction())
    from nexus.mcp_infra import inject_t3

    inject_t3(db)
    yield db
    inject_t3(None)


class _T2:
    """The T2 store as the command sees it. ``promote_cmd`` closes the handle it is given, so every
    access here opens a fresh one over the same tenant, which is also what a second CLI run does."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def open(self) -> T2Database:
        return T2Database(self._path)

    def put(self, **kw) -> int:
        with self.open() as db:
            return db.put(**kw)

    def get(self, **kw):
        with self.open() as db:
            return db.get(**kw)


@pytest.fixture
def t2(tmp_path: Path) -> _T2:
    return _T2(tmp_path / "promote-t2.db")


@pytest.fixture
def vec(t2_service_env):
    import nexus.db.http_vector_client as hvc

    return hvc.HttpVectorClient(tenant=t2_service_env)


def _promote(t2: _T2, t3, row_id: int, *extra: str):
    with patch("nexus.commands.memory.t2_handle", side_effect=t2.open), \
         patch("nexus.db.make_t3", return_value=t3):
        return CliRunner().invoke(main, [
            "memory", "promote", str(row_id), "--collection", "fixture-subject", *extra])


def _entry(t2: _T2, title: str):
    return t2.get(project="proj", title=title)


def _echo(result) -> str:
    """What the command printed, without the structured-log lines the CLI runner also captures."""
    return "\n".join(l for l in result.output.splitlines() if not l.startswith("event=")).strip()


def _error(result) -> str:
    """The command's ``Error:`` message (a ClickException), which is what the operator reads."""
    return " ".join(l for l in result.output.splitlines() if l.startswith("Error:"))


def _manifest(title: str) -> list[str]:
    from tests._catalog_fixture_ops import active_reader

    (doc,) = documents_by_title(title)
    return list(active_reader().get_chunk_chashes(str(doc.tumbler)))


def _present(vec, chashes: list[str]) -> set[str]:
    import nexus.db.http_vector_client as hvc

    try:
        return set(vec.existing_ids(_COLLECTION, chashes))
    except hvc.VectorServiceError as exc:
        assert "not registered" in str(exc), exc
        return set()


@pytest.fixture
def refused_write(monkeypatch: pytest.MonkeyPatch):
    """Make the engine refuse the note's one request: add a manifest row naming a chunk that is not
    in the request. The refusal is real (the per-document transaction rolls back), not a stub."""
    from nexus.catalog.http_catalog_client import HttpCatalogClient

    real = HttpCatalogClient.write_manifest_many

    def _refused(self, docs, *a, **k):
        doc, rows = docs[0]
        return real(self, [(doc, [*rows, {"chash": "f" * 64, "position": len(rows)}])], *a, **k)

    return type("Refused", (), {
        "arm": staticmethod(lambda: monkeypatch.setattr(HttpCatalogClient, "write_manifest_many", _refused)),
        "disarm": staticmethod(lambda: monkeypatch.setattr(HttpCatalogClient, "write_manifest_many", real)),
    })


class TestStored:
    def test_a_stored_promote_keeps_the_t2_entry_and_a_remove_deletes_it(self, t2, local_t3, vec):
        content = "z0o2p17 stored promote body"
        row_id = t2.put(project="proj", title="z0o2p17-stored", content=content, ttl=None)
        result = _promote(t2, local_t3, row_id)
        assert result.exit_code == 0, result.output
        assert _echo(result).startswith("Promoted: proj/z0o2p17-stored")
        assert _chash(content) in result.output
        assert _entry(t2, "z0o2p17-stored") is not None, "no --remove: the T2 entry stays"
        assert _manifest("z0o2p17-stored") == [_chash(content)]
        assert _present(vec, [_chash(content)]) == {_chash(content)}
        assert documents_by_title("z0o2p17-stored")[0].index_state == "complete"

        result = _promote(t2, local_t3, row_id, "--remove")
        assert result.exit_code == 0, result.output
        assert "Promoted and removed: proj/z0o2p17-stored" in result.output
        assert _entry(t2, "z0o2p17-stored") is None, "a verified store with --remove deletes the T2 entry"
        assert _manifest("z0o2p17-stored") == [_chash(content)], "and the re-promote left the note whole"

    def test_the_remaining_ttl_reaches_the_writer(self, t2, local_t3, monkeypatch):
        from nexus.catalog import note_write

        seen: list[dict] = []
        real = note_write.put_note

        def spy(**kw):
            seen.append(kw)
            return real(**kw)

        monkeypatch.setattr(note_write, "put_note", spy)
        row_id = t2.put(project="proj", title="z0o2p17-ttl", content="z0o2p17 ttl body", ttl=10, tags="ai")
        result = _promote(t2, local_t3, row_id, "--tags", "promoted")
        assert result.exit_code == 0, result.output
        (kw,) = seen
        assert kw["collection"] == _COLLECTION
        assert (kw["content"], kw["title"], kw["tags"]) == ("z0o2p17 ttl body", "z0o2p17-ttl", "promoted")
        assert kw["ttl_days"] in (9, 10), "the remaining window of a 10-day entry, never a reset or 0"

    def test_the_post_store_chains_fire_once_and_skip_the_manifest_hook(self, t2, local_t3, monkeypatch):
        """The request wrote the manifest and the stamp, so the batch chain must not write them again."""
        from nexus import hook_registry as hr
        from nexus.mcp_infra import manifest_write_batch_hook

        single: list = []
        batch: list = []
        doc: list = []

        class _Recording(hr.HookRegistry):
            def fire_single(self, doc_id, collection, content, *, invoke=None):
                single.append(doc_id)
                super().fire_single(doc_id, collection, content, invoke=invoke)

            def fire_batch(self, doc_ids, collection, contents, embeddings=None, metadatas=None, *,
                           catalog_doc_id="", manifest_complete=None, skip_hooks=None, invoke=None):
                batch.append((list(doc_ids), manifest_complete, skip_hooks))
                super().fire_batch(doc_ids, collection, contents, embeddings, metadatas,
                                   catalog_doc_id=catalog_doc_id, manifest_complete=manifest_complete,
                                   skip_hooks=skip_hooks, invoke=invoke)

            def fire_document(self, source_path, collection, content, *, doc_id="", invoke=None):
                doc.append((source_path, doc_id))
                super().fire_document(source_path, collection, content, doc_id=doc_id, invoke=invoke)

        monkeypatch.setattr(hr, "HookRegistry", _Recording)
        content = "z0o2p17 chains body"
        row_id = t2.put(project="proj", title="z0o2p17-chains", content=content, ttl=None)
        result = _promote(t2, local_t3, row_id)
        assert result.exit_code == 0, result.output
        assert single == [_chash(content)]
        ((ids, manifest_complete, skip),) = batch
        assert ids == [_chash(content)]
        assert manifest_complete is None, "the stamp rode the one request"
        assert skip and manifest_write_batch_hook in skip
        ((source, catalog_doc),) = doc
        assert source == _chash(content) and catalog_doc, "the document chain carries the catalog tumbler"


class TestEveryOutcomeLeavesTheT2EntryToTheOutcome:
    """Each put_note outcome: the CLI message, the exit code and what happens to the T2 entry."""

    def test_not_landed_says_retry_is_safe_and_keeps_the_t2_entry(self, t2, local_t3, vec, refused_write):
        content = "z0o2p17 refused promote"
        row_id = t2.put(project="proj", title="z0o2p17-refused", content=content, ttl=None)
        refused_write.arm()
        with patch("nexus.doc_indexer._fence_fail") as fence_fail:
            result = _promote(t2, local_t3, row_id, "--remove")
        assert result.exit_code == 1, result.output
        assert "Promoted" not in result.output
        assert "The note was not stored" in result.output and "retry is safe" in result.output
        assert "Nothing was written" not in result.output, "a refused request may have refreshed chunk metadata"
        assert "The T2 entry was left in place, even with --remove." in result.output
        assert _entry(t2, "z0o2p17-refused") is not None, "--remove must not delete the source of a failed promote"
        assert documents_by_title("z0o2p17-refused") == [], "the row this call minted is removed"
        assert _present(vec, [_chash(content)]) == set()
        assert fence_fail.call_count == 1

    def test_uncertain_says_could_not_confirm_and_keeps_the_t2_entry(self, t2, local_t3, monkeypatch):
        from nexus.catalog.http_catalog_client import HttpCatalogClient
        from nexus.errors import CombinedWriteEmbedTimeoutError

        def timeout(self, docs, *a, **k):
            raise CombinedWriteEmbedTimeoutError(collection="c", chunk_count=1, original="ReadTimeout")

        monkeypatch.setattr(HttpCatalogClient, "write_manifest_many", timeout)
        row_id = t2.put(project="proj", title="z0o2p17-timeout", content="z0o2p17 timeout body", ttl=None)
        with patch("nexus.doc_indexer._fence_fail") as fence_fail:
            result = _promote(t2, local_t3, row_id, "--remove")
        assert result.exit_code == 1, result.output
        assert "could not confirm" in result.output and "Nothing was rolled back" in result.output
        assert "Promoted" not in result.output
        assert "Nothing was written" not in result.output and "retry is safe" not in result.output
        assert "T2 entry" in result.output and "left in place" in result.output
        assert _entry(t2, "z0o2p17-timeout") is not None, (
            "an unknown outcome must never delete the T2 source, --remove or not")
        assert len(documents_by_title("z0o2p17-timeout")) == 1, "the write may have landed: the row stays"
        assert fence_fail.call_count == 1

    def test_a_refused_stamp_says_so_once_and_keeps_the_t2_entry(self, t2, local_t3):
        from nexus.catalog.note_write import StampRefusedError

        refused = StampRefusedError(
            "note 1.1.1 in c: the write was accepted but the engine refused to stamp it complete: "
            "referenced 2 != chunk_count 1", detail="referenced 2 != chunk_count 1")
        row_id = t2.put(project="proj", title="z0o2p17-stamp", content="z0o2p17 stamp body", ttl=None)
        with patch("nexus.catalog.note_write.write_note", side_effect=refused), \
             patch("nexus.doc_indexer._fence_fail") as fence_fail:
            result = _promote(t2, local_t3, row_id, "--remove")
        assert result.exit_code == 1, result.output
        assert "accepted the write" in result.output and "'indexing'" in result.output
        assert "referenced 2 != chunk_count 1" in result.output
        assert "could not confirm" not in result.output, "the cause is known, so it is said once"
        assert _error(result).count("refused to stamp") == 1, result.output
        assert "T2 entry" in result.output and "left in place" in result.output
        assert _entry(t2, "z0o2p17-stamp") is not None
        assert fence_fail.call_count == 0, "a refused stamp leaves the fence `indexing`: no failed-document heal"

    def test_no_catalog_entry_writes_nothing_and_keeps_the_t2_entry(self, t2, local_t3):
        row_id = t2.put(project="proj", title="z0o2p17-nocat", content="z0o2p17 nocat body", ttl=None)
        with patch("nexus.catalog.store_hook.catalog_store_hook_tracked", return_value=("", False)), \
             patch("nexus.catalog.note_write.write_note") as write:
            result = _promote(t2, local_t3, row_id, "--remove")
        assert result.exit_code == 1, result.output
        assert "could not catalog 'z0o2p17-nocat'" in result.output
        assert "Nothing was written" in result.output
        assert write.call_count == 0, "a note is never written without its catalog entry"
        assert _entry(t2, "z0o2p17-nocat") is not None

    def test_a_catalog_that_raises_names_its_cause_in_the_message(self, t2, local_t3):
        row_id = t2.put(project="proj", title="z0o2p17-nocat-cause", content="z0o2p17 cause body", ttl=None)
        with patch("nexus.catalog.factory.make_catalog_reader", side_effect=RuntimeError("catalog service is down")):
            result = _promote(t2, local_t3, row_id, "--remove")
        assert result.exit_code == 1, result.output
        assert "catalog service is down" in result.output, result.output
        assert "catalog registration failed" in result.output
        assert _entry(t2, "z0o2p17-nocat-cause") is not None

    def test_a_client_side_refusal_leads_with_its_remedy_and_keeps_the_t2_entry(self, t2, local_t3, monkeypatch):
        from nexus.corpus import LocalVoyageCredentialMissingError

        remedy = "no Voyage API key is configured. Set one with `nx config set voyage_api_key <key>`."

        def refuse(*_a, **_k):
            raise LocalVoyageCredentialMissingError(remedy)

        monkeypatch.setattr("nexus.corpus.ensure_collection_registered", refuse)
        row_id = t2.put(project="proj", title="z0o2p17-keyless", content="z0o2p17 keyless body", ttl=None)
        result = _promote(t2, local_t3, row_id, "--remove")
        assert result.exit_code == 1, result.output
        assert _error(result).startswith("Error: " + remedy[:40]), result.output
        assert "Nothing was sent to the engine and nothing changed" in result.output
        assert "The T2 entry was left in place, even with --remove." in result.output
        for wrong in ("retry is safe", "no chunk was left behind", "could not catalog", "may already have succeeded"):
            assert wrong not in result.output, (wrong, result.output)
        assert _entry(t2, "z0o2p17-keyless") is not None
        assert documents_by_title("z0o2p17-keyless") == [], "the row this call minted is removed"

    def test_an_unknown_status_is_an_error_that_fires_nothing_and_keeps_the_t2_entry(self, t2, local_t3):
        from nexus.catalog.note_write import PutNoteOutcome

        row_id = t2.put(project="proj", title="z0o2p17-bogus", content="z0o2p17 bogus body", ttl=None)
        bogus = PutNoteOutcome(
            status="bogus", collection=_COLLECTION, pieces=["x"], manifest_metadatas=[{"chunk_text_hash": _chash("x")}],
            chunk_ids=[_chash("x")], catalog_doc_id="1.2.3")
        with patch("nexus.catalog.note_write.put_note", return_value=bogus), \
             patch("nexus.hook_registry.HookRegistry.fire_single") as single, \
             patch("nexus.hook_registry.HookRegistry.fire_batch") as batch, \
             patch("nexus.hook_registry.HookRegistry.fire_document") as document:
            result = _promote(t2, local_t3, row_id, "--remove")
        assert result.exit_code == 1, result.output
        assert "Promoted" not in result.output and "unrecognised state ('bogus')" in result.output, result.output
        single.assert_not_called(), batch.assert_not_called(), document.assert_not_called()
        assert _entry(t2, "z0o2p17-bogus") is not None

    def test_an_oversized_entry_fails_before_anything_is_minted(self, t2, local_t3):
        from nexus.db.limits import QUOTAS

        big = "z0o2p17 oversized " + "x" * (QUOTAS.MAX_DOCUMENT_BYTES + 10)
        row_id = t2.put(project="proj", title="z0o2p17-big", content=big, ttl=None)
        result = _promote(t2, local_t3, row_id, "--remove")
        assert result.exit_code != 0, result.output
        assert "Promoted" not in result.output
        assert _entry(t2, "z0o2p17-big") is not None
        assert documents_by_title("z0o2p17-big") == []

    def test_an_unexpected_writer_error_fails_loudly_and_keeps_the_t2_entry(self, t2, local_t3):
        row_id = t2.put(project="proj", title="z0o2p17-boom", content="z0o2p17 boom body", ttl=None)
        with patch("nexus.catalog.note_write.write_note", side_effect=RuntimeError("engine 500: boom")):
            result = _promote(t2, local_t3, row_id, "--remove")
        assert result.exit_code != 0
        assert "engine 500: boom" in result.output or "boom" in str(result.exception)
        assert _entry(t2, "z0o2p17-boom") is not None
        assert documents_by_title("z0o2p17-boom") == [], "put_note removed the row this call minted"


class TestAFailedPromoteLeavesTheOldNoteIntact:
    def test_a_failed_repromote_keeps_the_old_manifest_chunks_and_t2_entry(
        self, t2, local_t3, vec, refused_write,
    ):
        title = "z0o2p17-reput"
        old, new = "z0o2p17 reput -- original body", "z0o2p17 reput -- replacement body"
        row_id = t2.put(project="proj", title=title, content=old, ttl=None)
        assert _promote(t2, local_t3, row_id).exit_code == 0
        row_id = t2.put(project="proj", title=title, content=new, ttl=None)
        refused_write.arm()

        for attempt in (1, 2):  # the second is the retry the error text recommends
            result = _promote(t2, local_t3, row_id, "--remove")
            assert result.exit_code == 1, result.output
            assert "retry is safe" in result.output
            assert _manifest(title) == [_chash(old)], f"attempt {attempt}"
            assert _present(vec, [_chash(old), _chash(new)]) == {_chash(old)}
            assert _entry(t2, title)["content"] == new, "the T2 entry is untouched"
            assert len(documents_by_title(title)) == 1

        refused_write.disarm()
        result = _promote(t2, local_t3, row_id, "--remove")
        assert result.exit_code == 0, result.output
        assert _manifest(title) == [_chash(new)]
        assert _present(vec, [_chash(old), _chash(new)]) == {_chash(new)}, "the supersede sweeps the old chunk"
        assert _entry(t2, title) is None, "only the verified store deletes the T2 entry"


class TestClientDeath:
    """Test Plan 8 at the command: the client dies after the note's one request."""

    def test_killed_at_the_transport_after_the_post_leaves_no_ownerless_chunk(
        self, t2, local_t3, vec, monkeypatch,
    ):
        import nexus.catalog.http_catalog_client as hcc

        real = hcc.HttpCatalogClient._post_embedding_write

        def kill_after_post(self, path, *a, **k):
            real(self, path, *a, **k)
            raise ClientDied()

        monkeypatch.setattr(hcc.HttpCatalogClient, "_post_embedding_write", kill_after_post)
        content = "z0o2p17 killed after the request"
        row_id = t2.put(project="proj", title="z0o2p17-killed", content=content, ttl=None)
        with pytest.raises(ClientDied):
            _promote(t2, local_t3, row_id, "--remove")
        assert _present(vec, [_chash(content)]) == {_chash(content)}, "control: the request committed"
        assert _manifest("z0o2p17-killed") == [_chash(content)], "and the chunk has its owner"
        assert _entry(t2, "z0o2p17-killed") is not None, "the dead client never reached the T2 delete"

    def test_killed_before_the_request_writes_nothing(self, t2, local_t3, vec, monkeypatch):
        import nexus.catalog.http_catalog_client as hcc

        def die(self, path, *a, **k):
            raise ClientDied()

        monkeypatch.setattr(hcc.HttpCatalogClient, "_post_embedding_write", die)
        content = "z0o2p17 killed before the request"
        row_id = t2.put(project="proj", title="z0o2p17-killed-early", content=content, ttl=None)
        with pytest.raises(ClientDied):
            _promote(t2, local_t3, row_id)
        assert _present(vec, [_chash(content)]) == set()
        assert _entry(t2, "z0o2p17-killed-early") is not None


class TestPromoteMakesNoOwnerlessChunkWrite:
    def test_no_store_put_or_upsert_chunks_request_is_made(self, t2, local_t3, monkeypatch):
        import nexus.db.http_vector_client as hvc

        paths: list[str] = []
        real_post = hvc._post

        def spy(path, *a, **k):
            paths.append(path)
            return real_post(path, *a, **k)

        monkeypatch.setattr(hvc, "_post", spy)
        row_id = t2.put(project="proj", title="z0o2p17-nostoreput", content="z0o2p17 no store-put", ttl=None)
        result = _promote(t2, local_t3, row_id)
        assert result.exit_code == 0, result.output
        assert not [p for p in paths if "store-put" in p or "upsert-chunks" in p], paths

    def test_promote_cmd_calls_none_of_the_split_write_machinery(self):
        """A source-level pin: the function writes through ``put_note`` and names no split-write helper."""
        tree = ast.parse((_SRC / "commands" / "memory.py").read_text())
        promote = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "promote_cmd")
        called = {
            (c.func.id if isinstance(c.func, ast.Name) else c.func.attr)
            for c in ast.walk(promote) if isinstance(c, ast.Call)
            and isinstance(c.func, (ast.Name, ast.Attribute))
        }
        imported = {a.name for n in ast.walk(promote) if isinstance(n, ast.ImportFrom) for a in n.names}
        assert "put_note" in called, "promote must write its note through note_write.put_note"
        banned = {
            "put", "put_note_pieces", "store_put_manifest_direct_with_recovery",
            "rollback_uncataloged_chunk_write", "describe_rollback_outcome", "fire_store_chains",
        }
        assert not (banned & (called | imported)), sorted(banned & (called | imported))
