# RDR-101 Phase 0 deliverables index

Cross-link for Phase 1 implementers. RDR-101 (Event-Sourced Catalog with Immutable Document Identity) was accepted 2026-04-30. Phase 0 (Acceptance and survey) closed with the 7 deliverables linked below. Phase 1 (event log infrastructure) is gated on these merging and on `nexus-o6aa.7` unblocking.

| Bead | Deliverable | PR | Decision |
|---|---|---|---|
| `nexus-o6aa.1` | [Field-by-field disposition audit](rdr-101-field-disposition.md) | #405 | 40 rows, every Phase 5 deletion-list field placed; surfaces 5 RDR-101 design gaps for Phase 1 |
| `nexus-o6aa.2` | [bib_semantic_scholar_id migration plan](rdr-101-bib-disposition.md) | #406 | Option A: bib fields move to Document projection (T2 SQLite) |
| `nexus-o6aa.3` | [RDR-086 chash_index doc_id collision](rdr-101-rdr086-collision.md) | #407 | Option A (rename): `chash_index.doc_id` becomes `chunk_chroma_id` |
| `nexus-o6aa.4` | [chunk_id generation rule](rdr-101-chunk-id-rule.md) | #408 | Option A (UUID7 fresh at `ChunkIndexed`); existing Chroma natural IDs copied verbatim during Phase 1 synthesis |
| `nexus-o6aa.5` | [Downstream caller survey](rdr-101-caller-survey.md) | #409 | 35 read-only call sites, 9 mutating sites, 30 test assertions; flags head_hash idempotency at `register():778-786` and JSONL replay at `rebuild():165-208` |
| `nexus-o6aa.6` | [Direct T3 metadata access survey](rdr-101-t3-direct-access-survey.md) | #410 | 27 production call sites + 22 test files; telemetry recommendation: option Y (per-call-site decoration) |
| `nexus-ov5y` | [nexus-3e4s catalog/T3 drift post-mortem](nexus-3e4s-catalog-t3-drift.md) | #411 | 8 PRs cited; lesson promoted to project-wide guidance: when a bug recurs at multiple join sites, the join key itself is fragile |

## RDR-101 design gaps surfaced by Phase 0 (Phase 1 owns)

The surveys uncovered the following gaps in the accepted RDR-101 design. Phase 1 work should resolve each before the event log ships.

1. **Frecency projection schema is unspecified** (from `nexus-o6aa.1`). §Entities ER omits Frecency entirely, but Phase 5 relocates `frecency_score`, `ttl_days`, `expires_at`, and `miss_count` there. Phase 1 needs `Frecency { chunk_id PK FK, embedded_at, ttl_days, frecency_score, miss_count, last_hit_at }` plus a decision on whether `expires_at` materializes as an indexed column.
2. **`Aspect.payload_json` schema fragment for `scholarly-paper-v1` is undefined** (from `nexus-o6aa.1` and `nexus-o6aa.2`). The bib migration plan slots `bib_*` into `Aspect.payload_json`, which forces an explicit schema definition.
3. **Prose vs checklist disagreement on `section_title` and `section_type`** (from `nexus-o6aa.1`). Phase 5 checklist does not list them; Phase 5 prose suggests removal. Pick one.
4. **`source_author` not enumerated in RDR-101 §Entities** (from `nexus-o6aa.1`). Add it or document its absence.
5. **`indexed_at` split rule** between projector synthesis vs explicit dual emission (from `nexus-o6aa.1`).
6. **Phase 5 strict-vs-loose projector semantics for dropped columns** (from `nexus-o6aa.5`). Residual edge of substantive critique Gap 2 item 2.
7. **Registration ordering at `Catalog.register():778-786`** (from `nexus-o6aa.5`). The Phase 5 plan to replace the `head_hash + title` idempotency guard with `(coll_id, chash, doc_id)` interacts with the code-indexer at `indexer.py:309-320`, which calls `register()` before chunking; chash is not yet known at that point.
8. **`aspect_readers.py:CHROMA_IDENTITY_FIELD` rewrite** (from `nexus-o6aa.6`). Substantive critique C2: this is the structural reproduction of the ART-lhk1 failure if not rewritten before Phase 5 ships. Plumb a `doc_id_lookup` argument through `extract_aspects`.
9. **Telemetry counter placement** (from `nexus-o6aa.6`). Per-call-site (option Y) is required to answer the RF-101-5 gating question; ships in the same PR as the first Phase 4 reader migration.

## What Phase 1 unblocks on

`nexus-o6aa.7` (Phase 1: event log infrastructure) becomes ready once these 7 PRs merge and the 7 beads close. Phase 1 deliverables (per RDR-101 §Implementation Plan / Phase 1):

- Event types as Pydantic / dataclass schemas in `src/nexus/catalog/events.py`
- Append-only writer (JSONL with `{type, v, payload, ts}` envelope per RF-101-2)
- Projector: events to SQLite state, tested by replay-equality
- `nx catalog doctor --replay-equality` verb confirming projector determinism

No production write path changes in Phase 1.
