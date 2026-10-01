# SPDX-License-Identifier: AGPL-3.0-or-later
"""One mixed collection shared by the RDR-192 Step 8 client tests (nexus-wbfpw.18, .19).

Built through real catalog write paths over a real engine substrate, never a raw manifest
seed, then aged past the engine's grace window with ``tests/_reapable_age.py`` (the route has no
tunable window and the 30 day default hides every fresh row). Which of its chunks are reapable is
the engine's own verdict (S1b rows, ``tests/test_wbfpw2_client_liveness_matrix.py``):

==========  ==========================================================  =========
name        shape                                                       reapable
==========  ==========================================================  =========
orphans     R1: chunk with no manifest row anywhere, old                yes
owned       R2: a live owner in this collection                         no
tombstoned  R3: only owner is a tombstoned document                     no
shared      R5: two documents named it, one was re-manifested away      no
fresh       R1 shape but written after the aging pass (inside grace)    no
==========  ==========================================================  =========
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from tests._chunk_seed import seed_chunks_direct
from tests._reapable_age import age_chunks_past_grace

_MODEL = "bge-base-en-v15-768"


def chash_of(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True)
class MixedCollection:
    name: str
    orphans: tuple[str, ...]
    owned: str
    tombstoned: str
    shared: str
    fresh: str

    @property
    def reapable(self) -> set[str]:
        return set(self.orphans)

    @property
    def everything(self) -> set[str]:
        return {*self.orphans, self.owned, self.tombstoned, self.shared, self.fresh}


def collection_name(tag: str) -> str:
    return f"knowledge__{tag}__{_MODEL}__v1"


def _doc(cat, owner, coll: str, title: str, chash: str) -> str:
    """A note-shaped document (no file_path, ``meta.doc_id`` set) with a manifest row naming *chash*.
    Note-shaped so nx t3 gc's RUNFENCE breaker, which exempts notes, does not fire on a fixture that
    never ran an index fence; the manifest row keeps it out of the manifest-less census."""
    doc = cat.register(
        owner, title, content_type="knowledge", physical_collection=coll, meta={"doc_id": chash},
    )
    cat.append_manifest_chunks(str(doc), [{"chash": chash, "position": 0}], collection=coll)
    cat.resync_chunk_count_cache(str(doc))
    return str(doc)


def write_chunks(coll: str, texts: list[str]) -> list[str]:
    chashes = [chash_of(t) for t in texts]
    seed_chunks_direct(
        coll, ids=chashes, documents=texts, embed=True,
        metadatas=[{"chunk_text_hash": h, "title": t[:30]} for h, t in zip(chashes, texts)],
    )
    return chashes


def build_mixed_collection(cat, tag: str) -> MixedCollection:
    coll = collection_name(tag)
    owner = cat.register_owner(f"{tag}-owner", "curator")
    o1, o2, owned, tomb, shared = write_chunks(coll, [
        f"{tag} orphan one: a chunk no document names.",
        f"{tag} orphan two: a chunk no document names either.",
        f"{tag} owned: a chunk a live document names.",
        f"{tag} tombstoned: a chunk only a deleted document names.",
        f"{tag} shared: a chunk two documents named, one of which let go.",
    ])
    _doc(cat, owner, coll, f"{tag}-owned", owned)
    dead = _doc(cat, owner, coll, f"{tag}-tombstoned", tomb)
    assert cat.delete_document(dead), "control: the tombstone write must report a real delete"
    keep = _doc(cat, owner, coll, f"{tag}-shared-keep", shared)
    drop = _doc(cat, owner, coll, f"{tag}-shared-drop", shared)
    assert keep != drop
    cat.write_manifest(drop, [], collection=coll)  # the drop document lets go of the chunk
    # The engine's grace is 30 days; stand for chunks that have been ownerless that long.
    age_chunks_past_grace(coll)
    # Written AFTER the aging pass, so it is the one ownerless chunk still inside its grace.
    (fresh,) = write_chunks(coll, [f"{tag} fresh: an ownerless chunk written just now."])
    return MixedCollection(
        name=coll, orphans=(o1, o2), owned=owned, tombstoned=tomb, shared=shared, fresh=fresh,
    )
