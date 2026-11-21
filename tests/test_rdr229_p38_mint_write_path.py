# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P3.8 (nexus-p2m1x.28) client write path: a NEW knowledge subject is registered by its attributes and the
write goes to the label the engine minted; no conformant name is rendered for it.

Unit level, against a stubbed catalog writer (the engine leg is
``tests/integration/test_rdr229_p38_mint_by_attributes.py``). What stays a name: a legacy two-segment knowledge
name that already holds data and has no row (the migration source), an existing conformant collection with no
row, a restore (``allow_placeholder``), a read, and a collection that is not ``knowledge``.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

import nexus.catalog.factory as factory
import nexus.corpus as corpus
from nexus.catalog.rows import legacy_names
import nexus.mcp_infra as mcp_infra
from nexus import collection_mint
from nexus.catalog.collection_name import CollectionAttributes
from nexus.catalog.engine_writes.floors import CollectionMintUnsupportedError
from nexus.corpus import (
    EmbeddingProfileMismatchError,
    _resolve_promoted_model_token,
    effective_embedding_model_for_writes,
    embedding_model_row_preferred,
    ensure_collection_registered,
    t3_collection_name,
)

_SUBJECT = "p38-new-subject"


def _model() -> str:
    return effective_embedding_model_for_writes("knowledge")


def _row(label: str = "c42", *, cid: int | None = 42, owner: str = _SUBJECT) -> CollectionAttributes:
    return CollectionAttributes(
        collection_id=cid, label=label, content_type="knowledge", owner_id=owner,
        embedding_model=_model(), model_version=1, dimension=768, lifecycle_state="live",
    )


class _Writer:
    """A catalog writer that mints: every nameless registration answers *row*."""

    def __init__(self, row: CollectionAttributes | None = None, error: Exception | None = None) -> None:
        self.row = row or _row()
        self.error = error
        self.calls: list[tuple[tuple, dict]] = []
        self.closed = 0

    def register_collection(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error is not None:
            raise self.error
        return self.row

    def close(self) -> None:
        self.closed += 1


class _T3:
    """A vector store holding *names*, a catalog answering *rows*, and the registrar its writes register through."""

    def __init__(self, names: set[str] | None = None, rows: dict | None = None, writer: _Writer | None = None) -> None:
        self.names = names or set()
        self.rows = rows or {}
        self.writer = writer or _Writer()
        self._collection_registrar = lambda: self.writer
        self.probed: list[str] = []

    def collection_exists(self, name: str) -> bool:
        self.probed.append(name)
        return name in self.names

    def find_collection(self, content_type: str, owner_id: str, embedding_model: str):
        return self.rows.get((content_type, owner_id))


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch):
    corpus._REGISTERED_COLLECTIONS.clear()
    corpus._REGISTERED_COLLECTIONS_SCOPED.clear()
    # No engine: the profile read answers "no profile row yet" and the row cache holds no listing.
    monkeypatch.setattr(corpus, "_profile_model_for_content_type", lambda ct: None)
    monkeypatch.setattr(mcp_infra, "_refresh_collections_cache_if_stale", lambda *a, **k: None)
    mcp_infra.invalidate_collections_cache()
    collection_mint.forget_all()
    yield
    collection_mint.forget_all()
    corpus._REGISTERED_COLLECTIONS.clear()
    corpus._REGISTERED_COLLECTIONS_SCOPED.clear()
    mcp_infra.invalidate_collections_cache()


# ── a new subject mints ─────────────────────────────────────────────────────


def test_a_write_to_a_new_subject_registers_by_attributes_and_returns_the_minted_label() -> None:
    t3 = _T3()

    label = t3_collection_name(_SUBJECT, t3=t3, for_write=True)

    assert label == "c42"
    assert t3.writer.calls == [((), {
        "content_type": "knowledge", "owner_id": _SUBJECT,
        "embedding_model": _model(), "model_version": "v1",
    })], "one nameless registration, attributes only"
    assert t3.writer.closed == 1


