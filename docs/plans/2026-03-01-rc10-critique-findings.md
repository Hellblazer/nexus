# rc10 / 1.0.0 Readiness Critique Findings
**Date**: 2026-03-01
**Scope**: Comprehensive audit of all 10 domains for 1.0.0 release readiness
**Current version**: 1.0.0rc9

---

## Critique Summary

The project is well-engineered at the source-code and architecture levels. The CLI, hook scripts, registry, and storage-tier documentation are largely accurate and consistent. However, the CHANGELOG has a serious structural gap — rc7, rc8, and rc9 are completely missing — and several related artifacts carry compounding inaccuracies. Three specific incorrect strings in hook scripts will actively mislead users at the worst possible moment (when `nx` is not found). The skill count is wrong across four documents. These are concrete defects, not polish items.

---

## Critical Issues

### C-01: CHANGELOG missing rc7, rc8, and rc9 entries

- **Location**: `CHANGELOG.md:7` (`[Unreleased]` section) and lines 254–261 (comparison links)
- **Problem**: The changelog jumps from `[1.0.0rc6]` directly to `[Unreleased]`. Tags `v1.0.0rc7`, `v1.0.0rc8`, and `v1.0.0rc9` exist in the repository. None have changelog entries. The `[Unreleased]` comparison link still points to `v1.0.0rc5...HEAD` (line 254), not `v1.0.0rc9...HEAD`. The link for `[1.0.0rc5]` is duplicated at lines 256 and 257 — the second occurrence should be `[1.0.0rc6]` pointing to `rc5...rc6`.
- **Impact**: The release process (docs/contributing.md step 6) extracts release notes from `CHANGELOG.md` via a regex matching `## [X.Y.Z]`. Because rc7/rc8/rc9 entries do not exist, the GitHub releases for those tags were created with the fallback text `"Release 1.0.0rc7"` (and rc8, rc9) — bare placeholders. The pattern will repeat for 1.0.0 if the [Unreleased] content is not correctly tagged. Users cannot understand what changed between releases.
- **What the entries should contain** (derived from `git log`):

  **`[1.0.0rc7] - 2026-03-01`** (commits v1.0.0rc6..v1.0.0rc7):
  ```
  ### Added
  - ChromaDB Cloud quota enforcement (RDR-005): hard limit of 300 records per write;
    automatic pagination in indexer and migrate commands
  - File-size scoring penalty (RDR-006): chunks from large files are down-ranked
    proportionally — `score *= min(1.0, 30 / chunk_count)`. Applied to all `code__` results.
  - `nx search --max-file-chunks N`: pre-filters code results to files with at most N chunks
  - `nx index repo --chunk-size N`: configurable lines-per-chunk (default 150, min 1)
  - `nx index repo --no-chunk-warning`: suppress large-file pre-scan warning
  - Large-file pre-scan warning: detects code files exceeding 30× chunk size before indexing
  - T2 multi-namespace prefix scan (`t2_prefix_scan.py`): SubagentStart and SessionStart
    hooks now surface all `{repo}*` T2 namespaces, not just the bare project namespace
  ```

  **`[1.0.0rc8] - 2026-03-01`** (commits v1.0.0rc7..v1.0.0rc8):
  ```
  ### Added
  - nx workflow integration (RDR-008): CONTEXT_PROTOCOL.md search guidance table;
    agent updates for plan-auditor, deep-research-synthesizer, code-review-expert,
    deep-analyst, java-developer (Grep-primary patterns, two-query discovery, T3 storage templates)
  - serena-code-nav standalone skill: LSP-backed symbol navigation via Serena MCP
  - T1 ChromaDB HTTP server with PPID session sharing (RDR-010): SessionStart hook
    allocates a localhost port, launches `chroma run`, writes session file at
    `~/.config/nexus/sessions/{ppid}.session`. Child agents share scratch via PPID chain.
  - using-nx-skills skill: enhanced skill directory, announce step, red flags, registry triggers
  - SubagentStart hook: T1 scratch entries injected into spawned agent context

  ### Removed
  - `nx search --agentic` flag (RDR-009): multi-step Haiku query refinement removed
  - `nx search --answer`/`-a` flag (RDR-009): cited answer synthesis removed
  - `anthropic` package dependency removed (consequence of RDR-009; Anthropic key still
    checked by `nx doctor` for PM archival)

  ### Fixed
  - T1 server startup: `chroma run --log-level` flag removed (incompatible with chroma 1.x)
  - T1 cross-process session key: PPID-based session file path stabilized
  - T1 SESSIONS_DIR isolated in test suite (prevents cross-test contamination)
  ```

  **`[1.0.0rc9] - 2026-03-01`** (commits v1.0.0rc8..v1.0.0rc9):
  ```
  ### Added
  - Storage tier awareness for agents: T1 injection protocol, tier selection guidance
    injected via SubagentStart hook; `using-nx-skills` updated with tier decision table

  ### Fixed
  - FTS5 query sanitization: hyphenated terms (e.g. "T1-scratch") no longer crash
    `nx memory search` — hyphens are now escaped before FTS5 query execution
  ```

