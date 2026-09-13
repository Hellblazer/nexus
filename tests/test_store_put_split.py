# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-spujb: store_put splits a note longer than its model's token window.

A bge collection embeds only the first 512 tokens of a chunk, so a long note
stored as one chunk could not be found past its head. store_put now stores
such a note as several chunks under one catalog document, and store_get puts
it back together by id or by title. The 16,384-byte accept limit is
unchanged, and a collection whose window the chunk cap keeps out of reach
(Voyage) still stores one chunk.

The window here is a synthetic 40-token WordLevel tokenizer patched into
``store_hook.window_for_model``, so the tests need no provisioned model.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest
from tokenizers import Tokenizer, models, pre_tokenizers

from nexus.catalog import recovery_bundle, store_hook
from nexus.corpus import t3_collection_name
from nexus.db.minilm_direct import MiniLMDirectEmbeddingFunction as DefaultEmbeddingFunction
from nexus.db.t3 import T3Database
from nexus.embed_window import TokenWindow
from nexus.mcp.core import store_get, store_put
from nexus.mcp_infra import inject_t3
from tests._catalog_fixture_ops import active_reader, documents_by_title, seed_manifest_chunks
from tests.conftest import make_vector_test_client

SUBJECT = "fixture-subject"
NOTE = " ".join(f"Sentence {i:03d} is part of a long note." for i in range(60))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def local_t3() -> T3Database:
    return T3Database(_client=make_vector_test_client(), _ef_override=DefaultEmbeddingFunction())


@pytest.fixture
def inject_local_t3(local_t3: T3Database):
    inject_t3(local_t3)
    yield local_t3
    inject_t3(None)


@pytest.fixture
def catalog_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    catalog_dir = tmp_path / "catalog"
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
    return catalog_dir


@pytest.fixture
def small_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TokenWindow:
    tok = Tokenizer(models.WordLevel(vocab={"[UNK]": 0}, unk_token="[UNK]"))
    tok.pre_tokenizer = pre_tokenizers.Whitespace()
    tok.save(str(tmp_path / "tokenizer.json"))
    window = TokenWindow(40, tmp_path / "tokenizer.json")
    monkeypatch.setattr(store_hook, "window_for_model", lambda model: window)
    monkeypatch.setattr(store_hook, "has_small_window", lambda model: True)
    return window


def _store(t3: T3Database, content: str, title: str) -> str:
    with patch("nexus.mcp.core._get_t3", return_value=t3):
        return store_put(content=content, collection=SUBJECT, title=title)


# ── the pieces ───────────────────────────────────────────────────────────────

def test_a_note_over_the_window_splits_into_pieces_that_fit(small_window) -> None:
    pieces = store_hook.note_pieces(NOTE, t3_collection_name(SUBJECT))
    assert len(pieces) > 1
    assert "".join(pieces) == NOTE
    assert all(small_window.fits(p) for p in pieces)


def test_a_note_inside_the_window_is_one_piece(small_window) -> None:
    assert store_hook.note_pieces("A short note.", t3_collection_name(SUBJECT)) == ["A short note."]


def test_a_collection_with_no_window_never_splits(monkeypatch) -> None:
    monkeypatch.setattr(store_hook, "window_for_model", lambda model: None)
    assert store_hook.note_pieces(NOTE, t3_collection_name(SUBJECT)) == [NOTE]


def test_one_piece_keeps_the_single_chunk_manifest_exactly() -> None:
    assert store_hook.note_manifest_metadata(["x"]) == store_hook.single_chunk_manifest_metadata("x")


# ── store_put and store_get through the real catalog ─────────────────────────

def test_store_put_writes_every_piece_and_a_manifest_in_order(
    inject_local_t3, catalog_env, small_window,
) -> None:
    col_name = t3_collection_name(SUBJECT)
    pieces = store_hook.note_pieces(NOTE, col_name)
    seed_manifest_chunks(col_name, [_sha(p) for p in pieces])

    result = _store(inject_local_t3, NOTE, "long-note")

    assert result.startswith(f"Stored: {_sha(pieces[0])}"), result
    for piece in pieces:
        entry = inject_local_t3.get_by_id(col_name, _sha(piece))
        assert entry is not None and entry["content"] == piece
    docs = documents_by_title("long-note")
    assert len(docs) == 1
    rows = sorted(active_reader().get_manifest(str(docs[0].tumbler)), key=lambda r: r.position)
    assert [r.chash for r in rows] == [_sha(p) for p in pieces]


def test_store_get_returns_the_whole_note_by_id_and_by_title(
    inject_local_t3, catalog_env, small_window,
) -> None:
    col_name = t3_collection_name(SUBJECT)
    pieces = store_hook.note_pieces(NOTE, col_name)
    seed_manifest_chunks(col_name, [_sha(p) for p in pieces])
    _store(inject_local_t3, NOTE, "long-note-get")

    with patch("nexus.mcp.core._get_t3", return_value=inject_local_t3):
        by_id = store_get(_sha(pieces[0]), SUBJECT)
        by_title = store_get("long-note-get", SUBJECT)

    assert NOTE in by_id, by_id[:300]
    assert NOTE in by_title, by_title[:300]
    assert "Multiple documents" not in by_title


