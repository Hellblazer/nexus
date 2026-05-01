# `nexus.catalog` — AGENTS.md

The catalog is a Xanadu-inspired document registry that tracks *what* is indexed and *how documents relate*. JSONL files are the source of truth; SQLite + FTS5 is the query cache, rebuilt automatically on mtime change.

## Core concepts

- **Tumbler** — hierarchical address (`1.2.5`) identifying a document. Every entry has one. `tumbler.py` provides depth, ancestors, lca, JSONL readers.
- **Owner** — the top-level tumbler segment. Repos use `owner_type='repo'` with a `repo_hash`; humans use `'curator'`. Repo owners without a hash are rejected (the shadow-registration bug class).
- **Source URI** — persistent identity. Validated at register time against `_KNOWN_URI_SCHEMES = {file, chroma, https, nx-scratch, x-devonthink-item}`. Bare paths normalize to `file://<abspath>`.
- **Link** — typed edge between documents. Built-in types: `cites`, `implements`, `implements-heuristic`, `supersedes`, `relates`, plus custom. Every link carries `created_by` provenance.

## Three link-creation paths

1. **Post-hoc** (batch, after indexing) — `link_generator.py`: `generate_citation_links()`, `generate_code_rdr_links()`, `generate_rdr_filepath_links()`. Run when a corpus is fully indexed.
2. **Auto-linker** — `auto_linker.py` fires on every `store_put` MCP call. Reads `link-context` from T1 scratch (tag `link-context`), creates links to seeded targets. Skills seed before dispatch; agents self-seed from their task prompt.
3. **Agent-direct** — agents call the `catalog_link` MCP tool during work for precise typed links.

## Two graph views

- `catalog_links` — **live documents only**. Use this for "what's actually linked right now."
- `catalog_link_query` — **all links including orphans** (where one endpoint has been deleted). Use for audits and provenance archaeology.

The `query` MCP tool has catalog-aware routing: `author`, `content_type`, `subtree`, `follow_links`, `depth`. Example: `query("how does path resolution work", follow_links="implements", subtree="1.1")`.

## Files

| File | Purpose |
|---|---|
| `catalog.py` | `Catalog` class — `register`, `update`, `link`, `link_query`, `graph`, `delete_document`, `link_audit`, `descendants`, `resolve_chunk`. Holds `_KNOWN_URI_SCHEMES` + `_normalize_source_uri` boundary validator. |
| `catalog_db.py` | SQLite schema, FTS5 tables, UNIQUE link constraint, `descendants()` SQL helper. |
| `tumbler.py` | `Tumbler` dataclass + `parse`, hierarchy helpers, JSONL readers with resilience (truncated rows, bad JSON). |
| `auto_linker.py` | Storage-boundary auto-linking from T1 scratch link-context. Hook firing site is `mcp/core.py`. |
| `link_generator.py` | Post-hoc batch linkers (citation, code↔RDR heuristic, RDR↔file-path). |
| `consolidation.py` | Merges per-paper `knowledge__<paper>` collections into corpus-level `knowledge__<corpus>`. |

## Adding a new source-URI scheme

The bar is **register a reader first, then add to the allow-list**. New schemes that have no reader cause silent register-success-but-extract-failure.

1. Add a `_read_<scheme>_uri()` function to `src/nexus/aspect_readers.py` returning `ReadOk` / `ReadFail`.
2. Register it in `_READERS` dict.
3. Add the scheme to `_KNOWN_URI_SCHEMES` in `catalog.py`.
4. Update the lock test in `tests/test_catalog.py::test_known_uri_schemes_table_is_locked_to_planned_set`.
5. Update the `--source-uri` CLI help text in `commands/catalog.py`.
6. Test the round-trip: register → resolve → read.

## Key invariants

- **`events.jsonl` is canonical; SQLite is a deterministic projection.** Under RDR-101 Phase 3 PR ζ (`NEXUS_EVENT_SOURCED` default ON, nexus-o6aa.9.5) every catalog mutation flows through `Catalog.register / update / link / unlink / set_alias / dedupe`, which emits an event into `events.jsonl` first and then projects to SQLite. The legacy per-class JSONL files (`owners.jsonl`, `documents.jsonl`, `links.jsonl`) are still written for the cutover window but are no longer canonical — the doctor's `--replay-equality` signal is the binding test.
- **Bootstrap guardrail.** Existing catalogs without an `events.jsonl` (or with one that is materially sparser than the legacy `documents.jsonl`) keep operating in legacy mode via `_event_log_covers_legacy()` in `_ensure_consistent`. Run `nx catalog synthesize-log --chunks --force` then `nx catalog t3-backfill-doc-id` to migrate; doctor `--replay-equality --t3-doc-id-coverage --strict-not-in-t3` validates the result.
- **Opting out.** Set `NEXUS_EVENT_SOURCED=0` (or `false`/`no`/`off`) to fall back to the legacy direct-write path. Test fixtures that exercise legacy-only behaviour (shadow-emit invariants, `synthesize-log` tests, `doctor --replay-equality` synthesizer fallback, etc.) pin to `0` explicitly.
- **Tumblers are append-only.** Updating an entry preserves the tumbler; deletion creates a tombstone, not a free slot. Reusing a tumbler corrupts the link graph.
- **Owners with `owner_type='repo'` MUST carry a `repo_hash`.** Enforced in `register_owner`. The empty-hash variant produced 83 orphan owners in the wild before the guard.

## Direct catalog writes outside this module are forbidden (RDR-101 Phase 3 ε)

Under RDR-101 the event log (`events.jsonl`) is canonical and the SQLite catalog is a deterministic projection. From PR ε onward, `tests/test_no_direct_catalog_writes_outside_projector.py` is a lint gate: no production source file outside `src/nexus/catalog/` may issue `INSERT / UPDATE / DELETE / REPLACE / CREATE / DROP / ALTER / TRUNCATE` through `_db.execute`. Reads (SELECT, plus `fetchone`/`fetchall` chains) remain fine.

The lint gate scope is intentionally `src/nexus/catalog/`, not just `projector.py` — `catalog.py`'s legacy direct-write branches are gated behind `if not self._event_sourced_enabled:` and remain in-module. Mutations from anywhere else must travel through public Catalog API (`register`, `update`, `link`, `unlink`, `set_alias`, …), which under `NEXUS_EVENT_SOURCED=1` emits a domain event and projects it.

Test fixtures that need to construct invariant-violating state (forced alias cycles, contaminated source_uri rows, backdated `created_at` for stale-span audits) tag the offending line with `# epsilon-allow: <reason>`. The reason is mandatory; bare markers do not suppress.

Repair operations that previously ran as `cat._db.execute(...)` ad-hoc scripts must land as `nx catalog repair-*` verbs that emit events. The doctor's `--replay-equality` signal is reliable only if the event log is the only write path; PR ζ (nexus-o6aa.9.5) flipped `NEXUS_EVENT_SOURCED=1` to ON by default and depends on this gate plus the dedupe-projector wiring (PR ζ-prereq, nexus-o6aa.9.4).