- **Recommendation**: Write all three sections with accurate content as shown above. Fix the `[Unreleased]` comparison link to `v1.0.0rc9...HEAD`. Fix the duplicated `[1.0.0rc5]` link — the second occurrence must become `[1.0.0rc6]: https://github.com/Hellblazer/nexus/compare/v1.0.0rc5...v1.0.0rc6`. Add missing links for `[1.0.0rc7]`, `[1.0.0rc8]`, `[1.0.0rc9]`.

---

### C-02: Hook scripts reference wrong package name for install

- **Location**: `nx/hooks/scripts/mcp_health_hook.sh:14` and `nx/hooks/scripts/setup.sh:17`
- **Problem**: Both scripts display `uv tool install nexus` as the remediation when `nx` is not found on PATH. The PyPI package name is `conexus`, not `nexus`. A user following this advice will get a package-not-found error or install an entirely different package.
  - `mcp_health_hook.sh:14`: `"nx CLI not found — run 'uv tool install nexus' or add nx to PATH"`
  - `setup.sh:17`: `echo '⚠ nx not found — install with: uv tool install nexus'`
- **Impact**: Users seeing this message at session startup will follow the wrong command. This is exactly the failure mode that a SessionStart hook warning should prevent.
- **Recommendation**: Change both to `uv tool install conexus`. Cross-check: `docs/getting-started.md:29`, `README.md:23`, and `nx/commands/nx-preflight.md:20` already use `conexus` correctly. The hook scripts are the only outliers.

---

### C-03: pyproject.toml Development Status classifier is wrong for 1.0.0

- **Location**: `pyproject.toml:26`
- **Problem**: `"Development Status :: 4 - Beta"` is the current classifier. For a 1.0.0 stable release, the correct classifier is `"Development Status :: 5 - Production/Stable"`. Releasing 1.0.0 to PyPI with `4 - Beta` sends contradictory signals: the version number says stable, the classifier says beta.
- **Impact**: Package indexers, dependency managers, and users making install decisions rely on this classifier. `pip install --pre` behavior can be affected by status.
- **Recommendation**: Change to `"Development Status :: 5 - Production/Stable"` for the 1.0.0 release commit. Keep `4 - Beta` for all rc releases.

---

### C-04: nx plugin CHANGELOG missing entries for rc8 and rc9 plugin changes

