---
title: "RDR-099: DEVONthink Integration — First-Class CLI Verbs for Selection-Based Ingest and Reverse Lookup"
id: RDR-099
type: Feature
status: accepted
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-04-28
accepted_date: 2026-04-29
related_issues: [nexus-bqda, nexus-srck]
related_tests: []
related: [RDR-096]
---

# RDR-099: DEVONthink Integration — First-Class CLI Verbs for Selection-Based Ingest and Reverse Lookup

This RDR proposes a single new command group, `nx dt`, that closes the friction loop between DEVONthink (DT) and Nexus for the macOS user. The substrate already exists: `x-devonthink-item://` is a real catalog source-URI scheme (`nexus-bqda`), `_devonthink_resolver_default` resolves UUIDs to filesystem paths via osascript, `meta.devonthink_uri` is recorded on entries that came in via DT, and `nx catalog remediate-paths` consults that meta to recover from DT-internal relocations (`nexus-srck`). What's missing is the CLI surface that turns that substrate into a daily workflow — selecting documents in DT and getting them into Nexus, finding documents in Nexus and getting back to DT.

The motivating user pain is concrete and repeated: each round trip currently requires manual UUID or path copying. A user reads a paper in DT, decides it should be searchable in Nexus, and has to (1) reveal the DT path of the file, (2) copy it, (3) paste into `nx index pdf`, (4) optionally copy the DT URL and paste into `--source-uri`. The reverse direction — finding a Nexus search result and opening it for annotation in DT — has no native path at all; the user pastes the catalog `file_path` into Finder.

This RDR is intentionally narrow. It does not propose tag/topic synchronization (different taxonomies, different update semantics — its own RDR), DT annotation extraction (read-only DT API surface, separate concern), or generalized document-manager integration (Zotero, Mendeley, Papers each have different identity models). It ships as a `nx dt` Click subcommand group plus a small `dt/` Claude Code plugin layered on top once the CLI shape settles.

## Problem Statement

### Gap 1: No DT-aware CLI verbs — every ingest is manual UUID-or-path copying

Today the Nexus CLI knows nothing about DEVONthink as a source. `nx index pdf <path>` accepts a filesystem path; the user has to know that path. For DT-managed PDFs the path lives under `~/Library/Application Support/DEVONthink/.../Files.noindex/...` — DT exposes it via "Reveal in Finder" but it's not directly typeable. `nx catalog register --source-uri x-devonthink-item://<UUID>` accepts a DT URI for identity but still requires the user to copy the UUID out of DT manually (or copy-as-link).

The natural verb the user reaches for — "index whatever I have selected in DT" — has no implementation. AppleScript `tell application id "DNtp" to get selection` returns the list of selected records as `[reference, ...]`; from there `uuid of` and `path of` per item resolve cleanly. There is no Nexus surface that calls that.

### Gap 2: DT's organizational taxonomy is invisible to Nexus ingest

DT users organize their reading via tags, groups, and smart groups. A user might have a smart group "AI 2025: unread" that updates dynamically. There is no path today to say "ingest the contents of that smart group into `knowledge__ai-2025`". Today the user manually drags files out, batches them, runs `nx index pdf` per-file or via shell loop, and loses the DT identity URLs in the process — the catalog ends up with `file://` URIs into the DT internal tree, which break the moment DT relocates the file. The `nexus-srck` remediation path can recover but only if `meta.devonthink_uri` was recorded, which today only happens when the user manually passes `--source-uri x-devonthink-item://<UUID>` at register time.

### Gap 3: No reverse path from a Nexus search result to the DT record

`nx search "graph rag"` surfaces hits with `file_path` and (if the entry has it) `source_uri`. For DT-sourced entries the URI is `x-devonthink-item://<UUID>` — a clickable URL on macOS that opens DT and reveals the record. There is no command in Nexus today that performs that lookup-and-open round trip. The user copies the URI manually and runs `open` from a shell, or pastes into the address bar.

The closing-the-loop verb — `nx dt open <tumbler>` — is two lines of glue: read `meta.devonthink_uri` from the catalog entry, shell out to `open <uri>`. Without it, the round trip "find semantically → annotate in DT" requires a manual paste step that breaks the agent-friendly story for the integration.

### Gap 4: No integration with DT's smart-rule auto-execution

DT supports per-group "smart rules" that run AppleScript or shell scripts when a document is moved/imported/tagged. A DT user with an "Inbox → AI papers" workflow could attach a smart rule that runs `nx dt index --uuid <newly-imported-uuid>` automatically — zero-touch ingest. Today there is no documented recipe for this, and the rule would have to construct the UUID-to-path resolution itself rather than hand it to a Nexus verb. The friction is high enough that no Nexus user ships this configuration.

