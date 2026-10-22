# SPDX-License-Identifier: AGPL-3.0-or-later
"""Idempotent collection building for the P43 harness.

A collection is named from the corpus content hash (owner id ``p43-<hash8>``), so
changed content is a new collection and unchanged content is reused. A state
file in the persistent workdir records, per collection, each document's catalog
id and whether its write finished; a rerun after a failure re-registers nothing
and re-embeds only the documents that did not finish. A collection counts as
complete only when the engine's own chunk count equals the number of distinct
chunk hashes written, checked on every run.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

import p43_engine as eng
from p43_corpus import Chunk, Corpus


class State:
    """JSON state file, written atomically after every document."""

    def __init__(self, path: Path):
        self.path = path
        self.doc: dict = {"collections": {}}
        if path.exists():
            self.doc = json.loads(path.read_text(encoding="utf-8"))
            self.doc.setdefault("collections", {})

    def collection(self, name: str) -> dict:
        return self.doc["collections"].setdefault(
            name, {"docs": {}, "complete": False, "registered": False})

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.doc, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)


@dataclass(frozen=True)
class CollectionSpec:
    name: str
    content_type: str
    owner_id: str
    owner_prefix: str
    model: str

    @classmethod
    def for_corpus(cls, corpus: Corpus, model: str) -> "CollectionSpec":
        owner_id = f"p43-{corpus.short_hash}"
        return cls(eng.collection_name(corpus.content_type, owner_id, model),
                   corpus.content_type, owner_id, owner_prefix_for(owner_id), model)


def owner_prefix_for(owner_id: str) -> str:
    """A stable catalog tumbler prefix ``9.<n>`` derived from the owner id."""
    n = int(hashlib.sha256(owner_id.encode()).hexdigest()[:8], 16) % 900_000_000 + 100_000
    return f"9.{n}"


def register(engine: eng.Engine, state: State, spec: CollectionSpec) -> None:
    """Register the owner and collection (idempotent). Under the Voyage profile
    a Voyage collection can be registered; under the bge profile a new Voyage
    collection is refused (HTTP 422), so Voyage collections are registered in the
    seed boot (``run.sh``) and this is then a no-op for them."""
    st = state.collection(spec.name)
    if st.get("registered"):
        return
    engine.upsert_owner(spec.owner_prefix, spec.owner_id)
    engine.upsert_collection(spec.name, spec.content_type, spec.owner_id, spec.model)
    st["registered"] = True
    st["owner_prefix"] = spec.owner_prefix
    state.save()


def ingest(engine: eng.Engine, state: State, spec: CollectionSpec,
           chunks_by_file: dict[str, list[Chunk]], *, log=print) -> dict:
    """Write every file's chunks into *spec*'s collection, one document per file.

    Returns ``{"name", "expected", "count", "reused", "written_chunks"}``."""
    expected = len({c.chash for cs in chunks_by_file.values() for c in cs})
    st = state.collection(spec.name)
    if st.get("complete") and engine.count(spec.name) == expected:
        return {"name": spec.name, "expected": expected, "count": expected,
                "reused": True, "written_chunks": 0}
    st["complete"] = False
    register(engine, state, spec)
    written = 0
    for i, (file, chunks) in enumerate(sorted(chunks_by_file.items()), 1):
        d = st["docs"].setdefault(file, {})
        if d.get("done"):
            continue
        if "doc_id" not in d:
            d["doc_id"] = engine.register_doc(
                spec.owner_prefix, f"p43 {spec.model} {file}", spec.content_type,
                f"/p43/{spec.name}/{file}")      # unique per collection: the engine keys a document on its path
            state.save()
        written += engine.write_doc(spec.name, spec.model, d["doc_id"], chunks,
                                    label=spec.name)
        d["done"] = True
        state.save()
        log(f"[p43] {spec.name}: {i}/{len(chunks_by_file)} documents")
    count = engine.count(spec.name)
    if count != expected:
        raise RuntimeError(
            f"{spec.name}: engine holds {count} chunks, expected {expected} distinct chunk hashes")
    st["complete"] = True
    state.save()
    return {"name": spec.name, "expected": expected, "count": count,
            "reused": False, "written_chunks": written}