- **Location**: `nx/CHANGELOG.md` — only has entries through `[0.5.0]` (2026-02-28)
- **Problem**: The plugin version in `.claude-plugin/marketplace.json` is `1.0.0rc9`. Changes made in rc8 and rc9 to the plugin (serena-code-nav skill added, T1 injection in SubagentStart, using-nx-skills polished, rdr-accept added, storage tier awareness protocol) have no changelog entries. The plugin CHANGELOG stops at `0.5.0` which corresponds to rc7 content.
- **Impact**: The `contributing.md` release process (step 4) requires updating `nx/CHANGELOG.md`. That step was skipped for rc8 and rc9. For 1.0.0 the CHANGELOG must be current.
- **Recommendation**: Add `[0.6.0]` entry for rc8 changes (serena-code-nav, using-nx-skills polish, rdr-accept, SubagentStart T1 injection) and `[0.7.0]` entry for rc9 changes (storage tier awareness, tier protocol in skills). Version numbering for the plugin is independent — use the plugin's own semver progression, not the CLI version.

---

## Major Issues

### M-01: Skill count is wrong in four documents

- **Locations**:
  - `nx/README.md:3` and `nx/README.md:38`: "27 skills — 5 standalone + 15 agent-delegating + 7 RDR workflow"
  - `docs/getting-started.md:173`: "15 agents, 27 skills"
  - `README.md:14` and `README.md:101`: "27 skills"
- **Problem**: The `nx/skills/` directory contains 28 subdirectories. The `serena-code-nav` skill was added in rc8 (via `09ca4fd rdr: accept RDR-008`) but the README counts were never updated. The breakdown `5 standalone + 15 agent-delegating + 7 RDR = 27` is also wrong: serena-code-nav is a 6th standalone skill (it has no corresponding agent and is registered under `standalone_skills` in `registry.yaml:398`), making it `6 standalone + 15 agent-delegating + 7 RDR = 28`.
- **Impact**: A user checking the skill count against the directory will find a discrepancy. The directory listing in `nx/README.md` (lines ~46-100) also omits `serena-code-nav/` from the `skills/` tree.
- **Recommendation**: Update all four documents to `28 skills — 6 standalone + 15 agent-delegating + 7 RDR workflow`. Add `├── serena-code-nav/      # Standalone: LSP-backed symbol navigation via Serena MCP` to the directory tree in `nx/README.md`.

---

### M-02: CHANGELOG [Unreleased] contains rc6 content, not rc10 content

- **Location**: `CHANGELOG.md:7-14`
- **Problem**: The `[Unreleased]` section contains two items (file-size scoring penalty and `--max-file-chunks`). These were released in rc7, not deferred. They appear in `docs/cli-reference.md` as implemented features. This section should either be empty (if nothing is deferred from rc9) or contain genuinely unreleased work.
- **Impact**: The release.yml workflow extracts release notes from the matching `## [X.Y.Z]` heading. When rc10 is tagged, the `[Unreleased]` section content will be left dangling — the release notes extractor will not pick it up automatically unless the section is renamed.
- **Recommendation**: Move the two [Unreleased] items into the `[1.0.0rc7]` entry (they belong there — see C-01). Leave [Unreleased] empty or remove it and add a fresh one when actual unreleased work accumulates.

---

### M-03: contributing.md release process references wrong commit step

- **Location**: `docs/contributing.md:119-120`
- **Problem**: Step 5 of the release process says:
  ```
  git add pyproject.toml CHANGELOG.md .claude-plugin/marketplace.json
  git commit -m "Release vX.Y.Z"
  ```
  The `.claude-plugin/marketplace.json` path is correct (the file is at `/.claude-plugin/marketplace.json`). However, step 4 says to update `nx/CHANGELOG.md` as well, but the `git add` command in step 5 does not include `nx/CHANGELOG.md`. Also, the GLOBAL.md instructions require `bd sync` before committing any beads changes; there is no mention of that in the release process. For a project that heavily uses beads, omitting `bd sync` from the release checklist is an oversight that will cause a diverged `.beads/issues.jsonl` on release day.
- **Recommendation**: Add `nx/CHANGELOG.md` to the `git add` command in step 5. Add a `bd sync` step between steps 2 and 3 if any beads are updated as part of the release.

---

### M-04: getting-started.md omits `chroma` CLI as prerequisite for T1 agent sharing

