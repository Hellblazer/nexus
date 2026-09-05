# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-4jj40 round 5 (T2 critique [24618]): end-to-end proof, against the
real engine substrate, that the combined-write path (``write_manifest_many``
with ``chunks=``, the ChunkBatcher flush's actual write mechanism for a real
``nx index repo`` run) refreshes a chunk's stored metadata for a chash whose
text is byte-identical to what is already stored, WITHOUT re-embedding it.

Before this round, ``CombinedWriteService.writeManyCombined`` OMITTED an
"already have identical text" chash from its resolved map entirely (original
nexus-kl2z6 design: "no chunks_<dim> write at all") -- so a plain re-index
whose ONLY change is chunk metadata (e.g. RDR-200 Phase 1c's
``section_type`` reclassification, this bead's own defect 4) never landed,
even with ``nx index repo --force``. This test proves the fix through the
REAL client -> HTTP -> engine round trip, mirroring
``tests/test_rdr191_gc_serverside_prune.py``'s substrate pattern
(``t2_service_env`` + ``ActiveCatalog`` + a real ``HttpVectorClient``).

The exact embed-CALL-COUNT proof (an in-process counting embedder, asserting
delta 0) lives in the Java substrate suite --
``service/src/test/java/dev/nexus/service/CombinedWriteRepositoryTest
.java::combinedWrite_metadataOnlyChange_updatesMetadataWithZeroEmbedCalls`` --
because that count is only observable where the embed call actually happens
(server-side); the Python client's ``write_manifest_many`` wrapper does not
surface the engine response's ``embed_skipped``/``embed_embedded`` fields.
This test instead proves the property a Python-level test CAN observe
end-to-end: the metadata written by a real client call actually lands.
"""
from __future__ import annotations

import hashlib

import pytest

pytestmark = [pytest.mark.integration]


def test_combined_write_metadata_only_change_lands_via_real_client(
    t2_service_env,
) -> None:
    """Seed a chunk via the real combined-write path, then resend the SAME
    chash/text with DIFFERENT metadata (force_re_embed left at its default,
    False) -- the new metadata must be readable back afterwards."""
    import nexus.db.http_vector_client as hvc
    from tests._catalog_fixture_ops import ActiveCatalog

    tenant = t2_service_env
    cat = ActiveCatalog()
    db = hvc.HttpVectorClient(tenant=tenant)

    coll_name = "code__jj40mr__bge-base-en-v15-768__v1"
    owner = cat.register_owner("jj40mr", "curator")
    stable_text = "def widget_processor(): return 'stable across both calls'\n"
    chash = hashlib.sha256(stable_text.encode()).hexdigest()

    doc_a = str(cat.register(
        owner, "widget_a.py", content_type="code",
        file_path="/tmp/jj40mr/widget_a.py",
        physical_collection=coll_name, chunk_count=1,
    ))
    doc_b = str(cat.register(
        owner, "widget_b.py", content_type="code",
        file_path="/tmp/jj40mr/widget_b.py",
        physical_collection=coll_name, chunk_count=1,
    ))

    # Call 1: brand-new chash, initial metadata (section_type unset).
    cat.write_manifest_many(
        [(doc_a, [{"chash": chash, "position": 0}])],
        collection=coll_name,
        chunks=[{"chash": chash, "text": stable_text, "metadata": {"section_type": ""}}],
        force_re_embed=False,
    )
    meta1 = db.get_collection(coll_name).get_all_metadata()
    idx1 = meta1["ids"].index(chash)
    assert meta1["metadatas"][idx1].get("section_type") == "", (
        "call 1: initial metadata must be stored as sent"
    )

    # Call 2: SAME chash, SAME text, DIFFERENT metadata (simulates a
    # reclassification-only reindex -- e.g. this bead's section_type
    # rule changing its mind about an already-indexed chunk). Before
    # nexus-4jj40 round 5, this chash would have been OMITTED from the
    # engine's resolved map entirely (identical text -> "already have
    # identical text" skip, no write of any kind) and the new metadata
    # would never have landed.
    cat.write_manifest_many(
        [(doc_b, [{"chash": chash, "position": 0}])],
        collection=coll_name,
        chunks=[{"chash": chash, "text": stable_text, "metadata": {"section_type": "imports"}}],
        force_re_embed=False,
    )
    meta2 = db.get_collection(coll_name).get_all_metadata()
    idx2 = meta2["ids"].index(chash)
    assert meta2["metadatas"][idx2].get("section_type") == "imports", (
        "call 2: the new metadata must have landed despite the embed skip -- "
        "this is the exact gap nexus-4jj40 round 5 closes"
    )
    # Sanity: still exactly one row for this chash (no duplicate/second
    # chunk row was created by the metadata-only path).
    assert meta2["ids"].count(chash) == 1
