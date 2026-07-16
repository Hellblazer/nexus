#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Seed a LEGACY on-disk Chroma store — the pre-cutover state a real user has.

`make_t3()` returns the service client post-RDR-155-P4a (no local-write escape
hatch), so the only faithful way to produce the migration SOURCE is to write the
Chroma PersistentClient on disk directly, exactly as a pre-cutover install left
it. `nx migrate-to-service --local-path <here>` then detects + ETLs it.

Chunk shape mirrors the repo convention (tests/migration/test_vector_etl.py):
id = sha256(text)[:32] (the chash; round-trips verbatim into pgvector.chash),
documents = the text the service RE-EMBEDS (source vectors are never read by the
ETL), metadata = {position, tag}. Two conformant collections:

  knowledge__rehearsal__minilm-l6-v2-384__v1   (ONNX leg — re-embedded locally)
  knowledge__rehearsal__voyage-context-3__v1   (cloud leg — re-embedded via Voyage)

Usage: seed_legacy.py <chroma_path> [--with-cloud] [--era-hop] [--n N]
       seed_legacy.py <chroma_path> --blocking=collision|pregate [--n N]
       seed_legacy.py <chroma_path> --remove-blocking[=collision|pregate]

Prints one JSON line: {"collections": {name: count, ...}} for the driver to assert
(--blocking prints {"blocking": {...}}; --remove-blocking prints {"removed": [...]}).