- **Location**: `docs/getting-started.md` Prerequisites section (lines 8-22)
- **Problem**: The T1 architecture (RDR-010, documented in `docs/storage-tiers.md:15`) requires `chroma` (the ChromaDB CLI) to be on PATH for multi-agent scratch sharing. Without it, the session falls back to an isolated `EphemeralClient` and spawned agents cannot share scratch entries. The `storage-tiers.md` documents this fallback correctly. However, `getting-started.md` lists `git` and `ripgrep` as prerequisites but says nothing about the `chroma` binary. A user who installs `conexus` but not `chroma` will get degraded T1 behavior with no explanation at setup time.
- **Impact**: The hook scripts say T1 is "shared across all agents" — a user relying on this without `chroma` installed will not get sharing and may be confused by incorrect agent-to-agent coordination behavior.
- **Recommendation**: Add `chroma` to the prerequisites table with: "Required for T1 multi-agent scratch sharing (`nx scratch` cross-agent visibility). Install via `pip install chromadb` (provides the `chroma` CLI). Falls back to isolated scratch per-agent if missing."

---

### M-05: CHANGELOG comparison links have structural errors

- **Location**: `CHANGELOG.md:254-261`
- **Problem**: Multiple errors in the link table:
  1. `[Unreleased]: https://github.com/Hellblazer/nexus/compare/v1.0.0rc5...HEAD` — should be `v1.0.0rc9...HEAD`
  2. Line 256: `[1.0.0rc6]: ...v1.0.0rc5...v1.0.0rc6` — correct
  3. Line 257: `[1.0.0rc5]: ...v1.0.0rc4...v1.0.0rc5` — duplicate of line 256; should be `[1.0.0rc6]` → `v1.0.0rc5...v1.0.0rc6` (line 256 already covers this); this line is a duplicate copy-paste error
  4. Missing links for `[1.0.0rc7]`, `[1.0.0rc8]`, `[1.0.0rc9]`
- **Recommendation**: Fix `[Unreleased]` link to `v1.0.0rc9...HEAD`. Remove duplicate `[1.0.0rc5]` entry. Add:
  ```
  [1.0.0rc9]: https://github.com/Hellblazer/nexus/compare/v1.0.0rc8...v1.0.0rc9
  [1.0.0rc8]: https://github.com/Hellblazer/nexus/compare/v1.0.0rc7...v1.0.0rc8
  [1.0.0rc7]: https://github.com/Hellblazer/nexus/compare/v1.0.0rc6...v1.0.0rc7
  ```

---

### M-06: `nx/README.md` Standalone Skills section omits serena-code-nav

- **Location**: `nx/README.md:115-125` (Standalone Skills table)
- **Problem**: The five-row standalone skills table lists `brainstorming-gate`, `cli-controller`, `nexus`, `using-nx-skills`, `writing-nx-skills`. `serena-code-nav` is not present in the table despite being present in `nx/skills/serena-code-nav/SKILL.md`, registered in `registry.yaml:398-409`, and mentioned in the hooks README directory tree as of rc8.
- **Recommendation**: Add a row: `| serena-code-nav | LSP-backed symbol navigation via Serena MCP — find definitions, callers, type hierarchies, safe rename |`

---

### M-07: nx plugin CHANGELOG version in marketplace.json does not match plugin CHANGELOG

- **Location**: `.claude-plugin/marketplace.json` (version `1.0.0rc9`) vs `nx/CHANGELOG.md` (latest entry `0.5.0`)
- **Problem**: The plugin has two version numbering schemes in use simultaneously: the marketplace uses the CLI version (`1.0.0rc9`) and the plugin's own CHANGELOG uses a separate semver (`0.5.0`). This is confusing but not technically wrong if it is intentional. However, the plugin CHANGELOG is stale (stops at rc7-era content), which means the two version schemes compound: the marketplace says rc9 but the CHANGELOG has no entries for rc8 or rc9 plugin changes.
- **Recommendation**: Decide on one versioning strategy for the plugin (aligned with CLI, or independent semver) and document it in `contributing.md`. Then update `nx/CHANGELOG.md` to be current regardless of which scheme is chosen.

