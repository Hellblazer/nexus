# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-z8mi2: a note is split to the window of the model its catalog row names.

``store_hook.note_pieces`` chose the token window from the collection NAME. A legacy
name carries no model token, so the derivation answers what the install embeds legacy
names with now: a collection whose row says ``voyage-context-3`` on a bge-profile
engine was split to bge's 512-token window instead of Voyage's none. The window now
comes from the row's ``embedding_model`` (the rule search uses since nexus-noayy and
nexus-ml9vu), with the name as the fallback.

The row comes from the shared collections cache, which fills itself once on a cold cache
(a fresh ``nx store put`` process) and is read from after that. ``mcp_infra.get_t3`` is a
fake that counts listings, so no test reaches the engine and the request count is pinned:
none on a warm cache, one on a cold cache however many notes follow, one more after each
invalidation.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from tokenizers import Tokenizer, models, pre_tokenizers

import nexus.corpus as corpus
import nexus.mcp_infra as mcp_infra
from nexus.catalog import store_hook
from nexus.chunker import split_text_to_char_cap
from nexus.embed_window import TokenWindow

BGE = "bge-base-en-v15-768"
VOYAGE = "voyage-context-3"
LEGACY = "knowledge__legacy-subject"
NOTE = " ".join(f"Sentence {i:03d} is part of a long note." for i in range(120))


def _row(name: str, model: str) -> dict:
    return {
        "name": name, "count": 1, "content_type": "knowledge", "owner_id": "legacy-subject",
        "embedding_model": model, "lifecycle_state": "live",
    }


@pytest.fixture
def asked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Models ``note_pieces`` asked a window for. bge has a 40-token window, Voyage none."""
    tok = Tokenizer(models.WordLevel(vocab={"[UNK]": 0}, unk_token="[UNK]"))
    tok.pre_tokenizer = pre_tokenizers.Whitespace()
    tok.save(str(tmp_path / "tokenizer.json"))
    bge_window = TokenWindow(40, tmp_path / "tokenizer.json")
    seen: list[str] = []

    def window_for_model(model: str) -> TokenWindow | None:
        seen.append(model)
        return bge_window if model == BGE else None

    monkeypatch.setattr(store_hook, "window_for_model", window_for_model)
    return seen


def _profile(monkeypatch: pytest.MonkeyPatch, model: str) -> None:
    """The model the install embeds a legacy name with now."""
    monkeypatch.setattr(corpus, "resolve_read_embedding_model", lambda content_type: model)


class _FakeT3:
    """The listing the collections cache fills from, counted."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.error: Exception | None = None
        self.listings = 0

    def list_collections(self) -> list[dict]:
        self.listings += 1
        if self.error is not None:
            raise self.error
        return list(self.rows)


@pytest.fixture(autouse=True)
def t3(monkeypatch: pytest.MonkeyPatch):
    fake = _FakeT3()
    mcp_infra.invalidate_collections_cache()
    monkeypatch.setattr(mcp_infra, "get_t3", lambda: fake)
    yield fake
    mcp_infra.invalidate_collections_cache()


def test_legacy_voyage_row_on_a_bge_profile_splits_to_the_voyage_window(asked, monkeypatch, t3):
    _profile(monkeypatch, BGE)
    t3.rows = [_row(LEGACY, VOYAGE)]

    pieces = store_hook.note_pieces(NOTE, LEGACY)

    assert asked == [VOYAGE]
    # Voyage has no window, so the note splits at the character cap, not at bge's 40 tokens.
    assert pieces == split_text_to_char_cap(NOTE, store_hook.NOTE_SPLIT_CHARS)
    assert "".join(pieces) == NOTE


def test_legacy_bge_row_on_a_voyage_profile_splits_to_the_bge_window(asked, monkeypatch, t3):
    _profile(monkeypatch, VOYAGE)
    t3.rows = [_row(LEGACY, BGE)]

    pieces = store_hook.note_pieces(NOTE, LEGACY)

    assert asked == [BGE]
    assert len(pieces) > len(split_text_to_char_cap(NOTE, store_hook.NOTE_SPLIT_CHARS))
    assert "".join(pieces) == NOTE