# ── failure, hook shape, resolver (review round 1) ───────────────────────────

class _FailingT3:
    """put() fails on the call numbered *fail_at*; records deletes."""

    def __init__(self, fail_at: int, existing: set[str]) -> None:
        self.fail_at = fail_at
        self.existing = existing
        self.calls = 0
        self.deleted: list[str] = []

    def get_by_id(self, collection: str, doc_id: str) -> dict | None:
        return {"id": doc_id} if doc_id in self.existing else None

    def put(self, *, collection: str, content: str, **kwargs) -> str:
        self.calls += 1
        if self.calls == self.fail_at:
            raise RuntimeError("engine said no")
        return _sha(content)

    def batch_delete(self, collection: str, ids: list[str]) -> int:
        self.deleted.extend(ids)
        return len(ids)


def test_a_failed_piece_write_removes_only_the_pieces_this_call_wrote() -> None:
    """Pieces written before the failure would otherwise stay searchable with
    no manifest. A piece that already existed is identical text another note
    also holds, so it must survive."""
    t3 = _FailingT3(fail_at=3, existing={_sha("b")})
    with pytest.raises(RuntimeError, match="engine said no"):
        store_hook.put_note_pieces(t3, "c", ["a", "b", "c", "d"], title="t")
    assert t3.deleted == [_sha("a")]


def test_one_piece_is_a_single_put_with_no_existence_probe() -> None:
    class _T3:
        def get_by_id(self, *a, **k):
            raise AssertionError("a one-piece note must not pay an existence probe")

        def put(self, *, collection: str, content: str, **kwargs) -> str:
            return _sha(content)

    assert store_hook.put_note_pieces(_T3(), "c", ["x"], title="t") == [_sha("x")]


def test_recovery_import_of_a_split_note_extracts_aspects_once_from_the_whole_note(monkeypatch) -> None:
    """MCP store_put's shape: every piece reaches the single and batch
    chains, and the document chain (aspect extraction) sees the note once,
    whole. fire_store_chains would fire it once per fragment."""

    events: list[tuple] = []

    class _Hooks:
        def fire_single(self, doc_id, collection, content, **kw):
            events.append(("single", doc_id, content))

        def fire_batch(self, doc_ids, collection, contents, embeddings=None, metadatas=None, **kw):
            events.append(("batch", tuple(doc_ids), tuple(contents), kw.get("catalog_doc_id")))

        def fire_document(self, source_path, collection, content, **kw):
            events.append(("document", source_path, content, kw.get("doc_id")))

        def fire_store_chains(self, *a, **kw):
            raise AssertionError("a split note must not ride fire_store_chains")

    monkeypatch.setattr(
        "nexus.corpus.t3_collection_name",
        lambda name, t3=None, for_write=False, allow_placeholder=False: "knowledge__x",
    )
    monkeypatch.setattr(store_hook, "note_pieces", lambda content, collection: ["ab", "cd"])
    monkeypatch.setattr(
        store_hook, "catalog_store_hook_tracked", lambda title, doc_id, collection_name: ("1.2.3", True),
    )
    monkeypatch.setattr(store_hook, "store_put_manifest_direct", lambda doc_id, metadatas, collection: None)
    monkeypatch.setattr("nexus.doc_indexer._fence_begin", lambda *a, **k: None)
    monkeypatch.setattr("nexus.hook_registry.HookRegistry", lambda: _Hooks())
    monkeypatch.setattr("nexus.hook_registry.install_default_hooks", lambda h: None)

    recovery_bundle._default_import_doc(_FailingT3(fail_at=0, existing=set()), {
        "record": "knowledge_doc", "source_uri": "", "collection": "x",
        "title": "t", "tags": "", "category": "", "content": "abcd",
    })

    assert events == [
        ("single", _sha("ab"), "ab"),
        ("single", _sha("cd"), "cd"),
        ("batch", (_sha("ab"), _sha("cd")), ("ab", "cd"), "1.2.3"),
        ("document", _sha("ab"), "abcd", "1.2.3"),
    ]


def test_the_split_uses_the_calibrated_model_resolver(monkeypatch) -> None:
    """embedding_model_for_collection reads a legacy two-segment local
    collection as Voyage (nexus-mc1l1), which would leave it unsplit."""
    seen: list[str] = []
    monkeypatch.setattr(
        "nexus.corpus.embedding_model_for_collection_calibrated", lambda name: "calibrated-token",
    )
    monkeypatch.setattr(store_hook, "window_for_model", lambda model: seen.append(model))
    store_hook.note_pieces("text", "knowledge__legacy")
    assert seen == ["calibrated-token"]
