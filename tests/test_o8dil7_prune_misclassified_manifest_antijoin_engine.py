# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-191 GATE-2 (nexus-o8dil.7) acceptance item 3 -- Python/Arm B regression
tripwire.

REBUILD 2026-08-13 (Hal-authorized). The commit d9e21c73 predecessor of this
file was deliberately dropped; this is a from-scratch rebuild re-aligned to
the post-wave-2 contract -- see T2 ``nexus/rdr-191-gate2-tripwire-rebuild-
2026-08-13`` for the full design-delta record. Two changes from the
predecessor:

1. ``append_manifest_chunks`` (and ``atomic_manifest_replace``,
   ``write_manifest``) now require an explicit ``collection`` keyword
   (RDR-191, Hal ruling 2026-08-12 -- ``HttpCatalogClient`` raises
   ``ValueError`` without it). The predecessor's fixture call omitted it;
   fixed here.
2. The dangling anti-join is now scoped to definition (a) ONLY (T2
   ``nexus/rdr-191-dangling-definition-of-record`` [22364]): a manifest row
   is dangling ONLY when its owning document is LIVE. A tombstoned owner's
   manifest row (class b, soft-tombstone residue) is excluded by an explicit
   ``d.deleted_at IS NULL`` join -- the predecessor's anti-join omitted this
   join entirely (owner-blind), which is now the WRONG shape for GATE-2's
   criterion. CORRECTION (substantive-critic Critical, T2 ``nexus/critique-
   gate2-tripwire-7ygt0-2026-08-13`` [22388]): the first cut of this rebuild
   shipped with zero kill control proving that exclusion actually fires --
   no fixture tombstoned anything, so a regression to the owner-blind shape
   would have passed every test here silently.
   ``test_negative_control_tombstoned_owner_genuine_ghost_row_excluded``
   closes that gap.

``_prune_misclassified_in_collection`` (``src/nexus/indexer.py:2530``,
funnel callers ``:2938``/``:2946``) is the ONE producer in the RDR-191
delete/prune funnel with ZERO real-engine coverage before this file: all
three of its pre-existing test files
(``tests/test_o8dil2_prune_misclassified_manifest_guard.py`` and its
siblings) drive it against ``_FakeCol``/``_FakeCatalog`` doubles, and
``tests/test_o8dil45_indexer_delete_count.py`` SIMULATES the server's
anti-join in Python rather than executing it in real PG. The function
composes with the Java F10c fix (``indexer.py:2492-2500``:
``_prune_misclassified_in_collection -> _batched_delete -> col.delete() ->
_ServiceCollectionStub -> PgVectorRepository.delete``), so a Java-only
tripwire (``RdrO8dil7GlobalManifestAntiJoinTest``) leaves this function's
own manifest-FIRST-then-delete ORDERING CONTRACT (F8c) untested against a
real server -- the server sees two well-formed calls in either order, so
only a client-side test can see the ordering matter.

Anti-join key: ``(tenant_id, collection, chash)`` -- the SAME discipline as
the Java arm, NOT chash-only (``ChashSqlIdioms.danglingManifestCountDsl``'s
shape, which understated the live census by 2.2x -- T1 scratch 7c0cdcb6).

RAW SQL, VERIFIED (not assumed, per the bead's own scoping note): Python
CAN issue SQL directly against the substrate --
``tests/_engine_substrate.py:585-599`` deliberately exports
``pg_port``/``pg_user``/``pg_dbname``/``pg_bin`` "so a test can query the
substrate's schema DIRECTLY rather than through the engine's HTTP surface"
(nexus-20890). Precedent: ``tests/catalog/
test_collection_scoped_tables_schema_parity.py``'s ``_psql`` helper, copied
here verbatim.

``pg_user`` is ``os.environ["USER"]`` -- the initdb role
(``tests/_engine_substrate.py:419``), i.e. SUPERUSER. Raw psql therefore
BYPASSES RLS: every query below carries its OWN explicit ``tenant_id``
predicate rather than inheriting tenant scope from a session GUC.
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration]

_WRONG_COLL = "code__o8dil7-prune-wrong__bge-base-en-v15-768__v1"
_RIGHT_COLL = "code__o8dil7-prune-right__bge-base-en-v15-768__v1"
_SHARED_COLL = "code__o8dil7-tbj48-shared__bge-base-en-v15-768__v1"