--era-hop (RDR-185 P4.3) layers the GH #1408 work-instance shape onto the main
seed: pre-RDR-108 16-char chunk ids as FULL catalog/T2 citizens, including a
store_put-only note that has no source content to re-index. The ladder's
substrate rung must converge these on the wire, so the manifest gains
legacy_ids (what must no longer exist), expected_reid (the exact conformant
chashes the wire transform must produce) and sourceless.
"""
from __future__ import annotations

import hashlib
import json
import os

# Build the legacy T2 + catalog stores as raw SQLite, never the service backend
# — these ARE the migration source a pre-cutover (pre-5.10) nx left on disk.
# Set before importing nexus.db so storage_backend_for() resolves to SQLITE.
# Isolated to this process; the migrate command runs separately in service mode.
os.environ["NX_STORAGE_BACKEND"] = "sqlite"

import sys
from pathlib import Path

import chromadb

_MINILM = "knowledge__rehearsal__minilm-l6-v2-384__v1"
# nexus-pi3s3: the voyage source is a SAME-MODEL passthrough (copied byte-for-byte
# into a voyage-mode service). Its name MUST NOT collide with the minilm→voyage
# cross-model remap target: in voyage mode (--with-cloud, voyage_key_present) the
# migrate re-embeds _MINILM (knowledge/voyage) into knowledge__rehearsal__voyage-
# context-3__v1 (detection.cross_model_target_model). A distinct version segment
# (__v2) keeps a single conformant owner ("rehearsal") while avoiding that clash.
_VOYAGE = "knowledge__rehearsal__voyage-context-3__v2"
# RDR-162 P2: a SOURCELESS store_put-style note — a minilm-384 collection with
# NO backing source file (only a topic_assignment references it). embed_migrate
# (re-reads source files) cannot upgrade it; the cross-model migrate re-embeds
# its STORED text and re-points the assignment to the bge-768 target. This is the
# case that motivated RDR-162.
_NOTE = "knowledge__rehearsal-note__minilm-l6-v2-384__v1"

# nexus-itme7 shape (iii): a pre-RDR-109 MISLABELED collection — voyage-NAMED,
# but its stored vectors are 768-dim local ONNX. Classification measures a
# stored vector (nexus-nb7hr / nexus-x7t5y) and cross-model-remaps it to the
# bge-768 target UNCONDITIONALLY — in voyage mode too (remap_target_model
# returns the local ONNX model for measured-768 content; vectors that were
# never voyage must never bill a voyage re-embed). Part of the MAIN seed:
# this shape MIGRATES (success phase), unlike the --blocking shapes below.
_MISLABEL = "knowledge__rehearsal-mislabel__voyage-context-3__v1"

# nexus-itme7 pre-write BLOCK shapes (GH #667/#1381/#1390 field classes).
# Seeded ONLY via --blocking=<group>; NEVER entered into the chashes dict, so
# _seed_t2_and_catalog never sees them (the guards fire at classification /
# collision-audit time, before any catalog row would matter).
#   (i)  token-less 2-segment name: dim MUST NOT be 768 (the measured-dim
#        override would rescue it into a remap) and ids MUST be 32-char
#        (16-char would mis-attribute the block to legacy_ids).
#   (ii) SUPPORTED-model name with pre-RDR-108 16-char chunk ids (the
#        canon-chat shape): the legacy-id probe blocks BEFORE model support.
#   (iv) collision pair: the stale voyage-named half MUST hold real 768-dim
#        vectors — the measured-dim override remaps it onto the honest
#        sibling's name (target-name collision). A non-768 half would instead
#        trip guided-upgrade's step-2a voyage-capability gate and exit with
#        the wrong diagnostic.
_LEGACYBARE = "knowledge__legacybare"
_SHORTID = "knowledge__rehearsal-shortid__bge-base-en-v15-768__v1"
_PAIR_HONEST = "knowledge__rehearsal-pair__bge-base-en-v15-768__v1"
_PAIR_STALE = "knowledge__rehearsal-pair__voyage-context-3__v1"

# RDR-185 P4.3 (nexus-n7u38.30): the ERA-HOP shapes — the 2026-07-16
# work-instance (GH #1408) footprint, which the LADDER converges rather than
# blocks. Distinct from the _BLOCKING shapes above in one load-bearing way:
# these are FULL CITIZENS (entered into the chashes dict, so
# _seed_t2_and_catalog writes catalog manifests + topic assignments keyed by
# their LEGACY chashes). The blocking shapes are deliberately excluded from
# T2/catalog because they only ever need to trip a pre-write guard — nothing
# downstream of them exists. Under RDR-185 the legacy ids CONVERGE, so the
# old->new map has to cascade through every chash-bearing store; a legacy
# collection with no manifest rows would make that cascade vacuous and let a
# broken cascade pass.
#
#   _ERA_LEGACY  file-backed, 16-char (pre-RDR-108) ids. Its catalog manifest
#                carries the legacy chashes -> the cascade must remap them or
#                the post-migration orphan scan finds every row dangling.
#   _ERA_NOTE    the incident's hard case: store_put-only (NO catalog file
#                document, only a topic_assignment) AND 16-char ids. It has no
#                source content, so re-indexing it is IMPOSSIBLE — the printed
#                remedy that made GH #1408 a dead end. ONLY wire re-id can
#                converge it, and its topic_assignment's doc_id is a legacy
#                chash the cascade must re-point.
_ERA_LEGACY = "knowledge__rehearsal-era__bge-base-en-v15-768__v1"
_ERA_NOTE = "knowledge__rehearsal-era-note__minilm-l6-v2-384__v1"

#: Collections with NO backing source file — only a topic_assignment references
#: them. `_seed_t2_and_catalog` skips the catalog document for these and seeds
#: the assignment instead. Keeping this a SET (not an `== _NOTE` check) is what
#: lets the era-hop add its own sourceless shape without the note-handling
#: silently applying to only one of them.
_SOURCELESS: frozenset[str] = frozenset({_NOTE, _ERA_NOTE})

#: blocking collection -> (seed text prefix, vector dim, chunk-id length)
_BLOCKING_SPEC: dict[str, tuple[str, int, int]] = {
    _LEGACYBARE: ("bare legacy chunk", 2, 32),
    _SHORTID: ("short id chunk", 2, 16),
    _PAIR_HONEST: ("pair honest chunk", 2, 32),
    _PAIR_STALE: ("pair stale chunk", 768, 32),
}

#: --blocking group -> collections. Per-shape granularity (plan-audit F1): the
#: collision guard fires BEFORE the sequencer pregate, so one guided-upgrade
#: run can emit only ONE of the two block types — Phase 0 seeds them in
#: SEPARATE sub-runs.
_BLOCKING_GROUPS: dict[str, tuple[str, ...]] = {
    "collision": (_PAIR_HONEST, _PAIR_STALE),
    "pregate": (_LEGACYBARE, _SHORTID),
}

# The model the cross-model migrate re-embeds the minilm sources into. This is
# MODE-AWARE (nexus-pi3s3, mirrors detection.cross_model_target_model): a voyage-
# mode service (--with-cloud, voyage_key_present) re-embeds knowledge collections
# into voyage-context-3; a local bge-768 service re-embeds into bge-base-en-v15-768.
# A stale unconditional bge-768 here made the voyage-mode parity assert the wrong
# target collection (service=0 [MISMATCH] false negative).
_BGE_MODEL = "bge-base-en-v15-768"
_VOYAGE_CTX_MODEL = "voyage-context-3"  # knowledge content-type → voyage-context-3


def _remap_model(source: str, model: str) -> str:
    """Swap the model segment of a conformant 4-segment collection name."""
    seg = source.split("__")
    seg[2] = model
    return "__".join(seg)


def _chash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:32]


def _seed(
    client, name: str, n: int, *, prefix: str, dim: int = 2, id_len: int = 32
) -> list[str]:
    """Seed a legacy Chroma collection; return the chunk chashes (ids).

    ``dim`` (nexus-pi3s3): the CROSS-MODEL re-embed legs (minilm→bge/voyage) never
    read the source vector — the ETL re-embeds the documents server-side — so a
    nonsensical 2-dim stub suffices (matches the repo's ETL fixtures). The
    SAME-MODEL voyage passthrough is different: it COPIES the stored vector
    byte-for-byte into chunks_1024, so its source vectors must be the real
    dimension (1024) or the service's RDR-156 schema guard rejects the upsert
    ("embedder produced a 2-dim vector ... dispatches to chunks_1024"). Values are
    irrelevant (parity asserts COUNT, not similarity) — only the dim matters.

    ``id_len`` (nexus-itme7): shape (ii) seeds pre-RDR-108 16-char chunk ids
    (``sha256[:16]``, the GH #1390 canon-chat era) so the legacy-id pregate
    block fires. Everything else keeps the 32-char chash identity.
    """
    texts = [f"{prefix} {i:04d}" for i in range(n)]
    ids = [_chash(t)[:id_len] for t in texts]
    col = client.get_or_create_collection(name)
    col.add(
        ids=ids,
        documents=texts,
        metadatas=[{"position": i, "tag": "rehearsal"} for i in range(n)],
        embeddings=[[float(i)] + [1.0] * (dim - 1) for i in range(n)],
    )
    return ids


def _seed_t2_and_catalog(collections: dict[str, list[str]]) -> dict[str, int]:
    """Build the legacy T2 memory.db (one note) + a catalog-CONSISTENT footprint.

    migrate-to-service sequences T2 → catalog → T3. The validation gate refuses
    to unlock when the migrated catalog is empty (orphan check would be vacuous —
    a false pass). So for each seeded Chroma collection we register a catalog
    document and write its document_chunks manifest referencing the SAME chashes,
    making the post-migration orphan scan (catalog manifest ⨝ pgvector chash)
    meaningful. Returns {"t2_notes": N, "catalog_docs": M}.
    """
    from nexus.config import nexus_config_dir
    from nexus.db.t2 import T2Database

    cfg = nexus_config_dir()
    cfg.mkdir(parents=True, exist_ok=True)
    db = T2Database(cfg / "memory.db", run_migrations=True)
    db.memory.put(
        project="rehearsal", title="legacy-note",
        content="pre-cutover note", tags="rehearsal", ttl=0,
    )

    # RDR-162 P2: a SOURCELESS note assignment — a topic + a topic_assignment
    # whose ``source_collection`` is the note collection, with NO catalog file
    # document. The cross-model migrate must re-point this assignment to the
    # bge-768 target so the post-migration taxonomy-consistency check resolves.
    #
    # RDR-185 P4.3: for the era-hop's _ERA_NOTE the assignment's doc_id is a
    # LEGACY (16-char) chash, so the rung's remap cascade must re-point the
    # doc_id as well as the collection. topic_assignments is exactly the store
    # RDR-180's original inventory missed (RDR-180 Failure Modes) and the .13
    # audit re-found — seeding it with a legacy key is what makes that leg of
    # the cascade falsifiable here.
    for note_coll in sorted(_SOURCELESS & set(collections)):
        tax = db.taxonomy
        label = f"{note_coll.split('__')[1]}-topic"
        tax.conn.execute(
            "INSERT INTO topics (label, collection, doc_count, created_at) "
            "VALUES (?, ?, ?, ?)",
            (label, note_coll, 1, "2026-06-18T00:00:00Z"),
        )
        topic_id = tax.conn.execute(
            "SELECT id FROM topics WHERE collection = ?", (note_coll,)
        ).fetchone()[0]
        tax.conn.execute(
            "INSERT INTO topic_assignments "
            "(doc_id, topic_id, assigned_by, source_collection) "
            "VALUES (?, ?, 'manual', ?)",
            (collections[note_coll][0], topic_id, note_coll),
        )
        tax.conn.commit()

    from nexus.catalog.catalog import Catalog

    cat_dir = cfg / "catalog"
    cat_dir.mkdir(parents=True, exist_ok=True)
    cat = Catalog.init(cat_dir) if not (cat_dir / ".catalog.db").exists() \
        else Catalog(cat_dir, cat_dir / ".catalog.db")

    repo_root = "/tmp/rehearsal-src"
    Path(repo_root).mkdir(parents=True, exist_ok=True)
    owner = cat.register_owner(
        "rehearsal", "project", repo_hash="rehearsal01", repo_root=repo_root,
    )

    # nexus-qeoxf: register EVERY seeded collection in catalog_collections
    # (RDR-103, the collection-name authority), mirroring a real pre-cutover
    # install. The cross-model migrate's reference cascade renames the collection
    # via POST /v1/catalog/collections/rename, which the service 404s when the
    # source is absent from catalog_collections (handleCollectionRename ->
    # repo.collectionExists == false). A real RDR-103 user HAS these rows (the
    # catalog ETL migrates the `collections` table), so the rehearsal must seed
    # them too — else it injects a spurious non-fatal cascade 404 that does not
    # occur in production. Includes _NOTE: sourceless as a DOCUMENT, but still a
    # registered COLLECTION. Names are conformant 4-segment
    # (<content_type>__<owner>__<model>__v<n>); supply the segments so they
    # round-trip exactly.
    for coll in collections:
        seg = coll.split("__")
        cat.register_collection(
            coll,
            content_type=seg[0],
            owner_id=seg[1],
            embedding_model=seg[2],
            model_version=seg[3],
        )

    docs = 0
    for coll, chashes in collections.items():
        # The SOURCELESS cases: no catalog file document, only the
        # topic_assignment seeded above references them.
        if coll in _SOURCELESS:
            continue
        fp = f"{repo_root}/{coll}.md"
        Path(fp).write_text("rehearsal legacy doc\n")
        doc = cat.register(
            owner, coll, content_type="knowledge", file_path=fp,
            physical_collection=coll, chunk_count=len(chashes),
        )
        cat.write_manifest(
            str(doc),
            [
                {"chash": c, "position": i, "line_start": None,
                 "line_end": None, "char_start": None, "char_end": None}
                for i, c in enumerate(chashes)
            ],
        )
        docs += 1
    return {"t2_notes": 1, "catalog_docs": docs}


def _blocking_group(args: list[str], flag: str) -> tuple[str, ...] | None:
    """Resolve ``--blocking=<group>`` / ``--remove-blocking[=<group>]`` args.

    Returns the group's collections, or ``None`` when *flag* is absent. A bare
    ``--remove-blocking`` resolves to ALL blocking collections (cleanup form);
    a bare ``--blocking`` is refused — seeding both groups in one store would
    let the collision guard mask the pregate (one run emits exactly ONE block
    type, plan-audit F1), silently making the pregate assertions vacuous.
    Unknown groups exit loud (2) for the same reason.
    """
    for a in args:
        if a == flag:
            if flag == "--blocking":
                print(
                    "--blocking requires a group: --blocking=collision|pregate "
                    "(one guided-upgrade run can emit only ONE block type)",
                    file=sys.stderr,
                )
                raise SystemExit(2)
            return tuple(_BLOCKING_SPEC)
        if a.startswith(flag + "="):
            group = a.split("=", 1)[1]
            if group not in _BLOCKING_GROUPS:
                print(
                    f"unknown {flag} group {group!r} "
                    f"(choose from {sorted(_BLOCKING_GROUPS)})",
                    file=sys.stderr,
                )
                raise SystemExit(2)
            return _BLOCKING_GROUPS[group]
    return None


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(
            "usage: seed_legacy.py <chroma_path> [--with-cloud] [--era-hop] [--n N]\n"
            "       seed_legacy.py <chroma_path> --blocking=collision|pregate [--n N]\n"
            "       seed_legacy.py <chroma_path> --remove-blocking[=collision|pregate]",
            file=sys.stderr,
        )
        return 2
    path = args[0]
    with_cloud = "--with-cloud" in args
    era_hop = "--era-hop" in args
    if era_hop and with_cloud:
        # The era shapes are seeded 768/384-dim local content whose remap target
        # is always the bge-768 name; a voyage-only service has no bge embedder
        # and 422s the leg (the same incoherence _MISLABEL documents above).
        print("--era-hop and --with-cloud are incoherent (era shapes remap onto "
              "the local bge-768 target; cloud is voyage-only)", file=sys.stderr)
        return 2
    n = 12
    if "--n" in args:
        n = int(args[args.index("--n") + 1])

    client = chromadb.PersistentClient(path=path)

    # nexus-itme7 blocking modes — early return BEFORE the T2/catalog seeding:
    # the block shapes must never enter the chashes dict (no catalog document,
    # no manifest, no T2 note) and these modes make zero config-dir writes, so
    # they are trivially sanity-runnable outside the container. NOTE: the
    # blocking shapes alone are NOT a runnable guided-upgrade fixture —
    # migrate_cmd's T2/catalog existence pre-check fires before any guard, so
    # Phase 0 layers them ON TOP of the main seed's footprint.
    blocking = _blocking_group(args, "--blocking")
    removing = _blocking_group(args, "--remove-blocking")
    if blocking is not None and removing is not None:
        print("--blocking and --remove-blocking are mutually exclusive", file=sys.stderr)
        return 2
    if blocking is not None:
        seeded_blocking: dict[str, int] = {}
        for bname in blocking:
            prefix, dim, id_len = _BLOCKING_SPEC[bname]
            seeded_blocking[bname] = len(
                _seed(client, bname, n, prefix=prefix, dim=dim, id_len=id_len)
            )
        print(json.dumps({"blocking": seeded_blocking}))
        return 0
    if removing is not None:
        removed: list[str] = []
        for bname in removing:
            try:
                client.delete_collection(bname)
            except Exception:  # noqa: BLE001 — absent collection: removal is idempotent
                continue
            removed.append(bname)
        print(json.dumps({"removed": removed}))
        return 0

    chashes: dict[str, list[str]] = {}
    chashes[_MINILM] = _seed(client, _MINILM, n, prefix="onnx chunk")
    chashes[_NOTE] = _seed(client, _NOTE, n, prefix="note chunk")
    # RDR-185 P4.3 (nexus-n7u38.30): the ERA-HOP footprint — the GH #1408
    # work-instance shape the ladder must converge UNATTENDED. Layered ON TOP
    # of the main seed (not instead of it): a real era install holds a mix, and
    # a conformant collection migrating beside a legacy one is what proves the
    # rung composes per-collection legs rather than treating the store as one
    # uniform era.
    if era_hop:
        chashes[_ERA_LEGACY] = _seed(
            client, _ERA_LEGACY, n, prefix="era legacy chunk", dim=768, id_len=16,
        )
        chashes[_ERA_NOTE] = _seed(
            client, _ERA_NOTE, n, prefix="era note chunk", id_len=16,
        )
    # Shape (iii): voyage-NAMED, but the stored vectors are real 768-dim — the
    # measured-dim override (nexus-nb7hr/x7t5y) reclassifies it and the migrate
    # re-embeds it into the bge-768 target. Registered in T2/catalog like every
    # other MAIN-seed collection: this one DOES migrate, so the rename cascade
    # and the post-migration orphan scan are meaningful for it.
    #
    # Safe for the LOCAL-mode main-seed callers (rehearse.sh default leg,
    # rehearse_cold.sh, rehearse_hole_punch.sh, rehearse_guided.sh — yaeex
    # critique): they drive the same _run_migration the guided hand-off uses;
    # their parity and rollback-safety checks iterate this manifest generically
    # (cross.get(name, name), no hardcoded counts).
    #
    # NOT seeded on --with-cloud (first with-cloud run post-itme7, 2026-07-13):
    # remap_target_model returns the local ONNX model UNCONDITIONALLY for
    # measured-768 content (measured-ONNX vectors must never bill a voyage
    # re-embed), so the target is always the bge-768 name — which a voyage-mode
    # service refuses with HTTP 422 (no bge embedder), failing the whole leg
    # structurally. The itme7 design scoped the legacy shapes to --guided
    # (amendment 7); the with-cloud leg keeps its original pre-itme7 manifest.
    # The mislabel-on-voyage-service PRODUCT behaviour (pregate should block it
    # up front rather than a mid-flight 422) is tracked separately.
    if not with_cloud:
        chashes[_MISLABEL] = _seed(client, _MISLABEL, n, prefix="mislabel chunk", dim=768)
    if with_cloud:
        # 1024-dim source vectors: the voyage same-model passthrough COPIES them
        # (no re-embed) into chunks_1024 (nexus-pi3s3).
        chashes[_VOYAGE] = _seed(client, _VOYAGE, n, prefix="voyage chunk", dim=1024)
    t2 = _seed_t2_and_catalog(chashes)
    seeded = {name: len(ids) for name, ids in chashes.items()}
    # cross_model: source -> the target the migrate re-embeds into, MODE-AWARE
    # (nexus-pi3s3). voyage_key_present (== with_cloud here) decides the target
    # model exactly as detection.cross_model_target_model does: voyage-context-3
    # in voyage mode, bge-768 in local mode. The voyage source itself is a
    # SAME-MODEL passthrough (NOT remapped) so it is absent from this map; the
    # parity check then verifies it under its own name (cross.get(name, name)).
    _tgt_model = _VOYAGE_CTX_MODEL if with_cloud else _BGE_MODEL
    cross_model = {
        _MINILM: _remap_model(_MINILM, _tgt_model),
        _NOTE: _remap_model(_NOTE, _tgt_model),
    }
    if era_hop:
        # _ERA_NOTE is minilm-384 -> re-embedded into the mode's target model,
        # exactly like _NOTE. _ERA_LEGACY is ALREADY bge-768-named, so it is a
        # same-name leg: only its chunk IDENTITY changes (wire re-id), not its
        # collection. Absent from cross_model => the parity check resolves it
        # under its own name via cross.get(name, name).
        cross_model[_ERA_NOTE] = _remap_model(_ERA_NOTE, _tgt_model)
    if not with_cloud:
        # Shape (iii) is NOT mode-aware: remap_target_model returns the local
        # ONNX model UNCONDITIONALLY for measured-768 content (voyage mode
        # included — measured-ONNX vectors must never bill a voyage re-embed),
        # so the target is always the bge-768 name. Distinct owner segment
        # ("rehearsal-mislabel") keeps it collision-free with every other
        # main-seed target. Skipped on --with-cloud (see the seeding note
        # above): a voyage-mode service cannot embed the bge target.
        cross_model[_MISLABEL] = _remap_model(_MISLABEL, _BGE_MODEL)
    out: dict[str, object] = {"collections": seeded, "cross_model": cross_model, **t2}
    if era_hop:
        # The driver asserts the CONVERGENCE, so it needs the before-state: the
        # exact legacy ids that must no longer exist anywhere post-walk, and the
        # text they were derived from (the rung recomputes sha256(text)[:32] on
        # the wire, so the expected new id is derivable here too — that is the
        # whole point of wire re-id, and it makes the assertion exact rather
        # than a "looks 32-char" shape check).
        out["legacy_ids"] = {
            _ERA_LEGACY: chashes[_ERA_LEGACY],
            _ERA_NOTE: chashes[_ERA_NOTE],
        }
        out["expected_reid"] = {
            _ERA_LEGACY: [_chash(f"era legacy chunk {i:04d}") for i in range(n)],
            _ERA_NOTE: [_chash(f"era note chunk {i:04d}") for i in range(n)],
        }
        out["sourceless"] = sorted(_SOURCELESS)
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