def test_no_conformant_name_is_rendered_for_the_new_subject() -> None:
    t3 = _T3()

    label = t3_collection_name(_SUBJECT, t3=t3, for_write=True)

    rendered = f"knowledge__{_SUBJECT}__{_model()}__v1"
    assert label != rendered
    assert not legacy_names.is_conformant_collection_name(label)
    (args, kwargs), = t3.writer.calls
    assert args == () and "name" not in kwargs, "the registration carries no name at all"


def test_the_minted_label_is_registered_in_this_process_so_the_first_write_does_not_re_register_it() -> None:
    """The write path's ``ensure_collection_registered(label)`` must be a cache hit: re-registering a label by
    deriving attributes from it would send ``owner_id=<label>``."""
    t3 = _T3()
    label = t3_collection_name(_SUBJECT, t3=t3, for_write=True)
    other = _Writer()

    ensure_collection_registered(label, registrar=lambda: other)

    assert other.calls == []


def test_the_first_write_reads_the_registered_row_because_the_listing_omits_a_collection_with_no_chunk() -> None:
    """``embedding_model_row_preferred('c42')`` would otherwise derive a model from a prefix-free name."""
    t3 = _T3(writer=_Writer(_row("c42")))
    label = t3_collection_name(_SUBJECT, t3=t3, for_write=True)

    assert mcp_infra.get_collection_row(label) is not None
    assert mcp_infra.get_collection_row(label)["embedding_model"] == _model()
    assert mcp_infra.get_collection_row(label)["content_type"] == "knowledge"
    assert mcp_infra.get_collection_row(label)["owner_id"] == _SUBJECT
    assert embedding_model_row_preferred(label) == _model()


def test_the_listing_wins_once_it_names_the_collection() -> None:
    t3 = _T3(writer=_Writer(_row("c42")))
    label = t3_collection_name(_SUBJECT, t3=t3, for_write=True)
    mcp_infra._collections_cache[2][label] = {  # the engine's listing now names it
        "content_type": "knowledge", "owner_id": _SUBJECT, "embedding_model": "listed-model", "lifecycle_state": "live",
    }

    assert mcp_infra.get_collection_row(label)["embedding_model"] == "listed-model"


def test_a_second_write_finds_the_row_and_registers_nothing() -> None:
    t3 = _T3(rows={("knowledge", _SUBJECT): _row("c42")})

    assert t3_collection_name(_SUBJECT, t3=t3, for_write=True) == "c42"
    assert t3.writer.calls == []


def test_an_underscored_subject_registers_its_owner_segment() -> None:
    t3 = _T3()

    t3_collection_name("p38_new_subject", t3=t3, for_write=True)

    assert t3.writer.calls[0][1]["owner_id"] == "p38-new-subject"


def test_a_two_segment_name_with_no_data_is_a_new_subject_too() -> None:
    t3 = _T3()

    assert t3_collection_name(f"knowledge__{_SUBJECT}", t3=t3, for_write=True) == "c42"
    assert t3.writer.calls[0][1]["owner_id"] == _SUBJECT


# ── what stays a name ──────────────────────────────────────────────────────


def test_a_legacy_two_segment_collection_with_data_and_no_row_stays_the_write_target() -> None:
    """The migration source (item 5): nothing is minted over data that sits under a legacy name."""
    t3 = _T3(names={f"knowledge__{_SUBJECT}"})

    assert t3_collection_name(_SUBJECT, t3=t3, for_write=True) == f"knowledge__{_SUBJECT}"
    assert t3.writer.calls == []


def test_an_existing_conformant_collection_with_no_row_stays_the_write_target() -> None:
    promoted = f"knowledge__{_SUBJECT}__{_resolve_promoted_model_token('knowledge', for_write=False)}__v1"
    t3 = _T3(names={promoted})

    assert t3_collection_name(_SUBJECT, t3=t3, for_write=True) == promoted
    assert t3.writer.calls == []


def test_a_read_of_a_new_subject_mints_nothing() -> None:
    t3 = _T3()

    label = t3_collection_name(_SUBJECT, t3=t3)

    assert label == f"knowledge__{_SUBJECT}__{_resolve_promoted_model_token('knowledge', for_write=False)}__v1"
    assert t3.writer.calls == []