def test_legacy_bge_row_on_a_bge_profile_keeps_the_bge_window(asked, monkeypatch, t3):
    _profile(monkeypatch, BGE)
    t3.rows = [_row(LEGACY, BGE)]

    pieces = store_hook.note_pieces(NOTE, LEGACY)

    assert asked == [BGE]
    assert len(pieces) > len(split_text_to_char_cap(NOTE, store_hook.NOTE_SPLIT_CHARS))


def test_a_legacy_name_with_no_row_still_follows_the_profile(asked, monkeypatch, t3):
    _profile(monkeypatch, BGE)
    t3.rows = [_row("knowledge__someone-else", VOYAGE)]

    store_hook.note_pieces(NOTE, LEGACY)

    assert asked == [BGE]


def test_a_warm_cache_makes_no_request(asked, monkeypatch, t3):
    _profile(monkeypatch, BGE)
    mcp_infra.prime_collections_cache([_row(LEGACY, VOYAGE)])

    store_hook.note_pieces(NOTE, LEGACY)
    store_hook.note_pieces(NOTE, LEGACY)

    assert asked == [VOYAGE, VOYAGE]
    assert t3.listings == 0


def test_a_cold_cache_fills_once_then_uses_the_row(asked, monkeypatch, t3):
    _profile(monkeypatch, BGE)
    t3.rows = [_row(LEGACY, VOYAGE)]
    assert t3.listings == 0

    store_hook.note_pieces(NOTE, LEGACY)
    assert t3.listings == 1
    store_hook.note_pieces(NOTE, LEGACY)
    store_hook.note_pieces(NOTE, LEGACY)

    assert asked == [VOYAGE, VOYAGE, VOYAGE]
    assert t3.listings == 1


def test_a_listing_that_cannot_be_fetched_follows_the_name(asked, monkeypatch, t3):
    from nexus.mcp_infra import ServiceNotReadyError

    _profile(monkeypatch, BGE)
    t3.error = ServiceNotReadyError("engine is starting")

    store_hook.note_pieces(NOTE, LEGACY)

    assert asked == [BGE]
    assert t3.listings == 1


@pytest.mark.parametrize("error", [
    RuntimeError("boom"), TimeoutError("read timed out"), ValueError("bad body"),
])
def test_an_unexpected_listing_failure_follows_the_name(asked, monkeypatch, t3, error):
    # get_collection_row swallows only the not-ready and engine errors; anything else is
    # caught by resolve_row_preferred's generic catch, so the write never fails on the read.
    _profile(monkeypatch, BGE)
    t3.error = error

    store_hook.note_pieces(NOTE, LEGACY)

    assert asked == [BGE]


def test_an_invalidated_cache_refills_for_the_next_note(asked, monkeypatch, t3):
    # MCP store_put empties the cache after each committed note, so each later note in a
    # stream pays one listing (the cost the CHANGELOG states).
    _profile(monkeypatch, BGE)
    t3.rows = [_row(LEGACY, VOYAGE)]

    store_hook.note_pieces(NOTE, LEGACY)
    mcp_infra.invalidate_collections_cache()
    store_hook.note_pieces(NOTE, LEGACY)

    assert asked == [VOYAGE, VOYAGE]
    assert t3.listings == 2


def test_a_row_without_a_model_follows_the_name(asked, monkeypatch, t3):
    _profile(monkeypatch, BGE)
    row = _row(LEGACY, VOYAGE)
    del row["embedding_model"]
    t3.rows = [row]

    store_hook.note_pieces(NOTE, LEGACY)

    assert asked == [BGE]


@pytest.mark.parametrize("model", [BGE, VOYAGE])
def test_a_name_with_a_model_token_keeps_working(asked, monkeypatch, model):
    _profile(monkeypatch, "profile-model-not-asked")
    name = f"knowledge__legacy-subject__{model}__v1"

    store_hook.note_pieces(NOTE, name)

    assert asked == [model]


