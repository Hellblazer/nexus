# `nexus.catalog` — AGENTS.md

The catalog is a Xanadu-inspired document registry that tracks *what* is indexed and *how documents relate*. Since the nexus-i711w terminal deletion the catalog is SERVICE-OWNED in every mode: the authoritative store is the Java engine's Postgres tables, reached through `HttpCatalogClient`. The local SQLite + JSONL catalog (event log, projector, `.catalog.db`) is deleted; a surviving on-disk `~/.config/nexus/catalog/` is a frozen migration source only.

## Core concepts

- **Tumbler** — hierarchical address (`1.2.5`) identifying a document. Every entry has one. `tumbler.py` provides depth, ancestors, lca, and JSONL readers (migration-source and recovery-bundle use only; the JSONL catalog is deleted).
- **Owner** — the top-level tumbler segment. Repos use `owner_type='repo'` with a `repo_hash`; humans use `'curator'`. Repo owners without a hash are rejected (the shadow-registration bug class).
- **Source URI** — persistent identity. Validated at register time against `_KNOWN_URI_SCHEMES = {file, nx-note, https, nx-scratch, x-devonthink-item, nx-orphan-backfill}`. Bare paths normalize to `file://<abspath>` (`types.py:_normalize_source_uri`).
- **Link** — typed edge between documents. Built-in types: `cites`, `implements`, `implements-heuristic`, `supersedes`, `relates`, plus custom. Every link carries `created_by` provenance.

## Three link-creation paths

1. **Post-hoc** (batch, after indexing): `link_generator.py` exposes `generate_citation_links()`, `generate_rdr_filepath_links()`, `generate_prose_filepath_links()`, `generate_pdf_corpus_links()`. Run when a corpus is fully indexed.
2. **Auto-linker** — `auto_linker.py` fires on every `store_put` MCP call. Reads `link-context` from T1 scratch (tag `link-context`), creates links to seeded targets. Skills seed before dispatch; agents self-seed from their task prompt.
3. **Agent-direct** — agents call the `catalog_link` MCP tool during work for precise typed links.

## Two graph views

- `catalog_links` — **live documents only**. Use this for "what's actually linked right now."
- `catalog_link_query` — **all links including orphans** (where one endpoint has been deleted). Use for audits and provenance archaeology.

The `query` MCP tool has catalog-aware routing: `author`, `content_type`, `subtree`, `follow_links`, `depth`. Example: `query("how does path resolution work", follow_links="implements", subtree="1.1")`.

## Key files