def test_a_restore_keeps_the_name_it_is_restoring() -> None:
    """``allow_placeholder`` is the recovery import: the bundle names the collection it carries."""
    t3 = _T3()

    label = t3_collection_name("default", t3=t3, for_write=True, allow_placeholder=True)

    assert label.startswith("knowledge__default__")
    assert t3.writer.calls == []


def test_a_collection_that_is_not_knowledge_is_not_minted_here() -> None:
    """code, docs and rdr collections are minted by ``nx index repo`` with a tumbler owner (collections.md)."""
    t3 = _T3()

    label = t3_collection_name("docs__some-subject", t3=t3, for_write=True)

    assert label.startswith("docs__some-subject__")
    assert t3.writer.calls == []


# ── failure is loud, never a rendered name ────────────────────────────────


def test_a_registration_failure_propagates_and_no_name_is_rendered() -> None:
    t3 = _T3(writer=_Writer(error=httpx.ConnectError("down")))

    with pytest.raises(httpx.ConnectError):
        t3_collection_name(_SUBJECT, t3=t3, for_write=True)
    assert t3.writer.closed == 1


def test_an_engine_below_the_mint_floor_is_refused_naming_the_version() -> None:
    err = CollectionMintUnsupportedError("engine 0.1.199 does not mint collection labels; minimum 0.1.201")
    t3 = _T3(writer=_Writer(error=err))

    with pytest.raises(CollectionMintUnsupportedError, match="0.1.201"):
        t3_collection_name(_SUBJECT, t3=t3, for_write=True)


def test_an_off_profile_model_is_refused_before_the_wire_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(corpus, "_profile_model_for_content_type", lambda ct: "profile-says-another-model")
    t3 = _T3()

    with pytest.raises(EmbeddingProfileMismatchError):
        t3_collection_name(_SUBJECT, t3=t3, for_write=True)
    assert t3.writer.calls == []


def test_a_row_the_engine_answers_for_another_subject_is_refused() -> None:
    t3 = _T3(writer=_Writer(_row("c42", owner="somebody-else")))

    with pytest.raises(ValueError, match="somebody-else"):
        t3_collection_name(_SUBJECT, t3=t3, for_write=True)


def test_a_non_row_answer_is_refused() -> None:
    """A test double that answers a MagicMock must not have its attribute taken for a label."""
    t3 = _T3(writer=_Writer(MagicMock()))

    with pytest.raises(TypeError):
        t3_collection_name(_SUBJECT, t3=t3, for_write=True)


# ── a writer with no vector store (nx index md / pdf, nx dt index) ─────────


def test_a_writer_with_no_store_registers_through_the_default_catalog_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    writer = _Writer()
    monkeypatch.setattr(factory, "make_catalog_writer", lambda *a, **k: writer)

    label = t3_collection_name(_SUBJECT, for_write=True, subject_finder=lambda ct, owner, model: None)

    assert label == "c42"
    assert writer.calls[0][1]["owner_id"] == _SUBJECT


def test_a_writer_with_no_store_finds_the_row_first(monkeypatch: pytest.MonkeyPatch) -> None:
    writer = _Writer()
    monkeypatch.setattr(factory, "make_catalog_writer", lambda *a, **k: writer)

    label = t3_collection_name(_SUBJECT, for_write=True, subject_finder=lambda ct, owner, model: _row("c7", cid=7))

    assert label == "c7"
    assert writer.calls == []


def test_a_writer_with_no_store_and_no_finder_is_a_dry_run_and_registers_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """``nx index pdf --dry-run`` passes no finder: it writes nothing, so it renders and registers nothing."""
    writer = _Writer()
    monkeypatch.setattr(factory, "make_catalog_writer", lambda *a, **k: writer)

    label = t3_collection_name(_SUBJECT, for_write=True)

    assert label == f"knowledge__{_SUBJECT}__{_model()}__v1"
    assert writer.calls == []