---

## Minor Issues

### N-01: `CHANGELOG.md` rc1 entry still references removed flags as features

- **Location**: `CHANGELOG.md:108-109` (`[1.0.0-rc1]` Added section)
- **Problem**: The rc1 entry lists `--agentic` and `--answer` as added features. These were removed in rc8 (RDR-009). For a complete and accurate historical record, the rc8 changelog entry (which C-01 says must be written) should note the removal. The rc1 entry is accurate as history; no change needed there. This is an observation confirming why the rc8 entry is important.
- **Recommendation**: Addressed by C-01.

---

### N-02: `pyproject.toml` missing "Changelog" URL

- **Location**: `pyproject.toml:52-56` (`[project.urls]`)
- **Problem**: The URLs table has `Homepage`, `Repository`, `Documentation`, and `Bug Tracker` but no `Changelog` entry. PyPI displays changelog links prominently when present.
- **Recommendation**: Add `"Changelog" = "https://github.com/Hellblazer/nexus/blob/main/CHANGELOG.md"` to `[project.urls]`.

---

### N-03: `docs/cli-reference.md` `-B` context flag undocumented

- **Location**: `docs/cli-reference.md:26-27`
- **Problem**: The CLI reference documents `-A N` and `-C N` (alias for `-A`) for context lines. The actual `search_cmd.py` source (line 94-97) implements `-A` and `-C` but not `-B`. This matches the docs. However, the description for `-C` says "alias for -A N" which is slightly misleading — `-C` and `-A` both set `lines_after` (lines shown after the match), not symmetric context. The table entry `| -C N | Show N lines of context after each result chunk (alias for -A) |` is accurate in behavior but naming convention (`-C` conventionally means symmetric context in tools like `grep`). This may confuse users expecting `-C` to show lines both before and after.
- **Recommendation**: Clarify the description: `| -C N | Show N lines of context after each result chunk (synonym for -A; does not show lines before the chunk) |`

---

### N-04: `docs/contributing.md` unit tests description is stale

- **Location**: `docs/contributing.md:21`
- **Problem**: "Unit tests use `chromadb.EphemeralClient` + bundled ONNX MiniLM model — no accounts needed." This was accurate before rc8 when T1 moved to an HTTP server architecture. Tests now use `SESSIONS_DIR` isolation (commit `010caa7`) rather than `EphemeralClient` directly. The claim is still approximately true (tests do not require external accounts) but the technical detail is slightly wrong.
- **Recommendation**: Update to: "Unit tests use an isolated T1 session (ephemeral, no server required) and do not require external accounts or API keys."

---

### N-05: `registry.yaml` `lifecycle.testing` smoke_test is a stub

- **Location**: `registry.yaml:589-590`
- **Problem**: The `lifecycle.testing` section contains `smoke_test: "Invoke each skill, verify no errors"` and `integration_test: "Run feature pipeline end-to-end"`. These are vague placeholders that describe nothing about how to actually run the tests. The actual test commands are `uv run pytest` (documented in `contributing.md`).
- **Recommendation**: Either remove the `lifecycle.testing` stub or replace with a reference to the actual test commands. As a registry descriptor this section adds no value as currently written.

---

### N-06: `nx/README.md` directory tree omits `serena-code-nav` from skills listing

- **Location**: `nx/README.md:76-99` (Directory Structure skills listing)
- **Problem**: All 27 listed skill directories are present in the tree, but `serena-code-nav/` is missing from the `skills/` subtree. The tree ends at `writing-nx-skills/`. Adding serena-code-nav would bring it to 28 entries, matching the actual filesystem.
- **Recommendation**: Add `├── serena-code-nav/      # Standalone: LSP-backed symbol navigation via Serena MCP` before `├── strategic-planning/`.

---

## Verification Performed

