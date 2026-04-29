---
title: "RDR-099: DEVONthink Integration — First-Class CLI Verbs for Selection-Based Ingest and Reverse Lookup"
id: RDR-099
type: Feature
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-04-28
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

The canonical reference is the AppleScript dictionary bundled with the installed app, dumped via `sdef /Applications/DEVONthink.app` (or File → Open Dictionary in Script Editor). The website's automation page is a marketing pointer; the handbook PDF requires auth; the discourse forum has community examples but not authoritative syntax. `sdef` ships with the binary so it's automatically version-matched. All snippets below are sdef-derived and empirically validated against DT 4.2.2 (Research Findings § 099-research-5).

| What we want | DT 4 AppleScript (sdef-canonical) |
|---|---|
| Currently selected records | `tell app id "DNtp" to selected records` — DT4-preferred bulk form; sdef recommends over the legacy `selection` property "especially for bulk retrieval of properties like UUID" |
| Record by UUID | `get record with uuid "<UUID>"` |
| All records with tag X | `lookup records with tags {"X"} in database "Y"` — dedicated command `DTpacd92`; accepts a list of tags + optional `any:true` for OR semantics. **Not** `search "tags:X"` (that key is not honoured by DT 4's search syntax). |
| Enumerate smart groups | `parents of database "Y" whose record type is smart group` |
| Smart-group contents | Two-step: enumerate as above, then `set pred to search predicate of <sg>` and re-run via `search pred in root of database "Y"` — smart groups store a saved search, not a materialized list |
| Records inside a group at a path | `get record at "<path>" in database "Y"` (`DTpacd23`) — path format is finicky; the empirical probe found `"/"` and `"/<dbname>"` both return missing-value, so the convention requires further investigation in a 1-2 hour spike before the `--group` flag ships |
| Search scope (any search variant) | `search "<query>" in <group>` — the `in` clause requires a *record*, not a database. For database-wide search use `search "<q>" in root of database "Y"` |
| UUID + path of a record | `uuid of theItem`, `path of theItem` |

Every selector reduces to a list of (UUID, path) pairs. The ingest verb operates on those pairs uniformly.

### macOS-only constraint

DEVONthink is macOS-only (no Linux/Windows port, no plans for one). The `nx dt` command group is therefore macOS-only. Following the precedent set by the DT URI reader, non-darwin users get a clear error message at command-invocation time, not a subprocess crash. CI on Linux runners skips the DT command tests via `pytest.mark.skipif(sys.platform != "darwin", ...)`.

## Proposed Decision

Add a `nx dt` Click subcommand group inside the existing `conexus` package. Five verbs in v1, three of which are selection variants of the same underlying operation:

```
nx dt index --selection                        # whatever is selected in DT now
nx dt index --group <path-or-uuid>             # all items in a DT group
nx dt index --tag <name>                       # all items with this DT tag
nx dt index --smart-group <name>               # all items matching a DT smart group
nx dt index --uuid <UUID> [--uuid <UUID>...]   # explicit UUIDs (the smart-rule entry point)

nx dt open <tumbler-or-uuid>                   # reveal in DEVONthink
```

All `nx dt index` variants share a single internal pipeline:

1. Resolve the selector (selection / group / tag / smart-group / explicit UUIDs) to a `[(uuid, path), ...]` list via osascript. One osascript call per invocation regardless of selector — bulk-resolve, not one-call-per-item.
2. For each `(uuid, path)`: dispatch to the existing `nx index pdf` (or `nx index md` based on file extension) with `--source-uri x-devonthink-item://<uuid>`. The `meta.devonthink_uri` field is populated automatically by the indexer's catalog-registration hook because the source URI is in scope.
3. Honor the standard `--collection`, `--corpus`, `--dry-run` flags via passthrough so a caller can write `nx dt index --tag 'AI 2025' --collection knowledge__ai-2025`.

`nx dt open <tumbler-or-uuid>` resolves the argument:

- If it parses as a tumbler (`N.N.N`), look up the catalog entry and read `meta.devonthink_uri`. If absent, fall back to checking whether `source_uri` itself is `x-devonthink-item://`. If neither, error.
- If it parses as a UUID-shape, construct `x-devonthink-item://<UUID>` directly without a catalog lookup.

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

## Out of Scope

- Tag / topic synchronization (DT tags ↔ Nexus topics). Different taxonomies, different update semantics — a separate RDR.
- DT annotation / comment extraction into Nexus catalog meta. Read-only AppleScript surface on annotations is awkward; defer until there's a concrete query that wants the annotation text.
- Two-way write — Nexus writing back to DT (creating records, updating tags, etc.). Currently DT is the master for what's-in-the-collection; Nexus is the search/retrieval layer. Inverting that direction is a different design and bigger surface area.
- Other document managers (Zotero, Mendeley, Papers). Each has its own identity model; the right pattern is one URI scheme per manager (`x-zotero-item://`, etc.) with parallel CLI verbs (`nx zotero index --selection`). Out of scope here; `nexus-bqda`'s precedent is the template.
- A `nx dt watch` daemon. DT smart rules already cover the watch case; building a parallel daemon would duplicate functionality.

## Acceptance criteria

- `nx dt index --selection` works against a real DT instance (manual smoke; documented in `tests/e2e/devonthink-manual.md`).
- `nx dt index --uuid <UUID>` indexes a single DT-managed PDF and the resulting catalog entry has `source_uri = x-devonthink-item://<UUID>` and `meta.devonthink_uri` populated.
- `nx dt index --tag <name>` over a tag with N items produces N catalog entries; entries that fail to ingest (corrupted PDF, etc.) are reported in the summary but don't abort the batch.
- `nx dt open <tumbler>` for an entry with `meta.devonthink_uri` opens DEVONthink and reveals the record.
- Cross-platform CI passes on Linux without invoking osascript (skipif marker holds).
- A documented DT smart-rule recipe in `docs/devonthink-smart-rules.md` shows the inbox auto-index pattern.

## Out of band

A follow-up RDR (or bead) will scope the Claude Code plugin layer (`dt/` slash commands) once the CLI surface is settled. That plugin is purely a wrapper — it does not introduce new substrate.

## Research Findings

Recorded in T2 (`nexus_rdr/099-research-{1..5}`) on 2026-04-28. Pointers, not duplicates — read the full entries via `nx memory get --project nexus_rdr --title 099-research-N`.

1. **`099-research-1` — DT installed at 4.2.2; selection AppleScript works.** Verifies the substrate shipped in 4.17.0 (`nexus-bqda` + `nexus-srck`) is wired up on the user's machine; confirms `tell app id "DNtp" to selected records` returns UUID + path cleanly; surfaces 3 mounted databases (Inbox, Sims, Constantine) and the implied multi-database scoping question for `--group` / `--smart-group`.

2. **`099-research-2` — initial probe surfaced apparent gate blocker, since RESOLVED by `099-research-5`.** Three of four selectors as documented in the original draft (`every smart group`, `whose tags contains`, `every record of database`) failed empirically against DT 4.2.2. This was a documentation issue, not an architectural one — see finding 5 for the corrected dialect. The RDR draft has been updated to use the sdef-canonical forms and the blocker framing is withdrawn.

3. **`099-research-3` — osascript spawn cost ~100ms cold.** Empirical: 80-110ms over 5+ runs of selection-resolving AppleScript against DT 4.2.2. Matches the RDR's prior claim. AppleScript itself is sub-10ms; the cost is osascript fork+exec+JIT. Acceptable as a CLI baseline.

4. **`099-research-4` — prior-art survey: Hookmark, PyDT3, Org-DEVONthink.** All three converge on the same lessons: selection is the universal entry point, UUID is stable identity for *imported* content, path is stable for *indexed* content. Nexus already covers both via `meta.devonthink_uri` (UUID) and `file_path` (path) — substrate alignment validated. **No prior tool exposes DT as a CLI-addressable source with `--tag`/`--group`/`--smart-group` flags** — the v1 surface is genuinely novel in this dimension.

5. **`099-research-5` — authoritative DT 4.2.2 dialect from sdef + empirical (overrides RDR draft AND synthesizer).** The bundled `.sdef` (extracted via `sdef /Applications/DEVONthink.app`, 5764 lines) is the canonical reference — version-matched to the running app, supersedes website docs and forum posts. Key corrections: tag lookup uses the dedicated `lookup records with tags` command (not `search "tags:X"`); selection uses the `selected records` element (not the legacy `selection` property); smart groups enumerate via `parents whose record type is smart group`; `search`'s `in` clause requires a *record*, not a database. The RDR's AppleScript reference table now reflects all sdef-canonical forms; only `--group <path>` (path format finicky) needs a 1-2 hour spike before shipping. The synthesizer agent's `search "tags:X"` recommendation is wrong and should not be used.

**Net effect**: the v1 five-selector surface is viable. The dialect issues uncovered during research were documentation issues in the draft, not architectural gaps. The Proposed Decision section stands; the Context table has been corrected; only `--group <path>` carries a residual spike obligation before shipping.