## Context

### Existing primitives (post `nexus-bqda` + `nexus-srck`, shipped 4.17.0)

- `x-devonthink-item://<UUID>` is in `_KNOWN_URI_SCHEMES` (`src/nexus/catalog/catalog.py`). Register-time validation accepts it; the lock test pins the allow-list.
- `_devonthink_resolver_default(uuid) -> (path|None, error_detail)` lives in `src/nexus/aspect_readers.py`. It runs osascript against application id `DNtp` and surfaces typed failure detail (osascript not found, timeout, missing record, non-zero exit). Production callers pass nothing; tests inject a stub.
- `_read_devonthink_uri()` is registered in the `_READERS` dict so `read_source(uri)` dispatches DT URIs through the resolver and reads the file. macOS-only, gated on `sys.platform == "darwin"`, returns `ReadFail("unreachable", "DEVONthink integration is macOS-only")` on Linux/Windows.
- `nx catalog remediate-paths` consults `entry.meta.devonthink_uri` before basename scanning and persists the resolved path when DT reports the file exists on disk. The report shows `of which N via DEVONthink` so operators can see the DT-resolved subset.

### DEVONthink AppleScript surface (DT 3 / DT 4, application id `DNtp`)

The canonical reference is the AppleScript dictionary bundled with the installed app, dumped via `sdef /Applications/DEVONthink.app` (or File → Open Dictionary in Script Editor). The website's automation page is a marketing pointer; the handbook PDF requires auth; the discourse forum has community examples but not authoritative syntax. `sdef` ships with the binary so it's automatically version-matched. All snippets below are sdef-derived and empirically validated against DT 4.2.2 (Research Findings § 099-research-5 and § 099-research-6).

| What we want | DT 4 AppleScript (sdef-canonical) |
|---|---|
| Currently selected records | `tell app id "DNtp" to selected records` — DT4-preferred bulk form; sdef recommends over the legacy `selection` property "especially for bulk retrieval of properties like UUID" |
| Record by UUID | `get record with uuid "<UUID>"` |
| All records with tag X | `lookup records with tags {"X"} in database "Y"` — dedicated command `DTpacd92`; accepts a list of tags + optional `any:true` (default `false` = AND semantics). **Critical sdef detail**: the `in` parameter description reads *"Uses current database if not specified."* Calling without `in` silently scopes to whatever database is frontmost in DT's UI — *not* all open databases. The implementation MUST iterate every open database (`databases of application`), call `lookup records with tags` once per database with `in database "<name>"`, and merge results by UUID. Multi-database support is a v1 invariant, not a v2 enhancement. |
| Enumerate smart groups | `parents of database "Y" whose record type is smart group` |
| Smart-group contents | **Three-property read**, not a one-liner. A smart group's full scope is `(search predicates, search group, exclude subgroups)`. The implementation must read all three:<br>(a) `set pred to search predicates of <sg>` (plural — singular errors).<br>(b) `set scope to search group of <sg>` — when this is `missing value`, fall back to `root of database "Y"`; otherwise scope is the named subgroup.<br>(c) `set excludeSubs to exclude subgroups of <sg>` — pass to `search` as the `exclude subgroups` parameter so the re-execution honours the smart group's setting.<br>Then `search pred in scope exclude subgroups excludeSubs`. **A smart group scoped to `/AI Papers/2025` re-executed against `root of database` will silently return whole-database matches** — the smart group becomes wider than the user authored. This is the load-bearing correctness detail for `--smart-group`. |
| Records inside a group at a path | `get record at "<path>" in database "Y"` (`DTpacd23`); path format is `/<group-name>` at the database root, `/<parent>/<sub>` for nested. The `location` property of a group is the parent path; round-trip is `<location><name>`. Slashes inside record names must be escaped as `\/` per the sdef. Empirically validated: `get record at "/Trash" in database "Inbox"` returns the Trash group. |
| Search scope (any search variant) | `search "<query>" in <group>` — the `in` clause requires a *record*, not a database. For database-wide search use `search "<q>" in root of database "Y"` |
| Reference URL | `reference URL of <record>` returns `x-devonthink-item://<UUID>` literally. **Two use cases, two answers.** For the `nx dt open <tumbler>` path (where we have a record handle in scope), `reference URL of` avoids re-running osascript on an already-resolved entry. For the `nx dt open <UUID>` path (where we have only the UUID), direct concatenation `f"x-devonthink-item://{uuid}"` is preferred — the scheme is sdef-stable, `reference URL` would just *return* the same string, and the round-trip would force DT to be running. |
| UUID + path of a record | `uuid of theItem`, `path of theItem` |

