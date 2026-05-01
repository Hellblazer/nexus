---
title: "Catalog/T3 Metadata Drift: From ART-lhk1 Aspect Skips to RDR-101"
date: 2026-04-30
references:
  rdr: RDR-101
  beads: [ART-lhk1, nexus-3e4s, nexus-p03z, nexus-v9az]
  prs: [#381, #382, #383, #385, #386, #388, #389]
status: post-mortem
---

# Catalog/T3 Metadata Drift: From ART-lhk1 Aspect Skips to RDR-101

## TL;DR

A single aspect-extraction symptom (140 of 140 RDR docs reporting `empty`) unwound into a 60-day pattern of catalog/T3 metadata drift driven by independent writers and a broken path-normalization helper. Four bugs in three days were fixed at four different join sites without ever closing the duplication surface that produced them. RDR-101 (accepted 2026-04-30) replaces the field-ownership architecture with an event-sourced catalog where document identity is a UUID7 surrogate, the log is the only authoritative store, and SQLite plus T3 are deterministic projections. This post-mortem narrates how the symptom chain produced the design.

## Timeline

- **2026-04-08**: PR #134 (RDR-060, "Catalog Path Rationalization and Link Graph Usability") lands. `OwnerRecord` gains `repo_root`; the catalog moves from absolute to relative `file_path` with `_normalize_source_uri` reconstructing absolute URIs at register time. The implementation uses `os.path.abspath()` on the `file_path` argument when it is relative, which silently anchors on the current working directory rather than `owner.repo_root`. This is the latent root cause of nexus-3e4s, dormant for ~3 weeks.
- **2026-04-09 to 2026-04-26**: Cross-project contamination accumulates. Release-shakeout windows produce ~6,500 catalog rows whose `source_uri` points to one repo's tree while attributing the row to a different owner. Largest peaks: 4,452 entries on 2026-04-09 and 1,698 on 2026-04-18. None of this is visible to end users; it is silent attribution drift inside the catalog.
- **2026-04-29 morning**: ART-lhk1 surfaces. `nx enrich aspects rdr__ART-8c2e74c0 --dry-run` reports `by_reason: empty=140` for every RDR catalog row. Initial investigation (also 2026-04-29) reveals three different counts (2,722 chunks vs 114 catalog entries vs 245 aspect-extraction rows) and the cross-project contamination signature.
- **2026-04-29 afternoon**: nexus-3e4s opened (P1) once the contamination scale becomes clear: 22 collections affected, 6,500+ rows, one-way leakage from nexus into other projects' collections. PR #381 ("surface cross-project catalog contamination + correct cost estimate") makes the symptom visible by reporting the `(empty)` skip reason with attribution data instead of swallowing it. PR #382 ("cross-project source_uri guard + relative-path anchor") fixes `_normalize_source_uri` to anchor on `owner.repo_root` when the path is relative, plus adds a register-time guard that rejects file URIs resolving outside the owner's working tree. PR #383 adds `nx catalog audit-membership --all-collections` for sweep mode. PR #385 addresses critique findings on the guard. ~6,500 contaminated rows are touched during live cleanup that day.
- **2026-04-29 evening**: ART-lhk1 verifies fixed end-to-end: `nx enrich aspects rdr__ART-8c2e74c0` extracts 105 of 105 documents (was 140 of 140 skipped). Bead closed.
- **2026-04-30 morning**: nexus-p03z opened (P2). `nx catalog backfill` crashes with `AttributeError: 'NoneType' object has no attribute 'encode'` at `src/nexus/commands/collection.py:490` during `_backfill_chunk_text_hash` because some T3 chunks have `documents[i] is None`. Same bead carries a second issue: the per-file recovery path needed during nexus-3e4s cleanup (rebuild catalog rows from T3 chunk metadata without re-indexing) does not exist. PR #386 ships the one-line None guard. PR #388 ships `nx catalog backfill --from-t3` for explicit per-file recovery from T3 metadata.
- **2026-04-30 midday**: nexus-v9az opened (P2). After PR #388's recovery, `docs__ART-8c2e74c0` catalog rows have relative `file_path` (anchored to `repo_root` per the nexus-3e4s guard) while T3 chunks carry absolute `source_path`. `extract_aspects` builds `chroma://<col>/{file_path}` and the chroma reader matches by `source_path` string equality: relative versus absolute, no match, 365 of 365 empty. Same shape as ART-lhk1, different join site. PR #389 adds a `lookup_path` parameter to `extract_aspects` so the chroma reader resolves the catalog `file_path` to absolute via `owner.repo_root` before joining.
- **2026-04-30 afternoon**: PR #399 cleans up `nx catalog update --source-uri` to emit a clean error rather than a traceback when the new URI resolves outside the owner's tree (the guard from #382 was raising correctly but the CLI surface was ugly).
- **2026-04-30 late afternoon**: a4c16dbd lands the first RDR-101 draft (field-ownership matrix plus phased migration). `nx:substantive-critic` surfaces three critical gaps: `head_hash` is load-bearing for `Catalog.register()` idempotency, JSONL replay reintroduces dropped columns at `CatalogDB.rebuild()`, and the proposed `title` rename collides with already-shipped RDR-096 identity dispatch. 9bde077c rewrites the RDR as a greenfield event-sourced design. 7a2a71e4 remediates the second round of critique findings (3 critical plus 4 significant). 81a982e4 adds Mermaid diagrams. 8db21143 marks RDR-101 accepted (gate PASSED). RDR-101 §Phase 0 schedules this post-mortem.

## Root cause class

Catalog/T3 metadata duplication without referential integrity. The catalog (T2) and T3 (ChromaDB Cloud) both populate `source_path`/`source_uri`, `content_hash`/`head_hash`, `chunk_count`, `title`, and `indexed_at` independently at index time, computed by different code paths, updated through asymmetric mutation surfaces. Nothing enforces equality. Every join across the boundary is a drift surface.

Each episode in the timeline was a different surfacing of the same class:

- **chunk_count drift**: the T3 chunk count is the truth; the catalog `chunk_count` column lags because the indexer hook does not write it back. `code__ART-8c2e74c0` showed 4,182 catalog rows with `chunk_count = 0` against 63,077 actual T3 chunks.
- **source_uri CWD bug** (nexus-3e4s): the catalog's `source_uri` is computed by `_normalize_source_uri` from a relative `file_path` plus an implicit CWD; T3's `source_path` is computed from the indexer's `abs_path`. Two writers, two computations, no equality check. CWD-anchoring was the proximate bug; duplication-without-integrity was the structural one.
- **relative-vs-absolute join key** (nexus-v9az): the nexus-3e4s fix made the catalog correct in the relative-path representation while T3 chunks still hold absolute paths. The join (string equality on `source_path`) breaks because the two stores chose different representations of the same fact.

In every drift episode, T3 was correct and the catalog was wrong. RDR-101 §Research Findings names this directly: T3 is right by accident, not by structure. T3's `source_path` is computed once at index time from the actual filesystem path the indexer touched; the catalog's `source_uri` is computed by a separate normalization function that has bugs. Remove the duplication and neither side can be wrong.

## Why each patch was insufficient

| Patch | What it fixed | What it left intact |
|---|---|---|
| PR #382 (`_normalize_source_uri` anchor + register guard) | The CWD-anchoring bug in one helper; rejected new cross-project rows at the register/update boundary. | Catalog and T3 still hold the same path field independently. The next divergence in representation (which arrived two days later as nexus-v9az) hits a different join site. |
| PR #386 (None guard in backfill) | An unrelated NPE that blocked recovery. | The reason recovery was needed at all (catalog rows wiped during cleanup, T3 chunks intact) is the duplication surface. |
| PR #388 (`backfill --from-t3` per-file recovery) | Reconstructed catalog rows from T3 metadata. Useful exactly because T3 was the more reliable side. | Encodes the pattern "catalog reads from T3 to rebuild itself." Each side reads from the other for recovery; neither is structurally a stable source. |
| PR #389 (`lookup_path` shim in extract_aspects) | The relative-vs-absolute mismatch at the chroma reader's join site. | The path is still stored in two places with two representations. The next code path that joins on `source_path` will need its own shim. |
| PR #399 (clean error on update guard) | UX of the register-time guard. | The guard is necessary because the duplication exists. |

Every patch addressed a symptom; the duplication surface stayed live. The patches accumulated into a system where the catalog and T3 each held partial, asymmetric truth and any new code path joining them was a fresh opportunity for the same class of bug. RDR-101 §Problem Statement Gap 3 names this: "Each side reads from the other for recovery; neither is a stable source. The only structural fix is to stop duplicating."

## The systemic fix (RDR-101)

RDR-101 redesigns the catalog/T3 boundary around three core invariants (RDR-101 §Core invariants). Every document has an immutable surrogate identity (`doc_id`, UUID7) assigned at first registration and never derived from path. An append-only JSONL event log is the only authoritative store; SQLite and T3 are deterministic projections. One canonical fact per attribute: no field exists in two stores.

T3 chunks carry `{chunk_id, doc_id, coll_id, position, chash, content_hash}` and nothing else. The path lives exactly once, in the catalog's `Document` projection, materialized from `DocumentRegistered` and `DocumentRenamed` events. The drift surface for `source_path`/`source_uri` collapses to zero. Renames are events, not re-indexes; cross-owner moves emit `DocumentDeleted + DocumentRegistered` because chunks must physically move to a new owner's collection.

Aspect extraction (the original ART-lhk1 flow) joins via `doc_id`, not `source_path`. RDR-101 §Read paths specifies that `aspect_readers.py:CHROMA_IDENTITY_FIELD` is rewritten in Phase 4 to dispatch on `doc_id` uniformly across collection prefixes, structurally preventing the ART-lhk1 failure mode from recurring at any join site.

The doctor verb becomes deterministic. It replays the event log into an ephemeral expected-state, diffs against actual SQLite, and diffs against actual T3 chunk-sets per collection. Replay-equality is the test gate: any two replays of the same log produce identical SQLite. Today's `nx catalog doctor` uses a per-home dominance heuristic that is right by accident in the same way T3 was right by accident; greenfield doctor is a pure diff.

The migration is six phases (RDR-101 §Implementation Plan), reversible through Phase 4. Phase 5 (deprecated-field removal) is the irreversible commitment, gated on 30+ contiguous days of zero direct-T3-metadata reads in the target field set.

## Lessons for future RDRs

- **When a bug recurs at multiple join sites, the join key itself is fragile.** ART-lhk1, nexus-3e4s, and nexus-v9az were three different join sites failing for the same reason. Patching each site is debt; redesigning the key (path string to surrogate UUID7) is the structural fix. Detection rule: if two patches in two weeks add string-resolution shims at different join sites, stop patching and audit the key.
- **"T3 was right" was an accident of computation order, not a structural property.** Every drift episode showed T3 holding the correct value because its `source_path` was computed once at index time from the actual filesystem path; the catalog's came from a buggy normalization helper. The asymmetry is a code-path accident. Remove the duplication and neither side can be wrong; do not declare the accidentally-correct side authoritative.
- **Phased migrations that keep duplication live during transition keep the bug class live during transition.** The first RDR-101 draft proposed a field-ownership matrix with phased deprecation. The migration window itself is when the next drift episode would land. Greenfield redesign that collapses the duplication surface in one cut is structurally safer than a long migration with the surface live.
- **Substantive critique caught structural gaps the original draft missed.** Three rounds of `nx:substantive-critic` against RDR-101 surfaced `head_hash` idempotency, JSONL replay reintroducing dropped columns, RDR-096 dispatch collision, and Phase 1 synthesis edge cases (tombstoned rows resurrecting silently, alias graphs flattening, empty-`source_uri` legacy rows). Budget AI critique as a gate, not optional polish.
- **Every drift episode was patched at the join site, never at the duplication source.** PR #382 patched `_normalize_source_uri`, PR #389 patched `extract_aspects`, PR #388 worked around the duplication by adding T3-to-catalog recovery. None touched the question "why does this field live in two places?" Future incident response asks the duplication question before patching the join site.
- **Recovery operations that read across the boundary are evidence of a broken boundary.** PR #388's `--from-t3` recovery existed because the catalog could not be reconstructed from its own state; PR #389's `lookup_path` shim existed because T3 reads needed catalog-side path resolution. When recovery in either direction requires the other side, the boundary is a foreign-key without referential integrity.
- **Catalog hygiene incidents compound.** ART-lhk1 surfaced contamination; cleanup created the relative-vs-absolute split; the split broke the next aspect-extraction call; recovery needed a path-resolution shim. Each fix changed invariants enough to surface the next bug. RDR-101's replay-equality test gate is partly a way to make compound failure modes loud rather than letting them ride out the next release.

## Open follow-ups

RDR-101 §Phase 0 (Acceptance and survey) is in flight as of 2026-04-30 under epic `nexus-o6aa`. Phase 0 surveys downstream callers of `entry.source_uri`, `entry.head_hash`, `entry.chunk_count`, and `entry.title`; runs the field-by-field disposition audit on `metadata_schema.py:ALLOWED_TOP_LEVEL`; resolves the `bib_semantic_scholar_id` migration plan, the RDR-086 `chash_index` `doc_id` column collision, and the `chunk_id` generation rule; and lands this post-mortem. Phases 1 through 6 (event-log infrastructure, log synthesis plus T3 backfill, new write path, reader migration, deprecated-surface removal, enforcement) ship over the subsequent release window with the irreversible commitment (Phase 5 default-on) gated on 30+ contiguous days of zero direct-T3-metadata reads in the target field set. Cross-link: epic `nexus-o6aa`; references RDR-004, RDR-060, RDR-086, RDR-087, RDR-096 (partial supersede).