| Domain | Evidence gathered |
|--------|------------------|
| CHANGELOG | Full read of `CHANGELOG.md`; `git tag` to enumerate all rc tags; `git log v1.0.0rcN..v1.0.0rcN+1 --oneline` for rc6→rc7, rc7→rc8, rc8→rc9, rc9→HEAD |
| cli-reference.md | Full read; cross-referenced against `src/nexus/commands/search_cmd.py`, `scratch.py`, `index.py`, `memory.py`, `migrate.py`, `store.py` |
| storage-tiers.md | Full read; verified against T1 architecture in `src/nexus/db/t1.py` (referenced via hook behavior) and `docs/rdr/rdr-010` |
| getting-started.md | Full read; cross-checked install commands against `pyproject.toml` `name` field |
| contributing.md | Full read; verified release.yml workflow against described process |
| nx/README.md | Full read; verified skill counts against `ls nx/skills/`; verified hook table against `hooks.json` |
| registry.yaml | Full read; verified all 15 agents present and described; verified standalone_skills section |
| Source code scan | `grep -rn "TODO\|FIXME"` across `src/nexus/` — no TODOs or FIXMEs found in production paths |
| pyproject.toml | Full read; verified classifiers, URLs, version, authors, keywords |
| hooks.json + scripts | Full read of `hooks.json`; read all 8 hook scripts; verified script references exist |
| Install name | Confirmed pyproject.toml `name = "conexus"`; found two hook scripts using wrong `nexus` name |
| Skill count | Enumerated `nx/skills/` (28 directories); compared to README claims (27); identified serena-code-nav as the missing entry |

---

## Summary Table

| ID | Severity | File | Issue |
|----|----------|------|-------|
| C-01 | Critical | CHANGELOG.md | rc7, rc8, rc9 entries missing; comparison links wrong/duplicated |
| C-02 | Critical | nx/hooks/scripts/mcp_health_hook.sh:14, setup.sh:17 | Wrong package name `nexus` in install command (should be `conexus`) |
| C-03 | Critical | pyproject.toml:26 | `4 - Beta` classifier wrong for 1.0.0 stable release |
| C-04 | Critical | nx/CHANGELOG.md | Plugin changelog stops at 0.5.0; rc8 and rc9 plugin changes undocumented |
| M-01 | Major | nx/README.md:3,38; docs/getting-started.md:173; README.md:14,101 | Skill count 27 wrong (actual: 28); breakdown wrong (5 standalone, should be 6) |
| M-02 | Major | CHANGELOG.md:7-14 | [Unreleased] contains rc7 content, not actually unreleased |
| M-03 | Major | docs/contributing.md:119-120 | Release process git add omits nx/CHANGELOG.md; no bd sync step |
| M-04 | Major | docs/getting-started.md:8-22 | `chroma` CLI dependency not listed as prerequisite for T1 agent sharing |
| M-05 | Major | CHANGELOG.md:254-261 | Comparison links: [Unreleased] points to rc5, [1.0.0rc5] duplicated, rc7/rc8/rc9 links absent |
| M-06 | Major | nx/README.md:115-125 | serena-code-nav omitted from Standalone Skills table |
| M-07 | Major | .claude-plugin/marketplace.json, nx/CHANGELOG.md | Plugin version schemes diverged; CHANGELOG stale |
| N-01 | Minor | CHANGELOG.md:108-109 | rc1 entry references removed flags (addressed by C-01) |
| N-02 | Minor | pyproject.toml:52-56 | Missing "Changelog" URL in [project.urls] |
| N-03 | Minor | docs/cli-reference.md:27 | -C N description misleadingly suggests symmetric context |
| N-04 | Minor | docs/contributing.md:21 | Unit test description references EphemeralClient (pre-RDR-010) |
| N-05 | Minor | registry.yaml:589-590 | lifecycle.testing is a vague stub |
| N-06 | Minor | nx/README.md:76-99 | Directory tree omits serena-code-nav/ from skills listing |