Every selector reduces to a list of (UUID, path) pairs. The ingest verb operates on those pairs uniformly.

### macOS-only constraint

DEVONthink is macOS-only (no Linux/Windows port, no plans for one). The `nx dt` command group is therefore macOS-only. Following the precedent set by the DT URI reader, non-darwin users get a clear error message at command-invocation time, not a subprocess crash. CI on Linux runners skips the DT command tests via `pytest.mark.skipif(sys.platform != "darwin", ...)`.

## Proposed Decision

Add a `nx dt` Click subcommand group inside the existing `conexus` package. Five verbs in v1, three of which are selection variants of the same underlying operation:

```
nx dt index --selection                                  # whatever is selected in DT now
nx dt index --group <path> [--database <name>]           # all items in a DT group
nx dt index --tag <name> [--database <name>]             # all items with this DT tag
nx dt index --smart-group <name> [--database <name>]     # all items matching a DT smart group
nx dt index --uuid <UUID> [--uuid <UUID>...]             # explicit UUIDs (the smart-rule entry point)

nx dt open <tumbler-or-uuid>                             # reveal in DEVONthink
```

`--database <name>` defaults to **all open databases** (the implementation iterates `databases of application` and merges results by UUID). The flag exists to scope explicitly when needed; the default exists because users with multiple databases would otherwise silently miss matches in non-frontmost DBs (sdef: `lookup records with tags` in-clause defaults to "current database"; same for `get record at` and the smart-group `search` re-execution). Inside `nx dt index --selection` the user's DT-side selection already crosses databases, so no `--database` flag applies. Inside `nx dt index --uuid` the UUID is a globally unique identity in DT — `get record with uuid <UUID>` resolves against all open databases without needing a scope hint, and the implementation simply propagates the result; no `--database` flag is meaningful.

`--tag` accepts exactly one tag value in v1. Multi-tag selection (`--tag A --tag B`) with explicit AND/OR is a v2 add — `lookup records with tags {"A","B"}` defaults to AND, `any:true` is OR; the v1 surface picks one tag-per-invocation rather than encoding semantics in the CLI.

All `nx dt index` variants share a single internal pipeline:

1. Resolve the selector to a `[(uuid, path), ...]` list via osascript. For multi-database selectors (`--tag`, `--group`, `--smart-group` without `--database`), this is one osascript call per database, results merged by UUID. The smart-group case reads three properties (`search predicates`, `search group`, `exclude subgroups`) and re-executes via `search <pred> in <scope> exclude subgroups <bool>` so the user's authored scope is preserved (see the AppleScript surface table for why this matters).
2. For each `(uuid, path)`: dispatch to the existing `nx index pdf` (or `nx index md` based on file extension) with `--source-uri x-devonthink-item://<uuid>`. The `meta.devonthink_uri` field is populated automatically by the indexer's catalog-registration hook because the source URI is in scope. **Substrate note**: `x-devonthink-item://` is already in `_KNOWN_URI_SCHEMES` (RDR-096 + `nexus-bqda`), so `_normalize_source_uri` accepts these URIs unchanged — `devonthink.py` adds no URI validation logic.
3. Honor the standard `--collection`, `--corpus`, `--dry-run` flags via passthrough so a caller can write `nx dt index --tag 'AI 2025' --collection knowledge__ai-2025`.

`nx dt open <tumbler-or-uuid>` resolves the argument:

- If it parses as a tumbler (`N.N.N`), look up the catalog entry and read `meta.devonthink_uri`. If absent, fall back to checking whether `source_uri` itself is `x-devonthink-item://`. If neither, error.
- If it parses as a UUID-shape, build the URL directly: `f"x-devonthink-item://{uuid}"`. The scheme is sdef-documented and stable; `reference URL of <record>` is *what DT returns* — it doesn't define the scheme, so concatenation is not a brittleness risk. Direct construction also avoids spawning osascript + DT for a trivial string operation, and works when DT is not yet running (macOS launches DT automatically when `open` resolves the URL).

Then `subprocess.run(["open", uri])` shells out. macOS resolves the URL to DT.

The CLI surface lives in `src/nexus/commands/dt.py`. The osascript wrappers live in `src/nexus/devonthink.py` (a new module distinct from `aspect_readers` because the surface is different — bulk selection resolution, not single-URI read). `_devonthink_resolver_default` is reused for the existing URI-read path; `dt.py` adds `_dt_selection`, `_dt_group_records`, `_dt_tag_records`, `_dt_smart_group_records` helpers each returning `[(uuid, path), ...]`.