| File | Purpose |
|---|---|
| `http_catalog_client.py` | `HttpCatalogClient` — the catalog handle in every mode. Full read surface + whitelisted writes — including the RUNFENCE index-run fence ops `begin_index_run`/`complete_index_run`/`fail_index_run` (nexus-5xn3k.3) — over the engine's `/v1/catalog/*` routes. `manifest_verify`/`manifest_verify_all`/`manifest_orphans`/`manifest_backfill` client methods RETIRED (RDR-191 Phase 6, nexus-o8dil.33, 2026-08-15) — the manifest-chunk FK makes the dangling state they diagnosed unreachable; `manifest_null_collection_report` (the FK's own explicit carve-out — NULL-collection rows are unenforced under `MATCH SIMPLE`) is the one census survivor. `nexus.manifest_verify(text)` itself is not dropped server-side (`CatalogRepository.completeIndexRun` depends on it internally), but this client no longer has a route onto it. |
| `catalog_protocol.py` | `CatalogReader` / `CatalogWriter` Protocols + `CATALOG_WRITE_OPS` (the tooling-enforced write whitelist). Annotate consumers with these, never a concrete class. |
| `factory.py` | `make_catalog_reader()` / `make_catalog_writer()` — the only sanctioned construction path (shared process-lifetime service client, nexus-5en9j). |
| `types.py` | Substrate-neutral value types: `CatalogEntry`, `CatalogLink`, `ManifestRow`, `make_relative`, `_normalize_source_uri`, `_default_registry_path`. |
| `tumbler.py` | `Tumbler` dataclass + `parse`, hierarchy helpers, JSONL readers with resilience (truncated rows, bad JSON); migration-source and recovery-bundle use only. |
| `auto_linker.py` | Storage-boundary auto-linking from T1 scratch link-context. Hook firing site is `mcp/core.py`. |
| `link_generator.py` | Post-hoc batch linkers (citation, RDR↔file-path, prose↔file-path, pdf same-as via shared file-content hash, never `head_hash`, which is the indexing commit for repo files: nexus-utkop). |
| `catalog_spans.py`, `store_hook.py`, `orphan_backfill.py`, `manifest_backfill.py` | Span resolution, post-store registration hook, backfill tooling. |
| `manifest_heal.py` | Shared manifest-gap heal core (nexus-8g0ch + nexus-c21fk) — rebuilds a MANIFEST-LESS document's `document_chunks` rows from T3 chunks already stored, without re-embedding, via `append_manifest_chunks`. It never writes into a document that already has rows, and leaves its `chunk_count` alone (a shared chunk carries the last writer's hash, so the fetch cannot say which positions are missing): that document, and a rebuild short of `chunk_count`, gets its RUNFENCE `index_state` set to `failed` and is reported in `ManifestHealResult.reindex`; re-indexing its source clears it (a failed fence is stale on every staleness path; the next `nx index repo` for a repo file, `nx index pdf`/`md`/`rdr` or `nx store put` again for the others). It never writes `chunk_count` itself, but the append that rebuilds a manifest-less document folds it to the rows written, so a rebuilt-short document is no longer gapped and is not listed again. A manifest-less document with `chunk_count` above zero that the pass cannot rebuild (T3 holds no chunk under its content hash, no content hash or collection is recorded, or its collection does not exist) is reported LOST and marked the same way (nexus-v9xwj); the mark changes what the fence says (the PDF early probe in `nx index repo` and `nx t3 gc` read it), not whether the next index run that walks the file indexes it, which a manifest-less document already does while none of its T3 chunks carries a `doc_id` metadata key (nexus-3ajpd; a pre-Phase-3 chunk that still carries a `doc_id` metadata key puts the document in the staleness cache by that key, and then the mark is what makes the run re-index it); one whose T3 fetch failed, or whose collection lookup failed for a service error (`HttpVectorClient.get_collection` lists strictly and raises `CollectionNotFoundError` with the `VectorServiceError` as `__cause__`), is reported and not marked, and a never-chunked document is never marked. A marked document makes `nx t3 gc` refuse its collection unless `--allow-incomplete-index-state` is passed (nexus-8o06q). Consumed by both `nx catalog reconcile` and the `nx index repo` self-heal pass. |
| `chunk_quarantine.py` | Orphan-chunk soft delete (nexus-xukbj) — moves GC orphans to a sibling `quarantine__*` collection (excluded from every search corpus by construction) instead of hard-deleting; restores chashes that become referenced again. |
| `dt_link_generator.py` | DEVONthink semantic + structural link generation (RDR-139 Layer B) — writes `relates` edges from DT 'See Also' similarity and author-curated item links; gated on `devonthink.available`. |
| `collection_name.py` | `CollectionName` value object (RDR-103 Phase 1) — the four-segment `(content_type, owner_id, embedding_model, model_version)` tuple rendered as `<content_type>__<owner_id>__<embedding_model>__v<n>`; `parse` is strict. |
| `rdr_canonical.py` | RDR-201 Phase 3.1 (nexus-j9z30.20) canonical-tumbler resolution — see § RDR canonical-tumbler rule below. |
| `owner_scope.py` | Resolves a scope argument that may be an owner NAME to its tumbler prefix, so `query(subtree=...)`, `nx_answer(scope=...)`, `catalog_resolve(owner=...)`, and `nx search --repo` all accept either form (GH #1527, nexus-qiah5). |
| `multi_batch_write.py`, `multi_document_write.py`, `note_write.py`, `metadata_merging_catalog.py`, `write_outcome.py` | The RDR-223 writers: one document in N batches, many documents page by page (import), a note's pieces and manifest in one request, the metadata-merging catalog wrapper the per-file writers share, and the one rule for "did this failed request possibly change the engine". Every chunk lands together with its owner row. |
| `note_ttl.py` | When a note's own TTL lapses, from its catalog entry's `meta` (nexus-6pfgb.15): `ttl_days` absent or `0` is permanent (`0` is the retraction value, never handed to `metadata_schema.is_expired`), the clock is `meta.put_at` (written on every put) and not the entry's `indexed_at`, which the engine refreshes on every manifest write. `store_hook.lapsed_note_documents` / `reap_lapsed_note_documents` and `HttpVectorClient.expire_report` read it; so does the `expires in Nd` display (`context_annotations.entry_facts`). `row_written_by(owner, row_meta, owner_title=...)` says whether a chunk row is that owner's own write (the row's `catalog_doc_id` is the owner's tumbler and its `title` the owner's): the only way a keyless legacy note may be judged by the shared row's TTL; the recovery-bundle export uses it the same way for a keyless note's ttl_days (nexus-6pfgb.34). `row_may_give_tags(owner, row_meta, owner_title=...)` is the looser rule for tags and category alone, on sole ownership (nexus-6pfgb.35 option B): it refuses only a row whose stamp AND title both name another note; never a basis for a TTL. `catalog_ttl_stale(meta, indexed_at, rows, ...)` (nexus-6pfgb.30) says whether the chunk rows that name the note record a put its catalog keys do not (older client, `.nxexp` import); the sweep refuses to retire such a note. A row names a note (`row_names_note`, refuse-only, so it over-matches) when its `catalog_doc_id` is the note's tumbler whatever its title, or its `title` is the note's non-empty title; `row_written_by` stays strict (stamp AND title) wherever a row's TTL is read as the note's. Pure, no I/O. |
| `membership.py`, `tombstones.py`, `path_ambiguity.py` | Known-vs-unknown collection check for maintenance verbs; tombstoned-document views for the same verbs; the warning when a writer mints a second document for one path (several documents per `file_path` is normal, not damage). |
| `recovery_bundle.py` | Export/import for the catalog link graph and `store_put`-origin knowledge content across a reinstall, as one human-inspectable JSONL bundle (nexus-xn3fr, GH #1419 Issue 9). |

Deleted in nexus-i711w (RDR-158 P4 terminal deletion): `catalog.py` (the local `Catalog` class), `catalog_db.py`, `event_log.py`, `projector.py`, `events.py`, `catalog_owners.py`, `catalog_sync.py`, `catalog_links.py`, `catalog_docs.py`, `catalog_backup.py`, `catalog_git.py`, `catalog_writes.py` (ManifestRow relocated to `types.py`), `consolidation.py`, `collections_owner_backfill.py`, `synthesizer.py`.

## RDR canonical-tumbler rule (RDR-201 Phase 3.1, nexus-j9z30.20)

The catalog has no notion of "the one true tumbler for RDR-NNN" — a re-index
run mints a fresh owner every time the repo's derived `repo_hash` shifts
(worktree moves, historical registration churn), and RDR files predate the
`content_type="rdr"` scheme (they used to register as `content_type="prose"`).
Measured against the live catalog (2026-09-01): 206 on-disk RDR files are
registered under roughly ten different owner ids, plus a legacy `prose`
registration under owner `1.10` for any RDR never re-indexed since. Nothing
that depends on "which tumbler is RDR-NNN" (RDR-to-RDR dependency edges,
Phase 3.2) means anything until this is resolved to exactly one tumbler per
RDR. `nexus.catalog.rdr_canonical` owns that resolution — no catalog writes,
no edge creation (that is bead nexus-j9z30.21).

**Identity key**: two registrations are candidates for the SAME RDR only
when their `file_path` basenames are identical AND the file sits directly
inside a `rdr/` directory (`docs/rdr/rdr-201-….md`, absolute or relative).
This is narrower than "contains an `RDR-NNN` substring" on purpose:
`docs/rdr/post-mortem/rdr-191-….md` (a different subdirectory) and sibling
artifacts that share an RDR's numeric prefix but not its basename
(`rdr-200-phase1-prereg.md`, `rdr-200-phase1-gate-result.md`) are DISTINCT
catalog documents, not duplicate registrations of the RDR they discuss — a
loose numeric-prefix match collapses them together, which is a false
ambiguity the rule must not manufacture. (Found while building this module:
6 of the corpus's apparent duplicates were exactly this before the basename
fix landed.)

**Repo scoping is load-bearing, not optional** (CRITICAL fix, code-review +
critique, 2026-09-01): the SAME catalog holds other repos' RDR registrations
under other owners — measured live, `/Users/hal.hildebrand/git/ART` is
registered `content_type="rdr"` in this same catalog under a different owner
— so a same-basename collision across repos is real, not hypothetical. Repo
identity is `rdr_source_prefix(repo_root)`, a `file://` prefix anchored on
`docs/rdr/` (the exact directory `nx index rdr` walks) — **not the bare repo
root**: a nested agent-worktree checkout
(`<repo_root>/.claude/worktrees/<name>/docs/rdr/…`, a real fragmentation
source found live in this repo's own catalog) sits inside the repo root's
own path but is a different `docs/rdr/` directory and must not be treated
as in-repo. A bare-root prefix would wrongly admit it; the `docs/rdr/`-
anchored one does not.

**Resolution rule**, given the candidate registrations sharing one identity
key, in order:

1. `content_type == "rdr"` beats the legacy `"prose"` registration — when at
   least one `"rdr"` candidate exists, only `"rdr"` candidates survive to
   step 2.
2. ADMISSION (unconditional — applies to a single surviving candidate too,
   not only a tie): a candidate is admitted only when it is verifiably THIS
   repo's own — its owner is the CURRENT repo owner (the owner id
   `nx index rdr` registers new content under today — resolved via
   `_repo_identity`'s `repo_hash` → `CatalogReader.owner_for_repo`, see
   `current_rdr_owner`) OR its `source_uri` sits under this repo's own
   `rdr_source_prefix`. A candidate that fails BOTH checks is never a
   winner, however few other candidates exist — the bug this closes: the
   original single-candidate fast path accepted a lone registration
   unconditionally, so a same-basename document from a different repo (the
   `ART` shape above) or a stale nested-worktree copy could be silently
   resolved as canonical with no warning.
3. WINNER SELECTION among admitted candidates: the one under the current
   owner wins when unique; otherwise a single admitted candidate still
   resolves unambiguously (the common case: a legacy registration under a
   stale owner that `source_uri` nonetheless confirms is this repo's own
   file — roughly 150 of this repo's 184 RDR-shaped documents have never
   been re-indexed under the current owner and resolve this way). Zero
   admitted candidates, or two or more with no unique current-owner match,
   is UNRESOLVABLE: no guess, no silent pick. `resolve_canonical_tumbler`
   logs a structlog `rdr_tumbler_unresolvable` warning naming every
   surviving candidate and returns `None` — the caller creates no edge for
   that record.

`resolve_all` is the single authority that runs this rule over a full
catalog fetch (group → resolve, one entry point) — every caller needing a
full-catalog resolution routes through it rather than re-deriving the loop.

The dry-run/collapse tool that once sat beside this rule
(`scripts/collapse_rdr_registrations.py`) was deleted 2026-09-05 (nexus-ph718):
its live dry-run census collapsed nothing (every RDR resolved to one KEEP), its
`--apply` had never run, and the read path tolerates duplicate registrations by
construction, so there was nothing for it to do.

## Adding a new source-URI scheme

The bar is **register a reader first, then add to the allow-list**. New schemes that have no reader cause silent register-success-but-extract-failure. The one documented exception is a marker scheme with no reader by design (`nx-orphan-backfill`, whose underlying source is genuinely unavailable; consumers get `scheme_unknown` and skip).

1. Add a `_read_<scheme>_uri()` function to `src/nexus/aspect_readers.py` returning `ReadOk` / `ReadFail`.
2. Register it in `_READERS` dict.
3. Add the scheme to `_KNOWN_URI_SCHEMES` in `types.py`.
4. Update the lock test in `tests/test_catalog.py::test_known_uri_schemes_table_is_locked_to_planned_set`.
5. Update the `--source-uri` CLI help text in `commands/catalog.py`.
6. Test the round-trip: register → resolve → read.

## Key invariants

- **The engine's Postgres catalog is canonical.** Every mutation flows through the `CATALOG_WRITE_OPS` whitelist on a `make_catalog_writer()` proxy; reads through `make_catalog_reader()`. Direct construction of catalog handles outside `factory.py` is lint-banned (`storage_boundary_lint.py`).
- **Every new writer method MUST be added to `CATALOG_WRITE_OPS`** (`catalog_protocol.py`) or it silently raises `AttributeError` through the closed `_ServiceCatalogWriter` whitelist, which a bare `except` upstream can turn into a feature that no-ops forever (the nexus-kgos1 trap).
- **Tumblers are append-only.** Updating an entry preserves the tumbler; deletion creates a tombstone, not a free slot. Reusing a tumbler corrupts the link graph.
- **Owner prefix is exactly 2 segments, documents >= 3 (segment COUNT, not numeric content) — enforced at the engine's API boundary (HTTP 400, rule `tumbler-grammar`); a schema CHECK follows once the engine test fixtures conform (nexus-ia69x).** Never widen. The ENGINE's grammar allows non-numeric segments (`bt.1` is a valid owner prefix there). The PYTHON client is narrower: `Tumbler.segments` is `tuple[int, ...]` (`tumbler.py`) and `Tumbler.parse` raises `ValueError` on a mnemonic prefix — `nx catalog show` / `catalog_show`'s depth-2-is-owner branch only ever fires for a numeric tumbler; a mnemonic owner prefix falls through to the pre-existing title-fallback/parse-error path instead of rendering an owner card. Disclosure, not a bug to fix here: mnemonic owner prefixes exist ONLY in the engine's own Java test fixtures (raw-SQL seeding, e.g. `bt.1`, `cov2.1`) — the 2026-08-28 production census found 72 owners, every one shaped `1.N`. `Tumbler.parse` staying int-segmented is therefore not a live client-visible gap today; it would only start to matter if a mnemonic-shaped owner prefix ever reached production.
- **Owners with `owner_type='repo'` MUST carry a `repo_hash`.** Enforced in `register_owner`. The empty-hash variant produced 83 orphan owners in the wild before the guard.
- **No `._db` / `._dir` reach-ins.** `HttpCatalogClient._db` raises by design; the boundary-lint baselines (`CATALOG_DB_ACCESS_BASELINE`, `CATALOG_DIR_ACCESS_BASELINE`) are enforced at 0.