def test_a_row_wins_over_the_token_in_the_name(asked, monkeypatch, t3):
    # A rename keeps the source row's model under the new name, so a conformant token
    # can disagree with its row; the engine embeds by the row, so the row decides.
    _profile(monkeypatch, BGE)
    name = f"knowledge__legacy-subject__{BGE}__v1"
    t3.rows = [_row(name, VOYAGE)]

    store_hook.note_pieces(NOTE, name)

    assert asked == [VOYAGE]


# nexus-ldh33: each chunk's metadata stamps the model the row names, the same one the
# window came from, read from the same cached row (no second request).


def _stamped(collection: str) -> list[str]:
    from nexus.catalog.note_write import _chunk_payload

    pieces = ["first piece", "second piece"]
    rows = [{"chash": f"{i:064x}"} for i in range(len(pieces))]
    chunks = _chunk_payload(
        collection, pieces, rows, title="t", tags="", category="", session_id="",
        source_agent="", ttl_days=None, catalog_doc_id="1.1.1", content_type="prose",
    )
    return [c["metadata"]["embedding_model"] for c in chunks]


def test_a_legacy_bge_row_on_a_voyage_profile_stamps_bge(monkeypatch, t3):
    _profile(monkeypatch, VOYAGE)
    t3.rows = [_row(LEGACY, BGE)]

    assert _stamped(LEGACY) == [BGE, BGE]


def test_a_legacy_voyage_row_on_a_bge_profile_stamps_voyage(monkeypatch, t3):
    _profile(monkeypatch, BGE)
    t3.rows = [_row(LEGACY, VOYAGE)]

    assert _stamped(LEGACY) == [VOYAGE, VOYAGE]


def test_the_stamp_follows_the_row_over_the_token_in_the_name(monkeypatch, t3):
    _profile(monkeypatch, BGE)
    name = f"knowledge__legacy-subject__{BGE}__v1"
    t3.rows = [_row(name, VOYAGE)]

    assert _stamped(name) == [VOYAGE, VOYAGE]


def test_a_legacy_name_with_no_row_stamps_the_profile_model(monkeypatch, t3):
    # The name fallback is the calibrated one the window uses: the plain derivation
    # stamped voyage-context-3 on a bge-profile install.
    _profile(monkeypatch, BGE)

    assert _stamped(LEGACY) == [BGE, BGE]


def test_window_and_stamp_share_one_listing(asked, monkeypatch, t3):
    _profile(monkeypatch, VOYAGE)
    t3.rows = [_row(LEGACY, BGE)]

    store_hook.note_pieces(NOTE, LEGACY)
    stamped = _stamped(LEGACY)

    assert asked == [BGE]
    assert stamped == [BGE, BGE]
    assert t3.listings == 1


def test_a_row_without_a_model_stamps_the_profile_model(monkeypatch, t3):
    _profile(monkeypatch, BGE)
    row = _row(LEGACY, VOYAGE)
    del row["embedding_model"]
    t3.rows = [row]

    assert _stamped(LEGACY) == [BGE, BGE]


def test_a_listing_that_cannot_be_fetched_stamps_the_profile_model(monkeypatch, t3):
    from nexus.mcp_infra import ServiceNotReadyError

    _profile(monkeypatch, BGE)
    t3.error = ServiceNotReadyError("engine is starting")

    assert _stamped(LEGACY) == [BGE, BGE]


@pytest.mark.parametrize("prefix", ["knowledge", "docs", "rdr"])
@pytest.mark.parametrize("model", [BGE, VOYAGE])
def test_the_no_row_stamp_is_the_model_a_first_write_registers(monkeypatch, prefix, model):
    # With no row, the first write registers the collection: the stamp must name the
    # model that registration gives the row, or the first note is stamped wrong.
    monkeypatch.setattr(corpus, "effective_embedding_model_for_writes", lambda content_type: model)
    _profile(monkeypatch, model)
    name = f"{prefix}__legacy-subject"

    assert (corpus.embedding_model_for_collection_calibrated(name)
            == corpus.collection_registration_kwargs(name)["embedding_model"] == model)