A small Claude Code plugin layer (`dt/` next to `nx/` and `sn/`) ships in a follow-up bead, providing `/dt:index-selection` and `/dt:open-result` slash commands. The plugin is *agent-facing surface* — it wraps the CLI verbs and exposes them as one-shot skill invocations. It is not on the v1 critical path.

## Alternatives Considered

- **Standalone `conexus-dt` PyPI package.** Cleanly isolates the macOS-only surface from the cross-platform `conexus` core. Rejected because (a) the actual macOS-only code is small (~150 lines), already gated by `sys.platform`, and adding it adds nothing material to the wheel size on Linux; (b) a separate package adds another release cadence to manage, another `uv sync` for the user, and a discoverability tax — users won't know it exists; (c) the integration depends on Nexus internals (`Catalog`, `Indexer`, `aspect_readers._devonthink_resolver_default`) and a separate package would either re-export them or vendor them, both worse than just adding a subcommand.
- **Claude Code plugin (`dt/`) only — no `nx` CLI verbs.** Rejected because the CLI shape is the substrate. Plugin-only ships a slash-command surface that would have to invoke its own osascript and re-implement the index pipeline, duplicating the substrate this RDR is trying to consolidate. The plugin is the right second step, not the first one.
- **DT smart-rule library only — pure AppleScript, no Python code in Nexus.** Rejected as a primary path. Smart rules are a great use case for the integration but they call out to `nx dt index --uuid <UUID>`; without that verb, smart rules would have to inline the path-resolution + file-read + indexer-invocation, recreating the substrate in AppleScript. The smart-rule recipe is documentation that ships alongside the verbs, not a substitute for them.
- **Tag-based two-way sync (DT tags ↔ Nexus topics).** Deferred to a future RDR. The two taxonomies have materially different update semantics (DT tags are hand-curated point updates; Nexus topics are HDBSCAN-discovered batch outputs). Resolving the sync direction question (write-master, conflict resolution, deletion semantics) is a tar pit that would block the v1 ingest workflow this RDR is trying to ship. Out of scope.
- **`nx dt watch` daemon.** Considered for the inbox-monitor case ("auto-index any PDF that lands in DT group X"). Rejected for v1 in favour of the smart-rule recipe — DT already has the watching infrastructure, no point building a parallel daemon. If the smart-rule recipe proves insufficient, revisit in v2.

## Risks and Mitigations