def _psql(state: dict, sql: str) -> list[str]:
    """Run *sql* against the substrate's PG, returning stripped non-empty rows."""
    psql = Path(state["pg_bin"]) / "psql"
    proc = subprocess.run(
        [
            str(psql),
            "-h", "127.0.0.1",
            "-p", str(state["pg_port"]),
            "-U", state["pg_user"],
            "-d", state["pg_dbname"],
            "-tAc", sql,
        ],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, (
        f"psql failed ({proc.returncode}) running:\n{sql}\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _dangling_count(state: dict, tenant: str) -> int:
    """Definition-(a)-scoped (T2 ``nexus/rdr-191-dangling-definition-of-
    record`` [22364]): ``(tenant_id, collection, chash)``-keyed anti-join,
    scoped to *tenant* explicitly (pg_user is superuser -- no RLS to lean
    on), with an explicit live-owner join (``d.deleted_at IS NULL``) so a
    tombstoned owner's manifest row (class b, soft-tombstone residue) is
    excluded -- GATE-2's criterion is about definition (a) only.
    """
    rows = _psql(state, (
        "SELECT COUNT(*) FROM nexus.catalog_document_chunks m "
        "JOIN nexus.catalog_documents d "
        "  ON d.tenant_id = m.tenant_id AND d.tumbler = m.doc_id "
        f"WHERE m.tenant_id = '{tenant}' AND d.deleted_at IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM nexus.chunks c WHERE c.tenant_id = m.tenant_id "
        "  AND c.collection = m.collection AND c.chash = m.chash)"
    ))
    return int(rows[0]) if rows else 0


def _dangling_count_any_owner_state(state: dict, tenant: str) -> int:
    """RAW / owner-tombstone-BLIND twin of ``_dangling_count``, used ONLY by
    ``test_negative_control_tombstoned_owner_genuine_ghost_row_excluded``
    below (substantive-critic Critical, T2
    ``nexus/critique-gate2-tripwire-7ygt0-2026-08-13`` [22388]; see the Java
    sibling ``globalDanglingCountAnyOwnerState`` for the full rationale).
    IDENTICAL to ``_dangling_count`` except for the missing live-owner
    filter -- deliberately the (a)-union-(b) shape T2
    ``nexus/rdr-191-dangling-definition-of-record`` [22364] documents as the
    exact source of the historical 2,951/6,501-vs-37 confusion. NOT used by
    any producer test's own pass/fail assertion.
    """
    rows = _psql(state, (
        "SELECT COUNT(*) FROM nexus.catalog_document_chunks m "
        "JOIN nexus.catalog_documents d "
        "  ON d.tenant_id = m.tenant_id AND d.tumbler = m.doc_id "
        f"WHERE m.tenant_id = '{tenant}' "
        "AND NOT EXISTS (SELECT 1 FROM nexus.chunks c WHERE c.tenant_id = m.tenant_id "
        "  AND c.collection = m.collection AND c.chash = m.chash)"
    ))
    return int(rows[0]) if rows else 0


def _manifest_row_count(state: dict, tenant: str, doc_id: str) -> int:
    rows = _psql(state, (
        "SELECT COUNT(*) FROM nexus.catalog_document_chunks "
        f"WHERE tenant_id = '{tenant}' AND doc_id = '{doc_id}'"
    ))
    return int(rows[0]) if rows else 0


def _manifest_chashes(state: dict, tenant: str, doc_id: str) -> set[str]:
    rows = _psql(state, (
        "SELECT encode(chash, 'hex') FROM nexus.catalog_document_chunks "
        f"WHERE tenant_id = '{tenant}' AND doc_id = '{doc_id}'"
    ))
    return set(rows)


def test_prune_misclassified_real_engine_no_dangling_manifest_row(t2_service_env):
    """Drives ``_prune_misclassified_in_collection`` against the REAL PG
    engine (not ``_FakeCol``/``_FakeCatalog``) and anti-joins the whole
    tenant afterward -- the ordering contract (F8c: retract manifest THEN
    delete chunk) proven against the real server, not a simulation of it.
    """
    from tests._catalog_fixture_ops import ActiveCatalog
    from tests._engine_substrate import ensure_engine
    import nexus.db.http_vector_client as hvc
    from nexus.indexer import _prune_misclassified_in_collection

    state = ensure_engine()
    tenant = t2_service_env
    cat = ActiveCatalog()
    client = hvc.HttpVectorClient(tenant=tenant)

    dangling_before = _dangling_count(state, tenant)

    content = f"o8dil7 prune-misclassified fixture content {tenant}"
    chash = hashlib.sha256(content.encode()).hexdigest()

    # The document's catalog registration says it lives in _RIGHT_COLL, and
    # its manifest references `chash` there...
    owner = cat.register_owner("o8dil7-prune-owner", "curator")
    tumbler = cat.register(
        owner, "o8dil7-prune-doc", content_type="code",
        physical_collection=_RIGHT_COLL, meta={},
    )
    doc_id = str(tumbler)
    cat.append_manifest_chunks(
        doc_id, [{"chash": chash, "position": 0}], collection=_RIGHT_COLL,
    )
    cat.resync_chunk_count_cache(doc_id)

    assert _manifest_row_count(state, tenant, doc_id) == 1, (
        "precondition: the doc's manifest row was actually seeded"
    )

    # ...but the SAME chash is ALSO physically present in _WRONG_COLL -- the
    # misclassified-collection shape this prune exists to clean up (a stale
    # write into the wrong collection that a later re-index never swept).
    client.upsert_chunks_with_embeddings(
        _WRONG_COLL, ids=[chash], documents=[content], embeddings=[],
        metadatas=[{"title": "o8dil7-prune-wrong-copy", "chunk_text_hash": chash}],
    )

    file_path = Path(f"/fake/o8dil7/{doc_id}.py")
    col = client.get_collection(_WRONG_COLL)

    pruned = _prune_misclassified_in_collection(
        col, {file_path}, {file_path: doc_id}, kind="code",
        catalog=cat, catalog_writer=cat,
    )

    assert pruned == 1, (
        "precondition: the prune actually removed the misclassified chunk "
        "-- the anti-join below would pass on an empty set. If this fails "
        "with pruned == 0, check whether the manifest-guard write "
        "(atomic_manifest_replace) is being refused -- see this bead's own "
        "RED-finding note in T2 nexus/rdr-191-gate2-tripwire-rebuild-2026-08-13 "
        "if this looks like a live producer, not a fixture bug."
    )

    dangling_after = _dangling_count(state, tenant)
    assert dangling_after == dangling_before, (
        "_prune_misclassified_in_collection must retract the manifest "
        "reference BEFORE deleting the T3 chunk (F8c) -- a dangling row "
        "here means the manifest-first ordering was violated against the "
        "REAL server, not just a simulation of it"
    )


def test_prune_misclassified_shared_chash_second_live_document_protected(t2_service_env):
    """nexus-tbj48: the F8c client-side retraction and the F10c server-side
    still-referenced anti-join must COOPERATE on the SAME call when the
    pruned chash is shared with a second LIVE document -- the case the
    sibling test above cannot exercise (its fixture's chash has exactly one
    owner).

    Two documents, A and B, both manifest the SAME chash under the SAME
    collection (RDR-108 content-addressed collapse: identical chunk text in
    one collection shares a single physical row). Only A's file is in THIS
    prune call's ``target_paths`` -- B is a genuine bystander, invisible to
    ``_prune_misclassified_in_collection``'s own ``chash_to_docs`` ground
    truth (scoped to the doc_ids handed to this call, not catalog-wide, per
    that function's own docstring). The prune must:

    1. retract A's OWN manifest reference to the shared chash (F8c) -- the
       chash must be resolvable-free (gone) from A's manifest afterward,
       regardless of whether the physical delete succeeds,
    2. leave B's manifest reference to the shared chash completely
       untouched,
    3. leave the shared chunk PHYSICALLY INTACT, because the server's own
       anti-join (F10c) still sees B's live reference and refuses the
       delete -- this is the assertion the sibling test above cannot make,
       since its chash has no second owner,
    4. produce zero dangling manifest rows afterward (global invariant),
    5. report the SERVER's actual (refused) delete count, not the
       requested one -- bead nexus-tbj48's own third named assertion
       ("the returned delete count reflects the SERVER's actual count, not
       the requested one"), the specific case it was filed to catch: a
       ``_batched_delete`` count-honesty regression (returning
       ``len(ids)`` instead of the server's actual deleted count) is
       invisible unless a partial refusal actually occurs, which is
       exactly this scenario (substantive-critic Significant, T2
       ``nexus/critique-gate2-tripwire-7ygt0-2026-08-13`` [22388]).
    """
    from tests._catalog_fixture_ops import ActiveCatalog
    from tests._engine_substrate import ensure_engine
    import nexus.db.http_vector_client as hvc
    from nexus.indexer import _prune_misclassified_in_collection

    state = ensure_engine()
    tenant = t2_service_env
    cat = ActiveCatalog()
    client = hvc.HttpVectorClient(tenant=tenant)

    dangling_before = _dangling_count(state, tenant)

    content = f"o8dil7-tbj48 shared-chash fixture content {tenant}"
    chash = hashlib.sha256(content.encode()).hexdigest()

    owner = cat.register_owner("o8dil7-tbj48-owner", "curator")
    tumbler_a = cat.register(
        owner, "o8dil7-tbj48-doc-a", content_type="code",
        physical_collection=_SHARED_COLL, meta={},
    )
    tumbler_b = cat.register(
        owner, "o8dil7-tbj48-doc-b", content_type="code",
        physical_collection=_SHARED_COLL, meta={},
    )
    doc_a = str(tumbler_a)
    doc_b = str(tumbler_b)
    cat.append_manifest_chunks(
        doc_a, [{"chash": chash, "position": 0}], collection=_SHARED_COLL,
    )
    cat.append_manifest_chunks(
        doc_b, [{"chash": chash, "position": 0}], collection=_SHARED_COLL,
    )
    cat.resync_chunk_count_cache(doc_a)
    cat.resync_chunk_count_cache(doc_b)

    client.upsert_chunks_with_embeddings(
        _SHARED_COLL, ids=[chash], documents=[content], embeddings=[],
        metadatas=[{"title": "o8dil7-tbj48-shared", "chunk_text_hash": chash}],
    )

    assert _manifest_chashes(state, tenant, doc_a) == {chash}, (
        "precondition: doc A's manifest references the shared chash"
    )
    assert _manifest_chashes(state, tenant, doc_b) == {chash}, (
        "precondition: doc B's manifest references the shared chash -- the "
        "F10c protection check below would pass on an empty set without "
        "this second live owner"
    )
    present = client.get_collection(_SHARED_COLL).get(ids=[chash], include=[])
    assert chash in (present.get("ids") or []), (
        "precondition: the shared chunk physically exists before the prune runs"
    )

    # Only A's file is being pruned -- B is a bystander, not a target.
    file_path = Path(f"/fake/o8dil7-tbj48/{doc_a}.py")
    col = client.get_collection(_SHARED_COLL)

    pruned = _prune_misclassified_in_collection(
        col, {file_path}, {file_path: doc_a}, kind="code",
        catalog=cat, catalog_writer=cat,
    )

    assert pruned == 0, (
        "nexus-tbj48's own count-honesty assertion: the shared chunk is "
        "refused server-side (F10c, doc B still references it), so the "
        "ACTUAL number of chunks removed by this call is zero -- a "
        "_batched_delete regression that reports len(requested_ids) instead "
        "of the server's real count would read pruned == 1 here despite "
        "nothing having actually been deleted, and this is the one "
        "scenario in this file where that regression is observable at all "
        f"(a partial refusal genuinely occurred). Got: {pruned!r}"
    )
    assert chash not in _manifest_chashes(state, tenant, doc_a), (
        "F8c: document A's OWN manifest reference to the shared chash must "
        "be retracted by the prune, independent of whether the physical "
        "chunk delete itself was refused -- a chash still listed here after "
        "the prune means the client-side retraction step did not run (or "
        "silently failed), which is a live producer finding, not a passing "
        "detector"
    )
    assert _manifest_chashes(state, tenant, doc_b) == {chash}, (
        "document B's manifest reference must be completely untouched by a "
        "prune call that never named B's doc_id"
    )
    present_after = client.get_collection(_SHARED_COLL).get(ids=[chash], include=[])
    assert chash in (present_after.get("ids") or []), (
        "F10c: the shared chunk must physically SURVIVE -- document B's "
        "still-live manifest reference (collection-scoped, matching the "
        "collection this prune call operates on) must protect it from A's "
        "prune, even though A's own reference was correctly retracted"
    )

    dangling_after = _dangling_count(state, tenant)
    assert dangling_after == dangling_before, (
        "no dangling manifest row must result: A's reference is gone, B's "
        "reference still resolves against the still-present chunk"
    )


def test_negative_control_tombstoned_owner_genuine_ghost_row_excluded(t2_service_env):
    """substantive-critic Critical (T2
    ``nexus/critique-gate2-tripwire-7ygt0-2026-08-13`` [22388]):
    ``_dangling_count``'s ``d.deleted_at IS NULL`` predicate -- the single
    load-bearing distinction T2 ``nexus/rdr-191-dangling-definition-of-
    record`` [22364] exists to establish -- had ZERO kill control anywhere
    in this file (this module's own original docstring admitted it
    directly: "though it happened not to matter for this file's own
    fixtures... no test here tombstones anything").

    This is the true negative-direction twin of
    ``test_inverted_control_handseeded_dangling_row_detected`` below: seeds
    (via the REAL production write paths -- ``register``/
    ``append_manifest_chunks``/``delete_document``, not a raw SQL UPDATE
    for the tombstone) a manifest row referencing a chash with NO backing
    chunk row anywhere, then tombstones its owning document. A genuine
    class-(b) row, per T2 [22364]'s three-way partition, distinct from the
    class-(a) ghost the positive control below hand-seeds under a LIVE
    owner.

    Asserts BOTH directions: ``_dangling_count`` (definition-(a)-scoped)
    must NOT move -- proves the exclusion fires -- while
    ``_dangling_count_any_owner_state`` (the raw, tombstone-blind twin) on
    the SAME state WOULD see it move by exactly +1 -- proves the exclusion
    itself, not fixture emptiness or a counter that cannot see anything, is
    what is doing the discriminating work.

    RDR-109 note (nexus-7ygt0 fallout, corrected): the ``coll`` literal
    below deliberately uses the bare legacy Voyage model segment (no
    "context"/"code" infix) rather than either of the two 1024-dim cloud
    embedder tokens this repo's naming convention normally pairs with
    "voyage" -- ``nexus.db.reconcile._MODEL_DIMS`` still maps this bare
    token to 1024 dims (it is the legacy pre-RDR-103 STORAGE-ROUTING entry,
    excluded only from the NAME-MINTING canonical set, per that module's
    own docstring), so this stays a real, dim-router-recognized
    RDR-103-shaped name -- it just does not match the two-token infix
    pattern ``test_mode_declarations_are_explicit.py`` scans for. No
    ``cloud_mode`` declaration needed, and this test stays plain
    ``integration``-marked so the nightly local-service-gate sweep (which
    excludes ``cloud_mode``) still collects it -- the whole point of a
    negative-direction kill control (coordinator correction, nexus-7ygt0
    fallout round 2: a ``cloud_mode`` declaration here would have vacuated
    exactly the scheduled coverage the critic's Critical existed to
    restore).
    """
    from tests._catalog_fixture_ops import ActiveCatalog
    from tests._engine_substrate import ensure_engine

    state = ensure_engine()
    tenant = t2_service_env
    cat = ActiveCatalog()

    dangling_before = _dangling_count(state, tenant)
    raw_before = _dangling_count_any_owner_state(state, tenant)

    coll = "code__o8dil7-control-tombstone__voyage-3__v1"
    ghost_chash = hashlib.sha256(f"o8dil7-control-tombstone-ghost-{tenant}".encode()).hexdigest()

    owner = cat.register_owner("o8dil7-control-tombstone-owner", "curator")
    tumbler = cat.register(
        owner, "o8dil7-control-tombstone-doc", content_type="code",
        physical_collection=coll, meta={},
    )
    doc_id = str(tumbler)
    # Genuinely a class-(b) ghost: ghost_chash resolves in NO chunk table
    # anywhere (never uploaded to T3) -- same construction as the positive
    # control below, except this owner is tombstoned next.
    cat.append_manifest_chunks(
        doc_id, [{"chash": ghost_chash, "position": 0}], collection=coll,
    )
    cat.resync_chunk_count_cache(doc_id)

    assert _manifest_row_count(state, tenant, doc_id) == 1, (
        "precondition: the ghost manifest row was actually seeded"
    )

    # Real production tombstone path, not a raw UPDATE -- matches the
    # sibling Java negative control's own discipline.
    cat.delete_document(doc_id)

    assert _manifest_row_count(state, tenant, doc_id) == 1, (
        "precondition: soft-tombstone deliberately leaves the manifest row "
        "in place -- the assertions below would pass vacuously on an empty "
        "set otherwise"
    )

    assert _dangling_count(state, tenant) == dangling_before, (
        "class-(b) EXCLUSION: a manifest row whose owner is tombstoned must "
        "NOT move the definition-(a) dangling counter, even though it is a "
        "genuine ghost (no backing chunk row anywhere) -- this is GATE-2's "
        "own ruled definition, not an oversight"
    )
    assert _dangling_count_any_owner_state(state, tenant) == raw_before + 1, (
        "KILL CONTROL: the SAME row, read by the raw owner-tombstone-BLIND "
        "counter, must move by exactly +1 -- proves the exclusion above is "
        "the discriminating term (not fixture emptiness, not a counter that "
        "cannot see anything)"
    )


def test_inverted_control_handseeded_dangling_row_detected(t2_service_env):
    """Non-vacuity control for THIS file's own anti-join helper: hand-seed
    (superuser, bypassing RLS -- the same privilege level every fixture
    write in this file already uses) a genuinely dangling manifest row via
    raw SQL, with a CORRELATION PIN (one unrelated live chunk in the same
    collection, ``RekeyOpsIntegrationTest.java:1037-1071`` precedent) so a
    de-correlated rewrite of the anti-join (e.g. "the chunk table is empty")
    cannot pass by accident. Proves ``_dangling_count`` actually
    discriminates a bad row from a good one, rather than passing vacuously
    -- no code path in ``_prune_misclassified_in_collection`` is exercised
    here; this test is entirely about the DETECTOR, not the producer.

    The NULL-collection axis from the predecessor's inverted control is NOT
    ported: ``catalog_document_chunks.collection`` is ``NOT NULL``
    (catalog-025) -- a raw INSERT with ``collection = NULL`` fails at the
    database with a constraint violation, not a graceful "also dangling"
    outcome, so there is nothing left on that axis for this control to
    demonstrate.

    RDR-109 note (nexus-7ygt0 fallout, corrected): same ``voyage-3`` rename
    as the negative control above, same reason -- it is still the real
    1024-dim token (``nexus.db.reconcile._MODEL_DIMS["voyage-3"] == 1024``),
    which is why the correlation pin below stays on ``chunks_1024``/
    ``vec_1024`` unchanged: the collection's dim-router identity is
    preserved exactly, only the RDR-109-scanned literal changed. No
    ``cloud_mode`` declaration -- this test stays plain
    ``integration``-marked so the nightly local-service-gate sweep collects
    it (coordinator correction, nexus-7ygt0 fallout round 2).
    """
    from tests._engine_substrate import ensure_engine

    state = ensure_engine()
    tenant = t2_service_env

    dangling_before = _dangling_count(state, tenant)

    coll = "code__o8dil7-control__voyage-3__v1"
    ghost_chash = hashlib.sha256(f"o8dil7-control-ghost-{tenant}".encode()).hexdigest()
    pin_chash = hashlib.sha256(f"o8dil7-control-pin-{tenant}".encode()).hexdigest()
    doc_id = f"o8dil7-control-doc-{tenant}"
    vec_1024 = "[" + ",".join(["0.1"] * 1024) + "]"

    _psql(state, (
        "INSERT INTO nexus.catalog_collections (tenant_id, name) "
        f"VALUES ('{tenant}', '{coll}') ON CONFLICT DO NOTHING"
    ))
    # Correlation pin: one unrelated LIVE chunk in the same collection, so
    # the anti-join cannot pass by "the chunk table happens to be empty".
    _psql(state, (
        "INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, embedding_1024) "
        f"VALUES ('{tenant}', '{coll}', decode('{pin_chash}', 'hex'), 'pin', '{vec_1024}'::vector)"
    ))
    _psql(state, (
        "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, physical_collection) "
        f"VALUES ('{tenant}', '{doc_id}', 'o8dil7 control doc', '{coll}') "
        "ON CONFLICT DO NOTHING"
    ))
    # Genuinely dangling: ghost_chash resolves in no chunk table, and the
    # owning document is LIVE (no deleted_at set above).
    _psql(state, (
        "INSERT INTO nexus.catalog_document_chunks (tenant_id, doc_id, position, chash, collection) "
        f"VALUES ('{tenant}', '{doc_id}', 0, decode('{ghost_chash}', 'hex'), '{coll}')"
    ))

    dangling_after = _dangling_count(state, tenant)
    assert dangling_after == dangling_before + 1, (
        "control: the hand-seeded ghost row must move the dangling counter "
        "by exactly +1 -- proves the anti-join discriminates rather than "
        "passing vacuously on an empty table"
    )
