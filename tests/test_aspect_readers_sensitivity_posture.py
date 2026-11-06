# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-243 Phase 2 review r1 F7 (nexus-rfvyy.7.11): the aspect reader hands chunk text to an LLM extractor, so its
chunk reads ask for ``sensitivity="exclude"`` (the RDR item 4 shared-chunk rule: a chunk with any live sensitive
owner is withheld), on top of the per-document gate. Both read shapes are pinned: by manifest ids and by the
identity-field filter. A refused ``exclude`` read comes back as a typed ``ReadFail``, never as an empty document."""
from __future__ import annotations

from dataclasses import dataclass

from nexus.aspect_readers import ReadFail, ReadOk, _gather_chroma_chunks_by_field
from nexus.db.http_vector_client import SensitivityExcludeUnavailable


@dataclass
class _Row:
    chash: str
    position: int


class _Coll:
    def __init__(self) -> None:
        self.postures: list[str] = []

    def get(self, ids=None, include=None, *, where=None, limit=None, offset=None, sensitivity):
        self.postures.append(sensitivity)
        if ids is not None:
            return {"ids": list(ids), "documents": [f"text-{i[:4]}" for i in ids], "metadatas": [{"chunk_text_hash": i} for i in ids]}
        if offset:
            return {"ids": [], "documents": [], "metadatas": []}
        return {"ids": ["x"], "documents": ["filtered text"], "metadatas": [{"chunk_text_hash": "", "chunk_index": 0}]}


def _gather(coll, **kw):
    return _gather_chroma_chunks_by_field(
        coll=coll, collection="knowledge__x", source_id="s", match_value=kw.pop("match_value", "1.1.1"),
        identity_field=kw.pop("identity_field", "doc_id"), **kw,
    )


def test_the_manifest_id_read_asks_for_exclude():
    coll = _Coll()
    result = _gather(coll, manifest_lookup=lambda doc_id: [_Row("a" * 64, 0), _Row("b" * 64, 1)])

    assert isinstance(result, ReadOk)
    assert coll.postures == ["exclude"]


def test_the_identity_field_read_asks_for_exclude():
    coll = _Coll()
    result = _gather(coll, identity_field="title", match_value="a title")

    assert isinstance(result, ReadOk)
    assert coll.postures and set(coll.postures) == {"exclude"}


def test_a_refused_exclude_read_is_a_typed_read_failure_not_an_empty_document():
    class Refusing:
        def get(self, ids=None, include=None, *, where=None, limit=None, offset=None, sensitivity):
            raise SensitivityExcludeUnavailable("sensitivity='exclude' read refused: the engine does not filter")

    result = _gather(Refusing(), identity_field="title", match_value="a title")

    assert isinstance(result, ReadFail)
    assert result.reason != "empty", "a refusal must not read as a document with no text"
    assert "refused" in result.detail


def test_no_literal_include_remains_in_the_reader():
    import re
    from pathlib import Path

    import nexus.aspect_readers as mod

    assert not re.search(r"""sensitivity\s*=\s*["']include["']""", Path(mod.__file__).read_text())