- **DT not installed / not running.** osascript surfaces this cleanly: `tell application id "DNtp"` errors with `Application isn't running`. The selector helpers in `devonthink.py` translate that into a clear `DTNotAvailableError` with operator guidance ("DEVONthink is not running. Open it and retry, or pass `--uuid` for a UUID you already have"). The verbs exit non-zero.
- **DT version drift.** DT 3 / DT 4 / DT Pro / DT Server all register as application id `DNtp`. v1 targets **DT 4.x as the validated platform** (empirically probed against 4.2.2 in Research Findings § 099-research-5); DT 3.x is expected to work given app-id stability and is the historical compatibility claim, but is not in the v1 test matrix. DT 3 → 4 introduced material AppleScript changes — `selected records` element replaces the legacy `selection` property as the preferred bulk-retrieval form; tag lookup canonical command became `lookup records with tags`; `every record of database` was removed; smart-group enumeration goes through `parents whose record type is smart group`. The RDR's documented snippets reflect the DT 4 dialect.
- **osascript spawn cost.** Each verb spawns one osascript subprocess. Empirically (`nexus-bqda` measurement) that's ~80–150ms cold. For `--selection` with a typical 1-10 item selection, the AppleScript itself runs in ~10ms. Bulk selectors that return 100+ records add proportional time but stay under 1s in practice. Acceptable for a CLI verb. If we ever want sub-100ms response we'd switch to JXA or write a native binding — out of scope for v1.
- **Files outside DT's `Files.noindex` tree.** DT can index files in-place rather than copying them into the database. The `path of theItem` AppleScript returns the in-place path correctly; the indexer doesn't care which tree the file lives in. No special-casing needed.
- **Smart-group queries that return non-PDF files.** A user's smart group might contain mixed content. The dispatcher inspects file extension and routes to `index pdf` / `index md` / `index repo` (or skips for unrecognized extensions, with a warning per skipped item). The skip behavior is the same as `nx index repo` over a directory of mixed content.
- **CI on Linux.** Tests for `nx dt` verbs are gated by `pytest.mark.skipif(sys.platform != "darwin", ...)`. An additional set of unit tests inject a fake selector helper (returning canned `[(uuid, path), ...]` lists) so the dispatch logic, error paths, and `nx dt open` argument parsing get coverage on every platform. Following the pattern from `test_aspect_readers.py::TestReadDevonthinkUri`.
- **Multi-database scoping.** A user with N open databases (this RDR's author has 3: Inbox, Sims, Constantine) would silently miss results in non-frontmost databases without the multi-database default. v1 iterates `databases of application` for `--tag`, `--group`, `--smart-group` — with explicit `--database <name>` to scope to one. Deduplication by UUID handles the corner case where a record (rare but possible) appears via `replicate` in multiple DBs.
- **Concurrent smart-rule invocations.** A DT smart rule that calls `nx dt index --uuid <UUID>` on every imported document will fire concurrently when the user batch-imports K records — potentially K simultaneous nexus invocations contending on the catalog SQLite + T3 ChromaDB writes. v1 mitigation: the smart-rule recipe documents `do shell script "nx dt index --uuid " & uuid & " &"` (background) with an explicit "for batch imports of >5 records, prefer `nx dt index --selection` after import completes" recommendation. The catalog's per-document locking already serializes registration; the bottleneck is T3 write contention. If contention proves problematic in practice, a v2 follow-up adds a `--queue` flag that writes to T2 `aspect_extraction_queue`-style WAL queue and a daemon drains it sequentially.
- **DT Server users on Linux.** DT Server exposes DT over HTTP for cross-machine access. The web interface has no AppleScript surface, so `nx dt` cannot bridge to a Server instance from a Linux/Windows client. The macOS-only constraint is therefore complete: `nx dt` works on the macOS machine where DT runs, and only there. Documented in the macOS-only constraint section above.

## Out of Scope

### Permanently out (by construction)

- **Apple Intelligence integration.** DT 4.x hooks Apple Intelligence as a Writing Tools passthrough only. The handbook explicitly does not document it (*"Because this is a systemwide feature, I don't cover it in further detail in this book."*); the `.sdef` exposes zero Apple Intelligence surface; it is not scriptable from outside DT. There is no integration path **as of DT 4.2.2** — revisit if a future DT release exposes scriptable Apple Intelligence commands in the sdef. The on-device ML that powers OCR/transcribe (Vision/Speech frameworks) is a separate pre-existing capability and *is* surfaceable, but that's not what "Apple Intelligence" names in DT 4.
- **DT `classify` as a substitute or complement to Nexus BERTopic taxonomy.** DT classify is a filing suggestion against the user's hand-built group taxonomy; Nexus BERTopic is emergent latent topics across the corpus. Different questions, and DT classify is too slow (per-document AppleScript round-trip) for ingest-pipeline use.
- **Routing `nx_answer` queries through DT's `get chat response for message`.** DT chat is bounded by what's selected in the DT UI at invocation time; it has no concept of Nexus's T3 vector retrieval or plan execution. Using DT as the LLM endpoint for `nx_answer` would bypass the entire retrieval layer and produce context-blind answers.
- **DT tag ↔ Nexus topic two-way synchronization.** Two-source-of-truth tar pit: DT tags are user-curated point updates, Nexus topics are HDBSCAN-discovered batch outputs. Resolving sync direction, conflict resolution, and deletion semantics is a separate RDR's worth of work, and the current model (DT tags ingested as read-only metadata; Nexus topics derived independently) avoids the conflict entirely.
- **Replicating DT's OCR / transcription path in Nexus.** DT owns media-to-text for files inside DT. The right posture is to *read* DT-stored OCR'd text or transcription annotations during ingest (a v2 enhancement, see below), not to add an OCR layer to Nexus.

### Deferred (separate RDR / bead when motivated)

- **Two-way write — Nexus mutating DT** (creating records, updating tags). Inverts the current DT-as-source-of-truth direction; bigger surface than this RDR can absorb.
- **Other document managers** (Zotero, Mendeley, Papers). Each has its own identity model; the right pattern is one URI scheme per manager (`x-zotero-item://`, etc.) with parallel CLI verbs. `nexus-bqda`'s precedent is the template.
- **A `nx dt watch` daemon.** DT smart rules + macOS folder-action scripts (see Out of band #4) already cover the watch case; building a parallel daemon duplicates infrastructure.

## Acceptance criteria

- **`nx dt index --selection`** against N records selected in DT produces exactly N catalog entries, each with `source_uri = x-devonthink-item://<uuid>` and `meta.devonthink_uri` set to the same value. `--dry-run` prints N records to stdout with no side effects. Manual smoke documented in `tests/e2e/devonthink-manual.md` (a new file the v1 PR ships) — must include a 5-step repro: "select N items in DT → run nx dt index --selection --dry-run → verify count = N → run without --dry-run → verify catalog has N new entries with the expected source_uri".
- **`nx dt index --uuid <UUID>`** indexes a single DT-managed PDF and the resulting catalog entry has `source_uri = x-devonthink-item://<UUID>` and `meta.devonthink_uri` populated.
- **`nx dt index --tag <name>`** with NO `--database` flag, against a tag present in K open databases (K ≥ 2 in the test harness), produces a catalog entry per record across **all K databases** — verifies the multi-database default. `--database <name>` with a single database scopes to that database only.
- **`nx dt index --smart-group <name>`** against a smart group whose `search group` is a sub-folder (not the database root) produces results equal to what the smart group displays in the DT UI — verifies the `search group` + `exclude subgroups` properties are honoured. A smart group with `search group = missing value` falls through to root-of-database scoping.
- **`nx dt open <tumbler>`** for an entry with `meta.devonthink_uri` opens DEVONthink and reveals the record. **`nx dt open <UUID>`** works without DT running at invocation time (macOS auto-launches DT via the `open` URL resolution).
- **Cross-platform CI passes on Linux** without invoking osascript: tests inject a fake selector helper returning canned `[(uuid, path), ...]` lists; `pytest.mark.skipif(sys.platform != "darwin", ...)` gates the live-DT tests. A non-darwin invocation of `nx dt index` exits non-zero with the documented "DEVONthink integration is macOS-only" message.
- **`docs/devonthink-smart-rules.md`** ships in the v1 PR and documents at minimum: (a) the AppleScript stanza that calls `do shell script "nx dt index --uuid " & uuid` from a smart rule, (b) where to drop the `.scpt` file (`~/Library/Application Scripts/com.devon-technologies.think/Smart Rules`), (c) the error-handling pattern when `nx` exits non-zero, (d) the concurrency caveat (see Risks).
- **`tests/e2e/devonthink-manual.md`** ships in the v1 PR and documents the **fixture-creation steps** required to verify the DT-side ACs above: at minimum, a smart group with a non-root `search group` (for the `--smart-group` AC), at least one tag present in ≥2 open databases (for the `--tag` AC), and a parent group with nested children (for the `--group <path>` AC). Without these, the manual smoke is unverifiable by anyone but the author.

## Out of band

Follow-ups identified during the research phase, each its own bead or RDR. Ranked by ROI.

1. **Nexus topic writeback to DT via `add custom meta data`** *(v2 lead — single highest-value follow-up identified in the research phase).* After Nexus assigns a BERTopic label to an ingested DT record, dispatch one AppleScript call: `add custom meta data "<topic-label>" for "nexus_topic" to <record>`. DT auto-creates the metadata key in Settings > Data, the value is searchable in DT's smart groups and metadata inspector, and DT users can build smart groups that filter by Nexus-derived topics. **One AppleScript call per record, no schema changes, no two-source-of-truth issue** (DT side is read-only for Nexus's purposes — it's a derived label, not a sync target). The single most valuable v2 move uncovered in the AI-integration research (research-finding 8).

2. **Capture DT-stored OCR text and transcription annotations during `nx dt index`.** DT can OCR scanned PDFs and transcribe audio/video; the resulting text lives in DT's `plain text` property or as a record annotation. Currently `nx dt index` only ingests the raw file bytes — for an image-only PDF that DT has OCR'd, Nexus would re-OCR (or fail to OCR) instead of using DT's existing text. v2 enhancement: read `plain text of <record>` during ingest and pass to the chunker as an alternative content source. Bridges the "DT owns media→text, Nexus consumes" boundary cleanly.

3. **Reverse-resolution migration toolkit** for existing nexus catalog entries with stale `file://` URIs into DT's `Files.noindex/` tree. DT exposes `lookup records with content hash`, `lookup records with path`, `lookup records with file`, and `lookup records with URL` — five identity dimensions beyond UUID. A migration verb (`nx catalog dt-resolve`) could walk unmigrated entries, query DT via these lookups, and back-fill `meta.devonthink_uri`. The lookup primitives are sdef-documented and validated.

4. **macOS folder-action scripts as a third zero-touch ingest pattern** (alongside DT smart rules and the deferred `nx dt watch`). DT bundles `~/Library/Scripts/Folder Action Scripts/DEVONthink - Import.scpt` (and OCR / Import-and-Delete variants); a user could attach a folder-action that auto-imports anything dropped into a designated folder, then chain a DT smart rule to call `nx dt index --uuid <UUID>`. Or invert: nexus drops into the folder, DT auto-imports + tags. Documented in the smart-rule recipe (`docs/devonthink-smart-rules.md`) as an alternative path.

5. **`--capture-dt-meta` flag** for `nx dt index` (was v2 follow-up #3 in the prior draft). DT records carry `aliases`, `URL`, `comment`, `addition date`, and arbitrary custom metadata. Capturing into `meta.dt.*` preserves user-curated context. Lower priority than #1–#4 because it's quality-of-life; the others address structural gaps.

6. **Optional `--with-summary` flag** that calls DT's `summarize contents of` (DT Pro/Server only, requires LLM endpoint configured) before ingest, writes the summary as a new DT record, and ingests both. Targeted use only — bulk ingest cost is prohibitive. Worth implementing if a specific dense-document workflow demands it.

7. **`extract keywords from` as a cheap pre-aspect seed** (DT Pro/Server only). DT's local statistical keyword extractor is fast and free; could populate a `dt_keywords` chunk-metadata field at ingest time, seeding the RDR-089 aspect-extraction queue with a pre-computed signal. Marginal ROI but trivial to implement.

8. **Claude Code plugin layer** (`dt/` slash commands) once the CLI surface is settled. Purely a wrapper — does not introduce new substrate.

**DT Pro/Server gating risk**: items 1, 6, and 7 invoke commands that exist only in DT Pro or DT Server (the user's current install is the base edition). Implementations must runtime-check edition (`tell application id "DNtp" to type of database "Inbox"` or similar) and degrade gracefully — log + skip rather than error.

## Research Findings

Recorded in T2 (`nexus_rdr/099-research-{1..8}`) across 2026-04-28; gate-driven correction recorded as `nexus_rdr/099-gate-latest` 2026-04-29. Pointers, not duplicates — read the full entries via `nx memory get --project nexus_rdr --title 099-research-N`.

1. **`099-research-1` — DT installed at 4.2.2; selection AppleScript works.** Verifies the substrate shipped in 4.17.0 (`nexus-bqda` + `nexus-srck`) is wired up on the user's machine; confirms `tell app id "DNtp" to selected records` returns UUID + path cleanly; surfaces 3 mounted databases (Inbox, Sims, Constantine) and the implied multi-database scoping question for `--group` / `--smart-group`.

2. **`099-research-2` — initial probe surfaced apparent gate blocker, since RESOLVED by `099-research-5`.** Three of four selectors as documented in the original draft (`every smart group`, `whose tags contains`, `every record of database`) failed empirically against DT 4.2.2. This was a documentation issue, not an architectural one — see finding 5 for the corrected dialect. The RDR draft has been updated to use the sdef-canonical forms and the blocker framing is withdrawn.

3. **`099-research-3` — osascript spawn cost ~100ms cold.** Empirical: 80-110ms over 5+ runs of selection-resolving AppleScript against DT 4.2.2. Matches the RDR's prior claim. AppleScript itself is sub-10ms; the cost is osascript fork+exec+JIT. Acceptable as a CLI baseline.

4. **`099-research-4` — prior-art survey: Hookmark, PyDT3, Org-DEVONthink.** All three converge on the same lessons: selection is the universal entry point, UUID is stable identity for *imported* content, path is stable for *indexed* content. Nexus already covers both via `meta.devonthink_uri` (UUID) and `file_path` (path) — substrate alignment validated. **No prior tool exposes DT as a CLI-addressable source with `--tag`/`--group`/`--smart-group` flags** — the v1 surface is genuinely novel in this dimension.

5. **`099-research-5` — authoritative DT 4.2.2 dialect from sdef + empirical (overrides RDR draft AND synthesizer).** The bundled `.sdef` (extracted via `sdef /Applications/DEVONthink.app`, 5764 lines, 140 commands) is the canonical reference — version-matched to the running app, supersedes website docs and forum posts. Key corrections: tag lookup uses the dedicated `lookup records with tags` command; selection uses the `selected records` element; smart groups enumerate via `parents whose record type is smart group`; `search`'s `in` clause requires a *record*, not a database.

6. **`099-research-6` — full sdef pass closes every remaining gap; v1 ships as proposed, no spike obligations.** Empirical resolution of the path format (`get record at "/<group-name>" in database "Y"` works — root is `/Trash`, `/Tags`, etc.; the earlier "/" probe failed because the root is unnamed). Smart-group re-execution corrected: property is `search predicates` *plural*, not singular. `reference URL of <record>` returns the canonical `x-devonthink-item://<UUID>` so `nx dt open` should call this rather than concatenating. Plus three migration primitives (`lookup records with content hash / path / file / URL`) that motivate the new follow-up bead in the Out-of-band section, and the `aliases` / `URL` / `comment` / custom metadata API that motivates the `--capture-dt-meta` v2 flag.

7. **`099-research-7` — official DT 4 handbook (Take Control of DEVONthink 4 v1.0, sponsored by DEVONtechnologies, technically reviewed by Christian Grunenberg).** Indexed locally in `knowledge__devonthink` (402 chunks). The handbook itself **defers to the .sdef for syntax** (p. 230: *"To view the complete list of AppleScript commands available in DEVONthink, in Script Editor, choose File > Open Dictionary, select DEVONthink, and click Choose."*). Confirms 099-research-5's framing. New concretes: smart-rule scripts live at `~/Library/Application Scripts/com.devon-technologies.think/Smart Rules`; folder-action scripts at `~/Library/Scripts/Folder Action Scripts` are a *third* zero-touch ingest pattern (now Out-of-band #4); `Update Indexed Items` command exists for forcing DT to re-read external files before bulk reads. Import vs Index semantics confirmed in prose: *indexed* records carry the user's original filesystem path with bidirectional sync; *imported* records live in `Files.noindex/` (path is stable identity but DT-internal).

8. **`099-research-8` — DT × Nexus AI v1/v2/never matrix.** Deep-research-synthesizer pass over indexed handbook + sdef. Three DT AI surfaces resolved: Built-In AI (statistical, pre-LLM, pre-DT-1 era), Generative AI (DT 4 LLM integration, **Pro/Server only**), Apple Intelligence (Writing Tools passthrough only — not scriptable, **out by construction**). The single highest-value v2 move identified: **`add custom meta data "<topic>" for "nexus_topic" to <record>` writeback** — one AppleScript call per record, makes Nexus topics searchable in DT smart groups, no schema changes (now Out-of-band #1). Apple Intelligence and DT-routed `nx_answer` and DT classify-as-Nexus-topic-substitute and DT tag↔Nexus topic two-way sync moved from soft "deferred" to hard "permanent NEVER (by construction)" in the Out of Scope section. DT Pro/Server gating risk added — most v2 LLM moves require Pro edition; user's current install is base edition.

9. **Gate-driven corrections (2026-04-29)** — the substantive-critic agent identified two sdef-evidenced behavioural correctness bugs in the AppleScript reference table that the empirical probing missed because each bug was a *missing* property reference, not a *wrong* one:
   - **`lookup records with tags` defaults to current database, not all open databases.** sdef in-clause text: "Uses current database if not specified." A user with multiple open databases would silently miss records in non-frontmost DBs. Fixed by making multi-database iteration the v1 default and adding an explicit `--database <name>` flag for single-database scoping.
   - **Smart-group two-step ignored `search group` and `exclude subgroups` properties.** A smart group scoped to `/AI Papers/2025` re-executed via `search pred in root of database` would silently return whole-database matches. Fixed by reading all three smart-group properties (`search predicates`, `search group`, `exclude subgroups`) and reconstructing the correct scope.
   
   Plus four significant items addressed: undocumented multi-tag any/all semantics (v1 takes one tag; multi-tag deferred to v2), spurious osascript round-trip in `nx dt open <UUID>` removed (direct concatenation works and avoids requiring DT to be running), `--selection` AC made testable with a 5-step repro, RDR-096 cross-reference made explicit. Apple Intelligence "permanent NEVER" softened to "as of DT 4.2.2; revisit if future DT exposes scriptable surface". Multi-database scoping and concurrent-smart-rule risks added to the Risks section. DT Server users on Linux noted as out-of-reach (web interface has no AppleScript surface).

**Net effect**: the v1 five-selector surface is fully shippable. The research-phase findings (1–8) established the correct dialect and sdef-canonical forms; the gate-driven finding (9) caught two behavioural correctness bugs hidden inside that dialect — *missing* properties on commands the RDR named correctly but described incompletely. With those fixes, the v1 surface preserves DT's authored scope semantics on `--smart-group`, defaults to multi-database for cross-database tags, and avoids unnecessary osascript spawns on `nx dt open <UUID>`. Eight Out-of-band follow-ups ranked by ROI, topic-writeback as v2 lead, eight critic observations folded into Risks / Out of Scope wording.
