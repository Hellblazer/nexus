# Changelog

All notable changes to the conexus plugin are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [5.10.2] - 2026-06-05

Plugin version aligned with conexus 5.10.2 (T1 scratch session-id-divergence
fix: the MCP's `NX_SESSION_ID` lease key vs the SessionStart hook's
`current_session` could diverge on resume / multi-frontend / version skew,
hard-failing `nx scratch`; the Claude-ancestor-pid fallback now recovers it —
nexus-gff3g). No plugin-side changes.

## [5.10.1] - 2026-06-05

Plugin version aligned with conexus 5.10.1 (T2 daemon reliability fixes:
reclaim-contention, lease-takeover zero-daemon gap, bounded socket teardown,
reclaim-on-restart — nexus-we61e/64w50/saigj/nhqll).

## [5.10.0] - 2026-06-05

Plugin version aligned with conexus 5.10.0 (RDR-149 unified daemon-lifecycle
substrate on the nexus package side).

### Fixed

- **phase-review-gate close hook: sentinel-staleness check restored (RDR-149
  P4/P5).** The hook anchored its "sentinel must postdate session start" check
  on the `t1_addr.<claude_pid>` file, which RDR-149 P4 stopped writing (T1 now
  keys its lease on the session-id). The check silently no-opped, accepting
  stale PASSED sentinels from prior sessions. It now anchors on the
  `current_session` pointer, restoring the fail-closed staleness guard.

## [5.9.3] - 2026-06-04

Plugin version aligned with conexus 5.9.3. No plugin-side changes; the release
is the RDR-146 catalog-behind-daemon work (#1046 starvation fix) on the nexus
package side.

## [5.9.2] - 2026-06-03

Plugin version aligned with conexus 5.9.2. The bug-fix batch (#1068 mcpb local
embedder, #1069/#1073 plan scope_tags, #981 `nx index md --collection`, #1049
LaTeX) is server-side / CLI; no plugin-side changes.

## [5.9.1] - 2026-06-02

Plugin version aligned with conexus 5.9.1. The first-run banner delivery fix
(RDR-126 §3 / nexus-vlo2b) is server-side (`nx-mcp`); no plugin-side changes.

## [5.9.0] - 2026-06-02

### Changed

- **`daemon_uninstall` added to the auto-approve allow-list (RDR-126 §4).**
  `hooks/scripts/auto-approve-nx-mcp.sh` now auto-approves the new
  `mcp__plugin_conexus_nexus__daemon_uninstall` tool (its own `confirm=false`
  default + dry run remains the destructive-op safety gate).

Plugin version aligned with conexus 5.9.0. The first-run banner and
`daemon_uninstall` tool are server-side (`nx-mcp`); no other plugin-side changes.

## [5.8.0] - 2026-06-02

### Added

- **Version-lockstep SessionStart hook (RDR-143).** The plugin now ships two
  new hook scripts, `hooks/scripts/version_lockstep_hook.py` and
  `hooks/scripts/version_lockstep_action.py`, wired into a dedicated
  `SessionStart` (matcher `startup`) block in `hooks/hooks.json`. On a detected
  plugin/CLI version skew the hook nudges and runs a detached, extras-preserving
  upgrade that takes effect next session. Fail-safe (never blocks startup), skips
  dev/editable installs, and never strips the `[local]` extra. This closes the
  silent-drift gap between the marketplace-pinned plugin version and the
  independently-updated `nx` CLI.

## [5.7.0] - 2026-06-02

Plugin version aligned with conexus 5.7.0. No plugin-side component changes; the
release ships the `nx`-core RDR-144 guided-onboarding feature (`nx init`,
embedder choice, safe 384 to 768 migration, `nx doctor` advisories). The MCP
server now surfaces the embedder advisory to plugin/Desktop/Cowork users via
its server instructions.

## [5.6.2] - 2026-06-01

Plugin version aligned with conexus 5.6.2. No plugin-side changes; the release
ships the `nx`-core P0 hotfix (#1065) that restores local-mode `search` /
`query` / `store_put` through the T3 daemon by embedding client-side, plus an
all-collections-dimension-skipped diagnostic and a collection-model display fix.

## [5.6.1] - 2026-06-01

Plugin version aligned with conexus 5.6.1. No plugin-side changes; the release
ships `nx`-core bug fixes that restore local-mode search (#1058) and collection
renames (#1057) on the 4.28→5.6.0 upgrade path, the `mineru-api` PATH resolver
(#1059), collection-name overflow guidance (#1060), daemon-unreachable log
de-spam (#1048), the RDR-141 version-skew single-writer fix (#1055), and
`nx doctor` / `nx upgrade --dry-run` / T2-down diagnostics improvements (#1061).

## [5.6.0] - 2026-05-31

Plugin version aligned with conexus 5.6.0. No plugin-side changes; the release
ships the RDR-140 T2 daemon supervisor & ownership model (single-flight
election, loser quiet-attach, ownership-aware reaping, crash-loop guard +
`restarts_in_window` status) and the binary-asset SKIP classifier in the
`nx` core.

## [5.5.1] - 2026-05-31

### Fixed

- **Routing hooks now deliver their deny reason to the model.** The conexus
  routing hooks (`git add` wildcard guard, phase-review-close gate) emitted only
  the legacy `reason` field, which current Claude Code does not read on a deny.
  They now emit `permissionDecisionReason` (full remediation, model-facing) plus
  a short top-level `systemMessage` transcript banner. Plugin version aligned
  with conexus 5.5.1. [nexus-rpvqu]

## [5.5.0] - 2026-05-31

### Added

- **DEVONthink integration (RDR-139).** New `devonthink` MCP server
  (`nx-mcp-devonthink`) ships with the plugin alongside `nexus` and
  `nexus-catalog`. It advertises ~17 curated DEVONthink tools plus the
  `dt_incorporate` composite when DEVONthink is reachable, and only a
  `devonthink_status` stub when it is absent (it always spawns, gating
  internally). Tools surface as `mcp__plugin_conexus_devonthink__*`. Declared
  `alwaysLoad:false` in `conexus/.mcp.json` as a tool-search startup
  optimization. The `nx dt` CLI gains `capture`, `highlights`, and the layered
  `index` flags (`--link-semantic` / `--writeback` / `--enrich` / `--dt-content`
  / `--highlights` / `--extractor`). See the root CHANGELOG for the full layer
  list and `docs/mcp-servers.md` for the tool catalog.

## [5.4.5] - 2026-05-30

### Fixed

- substantive-critic is now wired into the developer review loop across all five
  orchestration surfaces (developer agent, development skill, implement command,
  orchestration reference pipelines, strategic-planner review gates). The
  developer agent previously stopped after code-review, skipping the critic,
  because no surface named it on the ad-hoc OR plan-driven path. Both reviewers
  now run as a non-optional pair; the developer hands back rather than
  self-committing.

## [5.4.4] - 2026-05-30

### Fixed

- sn session-start banner made backend-agnostic (JetBrains + LSP), completing
  the 5.4.3 backend-agnostic work. The banner edit was lost in 5.4.3 (only the
  grep→Serena redirect message landed); a post-release shakeout caught it.

## [5.4.3] - 2026-05-29

### Fixed

- Routing-hook deny messages now reach the model via `permissionDecisionReason`
  + `systemMessage` (previously emitted only under the unread `reason` key, so
  denies arrived as a bare "denied"). The grep→Serena redirect is now
  backend-agnostic (JetBrains + LSP) and lists concrete remedies.

## [5.4.2] - 2026-05-29

### Fixed

- **Shell-quoting safety for all 25 slash commands.** `$ARGUMENTS` is
  substituted textually into the `` !`…` `` preamble line *before* the shell
  parses it (confirmed against Claude Code skills docs), so any prompt
  containing a double-quote, backtick, paren, or `$` broke the command with
  `(eval):1: unmatched "`. Hit in practice by `/conexus:architecture` with a
  prose task description. Fix: the 16 `command-context` commands (which ignore
  their args) and `rdr-create` / `rdr-list` (preamble ignores args; title is
  free-form) drop the arg entirely. The 5 `rdr preamble` commands whose arg is
  a pure token (`rdr-show`, `rdr-gate`, `rdr-accept`, `rdr-research`,
  `rdr-audit` — id / `add N` / project name) switch to single-quoted
  `'$ARGUMENTS'`: behaviourally identical (the preamble re-joins args), immune
  to every metacharacter, and the token form cannot contain the lone
  single-quote residual (a literal apostrophe). The 2 commands that can carry
  free-form text (`rdr-close --reason`, `phase-review-gate` deferral
  justifications) drop the arg from the shell line entirely and instead parse
  the id from `$ARGUMENTS` in the body, then load targeted context by invoking
  the preamble via the Bash tool with the id as a real argv token — closing the
  apostrophe residual completely.
- Verified end-to-end, not just by reasoning: `test_command_shell_quoting_e2e`
  reproduces Claude Code's textual-substitution + shell `eval` against hostile
  inputs (quotes, parens, backticks, `$(…)`, `&&`) in both bash and zsh, and
  asserts dropped-arg commands survive every input while single-quoted commands
  pass injection payloads through INERT (the program receives one intact literal
  token; no subshell/operator executes). A static guard
  (`test_no_command_file_double_quotes_arguments_in_backtick`) locks the safe
  form across every command surface. Hooks were audited and are clean (they
  escape stdin via `python3 … json.dumps`, never splicing untrusted input into a
  shell line).

## [5.4.1] - 2026-05-28

Plugin version aligned with conexus 5.4.1. No plugin-side changes; the
release is a single nx CLI fix (`nx index repo` crash on a never-cataloged
repo with a legacy `repos.json` collection name, nexus-5ut2a).

## [5.4.0] - 2026-05-28

Plugin version aligned with conexus 5.4.0. One plugin-side change; the
release is otherwise nx CLI / library work (RDR-137 `repos.json`
elimination, T2/T3 daemon reliability, `nx enrich aspects` hardening).

### Changed

- SessionStart hook `nx daemon t2 ensure-running` raised from
  `--timeout=5` to `--timeout=10`, and the hook step's wrapper timeout
  from 10s to 15s (nexus-u3mfr). A cold-start T2 daemon runs its one-time
  startup migration before it binds, which can cross 5s on a healthy boot
  and printed a spurious "did not become reachable" warning. The larger
  budget lets a healthy cold start complete; the underlying wait now also
  fails fast if the spawned process actually dies.

## [5.3.1] - 2026-05-28

Plugin SessionStart hook updates only (no agent / skill / command
changes). Two silent-failure bugs fixed and one new strategic-hint
signal added. Activation requires the hosting `nx-mcp` to restart.

### Fixed

- `t2_prefix_scan.py` now uses stdlib `sqlite3` instead of importing `nexus.db.t2`, so the `## T2 Memory (Active Project)` SessionStart section no longer silently disappears on bare-Python wrappers (nexus-vg6d4).
- `generate_context_l1` dedups identical `(collection, label, doc_count)` rows so the `## Knowledge Map` SessionStart section no longer shows the same label five times when a collection is in a degenerate clustering state (nexus-9iw41).

### Added

- `## Hygiene` block in `session_start_hook.py`: emits actionable maintenance signals only when present, silent when healthy. v1 signal: L1 cache age > 7 days (nexus-1if7b).

## [5.3.0] - 2026-05-27

Plugin version aligned with conexus 5.3.0. No plugin-component
(agent/skill/command/hook) files changed; the release is a memory write-path
feature (`nx memory put --merge`) plus a T2 aspect-worker single-writer fix,
both in the `nx` CLI / MCP substrate the plugin consumes.

## [5.2.0] - 2026-05-27

Plugin version aligned with conexus 5.2.0 (RDR-129 T2 daemon write-path
hardening). The user-visible plugin surface gains two `nx doctor` checks and
a softened integrity check; no plugin-component (agent/skill/command/hook)
files changed.

### Added

- **`nx doctor` gains `T2 daemon singleton` (hard error on >1 daemon per db) and `T2 best-effort writes` (soft warning + dropped-write count)** (RDR-129 A3/B4).

### Fixed

- **`nx doctor` reports a transient FTS5 write-lock during active indexing as a soft warning, not a hard failure** (RDR-129 B4).

## [5.1.5] - 2026-05-27

### Changed

- **RDR-130 P2: the 16 agent-relay commands now inject their preamble via `` !`nx command-context <name> -- "$ARGUMENTS"` ``** instead of inlined bash (analyze-code, architecture, create-plan, implement, debug, deep-analysis, enrich-plan, knowledge-tidy, pdf-process, plan-audit, research, review-code, substantive-critique, test-validate, nx-preflight, continuation). With P1 (5.1.4) this completes RDR-130: no command inlines bash, and a static guard locks the single-line form across all 25 commands.

### Fixed

- **MCP operator tools no longer prompt for permission (nexus-k1vr5):** added operator_filter / operator_check / operator_verify / operator_groupby / operator_aggregate to the `PermissionRequest` auto-approve allow-list, plus a drift guard parametrized over the live MCP registry against future omissions.

## [5.1.4] - 2026-05-27

### Changed

- **RDR-130 P1: the 9 RDR-lifecycle commands now inject their preamble via `` !`nx rdr preamble <name> -- "$ARGUMENTS"` ``** instead of inlined bash. rdr-create, rdr-list, rdr-show, rdr-gate, rdr-accept, rdr-close, rdr-research, rdr-audit, and phase-review-gate became single-line invocations; the preamble logic moved into the tested `nx` CLI. Removes the fenced-block truncation brittleness (the 5.1.3 hotfix was interim; this is the structural fix). The 9 dead `resources/rdr_commands/*.py` scripts were deleted and a static guard locks the single-line form.

## [5.1.3] - 2026-05-26

### Fixed

- **Slash-command preambles no longer truncate on a literal triple-backtick (nexus-61fzg).** The 5.1.2 fenced-block conversion broke 17/25 commands because Claude Code closes a fenced-bang block at the first literal triple-backtick in the source. Block sources are now free of literal triple-backticks (shell drops cosmetic fence-echoes; RDR Python builds the fence at runtime), verified through real Claude Code. Durable fix tracked in RDR-130 (move preamble logic into the nx CLI).

## [5.1.2] - 2026-05-26

### Fixed

- **All slash commands execute their context preamble again (nexus-ln9y5).** The 5.1.1 fix (nexus-t1b1k) did not take effect: every command still wrapped its preamble in a `!{ ... }` brace block, which is not a recognized Claude Code bash-injection syntax (only inline backtick `` !`cmd` `` and the fenced ` ```! ` block execute), so the brace form was emitted as raw source and the preamble never ran. Moving heredocs to scripts in 5.1.1 also introduced a by-path failure because `$CLAUDE_PLUGIN_ROOT` is empty in the command-bash context. All 25 commands now use the documented fenced form; the 9 RDR-lifecycle preambles inline their script (byte-synced to `resources/rdr_commands/*.py`). Verified against a real Claude Code by cc-validation scenario 19.
- **Project-type detection in `analyze-code`, `architecture`, `create-plan`, and `implement` now spans ~21 ecosystems (nexus-ln9y5).** The four ad-hoc detectors recognized only Maven, Gradle, and Node, mislabeling Python projects "Unknown". One shared marker-file detector now lists every detected stack.

## [5.1.1] - 2026-05-26

### Fixed

- **RDR-lifecycle commands render their discovery headers again (nexus-t1b1k).** The 9 RDR-lifecycle slash commands (`rdr-create`, `rdr-list`, `rdr-show`, `rdr-gate`, `rdr-accept`, `rdr-audit`, `rdr-close`, `rdr-research`, `phase-review-gate`) wrapped their discovery script in a Python heredoc inside the `!{ }` block, which Claude Code's command runner emits as raw source instead of executing. Each is now an extracted `resources/rdr_commands/*.py` script invoked by path; a plugin-structure test guards against reintroducing the heredoc form.

## [5.1.0] - 2026-05-25

Plugin version aligned with conexus 5.1.0. No plugin-side changes. The
release is RDR-128 (T2 single-writer enforcement), a root-cause fix for
the `database is locked` daemon crash-loop band-aided across 5.0.2 to
5.0.4: the hot and automated writers now route through the T2 daemon, the
daemon's startup migration is lock-tolerant, `ensure-running` aborts a
version-cycle when the DB lock is held, and `nx doctor
--check-storage-boundary` hard-fails any new direct `memory.db` open
without a documented justification. See root `CHANGELOG.md`.

## [5.0.4] - 2026-05-25

Plugin version aligned with conexus 5.0.4. The release wires the
version-aware `nx daemon t2 ensure-running` into install/upgrade paths
(nexus-5ldk1) so a stale T2 daemon is brought to the installed version
without a manual restart; the plugin's session-start hook already calls
`ensure-running`, so it self-heals on install. See root `CHANGELOG.md`
§ 5.0.4.

## [5.0.3] - 2026-05-25

Plugin version aligned with conexus 5.0.3. No plugin-side changes; the
release carries a T2 daemon observability fix (nexus-n8sbw) and an
integration-test corpus skip-guard (nexus-gudwb) in the core package.
See root `CHANGELOG.md` § 5.0.3.

## [5.0.2] - 2026-05-24

Plugin version aligned with conexus 5.0.2. Runtime fixes in the bundled
MCP server (orphan T1 chromadb sweep at startup, aspect-queue WAL
contention tolerance) plus a corrected plugin-rename drift hint. See
root `CHANGELOG.md` § 5.0.2 for details.

### Fix: plugin-rename drift hint requires BOTH install AND reload

`nx doctor`'s plugin-name-drift hint now instructs both
`/plugin install conexus@nexus-plugins` and `/reload-plugins` to
migrate the renamed `nx` plugin. The earlier "reload alone" guidance
was insufficient on a fresh shell.

## [5.0.1] - 2026-05-24

### Feature: tool annotations on all 41 MCP tools

Every MCP tool now carries `title` and `readOnlyHint` annotations via the
MCP `tools/list` response. Tools that mutate state additionally carry
`destructiveHint`. This was the headline Connector Directory submission
prerequisite. See root `CHANGELOG.md` § 5.0.1 for the read-only /
write-non-destructive / destructive classification table.

### Docs: support URL added to plugin manifest

`bugs` URL added to `conexus/.claude-plugin/plugin.json` pointing to
`https://github.com/Hellblazer/nexus/issues`. Surfaced for users seeking
the support channel.

## [5.0.0] - 2026-05-24

### Breaking: plugin renamed `nx` → `conexus` (nexus-mkj6u)

Plugin name changed from `nx` to `conexus`. Skill prefix `/nx:foo` → `/conexus:foo`. MCP tool prefix `mcp__plugin_nx_*` → `mcp__plugin_conexus_*`. Plugin install command: `/plugin install conexus@nexus-plugins`. See root `CHANGELOG.md` § 5.0.0 for full migration notes.

### Docs: four-round `nx → conexus` scrub across plugin internals

Storage-tier prose (`nx T3 store` → `T3 store`), skill cross-references (`nx:serena-code-nav` → `/conexus:serena-code-nav`), agent dispatch values (`subagent_type="nx:strategic-planner"` → `subagent_type="conexus:strategic-planner"`), section headers, and frontmatter descriptions all updated. Directory and file names with `nx` in them (`using-nx-skills/`, `writing-nx-skills/`, `nx-preflight.md`) intentionally kept for slash-command stability.

## [4.34.6] - 2026-05-23

### Fix: `/nx:continuation` chat output ergonomics

Three changes to the slash command shipped in 4.34.5:

- Handoff now written to `/tmp/nexus-continuation-<repo>-<slug>-<date>.md`
  (was `~/.cache/nexus/continuations/`). `/tmp` is purged on reboot;
  `~/.cache` accumulated stale handoffs forever.
- Chat response is plain text on its own line, not a wall of prose. A
  one-line hint, blank separator, then the bare `cat <path>` command on
  its own line. Triple-click selects only the command.
- Auto-priming the system clipboard was prototyped (pbcopy, `launchctl
  asuser pbcopy`, OSC 52 plain and DCS-wrapped) and abandoned. Remote tmux
  over mosh drops every clipboard escape between Bash and the local
  NSPasteboard. Plain text on its own line is the only portable affordance;
  the skill body documents the dead-end.

## [4.34.5] - 2026-05-23

### New utility command: `/conexus:continuation`

Session-close handoff prompt generator. Captures branch, beads,
PRs, T2 memory, and Claude Code auto-memory (if present) into a
10-section continuation document under
`~/.cache/nexus/continuations/`. Emits a paste-ready compressed
prompt in chat for the next session's bootstrap.

Argument optional; defaults to a slug derived from the current
branch. Same-day re-runs append HHMM so nothing is overwritten.

Registry entry: `utility_commands.continuation` in
`conexus/registry.yaml`.

### Fix: `phase_review_close_requires_gate` hook trigger tightened (GH #931)

The PreToolUse hook (`conexus/hooks/scripts/routing/phase_review_close_requires_gate.py`)
previously matched `\b(phase|review)\b` anywhere in the full `bd show`
output and false-positived on implementation beads in phased plans
whose description, parent epic, or rationale mentioned "phase" or
"review". The trigger now matches against the title line only with a
narrow regex requiring `Phase N review gate` or `PN phase-review-gate`.
Four regression tests added.

## [4.34.4] - 2026-05-23

### Restored tool/skill invocation discipline

Tuning for Claude Code v2.1.69+ (built-in tool deferral) and Opus 4.7
(literal instruction-following + reduced default tool calls). Without
these changes, plugin-heavy users were seeing Serena, sequential-thinking,
and nexus only fire when explicitly told.

- `conexus/.mcp.json`: added `"alwaysLoad": true` to `sequential-thinking`,
  `nexus`, and `nexus-catalog`. Claude Code v2.1.121+ honours this per
  server to skip tool-search deferral so schemas load eagerly.
- `conexus/skills/using-nx-skills/SKILL.md` and `conexus/skills/plan-first/SKILL.md`:
  rewrote descriptions and opening rules as MUST-form imperatives.
  Opus 4.7 reads soft hints as suggestions; explicit "MUST" / "defect"
  language survives the literalism shift. The `"Use when"` prefix
  required by `tests/test_plugin_structure.py::TestSkillDescriptionCSO`
  is preserved.

References: [anthropics/claude-code#31002](https://github.com/anthropics/claude-code/issues/31002),
[Opus 4.7 model card](https://platform.claude.com/docs/en/about-claude/models/whats-new-claude-4-7).

## [4.34.3] - 2026-05-22

Plugin version aligned with conexus 4.34.3 — documentation-only
release. ``conexus/README.md`` gains a SessionStart-hook explainer
showing how the plugin auto-spawns the T2 daemon on every Claude
Code session start, plus a cross-link to ``docs/container-integration.md``
for the full multi-process / Claude Cowork integration story.

No agent / skill / hook / MCP-server changes. See the conexus
4.34.3 entry in the root CHANGELOG for the full doc-sweep details.

## [4.34.2] - 2026-05-22

Plugin SessionStart hook now runs
`nx daemon t2 ensure-running --quiet --timeout=5` so the T2 daemon
auto-spawns on every Claude Code session start. Closes the
daemon-not-running cliff 4.34.1 introduced: fresh
`pip install conexus` + `/plugin install conexus-plugins` produces a working
substrate on first session without any manual incantation.

The hook is idempotent — when the daemon is already running (from
`nx daemon t2 install --autostart` or a manual start) it's a
silent no-op (~5ms).

## [4.34.1] - 2026-05-22

Plugin version aligned with conexus 4.34.1. No plugin-side changes;
this is a substrate patch release fixing the CLI-vs-daemon gap
that 4.34.0 left open. See the conexus 4.34.1 entry in the root
CHANGELOG for the full RDR-120 P6 follow-up details.
## [4.34.0] - 2026-05-22

Plugin version aligned with conexus 4.34.0. No plugin-side changes
in this release — RDR-120 is a substrate-only arc. The substrate
ships behind a daemon model; the conexus plugin's MCP tools and skills
continue to work unchanged because they call through the same
``T2Database`` / ``T3Database`` factories that now route to the
daemons automatically.

See the conexus 4.34.0 entry in the root CHANGELOG for the full
RDR-120 P0 → P6 details.

## [4.33.1] - 2026-05-21

Plugin version aligned with conexus 4.33.1. RDR-125 routing-hook
ownership migration: `grep_for_symbols_redirects_to_serena.py`
moved out of nx into the sn plugin (its rightful home — it
redirects to Serena MCP tools that sn ships).

### Removed (RDR-125 P1)

- `conexus/hooks/scripts/routing/grep_for_symbols_redirects_to_serena.py`
  (moved to sn — see sn/CHANGELOG.md if it exists)
- Corresponding entry in `conexus/hooks/scripts/routing/registry.yaml`
- Corresponding PreToolUse:Bash entry in `conexus/hooks/hooks.json`

### Updated (RDR-125 P2)

- `conexus/hooks/scripts/routing/README.md` documents the ownership
  rule (each plugin owns rules whose deny message redirects to its
  own tools) and the cross-plugin aggregate-cap accounting.

### Notes

- Two new monorepo-level CI lints landed under `tests/`:
  `test_routing_lib_drift.py` (byte-equality between vendored
  framework copies) and `test_routing_registry_aggregate_cap.py`
  (cross-plugin 4-hook cap + no-duplicate-names enforcement).

## [4.33.0] - 2026-05-20

Plugin version aligned with conexus 4.33.0. RDR-121 lands the routing-
hook substrate in `conexus/hooks/scripts/routing/` plus three PreToolUse
hooks and a sentinel writer in `conexus/skills/phase-review-gate/`. See
the top-level CHANGELOG for the full list.

### Added (RDR-121)

- `conexus/hooks/scripts/routing/_lib.py` and `registry.yaml`: framework
  for Python-native PreToolUse routing hooks.
- `conexus/hooks/scripts/routing/grep_for_symbols_redirects_to_serena.py`:
  redirects `grep` / `rg` of symbol-shaped patterns on code files to
  the Serena `find_symbol` / `find_referencing_symbols` MCP tools.
- `conexus/hooks/scripts/routing/git_add_all_redirects_to_explicit_paths.py`:
  blocks `git add -A` / `git add .` / `git add --all`.
- `conexus/hooks/scripts/routing/phase_review_close_requires_gate.py`
  (fail-closed): blocks `bd close` on phase-review beads unless
  `/conexus:phase-review-gate` has written a PASSED sentinel for the
  bead's `(rdr-id, phase)`.
- `conexus/skills/phase-review-gate/SKILL.md` and the command preamble
  now write the PASSED sentinel that the routing hook reads.

### Notes

- All three routing hooks honor the `# routing-allow: <reason>=8 chars>`
  escape token. The escape is logged for audit.

## [4.32.14] - 2026-05-20

Plugin version aligned with conexus 4.32.14. One repo-local skill
discovery fix; otherwise no plugin-side behavior changes.

### Fixed

- Repo-local skill discovery: `.claude/skills/release.md` moved to
  `.claude/skills/release/SKILL.md` so the `Skill` tool can find it
  by name. This is the repo-local equivalent of the standard
  `conexus/skills/<name>/SKILL.md` plugin layout. Flat `.md` files in the
  parent directory are invisible to auto-discovery.

## [4.32.13] - 2026-05-19

### Added: phase-review-gate (restored from archive/develop-2026-05-19)

- **`/conexus:phase-review-gate` slash command**: two-pass cross-walk gate
  for RDR §Approach items at phase boundaries. Pass 1 enumerates all
  numbered §Approach items; Pass 2 validates each has a bead-shaped
  evidence pointer. Blocks phase close when any approach item is
  unaccounted for. Modeled on the RDR-065 Problem Statement Replay
  gate (same Pass-1/Pass-2 preamble shape).
- **Regression test**: `tests/test_phase_review_gate.py` includes a
  direct regression against the actual RDR-112 Phase 1 (nexus-52lb,
  2026-05-15) closing-bead set; the gate blocks because §Approach
  item 2 (T3 daemon) had no closing bead, which is exactly the silent
  scope reduction that was discovered three phases later. Six tests
  in `TestRDR112Regression` skip with an explicit reason when the
  RDR-112 file is absent from `main` (e.g. before the companion
  tombstone PR merges); they auto-reactivate once the file is present.
- **`conexus/skills/using-nx-skills/SKILL.md`** updated with a phase-
  boundary paragraph in the RDR-lifecycle section, catted at
  SessionStart by `conexus/hooks/hooks.json`.
- **`conexus/hooks/scripts/subagent-start.sh`** updated with a conditional
  injection that fires on tasks matching phase/close/review-gate/
  cross-walk patterns, surfacing the gate to subagents doing phase-
  adjacent work.
- Originally built as bead `nexus-j327` on the (now scrapped) RDR-112
  arc (commit `122feaff`, 2026-05-17). Skill itself is substrate-
  agnostic and applies to any RDR. Restored to `main` as a P0
  prerequisite of RDR-120 (storage substrate split with co-shipped-
  consumer moratorium); RDR-120 §Enforcement Backstops cites this
  skill as a load-bearing per-phase cross-walk mechanism.

## [4.32.12] - 2026-05-13

Plugin version aligned with conexus 4.32.12. No plugin-side changes
in this release — root-package work is CI fixes (collection-cascade
lint, JSON-parse hardening) and the RDR-111/112/113 architecture
cycle. See root `CHANGELOG.md`.

## [4.32.3] - 2026-05-11

Plugin version aligned with conexus 4.32.3. No plugin-side changes;
the release closes the MinerU config-drift root cause (nexus-oa7r) —
PID file is now canonical, persistent config is no longer touched
by ephemeral auto-restarts. See root ``CHANGELOG.md``.

## [4.32.2] - 2026-05-11

Plugin version aligned with conexus 4.32.2. No plugin-side changes;
the release surfaces MinerU server unreachable state in ``nx doctor``
+ warn-on-fallback in pdf_extractor (nexus-h1jk). See root
``CHANGELOG.md``.

## [4.32.1] - 2026-05-11

Plugin version aligned with conexus 4.32.1. No plugin-side changes;
the release fixes a 4.32.0 migration-registration bug (nexus-m3dp)
that left the RDR-109 Phase 5 ``salient_sentences`` column missing
for users upgrading from 4.31.7. See root ``CHANGELOG.md``.

## [4.32.0] - 2026-05-11

Plugin version aligned with conexus 4.32.0. No plugin-side changes;
the release ships RDR-109 (Honest Local-Mode Naming + Cross-Encoder
Salience, 5 phases), RDR-108 Phase 1c PK migration reland +
``source_path`` column drop + je0b doc_id backfill, an AST chunker
mid-identifier fix, and two CI flake fixes. See root ``CHANGELOG.md``
for the full deltas.

## [4.31.7] - 2026-05-10

Plugin version aligned with conexus 4.31.7. No plugin-side changes;
the release ships a Linux-race fix in
``nx catalog synthesize-log --force`` (skip ``*.db-shm`` during
snapshot copytree). See root ``CHANGELOG.md``.

## [4.31.6] - 2026-05-10

Plugin version aligned with conexus 4.31.6. No plugin-side changes;
the release ships a test-assertion patch that 4.31.5 missed (mirror
of the 4.31.3 deferral flip for je0b). See root ``CHANGELOG.md``.

## [4.31.5] - 2026-05-10

Plugin version aligned with conexus 4.31.5. No plugin-side changes;
the release re-defers the RDR-108 Phase 1c PK migrations that 4.31.4
attempted to reland, but ships the ``_resolve_doc_id`` substrate so
the next reland is a one-line registry change. See root
``CHANGELOG.md``.

## [4.31.4] - 2026-05-10

Plugin version aligned with conexus 4.31.4. No plugin-side changes;
the release re-lands the RDR-108 Phase 1c PK migrations
(``nexus-je0b``) with a companion ``DocumentAspects.upsert``
doc_id resolver that auto-derives doc_id when callers pass empty.
``nexus-ocu9.11`` stays deferred pending a wider refactor of
DocumentAspects read/write methods. See root ``CHANGELOG.md``.

## [4.31.3] - 2026-05-10

Plugin version aligned with conexus 4.31.3. No plugin-side changes;
the release defers ``nexus-ocu9.11`` from the migration registry
pending companion fixes to the runtime upsert + enrich paths. See
root ``CHANGELOG.md``.

## [4.31.2] - 2026-05-10

Plugin version aligned with conexus 4.31.2. No plugin-side changes;
the release ships a migration-ordering fix in the conexus package
that makes ``nexus-ocu9.11`` defer (``MigrationRetry``) when its
prerequisite ``je0b`` hasn't run yet. See root ``CHANGELOG.md``.

## [4.31.0] - 2026-05-10

Plugin version aligned with conexus 4.31.0. No plugin-side changes;
the release ships in the conexus package: RDR-108 Phase 4 read-path
remediation, RDR-096 Phase 5.2 source_path retirement, operator
dispatch qwen-routing promotion, 3 new catalog doctor checks, prose
+ PDF auto-link generators, lazy T3 collection creation, aspects
write-side confidence floor, default-exclude implements-heuristic
from graph traversal, classifier skips minified bundles, and the
Windows winget hint block in ``nx doctor``. See root
``CHANGELOG.md`` for the full breakdown.

## [4.29.2] - 2026-05-09

Plugin version aligned with conexus 4.29.2. No plugin-side changes;
the release ships a Windows-compatibility fix in the conexus
package: every CLI invocation on Windows was failing with
``ModuleNotFoundError: No module named 'fcntl'`` because
``catalog.py``, ``event_log.py``, and ``indexer.py`` each did an
unconditional top-level ``import fcntl`` (Unix-only stdlib module).
Replaced with a cross-platform ``nexus._locking`` shim
(``fcntl.flock`` on POSIX, ``msvcrt.locking`` on Windows). See root
``CHANGELOG.md`` for the full breakdown.

## [4.29.1] - 2026-05-08

Plugin version aligned with conexus 4.29.1. No plugin-side changes;
the release ships in the conexus package: catalog destructive-verb
hardening (cwd bug fix on ``prune-stale``, default-flips on ``gc``
and ``link-bulk-delete``) plus a backup-before-delete safety net
for recovery via the new ``nx catalog undelete`` /
``list-backups`` / ``vacuum-backups`` verbs. RDR-106 filed as the
proper soft-delete architectural answer (follow-up).
See root ``CHANGELOG.md`` for the full breakdown.

## [4.29.0] - 2026-05-08

Plugin version aligned with conexus 4.29.0. No plugin-side changes; the
release ships an internal-architecture refactor in the conexus package:
the 4434-LOC ``catalog.py`` god object is decomposed into a 1683-LOC
facade plus six focused modules (-62% LOC) following the T2Database
domain-store pattern. Public API unchanged. See root ``CHANGELOG.md``
for the full breakdown of the new modules and the supporting test
isolation, regression guard, and shakedown-script fixes.

## [4.28.0] - 2026-05-08

Plugin version aligned with conexus 4.28.0. No plugin-side changes;
the release ships in the conexus package: the new
``nx catalog synthesize-log`` CLI verb (lossless in-place recovery
for catalogs in bootstrap-fallback mode), the ``NX_T1_ISOLATED=1``
precedence fix that lets operators opt into a sealed ephemeral T1
from inside a Claude session, the unified session_id resolution
chain (single source of truth across T1, tier-write telemetry,
and the SessionEnd launcher), and the orphan-multiprocessing-
tracker sweep that closes a chronic POSIX-semaphore-namespace
leak. See root ``CHANGELOG.md`` for the full breakdown.

## [4.27.1] - 2026-05-08

Plugin version aligned with conexus 4.27.1. No plugin-side changes;
the release ships a critical T1 cross-process session_id fix in the
conexus package that restores T1 visibility for every nx-plugin hook
that reads scratch from the shell (`subagent-start.sh`,
`post_compact_hook.sh`, `pre_close_verification_hook.sh`,
`divergence-language-guard.sh`). See root `CHANGELOG.md` for the
full breakdown.

## [4.27.0] - 2026-05-07

Plugin version aligned with conexus 4.27.0. No plugin-side changes;
the release ships the RDR-105 T1 architecture rewrite in the conexus
package: a single hybrid-discovery code path (env passdown for
MCP-dispatched subprocesses + single-writer
`~/.config/nexus/t1_addr.<claude_pid>` for siblings) replacing the
multi-writer session-record machinery that produced six consecutive
bug iterations (#567 / #572 / #574 / #575 / #576 / #579) in seven
days. ~5000 LOC removed from the conexus package. See root
`CHANGELOG.md` for the full breakdown.

## [4.26.8] - 2026-05-07

### Fixed

- **Sequential Thinking imperative in `using-nx-skills/SKILL.md`** — the April 25 trim (PR #320) collapsed the Essential MCP Tools entry from an active imperative to a noun-phrase listing; the May 5 restore (PR #519) targeted `nx_answer` and missed this. Restored the imperative ("use for any non-trivial decision"), the four use-case list (risk assessment back), the workflow recipe (hypothesis → evidence → evaluate → branch or proceed), and the `branchFromThought` param hint. Single line, tighter than pre-trim two-line form. Subagents were unaffected by the regression because `conexus/hooks/scripts/subagent-start.sh` SEQTHINK section was unchanged across the trim. (PR #578)

## [4.26.7] - 2026-05-06

Plugin version aligned with conexus 4.26.7. No plugin-side changes; the release ships a six-phase unified fix in the conexus package that closes the T1 data-loss class escaping three rounds of patches in 4.26.4–4.26.6 (PR #577 closes GH #576 silent data loss + GH #575 watchdog leak), plus an invariant regression scaffold (`tests/test_t1_invariants.py`) so the class cannot re-instantiate silently. See root ``CHANGELOG.md`` for the phase-by-phase breakdown.

## [4.26.6] - 2026-05-06

Plugin version aligned with conexus 4.26.6. No plugin-side changes; the release ships two T1-discovery follow-ups in the conexus package: post-spawn pointer reconciliation when the lifespan races SessionStart (PR #573 closes #572), and watchdog sticky-has-existed flag for the session-file-removed exit (PR #574). See root ``CHANGELOG.md`` for details.

## [4.26.5] - 2026-05-06

Plugin version aligned with conexus 4.26.5. No plugin-side changes; the release ships three fixes in the conexus package: `nx index repo` file-size guard (closes long-deferred #371 OOM + #436 progress halt), T1 raise-loud on missing session file (closes #567 silent write-loss), and `nx catalog list --type` SQL pushdown (#568). See root ``CHANGELOG.md`` for details.

## [4.26.4] - 2026-05-06

Plugin version aligned with conexus 4.26.4. No plugin-side changes; the release ships four fixes in the conexus package: store_list bare-prefix multi-match resolver, plan-matcher verb synonym carve-out, smoke-plan fence-off, and the catalog link-generate deprecation alias. See root ``CHANGELOG.md`` for details.

## [4.26.3] - 2026-05-06

Plugin version aligned with conexus 4.26.3. No plugin-side changes; the release ships twelve fixes in the conexus package, including two load-bearing user-facing fixes: the chromadb 1.5.9 regression that silently disabled session-level plan caching on every ``nx_answer`` call, and the double-JSON-wrapped ``final_text`` on generate-terminal plans that broke every prose-rendering skill. See root ``CHANGELOG.md`` for the full list.

## [4.26.2] - 2026-05-06

Plugin version aligned with conexus 4.26.2. No plugin-side changes; the release ships seven bug fixes plus one prep migration in the conexus package (store_list bare-prefix resolver, catalog list owner-name, search CSV corpus, CLI line-buffering, legacy session.lock cleanup, catalog WAL bound, and a document_aspects.source_uri backfill prerequisite for RDR-096 Phase 5). See root ``CHANGELOG.md`` for details.

## [4.26.1] - 2026-05-06

Plugin version aligned with conexus 4.26.1. No plugin-side changes; the release ships three MCP / runner bug fixes in the conexus package (catalog_search routing, catalog_list filter pushdown, catalog_resolve typed error, plan_run step-progress events). See root ``CHANGELOG.md`` for details.

## [4.26.0] - 2026-05-06

Plugin version aligned with conexus 4.26.0. Ships two plugin-side changes for the tier-discipline observability subsystem:

- **SubagentStart hook AGENT TAG injection**: one line added to the NX_AUTOLINK heredoc reaches every dispatched subagent. The line: ``AGENT TAG: pass agent="<your-role>" to memory_put so nx tier-status slices writes by agent``. Heredoc stays under the 500-byte bash 5.3 deadlock guard (322 → 428 bytes).
- **Per-role agent kwarg in 10 producing-findings agent files**: architect-planner, code-review-expert, codebase-deep-analyzer, debugger, deep-analyst, deep-research-synthesizer, developer, strategic-planner, substantive-critic, test-validator. Each agent's canonical Post-flight ``memory_put`` example now includes its role name baked in.

See root ``CHANGELOG.md`` for the full release notes including the conexus-side telemetry table, CLI surface, and MCP API additions.

## [4.25.4] - 2026-05-06

Plugin version aligned with conexus 4.25.4. No plugin-side changes; the release ships two aspect-worker fixes in the conexus package. See root ``CHANGELOG.md`` for details.

## [4.25.3] - 2026-05-05

Plugin version aligned with conexus 4.25.3. Two prompt-side changes ship with the plugin:

- **10 agent files** + **8 producing skill files**: tightened ``## Pre-flight`` lead now explicitly names the rationalization the using-nx-skills Red Flags table warns about (skipping pre-flight on grounds "the code is the answer / tiers won't help"). ``## Post-flight`` write-back is now audience-aware — T1 ``scratch_put`` added as a first-class write target for sibling agents downstream THIS session (the original 4.25.2 wording omitted T1 entirely from the write-back menu, framing all three tiers as "future sessions"; the live shakeout's sibling-sharing probe confirmed T1 IS the bus for in-session promotion).

The agent / skill changes make rationalizations VISIBLE in the agent's self-report but do NOT prevent them at the prompt-strength tried so far. Honest finding documented in root ``CHANGELOG.md`` for the 4.25.3 release notes; behavioral enforcement is a follow-up.

See root ``CHANGELOG.md`` for the AUTO-LINK observability + nx_answer planner retry fixes that ship in the same release.

## [4.25.2] - 2026-05-05

Plugin version aligned with conexus 4.25.2. Same composed-retrieval guidance from 4.25.1's SubagentStart hook restoration, now reinforced in the agents' and skills' own role descriptions:

- **10 agent files** get ``## Pre-flight (plan reuse + tier check)`` + ``## Post-flight (write-back)`` blocks (architect-planner, code-review-expert, codebase-deep-analyzer, debugger, deep-analyst, deep-research-synthesizer, developer, strategic-planner, substantive-critic, test-validator).
- **8 skill files** get a ``**Tier-aware discipline**`` block (research, research-synthesis, deep-analysis, analyze, query, document, knowledge-tidying, debug).

The 4.25.1 hook signal already drives behavior end-to-end (probes A and B confirmed verb-shape routing → ``nx_answer``, full 3-step AUTO-LINK recipe, and WRITE-BACK persistence). This release ships the same signal in the agents' own voice — redundant by design.

See root ``CHANGELOG.md``.

## [4.25.1] - 2026-05-05

Plugin version aligned with conexus 4.25.1. Two fixes ship together — a catalog-rebuild perf fix (transparent to plugin users) and a plugin-side restoration of behavior-driving signal in the agent-guidance hooks.

The catalog change removes a pre-RDR-104 ``_event_log_covers_legacy()`` O(N) scan from the rebuild dispatch path on catalogs whose event log is already steady-state. Post-write ``Catalog()`` construction on a 460K-event production catalog drops from ~850 ms to ~1 ms; the MCP server's per-tool-call latency floor immediately after any catalog write moves with it. Transparent to plugin users; same MCP surface.

The hook change restores prose that was over-trimmed in commit e2fc2408 (PR #320). Telemetry from 795 session transcripts (10-day pre/post-trim windows) showed agents stopped reaching for composed-retrieval tools after the trim — `nx_answer` use dropped 78%, `plan_search` 90%, `catalog_search` 100%, `store_put` 100% — exactly the tools whose recipes/examples/reasoning had been most condensed. This release restores:

- **``conexus/skills/using-nx-skills/SKILL.md``**: the "ALL analytical questions go through ``nx_answer``" header with verb-shape paragraph and reasoning; tier "when to check" cues; three specific ``search``→``nx_answer`` phrasings in Common Mistakes; the "Findings not stored are findings lost" exhortation; a focused five-row tool-skipping Red Flags table. 5803 chars vs 8681 pre-trim — 33% smaller while restoring the load-bearing content.

- **``conexus/hooks/scripts/subagent-start.sh``**: tier "when to check" cues; verb-shape routing line; WRITE-BACK exhortation; the ``catalog_search → scratch put → store_put`` AUTO-LINK 3-step recipe. Now emits via the documented ``{"hookSpecificOutput": {"hookEventName": "SubagentStart", "additionalContext": "..."}}`` envelope (plain stdout works today per cc-validation scenarios 13a/13b, but the JSON envelope is the explicit contract). All heredoc bodies remain under the 500-byte bash 5.3 deadlock guard. Verified end-to-end via ``tests/cc-validation/scenarios/12_real_nx_subagent.sh``.

See root ``CHANGELOG.md``.

## [4.25.0] - 2026-05-05

Plugin version aligned with conexus 4.25.0. No plugin-specific changes; the underlying conexus library lands RDR-104 (incremental catalog projection rebuild) so steady-state ``Catalog()`` construction after a single write completes in <100 ms instead of ~4 s on a 452K-event log. Transparent to plugin users; same MCP surface. See root ``CHANGELOG.md``.

## [4.24.4] - 2026-05-05

Plugin version aligned with conexus 4.24.4. No plugin-specific changes; the underlying conexus library closes a latent silent-corruption hazard in the catalog consistency-marker write by moving the marker into the rebuild transaction so projection + marker commit atomically. Standalone atomicity fix; the broader incremental-rebuild design (RDR-104, bead nexus-rr0u) builds on this baseline. See root ``CHANGELOG.md``.

## [4.24.3] - 2026-05-05

Plugin version aligned with conexus 4.24.3. No plugin-specific changes; the underlying conexus library closes two per-file ChromaDB-roundtrip blowups in ``nx index repo`` (batched misclassified-chunk prune, pre-built per-collection staleness cache), enriches the catalog-rebuild summary line with trigger file and projection size, and fixes a generator-contextmanager ``finally`` that was swallowing exceptions on the rebuild path. See root ``CHANGELOG.md``.

## [4.24.2] - 2026-05-05

Plugin version aligned with conexus 4.24.2. No plugin-specific changes; the underlying conexus library fixes a slow + silent catalog rebuild path. ``Catalog._ensure_consistent`` now uses FTS5 bulk-load (drops triggers during mass replay, rebuilds the FTS5 index once at the end) and emits a stderr heartbeat every 5 s during long rebuilds. See root ``CHANGELOG.md``.

## [4.24.1] - 2026-05-05

Plugin version aligned with conexus 4.24.1. No plugin-specific changes; the underlying conexus library fixes a silent no-op in ``.nexus.yml`` ``server.ignorePatterns`` matching that caused path-style globs (``docs/papers/**``) to never exclude anything. See root ``CHANGELOG.md`` for the full fix description.

## [4.24.0] - 2026-05-05

Plugin version aligned with conexus 4.24.0. No plugin-specific changes; the underlying conexus library promotes `mineru[all]` from optional to default, replaces silent formula loss with a loud failure, adds `nx doctor --check-mineru`, and cuts CI pytest runtime ~50% per push by marking integration suites and pagination-boundary tests with their existing markers. See root `CHANGELOG.md` for the full release notes.

## [4.23.3] - 2026-05-05

Plugin version aligned with conexus 4.23.3. No plugin-specific changes; the underlying conexus library fixes a misplaced `pipeline_version` stamp gate that was wasting operator embedding budget (`nexus-7yfm`). See root `CHANGELOG.md` for the fix description.

## [4.23.2] - 2026-05-04

Plugin version aligned with conexus 4.23.2. No plugin-specific changes; the underlying conexus library cleans up a misleading `nx doctor` warning against `taxonomy__*` collections (`nexus-l6mz`). See root `CHANGELOG.md` for the fix description.

## [4.23.1] - 2026-05-04

Plugin version aligned with conexus 4.23.1. No plugin-specific changes; the underlying conexus library fixes a release-day SQLite lock contention (`nexus-wehp`) that bit operators running `nx catalog backfill-collections --no-dry-run` while Claude Code was active. See root `CHANGELOG.md` for the full fix description.

## [4.23.0] - 2026-05-04

Plugin version aligned with conexus 4.23.0. Plugin-side updates for the RDR-103 conformant collection-naming arc (`nexus-yqnr.8`): example collection names in `conexus/skills/nexus/SKILL.md` and `conexus/skills/nexus/reference.md` reshaped to the 4-segment form `<content_type>__<owner_id>__<embedding_model>__v<n>` (e.g. `code__nexus-1-1__voyage-code-3__v1`); `conexus/agents/_shared/ERROR_HANDLING.md` carries the same shape for error-recovery surfaces. Operators may still type the short legacy form (`knowledge__topic`) at the `--collection` boundary; `t3_collection_name` auto-promotes before any T3 write. See root `CHANGELOG.md` for the underlying conexus library changes (RDR-101 Phase 4-6 closure, RDR-103 close, chunk-metadata schema reduction, six new operator verbs, five retired migration verbs).

## [4.22.0] - 2026-05-03

Plugin version aligned with conexus 4.22.0. No plugin-specific changes — all of v4.22.0's surface (RDR-101 event-sourced catalog migration through Phase 4, plus the cherry-pick bug-fix tail) is in the underlying conexus library. See root `CHANGELOG.md` for the full release notes.

## [4.21.2] - 2026-04-30

Plugin version aligned with conexus 4.21.2. Refines the v4.21.1 OpenAlex title-validation heuristic after live shakeout: pure Jaccard over-rejected legitimate matches when the source title was a 1-2 token filename derivative. Replaced with an asymmetric rule that accepts short-source single-token matches when the smaller token set is essentially the intersection. Net result on a 15-doc test collection: 6 correct enrichments (up from 3 in 4.21.1) and 1 known false-positive shape (filed as nexus-5cez for follow-up).

## [4.21.1] - 2026-04-30

Plugin version aligned with conexus 4.21.1. Hotfix for the OpenAlex bib enricher citation-DOI poisoning surfaced in v4.21.0 live shakeout (nexus-yy1m). Both DOI / arXiv direct lookup and title-search now validate the returned title against the source title; low-similarity matches return empty (operator audits via the `openalex_title_*_rejected` warnings) instead of stamping a foreign paper's metadata. See root `CHANGELOG.md` for the failure-mode description.

## [4.21.0] - 2026-04-30

Plugin version aligned with conexus 4.21.0. Bib enrichment gains an OpenAlex backend with DOI / arXiv-aware lookup; aspects acquire first-class read verbs; the PDF indexer surfaces silent zero-chunk failures as actionable errors:

- `nx enrich bib --source openalex` adds OpenAlex as an alternative to Semantic Scholar. DOI / arXiv direct-lookup before fuzzy title fallback. Citation links index both `bib_semantic_scholar_id` and `bib_openalex_id`.
- `nx enrich aspects-show <TUMBLER>` and `nx enrich aspects-list <COLLECTION>` are first-class read verbs for inspecting structured aspects. `--missing` inverts to gap detection; `--field` projects a single value.
- `nx index pdf` no longer reports success on zero chunks. PyMuPDF fallback, batch path, and streaming path all raise actionable errors on extraction-or-chunking mismatch.
- `nx dt index` now defaults PDFs to `knowledge__<collection>` (override with `--collection docs__<name>`).
- `nx catalog backfill --from-t3` adds a per-file recovery path; `nx catalog update --source-uri` emits a clean error on unknown scheme instead of leaking a traceback.

See root `CHANGELOG.md` for the full notes.

## [4.20.0] - 2026-04-29

Plugin version aligned with conexus 4.20.0. Closes the cross-project `source_uri` contamination class (nexus-3e4s) and ships an audit-membership sweep tool plus DEVONthink in-app install scripts:

- `nx catalog audit-membership <COLLECTION>` detects per-collection contamination; `--purge-non-canonical` (with `--canonical-home` override) deletes non-canonical entries after a confirmation prompt.
- `nx catalog audit-membership --all-collections` sweeps every physical_collection in one read-only pass and emits a contamination summary, owner-aware so single-home wrong-home collections do not silently pass as clean.
- Register-time and update-time guard against cross-project `source_uri` attribution; relative `file_path` values now anchor on the owning repo's `repo_root` instead of the process CWD. Backfill goes through the same guard.
- `nx dt install-scripts` installs the bundled in-DEVONthink toolbar / menu AppleScript wrappers under `~/Library/Application Scripts/com.devon-technologies.think/Menu/`.
- `NEXUS_CATALOG_ALLOW_CROSS_PROJECT=1` env var as the documented emergency-only escape hatch for the new guard.

See root `CHANGELOG.md` for the full notes.

## [4.19.2] - 2026-04-29

Plugin version aligned with conexus 4.19.2. Bundles six findings from a post-v4.19.1 audit plus the headline #377 fix:

- `nx enrich aspects` now supports `docs__*` collections (Closes #377). `nx catalog update --source-uri` adds a CLI recovery path for stamp failures.
- `nx dt index` forwards `--collection` for `.md` files (was silently dropping the flag), and surfaces stamp failures in the summary line.
- `nx dt open <tumbler>` checks platform before catalog I/O so non-darwin users see `macOS-only` instead of catalog errors.
- `docs/devonthink-smart-rules.md` drift fixed (drift class also addressed in PR #374 for the manual.md sibling), plus an install-path table covering Apple Silicon Homebrew and uv tool install defaults.

See root `CHANGELOG.md` for the full notes.

## [4.19.1] - 2026-04-29

Plugin version aligned with conexus 4.19.1. Two bug fixes from v4.19.0 post-release live shakeout:

- `nx dt index` now stamps `source_uri = x-devonthink-item://<UUID>` and `meta.devonthink_uri` on the catalog entry, restoring RDR-099 AC-1's round-trip promise (PR #376).
- `nx doctor --check-taxonomy` no longer hangs on real-size catalogs: the quadratic `NOT EXISTS` with `OR` query was restructured as `NOT IN (UNION)`, dropping runtime from 30s+ to 0.42s on a 526k-row database (PR #375).

See root `CHANGELOG.md` for the full notes.

## [4.19.0] - 2026-04-29

Plugin version aligned with conexus 4.19.0. RDR-099 ships first-class DEVONthink integration on macOS via a new `nx dt` Click command group (PR #363). Two subcommands: `nx dt index` ingests DT records by selection / tag / group / smart group / UUID, and `nx dt open` round-trips a tumbler or UUID back to DT. New `src/nexus/devonthink.py` selector helpers use sdef-canonical AppleScript dialect (`selected records`, `lookup records with tags`, `parents whose record type is smart group`, `search predicates` PLURAL). Companion docs `docs/devonthink-smart-rules.md` and `tests/e2e/devonthink-manual.md` ship in the same release. Cross-platform CI is unaffected: every test runs on Linux without invoking osascript, and non-darwin invocations refuse with a clear `macOS-only` message. See root `CHANGELOG.md` for the full notes.

## [4.18.2] - 2026-04-29

Plugin version aligned with conexus 4.18.2. Data-loss fix in the underlying CLI: `nx collection reindex` now refuses to delete a store_put-only collection when there's nothing to re-index from (#367); `--force` does not bypass; users are pointed at `nx collection delete` for the explicit-delete path. Plus two CI test-isolation fixes (Python 3.13 attribute strictness + console health template-branch test). See root `CHANGELOG.md` for the full notes.

## [4.18.1] - 2026-04-29

Plugin version aligned with conexus 4.18.1. Internal hardening release: `conexus/hooks/hooks.json` PreToolUse Bash timeout drops 300s → 5s (PR #364), the empty-matcher PostToolUse hook (`hook_telemetry`) is removed entirely (PR #366), and the Python 3.13 multiprocessing flake that hung the v4.18.0 release-job is fixed via spawn start method (PR #365). The `nx doctor --check-hooks` flag is removed (its data source no longer exists). See root `CHANGELOG.md` for the full notes.

## [4.18.0] - 2026-04-29

Plugin version aligned with conexus 4.18.0. Three new builtin plan templates (`hybrid-factual-lookup`, `traverse-then-generate`, `abstract-themes`) ship as `conexus/plans/builtin/*.yml`; the seed loader picks them up on first `nx catalog setup`. New `nx plan disable / enable` admin subcommands plus `nx doctor --check-post-store-hooks` / `--check-aspect-queue` observability flags. Console UI gains an Aspect Queue card on `/health`. RDRs 089, 093, 097, 098 closed. See root `CHANGELOG.md` for the full notes.

## [4.17.0] - 2026-04-28

Plugin version aligned with conexus 4.17.0. No plugin-level functional changes. The catalog backend gains the `x-devonthink-item://` URI scheme (`nexus-bqda`) and DT-aware `nx catalog remediate-paths` (`nexus-srck`) — see root `CHANGELOG.md` for details.

## [4.16.1] - 2026-04-27

Plugin version aligned with conexus 4.16.1. No plugin-level functional changes. Hotfix release for `nx index md` / `nx index pdf` silently returning 0 chunks when credentials were unset — see root `CHANGELOG.md` for the local-embedder fallback (PR #338) + cloud-mode error path (PR #337) details. Closes #336.

## [4.15.1] - 2026-04-26

Plugin version aligned with conexus 4.15.1. No plugin-level functional changes. Documentation-only point release: the root `README.md` now opens with the new Tensegrity blog posts (Post 0 *How I actually use Nexus*, Post 00 *Installing Nexus*) as the recommended on-ramp ahead of Post 1 *Nexus by Example*, and the header image is the series establishing shot. See root `CHANGELOG.md` for full notes.

## [4.15.0] - 2026-04-26

Plugin version aligned with conexus 4.15.0. No plugin-level functional changes. See root `CHANGELOG.md` for the metadata schema rationalisation arc: `make_chunk_metadata` factory unifying every indexer's chunk-metadata write, `section_type` populated on PDFs (was hardcoded empty since the chunker was written), `source_title -> title` collapse, `expires_at -> indexed_at + ttl_days` swap with new `is_expired` helper, the new `nx catalog remediate-paths` command for repairing basename and ghost file_paths in production catalogs, and the cargo cleanup that restored `bib_semantic_scholar_id` to the schema after discovering normalize() had been silently dropping the marker that drives `nx enrich`'s skip-already-enriched logic.

## [4.14.2] - 2026-04-26

Plugin version aligned with conexus 4.14.2. The SessionStart hook output line for the enrichment surface is updated for the new `nx enrich` subcommand structure: `nx enrich bib COLLECTION` (Semantic Scholar) | `nx enrich aspects COLLECTION` (RDR-089 structured aspects). See root `CHANGELOG.md` for the RDR-089 deliverable: document-grain post-store hook chain, async aspect-extraction worker, T2 `document_aspects` + `aspect_extraction_queue` stores, the new `nx enrich aspects` CLI, and the retirement of the RDR-037 four-database migration probe.

## [4.14.1] - 2026-04-25

Plugin version aligned with conexus 4.14.1. No plugin-level functional changes. See root `CHANGELOG.md` for the RDR-095 deliverable: post-store hook framework batch contract, symmetric fire on every storage event, and T2 schema migration adding `batch_doc_ids` + `is_batch` columns to `hook_failures`.

## [4.13.0] - 2026-04-25

Plugin version aligned with conexus 4.13.0. No plugin-level functional changes. See root `CHANGELOG.md` for the RDR-094 Phase F deliverable: the `NEXUS_MCP_OWNS_T1` env-var gate is removed entirely, nx-mcp's lifespan unconditionally owns chroma's lifecycle. Net source diff -283 lines.

## [4.12.1] - 2026-04-25

Plugin version aligned with conexus 4.12.1. No plugin-level functional changes. See root `CHANGELOG.md` for the 4.12.0 shakeout fix: `mcp_server_crashed` events on clean shutdowns were a SystemExit race between stdin EOF and SIGTERM, fixed by switching the signal handler to `os._exit` and adding a sticky in-flight flag that short-circuits re-entrant shutdown calls.

## [4.12.0] - 2026-04-25

Plugin version aligned with conexus 4.12.0. Two SessionEnd changes ship from RDR-094 Phase C: the `nx hook session-end-detach` fallback is dropped from `hooks.json` (it raced the same cold-start window the `nx-session-end-launcher` exists to solve, so falling back to it on launcher failure was a footgun), and the launcher's grandchild now dispatches to `hooks.session_end_flush` instead of `hooks.session_end`. Storage-only flush stays in the hook path; chroma teardown is owned by the t1_watchdog sidecar (dual-watch under the new default `NEXUS_MCP_OWNS_T1=on`). SessionEnd timeout drops from 5s to 3s: sub-second flush means a wedged hook is reaped faster. See root `CHANGELOG.md` for the full RDR-094 Phase 4 surface (default-on flag flip, T1 race retry, watchdog logging, `nx doctor --check-mcp-logs`).

## [4.11.1] - 2026-04-24

Plugin's SessionEnd hook switched from `nx hook session-end-detach` to `nx-session-end-launcher` (with a fallback to the old path for mixed-version installs). The launcher double-forks before any `nexus.*` module loads — cold-start time 256ms vs the 2s `nx hook session-end-detach` took, closing the race against Claude Code's shutdown SIGTERM that surfaced in the 4.11.0 shakeout. Graceful session cleanup now runs on close instead of being deferred to the next SessionStart's sweep. See root `CHANGELOG.md` for the full post-mortem.

## [4.11.0] - 2026-04-24

Plugin version aligned with Nexus CLI 4.11.0. No plugin-level skill or hook changes. See root `CHANGELOG.md` for the three new `operator_*` tools shipped under RDR-088 Phases 1+2 (`filter`, `check`, `verify`) plus the `nx memory get` prefix-match ergonomics fix and the T1 chroma leak fix for session-UUID rollover (nexus-886w) that closes the last gap in the 4.10.3 three-layer defense.

## [4.10.3] - 2026-04-23

Plugin's `SessionEnd` hook now calls `nx hook session-end-detach` (timeout 5s) instead of `nx hook session-end` (timeout 10s). The detach variant double-forks into a daemonized grandchild that survives Claude Code's shutdown SIGTERM, so per-session ChromaDB servers and tmpdirs actually get cleaned up. See root `CHANGELOG.md` for the full three-layer defense (watchdog sidecar + detached SessionEnd + liveness-based SessionStart sweep) that closes the leak, and for the upstream bug context (anthropics/claude-code #41577 / #17885) that motivated userspace daemonization.

## [4.10.2] - 2026-04-23

Plugin version aligned with Nexus CLI 4.10.2. No plugin-level functional changes. See root `CHANGELOG.md` for the 4.10.1 shakeout follow-ups: the operator-input arg rename for pre-hydrated steps and the builtin-bindings backfill migration for upgraded installs.

## [4.10.1] - 2026-04-23

Plugin version aligned with Nexus CLI 4.10.1. No plugin-level functional changes. See root `CHANGELOG.md` for the 4.10.0 shakeout fixes: the `nx_answer_runs` telemetry write path, the seed-loader binding-list gap, and the `_retire_legacy_operation_shape_plans` migration that retires the legacy `operation` / `params` shape rows RDR-092 Phase 0a left in place.

## [4.10.0] - 2026-04-23

Verb-skill wording rewrite: the five verb skills (`/conexus:research`, `/conexus:review`, `/conexus:analyze`, `/conexus:debug`, `/conexus:document`) and `/conexus:query` flip from descriptive ("Routes through `nx_answer`") to imperative ("You MUST call `nx_answer`"), matching the existing `brainstorming-gate` pattern agents already respect. Each skill gains a "When direct `search` is fine" carve-out for single-corpus keyword lookups so the imperative doesn't push retrieval-only questions through the slower path. Anti-patterns in every verb skill now cite composition (not a blanket "analytical") as the deciding factor. `using-nx-skills`'s common-mistakes table adds three mappings from `mcp__plugin_conexus_nexus__search(analytical-question)` anti-patterns to correct `mcp__plugin_conexus_nexus__nx_answer` shapes — full MCP prefix throughout, no short-form syntax. Combined with the operator-bundling latency work shipped in the root `CHANGELOG.md`, `nx_answer` is now fast enough (and loudly enough routed) to close the 6,537:0 usage deficit between direct `search` calls and `nx_answer` invocations observed over the prior 6 days. See root `CHANGELOG.md` for the operator-bundle feature and the `plans.use_count` telemetry wiring.

## [4.9.13] - 2026-04-23

Plugin version aligned with Nexus CLI 4.9.13. No plugin-level functional changes. See root `CHANGELOG.md` for the RDR-092 match-text rollup, the wheel-packaging fix that now ships `conexus/plans/*.yml` as importable package data (so installed CLIs find the YAMLs from any cwd), and the test-suite tightening that cut the unit run from 12:31 to 8:02.

## [4.9.11] - 2026-04-23

Adds a Layer 1 gap-structure pre-check to `/conexus:rdr-gate` so the `#### Gap N: <title>` requirement in Problem Statements is caught at gate time, not at close. Same regex and post-65 grandfathering the close skill already uses. `--skip-gaps` is the audit-trail override. Template and `rdr-create` SKILL both name both enforcement points so authors see "gate will block" during drafting. See root `CHANGELOG.md` for the RDR-091 retrofit that motivated the fix.

## [4.9.10] - 2026-04-23

Plugin version aligned with Nexus CLI 4.9.10. No plugin-level functional changes. See root `CHANGELOG.md` for the post-4.9.9 hardening arc (GitHub #249 / #250 / #251 / #252 / #253) shipped in this release — `nx catalog verify`, the `hook_failures` observability table, `nx doctor --check-taxonomy`, the `split → label` next-step hint, and the catalog-store-hook log fix.

## [4.9.9] - 2026-04-22

Plugin version aligned with Nexus CLI 4.9.9. No plugin-level functional changes. See root `CHANGELOG.md` for the store_put oversized-raise fix (nexus-akof, GH #244), inline-planner `author=` hint (nexus-sgrg), and the kxez / gwhy taxonomy fixes that did not make the 4.9.8 PyPI artifact.

## [4.9.8] - 2026-04-22

No plugin-side changes; version bumped for marketplace parity. See root `CHANGELOG.md` for the three MCP fixes + scratch get diagnostics (nexus-3o3t) and two-sided `operator_compare` for cross-corpus DAGs (nexus-km5i) shipped in this release.

## [4.9.6] - 2026-04-22

Plugin version aligned with Nexus CLI 4.9.6. No plugin-level functional changes. See root `CHANGELOG.md` for the RDR-091 scope-aware plan matching feature and critic / case follow-ups.

## [4.9.5] - 2026-04-21

No plugin-side changes; version bumped for marketplace parity. See root `CHANGELOG.md` for the doctor empty-collection transparency change (nexus-obp2).

## [4.9.4] - 2026-04-20

No plugin-side changes; version bumped for marketplace parity. See root `CHANGELOG.md` for the six CLI fixes (nexus-43pq) shipped in this release.

## [4.9.3] - 2026-04-20

### Added

- **Explicit `CLAUDE_PLUGIN_ROOT` env export in `conexus/.mcp.json`** for the `nexus` and `nexus-catalog` MCP servers. Without this, `${CLAUDE_PLUGIN_ROOT}` is only available for path substitution at config-parse time; the spawned MCP server itself wouldn't see it as an env var. Now `nx-mcp` and `nx-mcp-catalog` can `os.environ.get("CLAUDE_PLUGIN_ROOT")` reliably — used by the new plugin↔CLI version drift check on the Python-package side (see root `CHANGELOG.md`).

## [4.9.2] - 2026-04-20

### Added

- **`engines.python: ">=3.12"`** in `conexus/.claude-plugin/plugin.json` — declares the runtime requirement at the manifest layer (informational; Claude Code itself doesn't enforce, but tools and humans reading the manifest now see it).
- **`conexus/hooks/scripts/_run_python_hook.sh`** — Python launcher that probes `python3.13` then `python3.12` via `command -v` before falling back to plain `python3`. Lets a user with Homebrew Python and an older `/Library/Frameworks/Python.framework/.../python3` still hit the right interpreter without PATH gymnastics.
- **Section 5 in `/conexus:nx-preflight`** — bash check for `npx` with FAIL status if missing. Previously preflight claimed to "verify all plugin dependencies are present" but didn't check Node, so `npx`-spawned MCP servers (`sequential-thinking`, `context7`) silently failed at first tool call.

### Fixed

- **All five Python hook scripts now declare `from __future__ import annotations` and a `sys.version_info < (3, 12)` runtime guard.** Three of them (`session_start_hook.py`, `rdr_hook.py`, `t2_prefix_scan.py`) used PEP 604 union annotations (`str | None`) without the future import, so they refused to parse on system Python <3.10. The runtime guard exits cleanly with brew/apt/uv install hints instead of an opaque parser failure when Python is too old.
- **`hooks/hooks.json` Python-hook command lines route through `_run_python_hook.sh`** instead of bare `python3` — see Added above.
- **Bullet separators in `using-nx-skills/SKILL.md` "Going deeper" section** — minor doc polish.

### Notes

- v4.9.1's "atexit-based fallback in `start_t1_server`" is **withdrawn** as of v4.9.2 (Python-package side; see root `CHANGELOG.md`). The atexit fired in the wrong process and killed every chroma server right after spawn, silently breaking T1 across all conversations on 4.9.1. The `SessionEnd` hook re-registration from v4.9.1 (this changelog) is correct and remains in effect.

## [4.9.1] - 2026-04-20

### Fixed
- **`SessionEnd` hook re-registered in `hooks/hooks.json`** — retracts the
  v1.10.1 removal. The "T1 server stops with process tree" reasoning was
  wrong: chroma is intentionally spawned with `start_new_session=True`
  (so `safe_killpg` reaches its multiprocessing workers and avoids
  POSIX-named-semaphore exhaustion — beads nexus-dc57 / nexus-ze2a),
  which detaches it from the terminal's process group. Removing the
  hook removed the only thing that ever killed it; symptom was up to
  43 leaked `chroma run …nx_t1_*` processes per machine, oldest 2+ days
  old, accreting tmpdirs in `/var/folders/.../T/nx_t1_*` indefinitely.
  The cancellation-during-teardown error noted in v1.10.1 is now
  suppressed via `|| true` on the hook command.
- **`atexit`-based fallback in `start_t1_server`** (`src/nexus/session.py`) —
  registers `stop_t1_server(pid)` so the chroma child is reaped even when
  the SessionEnd hook never fires (harness teardown, OOM, terminal SIGHUP
  swallowed by the new-session boundary). Idempotent against already-dead
  PIDs.

### Changed
- **`using-nx-skills` SKILL.md trimmed by 110 lines** — removed the
  `Process Flow` graphviz block (was rendering as a 13.5KB token wall in
  the SessionStart hook output) and the `Skill Directory` table (was
  re-enumerating skills the harness already dispatches from each skill's
  description metadata, drifting out of sync over time). No behavioural
  change.

## [4.9.0] - 2026-04-19

Plugin version aligned with conexus 4.9.0. No plugin-level functional
changes (no new skills, agents, hooks, or slash commands). The arc
this release covers is entirely in the `nexus` Python package — see
the root CHANGELOG: `nx doctor --check-quotas` pre-flight diagnostic
(nexus-c590), `nx index --debug-timing` per-stage intra-file timing
breakdown (nexus-7niu scaffold + prose/PDF extension), the
`nx collection health` chunk-count-from-T3 data-integrity fix
(nexus-39zi live-shakeout finding), and the E2E tmux harness
remediation PR.

## [4.8.0] - 2026-04-18

Plugin version aligned with conexus 4.8.0. No plugin-level functional
changes (no new skills, agents, hooks, or slash commands). The arc this
release covers is entirely in the `nexus` Python package — see the root
CHANGELOG for the full story: `nexus-vatx` ingest-observability
surfaces (retry visibility, post-processing phase markers, ETA ticker,
retry-time summary), collection-management surfaces (delete cascade,
`nx collection rename`, `source_mtime`, `nx collection audit --live`),
`nx taxonomy validate-refs` proximity fix (nexus-7ay), plus a 17-finding
review-remediation sweep (3 Critical + 14 Important + suggestions)
across the retry, catalog, collection-audit, and safe-killpg surfaces.

## [4.7.0] - 2026-04-18

Plugin version aligned with conexus 4.7.0. No plugin-level functional
changes (no new skills, agents, hooks, or slash commands). See root
CHANGELOG for the full arc: RDR-086 chash span surface (Phases 1–5),
codebase-wide review remediation (29 findings across Critical /
Important / Suggestion tiers), and the `NEXUS_CONFIG_DIR` isolation
refactor that routes every ~/.config/nexus path through a canonical
helper.

## [4.6.5] - 2026-04-18

Plugin version aligned with conexus 4.6.5. No plugin-level functional
changes. See root CHANGELOG for the PDF extractor `on_page` replay
fix in the MinerU-failed fallback path (nexus-7ne1).

## [4.6.4] - 2026-04-18

Plugin version bump alongside conexus 4.6.4. See root CHANGELOG for
the POSIX-semaphore leak root-cause fix (nexus-ze2a + nexus-dc57)
and the new `nx doctor --check-resources` probe.

## [4.6.3] - 2026-04-17

Plugin version bump alongside conexus 4.6.3. See root CHANGELOG for
the dimension-mismatch skip-and-warn guard in `T3Database.search`
(issue #190 follow-up).

## [4.6.2] - 2026-04-17

Plugin version bump alongside conexus 4.6.2. See root CHANGELOG for
the issue #190 `plans(verb)` index crash fix on pre-4.4.0 DBs.

## [4.6.1] - 2026-04-17

Plugin version bump alongside conexus 4.6.1. See root CHANGELOG for
the RDR-087 review follow-ups: typed telemetry accessor on the hot
path and the `search_telemetry.dropped_count` → `kept_count` schema
rename migration.

## [4.6.0] - 2026-04-17

Plugin version bump alongside conexus 4.6.0. See root CHANGELOG for
RDR-087 Phase 2: `search_telemetry` table + migration, hot-path
writes from `search_cross_corpus`, `telemetry.*` opt-out config, and
`nx doctor --trim-telemetry` retention command.

## [4.5.3] - 2026-04-17

Plugin version bump alongside conexus 4.5.3. See root CHANGELOG for
the analytical-tool timeout raise and the T3 bare-constructor
credential fallback.

## [4.5.2] - 2026-04-17

Plugin version bump alongside conexus 4.5.2. See root CHANGELOG for
the nexus-51j case-insensitive RDR-file-glob fix in `{{rdr:…}}`
token resolution.

## [4.5.1] - 2026-04-17

Follow-up bug fix bundled with conexus 4.5.1. See root `CHANGELOG.md`.

## [4.5.0] - 2026-04-17

Plugin version bumped to track the `nx doc` command group shipped with
RDR-082 (doc-build tokens) and RDR-083 (corpus-evidence tokens). See the
root `CHANGELOG.md` for the full release notes covering RDR-082 / 083 /
084 / 085 implementations, RDR-086 draft filing, and the nexus-lub /
nexus-9ji bug fixes.

### Added

- **`nx doc render`** — resolve `{{bd:…}}` / `{{rdr:…}}` tokens in
  markdown against bead DB + RDR frontmatter. Fail-loud default;
  `--allow-unresolved` preserves literal tokens.
- **`nx doc validate`** — same engine, no emit, non-zero on unresolved.
- **`nx doc check-grounding`** — citation-coverage report
  (chash-shaped / prose / bracket counts + ratio).
- **`nx doc check-extensions`** — `[experimental]` — flags doc chunks
  that don't project into a primary-source collection.

### Changed

- `nx collection delete` now reports taxonomy-cascade counts
  (topics / assignments / links / meta).
- `nx index pdf --force` now works end-to-end against a partial prior
  ingest — wipes pipeline.db state + T3 orphan chunks pre-flight.

## [4.4.1] - 2026-04-16

### Fixed

- **Auto-approval allow list** — `conexus/hooks/scripts/auto-approve-nx-mcp.sh` was shipped with the 4.3.x tool surface. Added the 11 MCP tools introduced in 4.4.0 (`nx_answer`, `nx_tidy`, `nx_enrich_beads`, `nx_plan_audit`, `traverse`, `store_get_many`, 5 `operator_*`). Anyone running 4.4.0 from the marketplace saw a permission prompt on every call; 4.4.1 silences them.
- **SubagentStart operators guidance** — the "Analytical Operators" block was still telling subagents to `Agent` tool dispatch to the removed `analytical-operator` agent. Replaced with the 5 `operator_*` MCP tool signatures and a pointer to `nx_answer` for plan-matched multi-step retrieval.
- **`nexus` skill** — `SKILL.md` common-operations block was missing every tool added in 4.4.0. Rewrote to include `nx_answer`, `traverse`, `store_get_many`, the 5 operators, and the 3 hygiene tools, plus a "When to reach for each" guide. `reference.md` gained full entries for the same 11 tools; tool count corrected (15 → 26).
- **`_shared/README.md`** — "All 15 agents" updated to reflect the post-RDR-080 shape (13 agents = 10 active + 3 MCP-tool redirect stubs).

## [4.4.0] - 2026-04-16

### Added

- **9 RDR-078 verb skills** — `research`, `review`, `analyze`, `debug`, `document`, `plan-author`, `plan-inspect`, `plan-promote`, `plan-first`. Each calls `nx_answer(dimensions={"verb": <skill>})` so the plan-match gate narrows to templates of the appropriate verb. Picks up the record step (`nx_answer_runs` T2 table) automatically.
- **`/conexus:query` slash command** — pointer to `mcp__plugin_conexus_nexus__nx_answer`. Replaces the `query-planner` + `analytical-operator` agents from RDR-042.
- **`/conexus:pdf-process` command file** — pointer to `nx index pdf` CLI. Replaces the `pdf-chromadb-processor` agent.
- **Builtin plan templates** — `conexus/plans/builtin/*.yml` (9 scenario templates) + `conexus/plans/dimensions.yml` (dimension registry). Seed on `nx catalog setup`.
- **"Retrieval preference (RDR-080)" block** in all 10 active agents — recommends `nx_answer` for multi-source retrieval; keeps direct `search()` / `query()` appropriate for single-step scoped lookups.
- **Plugin-runtime validation suite** (`scripts/validate/09-plugin-runtime.py`) — runtime exercise of all 43 skills + 13 agents via `claude -p` with schema-aware assertions.

### Changed

- **Three agents are now 40-line stubs** — `knowledge-tidier.md`, `plan-auditor.md`, `plan-enricher.md` direct callers at `nx_tidy` / `nx_plan_audit` / `nx_enrich_beads` MCP tools respectively (RDR-080). Stubs remain in the registry so legacy dispatch references don't break; new code should invoke the MCP tool directly.
- **`registry.yaml`** — `agents:` 16 → 13 (removed `query-planner`, `analytical-operator`, `pdf-chromadb-processor`); `standalone_skills:` 11 → 24 (added 9 verb skills + 4 pointer skills). `model_summary` updated. Pipelines `feature` and `research` replace agent steps with MCP-tool invocations.

### Docs

- Ported from feature branch: `docs/plan-authoring-guide.md`, `docs/catalog-link-types.md`, `docs/catalog-purposes.md`, 3 implementation-plan references under `docs/plans/`.
- RDR index updated with RDR-081 (closed), RDR-082/083/084/085 (drafts).

## [4.3.2] - 2026-04-15

Plugin version aligned with Nexus CLI 4.3.2. No plugin-level functional changes.

## [4.3.1] - 2026-04-15

Plugin version aligned with Nexus CLI 4.3.1. No plugin-level functional changes.

## [4.3.0] - 2026-04-14

Release tracks the conexus CLI 4.3.0 RDR-077 projection-quality work. No new plugin skills, commands, or hooks; the version bump keeps `marketplace.json` aligned with the CLI so users on the matching CLI see the correct plugin version.

## [4.2.2] - 2026-04-14

(v4.2.1 was tagged but never published due to a CLI test failure. v4.2.2 supersedes it.)

### Fixed

- **SessionStart hook fallback for old CLI**: when `nx upgrade --auto` fails (CLI < 4.2.0 doesn't have the upgrade command), the hook now prints `conexus plugin requires conexus >= 4.2.0 — run: uv tool upgrade conexus` to stderr instead of the raw Click error.

## [4.2.0] - 2026-04-14

### Added

- **`nx upgrade --auto` SessionStart hook**: runs T2 schema migrations silently on every Claude Code session start (RDR-076)
- **RDR close gate heading normalization**: the close-time gap gate now accepts both `## Problem` and `## Problem Statement` heading variants; gap regex broadened to `^#{3,5} Gap \d+:` (accepts h3–h5)
- **`nx:rdr-create` skill** documents both heading variants and broadened gap regex format

### Changed

- **`nx:rdr-gate` SKILL.md**: Layer 1 structural validation now lists heading variants with `/` separator (`Problem / Problem Statement`, `Proposed Solution / Proposed Design / Decision`, `Implementation Plan / Approach / Steps / Phases`, `Finalization Gate / Success Criteria`) and includes explicit "do NOT silently skip" instruction

## [4.1.2] - 2026-04-13

### Fixed

- SubagentStart hook now actually injects the L1 knowledge map into subagent context (was emitting raw bash code due to quoted heredoc)

## [4.1.1] - 2026-04-13

Plugin version aligned with Nexus CLI 4.1.1. No plugin-level functional changes.

## [4.1.0] - 2026-04-13

### Added

- SessionStart hook injects L1 project knowledge map (topic summary) at session start
- Subagent start hook injects per-repo L1 context into child agents
- Query sanitizer active on MCP `search` and `query` tools (transparent — no plugin config needed)

## [4.0.3] - 2026-04-13

Plugin version aligned with Nexus CLI 4.0.3. No plugin-level functional changes.

## [4.0.0] - 2026-04-13

### Added

- `topic` parameter on `search` MCP tool for topic-scoped search
- Topic boost and grouping active on both `search` and `query` MCP tools
- `taxonomy_assign_hook` registered at server startup for incremental topic assignment
- Agent context protocol updated with topic-scoped search examples
- Session start hook mentions `topic=` parameter in search description
- Subagent start hook documents `topic=` in search signature
- Query planner skill includes topic-scoped routing option

### Changed

- `search` MCP tool description updated for topic grouping and boost
- `query` MCP tool now passes taxonomy for boosted ranking
- `store_put` triggers post-store hook for automatic topic assignment

## [3.9.3] - 2026-04-11

Agent model defaults restored to v3.9.1 originals after clean eval
showed haiku fails cold on complex tasks. Model Selection tables from
3.9.2 retained for per-task downgrade when appropriate.

### Fixed

- 4 agents restored to opus: debugger, deep-analyst, architect-planner,
  strategic-planner
- 8 agents restored to sonnet: substantive-critic, code-review-expert,
  plan-auditor, plan-enricher, test-validator, codebase-deep-analyzer,
  deep-research-synthesizer, query-planner
- 2 agents unchanged at haiku: knowledge-tidier, pdf-chromadb-processor
- Model Selection tables retained in 7 skills for per-task downgrade

## [3.9.3] - 2026-04-11

Agent model defaults recalibrated. Clean eval (no T2 injection) against
ART RDR-073 showed haiku fails on complex architectural critique. Six
agents restored to sonnet; three mechanical agents stay haiku. Escalation
tables in skills unchanged — opus remains an explicit escalation.

### Fixed

- substantive-critic: haiku → sonnet (can't hold dimensional thread cold)
- plan-auditor: haiku → sonnet (same reasoning class)
- deep-research-synthesizer: haiku → sonnet (multi-source synthesis)
- code-review-expert: haiku → sonnet (needs to understand intent)
- codebase-deep-analyzer: haiku → sonnet (architecture patterns)
- query-planner: haiku → sonnet (plan decomposition)

## [3.9.2] - 2026-04-11

Dynamic model selection: all 16 agents lowered to cheapest viable
default (haiku or sonnet). Skills include Model Selection tables
guiding when to escalate via Agent tool `model` parameter. No opus
defaults remain — opus is an explicit escalation for complex tasks.

### Changed

- 8 agents default haiku (was sonnet): substantive-critic, code-review-expert,
  plan-auditor, test-validator, codebase-deep-analyzer, plan-enricher,
  query-planner, deep-research-synthesizer
- 4 agents default sonnet (was opus): debugger, deep-analyst,
  architect-planner, strategic-planner
- 7 skills gain Model Selection tables: substantive-critique, debugging,
  deep-analysis, research-synthesis, code-review, architecture,
  strategic-planning

## [3.9.1] - 2026-04-11

Patch: code-verification gate for the RDR audit methodology, RDR-066
composition probe catch demonstration, and RDR housekeeping.

### Fixed

- **`skills/rdr-audit/SKILL.md`**: added mandatory code-verification gate
  for PARTIAL and SCOPE-REDUCED audit verdicts. The audit read RDR text
  (success criteria checkboxes) but not code — producing false-positive
  SCOPE-REDUCED on RDR-056 when all 4 features had shipped. The gate
  requires Grep spot-checks against the source before any non-CLEAN
  verdict. Canonical prompt bumped to v1.2 (T2
  `nexus_rdr/067-canonical-prompt-v1`).

### Changed

- **RDR-066 Phase 5a proven**: synthetic composition probe catch test
  demonstrated end-to-end FAIL→catch→attribution cycle (10-dim vs 5-dim
  mismatch correctly attributed to specific dependency beads).
- **RDR-067 CA-1 verified**: two independent audit runs against nexus
  confirmed the canonical prompt generalizes beyond ART. CA-2 partially
  verified (2/4 overlapping verdicts agree, calibration drifts on severity).
- **RDRs closed**: 057 (implemented), 061 (implemented), 062 (implemented),
  065 (status flip), 068 (won't-ship — reduces to formal verification).
  README index updated for 057-069.

## [3.9.0] - 2026-04-11

Plugin release: RDR-067 (Cross-Project RDR Audit Loop) Phase 2 of the
4-RDR silent-scope-reduction remediation. Ships the `nx:rdr-audit`
skill + management subcommands, cross-project incident template,
scheduling asset templates, and softens six research-class agents to
honor relay-specified storage targets.

### Added

- **`skills/rdr-audit/SKILL.md`** new skill: wraps the RDR-067 canonical
  audit prompt (T2-pinned at `nexus_rdr/067-canonical-prompt-v1`) as a
  one-command feedback loop. Dispatches `deep-research-synthesizer`
  with the substituted prompt, parses the verdict (VERIFIED /
  PARTIALLY VERIFIED / INCONCLUSIVE / FALSIFIED), and persists findings
  to T2 `rdr_process/audit-<project>-<date>`. Phase 2a invariants:
  transcript mining is non-delegatable to subagents (main session must
  pre-gather before dispatch); skill body owns `memory_put` persistence;
  current-project derivation via `git remote` → pwd → user-prompt
  precedence chain.
- **Management subcommands** on `rdr-audit`: `list` / `status` /
  `history` / `schedule` / `unschedule` with explicit read-only vs
  print-only safety split. Read-only commands shell out to
  `launchctl list` / `crontab -l` / `memory_search` / `memory_get` only
  with zero state mutation. Print-only commands render platform install
  templates for user review — they never execute `launchctl load`,
  `launchctl unload`, plist file writes, or crontab edits.
- **`commands/rdr-audit.md`** slash command with project-name
  derivation, evidence pre-scoping (worktree + transcript directory
  detection), and subcommand safety classification.
- **`resources/rdr_process/INCIDENT-TEMPLATE.md`** cross-project
  incident filing template: 6 frontmatter fields + 8 narrative
  sections. `drift_class` enum (`unwiring` / `dim-mismatch` /
  `deferred-integration` / `other`) exactly matches the canonical
  audit prompt's sub-pattern taxonomy so sibling project filings
  aggregate without translation.
- **Scheduling templates** (`scripts/cron-rdr-audit.sh` + plist + crontab
  + READMEs) for periodic 90-day audits via local cron/launchd.
- **Tests**: `tests/test_rdr_audit_skill.py` (46 tests),
  `tests/test_rdr_audit_scheduling.py` (29 tests),
  `tests/test_rdr_audit_incident_template.py` (16 tests).

### Changed

- **Research-class agent persistence directives softened to honor relay
  targets.** Six agents (`deep-research-synthesizer`, `deep-analyst`,
  `codebase-deep-analyzer`, `architect-planner`, `debugger`,
  `strategic-planner`) previously enforced "MUST store to T3 via
  `store_put` BEFORE returning" via primary directive + `<HARD-GATE>`
  block. Now: "MUST persist ... unless the dispatching relay specifies
  an alternative storage target (T2 `memory_put` or T1 `scratch`) in
  Input Artifacts, Deliverable, or Operational Notes". Default to T3
  when the relay is silent (preserves auto-linker + catalog graph
  behavior for `/conexus:research`, `/conexus:deep-analysis`, etc.).

## [3.8.5] - 2026-04-11

Plugin release: RDR-066 (Composition Smoke Probe at Coordinator Beads)
Phase 1 of the 4-RDR silent-scope-reduction remediation. Ships the
plan-enricher coordinator detection + the new composition-probe skill.

### Added

- **`agents/plan-enricher.md` coordinator detection**: per-bead walk
  now reads `bd show <id> --json` (was `bd show <id>`) and counts
  blocking dependencies. When `≥ 2`, the bead is tagged
  `metadata.coordinator=true` via `bd update --metadata`, a
  `/conexus:composition-probe <id>` instruction is appended to the enriched
  bead description, and a post-write verification step asserts the
  tag persisted. Verification failure surfaces a WARNING to the user
  explicitly — no silent drops.
- **`skills/composition-probe/SKILL.md`** new skill: reads coordinator
  bead + dependencies, dispatches general-purpose subagent with a
  pinned prompt to generate a 30-50 line composition smoke test,
  runs it via project-native test runner, reports PASS or FAIL with
  bead-level attribution. Read-only tool budget (Read + Grep + Glob
  only) per Phase 1a CA-1 verification — Nexus and ART coordinator
  targets use `Any` at injection boundaries with runtime dict-key
  contracts, so Serena symbol resolution is not required.
- **Coordinator Convention** section added to the plan-enricher
  agent prompt header — documents what a coordinator is, how the
  fallback detection heuristic works, what the tag enables downstream
  (probe dispatch), and why full CA-5 method-ownership lookup is
  deferred (cost + scope: Phase 1b verified CA-5 full would not
  improve catch rate on the historical target set).

### RDR

- RDR-066 Phase 1 shipped. Phase 0 (RDR-069) shipped in 3.8.3. Catch
  ceiling is 3/4 historical ART incidents (RDR-073, RDR-075, RDR-031
  are in-scope inter-bead composition failures; RDR-036 is an
  intra-class failure mode that re-attributes to RDR-068 dimensional
  contracts).

## [3.8.4] - 2026-04-11

Plugin release: surgical close-time reindex for the rdr-close skill,
paired with a CLI extension to `nx index rdr` that enables
single-file scoping. Both fixes ship together because the skill
change depends on the CLI change.

### Added

- **`nx index rdr <file.md>`** — single-file scoping. The command now
  accepts either a repo directory or a single `.md` file. File-mode
  resolves the repo root via `git rev-parse --show-toplevel` and
  writes to the same `rdr__{basename}-{hash8}` collection as the
  directory-mode invocation. This is the form used at rdr-close time
  when only one RDR changed.

### Fixed

- **`skills/rdr-close/SKILL.md` reindex step** — previously ran
  `nx index rdr` (no argument, whole-corpus walk) unconditionally in
  all three close flows (Implemented Step 4.4, Reverted/Abandoned
  Step 5, Superseded Step 3). Now:
  - Skip entirely for frontmatter-only edits (status / closed_date /
    close_reason flip). A concrete `git diff | grep` recipe is
    included so the user can check whether the diff is wholly inside
    the frontmatter block before deciding.
  - When a reindex IS warranted (divergence notes added to the body,
    cross-link notes inserted, etc.), use the single-file form
    `nx index rdr docs/rdr/rdr-NNN-<slug>.md`. The whole-corpus form
    is explicitly called out as NOT appropriate at close time.
  - Superseded flow uses two single-file invocations (one for the
    old RDR, one for the new RDR) since both documents get cross-link
    notes added to their bodies.

## [3.8.3] - 2026-04-11

Plugin release: RDR-069 automatic substantive-critic dispatch at RDR
close time. Adds the only silent-scope-reduction intervention with
empirical catch evidence (2/2 on the ART RDRs that motivated the
remediation cycle).

### Added

- **`skills/rdr-close/SKILL.md` Step 1.75 Automatic Critique** —
  dispatches `/conexus:substantive-critique <rdr-id>` via a fixed-shape
  minimal relay and parses the canonical `## Verdict` block from the
  response. Branches on outcome: `justified` passes through; `partial`
  blocks `implemented` without `--force-implemented`; `not-justified`
  blocks `implemented` without `--force-implemented` (while `reverted`
  and `partial` remain available without override — only `implemented`
  requires the audit override). Fallback parse rule (counting
  `### Issue:` headers) handles a missing Verdict block. Scenario 4
  explicitly surfaces dispatch timeouts and transport failures to the
  user.
- **`agents/substantive-critic.md` canonical Verdict block** — 5-field
  block (outcome / confidence / critical_count / significant_count /
  summary) added to the Output Format between Verification Performed
  and Operating Principles. Downstream parsers grep
  `- **outcome**:` for the verdict category.
- **`commands/rdr-close.md` `--force-implemented "<reason>"` flag** —
  audit-trail override for critic blocks. Parsed in the preamble
  alongside `--pointers`; non-empty reason is required. Surfaced to
  the SKILL.md body via a `Force Implemented (audit)` line. The skill
  body writes a T2 audit entry for every invocation
  (`nexus_rdr/<id>-close-override-<YYYY-MM-DD>`).
- **T2 override audit pattern** in Step 1.75 branch E — captures
  `critic_verdict` (or "skipped" when the user short-circuits the
  dispatch), `user_reason`, `final_close_reason`, `timestamp`, and
  `rdr_id`. Measurement surface for CA-4 (20% override-rate threshold
  over 30 days).

### Fixed

- **`commands/rdr-close.md` `--force` regex** — migrated both
  occurrences (detection at `force = bool(...)` and the `re.sub` in
  the `args_clean` stripping chain) from bare `r'--force'` to
  `r'--force(?!-)'` negative lookahead. Prevents `--force-implemented`
  from silently activating the status-override path. `\b` is
  explicitly rejected in a code comment — word-boundary fires between
  `e` and `-`. Plan-auditor SIG-1 / SIG-2 closed.

## [3.8.2] - 2026-04-11

Plugin release: RDR-065 close-time funnel hardening. New gates defend the
RDR close ritual against silent scope reduction.

### Added

- **RDR template scaffold (RDR-065 Gap 4)** — TEMPLATE.md grew an
  `### Enumerated gaps to close` subsection with `#### Gap N:` placeholders
  and a documented heading regex (`^#### Gap \d+:`). The close skill
  enumerates these.
- **Two-pass Problem Statement Replay preamble** in `commands/rdr-close.md`
  (RDR-065 Gap 1). Pass 1 lists gaps and exits cleanly; Pass 2 validates
  per-gap `--pointers` (key coverage + file existence) and sets a T1
  scratch active-close marker. ID-based grandfathering for pre-065 RDRs.
- **`### Step 1.5: Problem Statement Replay`** in `skills/rdr-close/SKILL.md`
  — user-facing wrapper around the preamble's four outcomes plus the
  verbatim "structural-not-semantic" framing prompt.
- **`hooks/scripts/divergence-language-guard.sh`** — new PostToolUse hook
  on `Write|Edit` for post-mortem files. Locked Rev 4 8-pattern regex bank;
  markdown header / table-row pre-filter; advisory only (never blocks).
- **`bd create` enforcement branch** in
  `hooks/scripts/pre_close_verification_hook.sh`. During an active RDR
  close, follow-up beads that mention the RDR must include `reopens_rdr`,
  `sprint`/`due`, and `drift_condition` metadata. Missing markers → deny.

## [3.8.1] - 2026-04-10

Plugin version aligned with Nexus CLI 3.8.1. Patch release — bug
fixes only, no plugin-level functional changes. All four fixes are
internal to the core CLI and MCP servers:

- T1.promote() overlap detection no longer misses similar-but-not-
  identical content (RDR-057 fix)
- T2Database.delete() now cascades to taxonomy topic_assignments
  so nx memory delete no longer leaves orphan rows
- nx catalog link --help lists `formalizes` among built-in types
- docs/mcp-servers.md corrects the `link-bulk` / `link-bulk-delete`
  command name

## [3.8.0] - 2026-04-10

Plugin version aligned with Nexus CLI 3.8.0 (RDR-063: T2 Domain Split).
No plugin-level functional changes — the T2 refactor is internal to the core
CLI and MCP servers. Documentation and README precision fixes only.

### Fixed

- `conexus/README.md` header: 32 → 33 skills.
- `conexus/README.md` "What You Get" bullets: 32 → 33 skills; 10 → 11 standalone
  skills (adds the missing `catalog` skill, which was orphaned from the
  listings).
- `conexus/README.md` Directory Structure: added `catalog/` to the skills tree
  and refreshed the `hooks/scripts/` listing to enumerate all 10 scripts
  (session_start, rdr, post_compact, stop_failure, stop_verification,
  pre_close_verification, subagent-start, auto-approve-nx-mcp, plus the
  two shared helpers). The previous listing showed only 4 of 10.
- `conexus/README.md` Standalone Skills (10) → (11), with a `catalog` row added.
- `conexus/README.md` Hooks table rewritten to match `hooks.json` — removed
  `bd prime` entries that don't exist in the hook wiring; added missing
  `PostCompact` (`post_compact_hook.sh`), `Stop` (`stop_verification_hook.sh`),
  `StopFailure` (`stop_failure_hook.py`), `PreToolUse` (Bash, bd-close gate),
  and `PermissionRequest` (auto-approve MCP) entries.
- `conexus/README.md` agent directory note: "14 specialized + 2 internal" reworded
  to "14 command-invoked + 2 query-dispatched" — the two dispatched agents
  (`analytical-operator`, `query-planner`) are not "internal"; they're
  invoked via the `query` skill's planner/operator pipeline.

## [3.7.0] - 2026-04-10

### Added
- **MCP dual-server architecture (RDR-062)** — Plugin now bundles two MCP
  servers instead of one: `nexus` (15 core tools: search, query, store,
  memory, scratch, plans, consolidation) and `nexus-catalog` (10 catalog
  tools with short names: search, show, list, register, update, link,
  links, link_query, resolve, stats — no `catalog_` prefix). Total 25
  registered tools (down from 30, with 6 admin operations demoted to
  CLI-only). Auto-approve hook updated; agents and skills migrated to
  new full tool names.
- **`memory_consolidate` tool** documentation in `conexus/skills/nexus/reference.md`
  with `dry_run` and `confirm_destructive` safety gates explained
- **`formalizes` link type** added to catalog skill documentation
- **Contradiction flag rendering** in search output: `[CONTRADICTS ANOTHER RESULT]`
  labels surface when same-collection results have conflicting provenance
- **PromotionReport return format** documented under `scratch_manage` tool

### Changed
- All skills and agents using catalog tools migrated from
  `mcp__plugin_conexus_nexus__catalog_*` to `mcp__plugin_conexus_nexus-catalog__*`
  (24 files touched by mechanical sed + 6 manual content cleanups)
- Agent CONTEXT_PROTOCOL updated: T2 row mentions `memory_consolidate`
  and heat-weighted TTL; catalog row uses short tool names
- Session-start and subagent-start hooks inject capability summaries
  reflecting the new dual-server layout

### Fixed
- Permission auto-approve hook now covers all 15 core + 10 catalog tools
  (previously 14 core — missing `memory_consolidate`)
- Plugin audit: all stale references to demoted tools
  (`store_delete`, `collection_info`, `collection_verify`, `catalog_unlink`,
  `catalog_link_audit`, `catalog_link_bulk`) removed from live agent and
  skill guidance
- Known-defect regression test for `get_topic_docs()` JOIN bug

## [3.6.5] - 2026-04-09

### Fixed
- Version bump only — no conexus plugin changes in this release.

## [3.6.4] - 2026-04-09

### Fixed
- **plugin.json version stuck at 3.2.3** — Claude Code uses `conexus/.claude-plugin/plugin.json` version to decide cache refresh. Was never bumped since initial creation, so no conexus plugin updates were reaching users. Now bumped to 3.6.4 and added to release checklist.

## [3.6.3] - 2026-04-09

### Fixed
- **Phantom Serena tool names** — `rename_symbol`, `restart_language_server`, `get_current_config`, `activate_project` replaced or removed across serena-code-nav skill, registry.yaml, and 3 downstream skills.
- **Wrong MCP prefixes** — `mcp__plugin_serena_serena__` → `mcp__plugin_sn_serena__` in serena-code-nav and registry; `mcp__sequential-thinking__` → `mcp__plugin_conexus_sequential-thinking__` in 7 skills + 1 command.

### Changed
- **Backend-agnostic Serena injection** — SubagentStart hook discovers tools via dual-variant ToolSearch (JetBrains + LSP) and delegates parameter docs to Serena's `initial_instructions`. Works for both backend configurations.
- **Generic Serena names in skills** — debugging, development, architecture skills use backend-neutral names in pseudocode.

## [3.6.1] - 2026-04-08

### Fixed
- **Subagent hook catalog context** — linked RDRs now shown for all agent types, including code-nav and review agents.

## [3.6.0] - 2026-04-08

### Added
- **`nx:catalog` skill** — agent-friendly catalog manipulation: resolve tumblers, create links, seed auto-linker context, discover unlinked entries.
- **Ambient catalog context** — subagent-start hook extracts file paths from task text and shows linked RDRs automatically.
- **Link-context seeding** across all T3-storing skills (15 total): code-review, codebase-analysis, plan-validation, substantive-critique, rdr-gate, knowledge-tidying, strategic-planning, rdr-close + the 6 previously seeding skills.

### Changed
- **Link-boosted query results** — `query` MCP tool now automatically boosts results from documents with `implements` links. No agent changes needed.
- **CONTEXT_PROTOCOL** — added catalog link graph as search source #4 for proactive agents.

## [3.5.2] - 2026-04-08

Plugin version aligned with Nexus CLI 3.5.2. No plugin-level changes.

## [3.5.1] - 2026-04-08

### Fixed
- **stop_failure_hook.py** now executable (was 644, hook would fail on StopFailure events).
- **All hook scripts hardened** — removed `set -euo pipefail` from advisory hooks (stop, close) and permission auto-approve hooks (nx, sn). Prevents silent failures under resource pressure.
- **Agent frontmatter** — 8 color mismatches and 2 version mismatches synced to registry.yaml.

### Docs
- README: 14→16 agents, 28→32 skills.

## [3.5.0] - 2026-04-08

Plugin version aligned with Nexus CLI 3.5.0. No plugin-level functional changes.

### Fixed
- **Advisory hooks hardened** — removed `set -euo pipefail` from stop and close verification hooks.
- **Hook stdout leak** — `nx catalog sync` output redirected to `/dev/null` in stop hook.

## [3.4.0] - 2026-04-08

### Removed
- **Orchestrator agent** — `conexus/agents/orchestrator.md` deleted, removed from registry.yaml agent block and sonnet model group.

### Changed
- **Orchestration skill** — converted to standalone reference skill (no agent dispatch). Routing tables, pipeline templates, and decision framework preserved in new `reference.md`.
- **using-nx-skills** — Process Flow DOT graph extended with plan library nodes. New "Plan Reuse" section wires `plan_search`/`plan_save` into multi-agent dispatch.
- **Cross-references** — CONTEXT_PROTOCOL, RELAY_TEMPLATE, rdr-accept updated from "orchestrator" to "caller".
- **README** — 14 agents, 10 standalone skills. Orchestration directory comment updated.

### Added
- **`conexus/skills/orchestration/reference.md`** — routing graph, quick reference table, decision framework, standard pipelines, and pipeline pattern catalog.
- **5 pipeline templates** in T2 plan library (permanent): RDR Chain, Plan-Audit-Implement, Research-Synthesize, Code Review, Debug.
- **`orchestration`** entry in `registry.yaml` `standalone_skills`.

## [3.3.1] - 2026-04-07

Plugin version aligned with Nexus CLI 3.3.1. No plugin-level functional changes.

## [3.3.0] - 2026-04-07

### Changed
- **Nexus reference skill** — `search` tool docs updated with `cluster_by` param, `section_type` filter example, automatic quality features note. `collection_verify` updated to multi-probe description.
- **Session start hook** — search capability line includes `cluster_by` and `section_type` hints.
- **Subagent start hook** — search tool signature includes `cluster_by` param and section_type filter hint.

### Docs
- RDR-056 closed (implemented).

## [3.2.5] - 2026-04-07

### Fixed
- **voyage-4 eradication** — removed from rdr-close skill, E2E test harness, and all user-facing docs.

### Docs
- RDR-056, RDR-057, RDR-058, RDR-059 added to RDR index.

## [3.2.4] - 2026-04-07

Plugin version aligned with Nexus CLI 3.2.4. No plugin-level functional changes.

## [3.2.3] - 2026-04-07

Plugin version aligned with Nexus CLI 3.2.3. No plugin-level functional changes.

## [3.2.2] - 2026-04-07

### Fixed
- Added `conexus/.claude-plugin/plugin.json` manifest
- Fixed 9 agents with non-standard color values (amber, teal, mint, gold, coral, emerald, indigo, lime)
- Added `sequential-thinking` to MCP auto-approve hook

## [3.2.1] - 2026-04-07

### Fixed
- MCP auto-approve hook uses explicit full tool names (28 tools) instead of wildcard
- 5 agents self-seed link-context from task prompt when dispatched without a skill
- Mandatory T3 store_put with HARD-GATE enforcement in 5 analysis/research agents
- rdr-gate stores critique to T3 on both pass and fail
- rdr-research seeds link-context before agent dispatch

## [3.2.0] - 2026-04-06

### Added
- 5 dispatching skills (development, debugging, research-synthesis, deep-analysis, architecture) now seed T1 scratch with `link-context` before agent dispatch, enabling automatic catalog link creation at storage boundaries.

## [3.1.2] - 2026-04-06

### Added
- SubagentStart hook documents sub-chunk span format `chash:<hex>:<start>-<end>`

## [3.1.1] - 2026-04-06

Plugin version aligned with Nexus CLI 3.1.1. No plugin-level functional changes.

## [3.1.0] - 2026-04-06

### Added
- `catalog_link_audit` MCP tool now performs content-hash span verification against T3 automatically
- Agents creating links can use `chash:<sha256hex>` spans for content-addressed chunk references (preferred over positional spans)
- All 7 link-creating agent tool signatures include `from_span`/`to_span` with chash: format
- SubagentStart hook injects chash: span guidance alongside catalog tools
- CONTEXT_PROTOCOL updated with Catalog tier in storage table and catalog-aware search options
- Session start hook surfaces `chunk_text_hash` and catalog routing params

## [3.0.0] - 2026-04-05

### Added
- Catalog tools injected for RDR lifecycle, knowledge, research, debug, and analysis agent contexts (SubagentStart hook)
- 12 agents/skills wired with catalog link creation: debugger (`relates`), deep-analyst (`relates`), codebase-analyzer (`relates`, `supersedes`), architect-planner (`relates`, `cites`), developer (`implements`), knowledge-tidier (`supersedes`, `relates`), deep-research-synthesizer (`cites`), all 7 RDR skills (`supersedes`, `cites`, `relates`)
- Query planner updated for `catalog_links` `{nodes, edges}` return format

### Changed
- `SubagentStart` hook catalog injection regex broadened — triggers on references, follow-on, catalog, RDR lifecycle, knowledge, research, debug, architecture, and analysis keywords

## [2.12.0] - 2026-04-04

Plugin version aligned with Nexus CLI 2.12.0. No plugin-level functional changes.

## [2.11.2] - 2026-04-03

Plugin version aligned with Nexus CLI 2.11.2. No plugin-level functional changes.

## [2.11.1] - 2026-04-03

Plugin version aligned with Nexus CLI 2.11.1. No plugin-level functional changes.

## [2.11.0] - 2026-04-03

### Added
- `query` MCP tool — document-level semantic search with full metadata
- `store_delete`, `memory_delete` MCP tools
- `search`/`query` `where` filter for metadata filtering
- `store_list` `docs` mode for document-level view
- `collection_info` peek (sample titles)
- `scratch` delete action

### Fixed
- All agent/skill tool references use full `mcp__plugin_conexus_nexus__` prefix
- Fixed `mcp__sequential-thinking__` prefix in 13 agent files
- Real offset pagination in T3 `list_store`
- `source_title` fallback in store_list and search
- FTS5 title search corrected in docs
- `search` param `n` → `limit`

### Changed
- MCP tool count: 12 → 17
- Session start hook references MCP tools (not CLI commands)
- Subagent start hook uses `Tool:` prefix with full names

## [2.10.8] - 2026-04-02

Plugin version aligned with Nexus CLI 2.10.8. No plugin-level functional changes.

## [2.10.7] - 2026-04-02

Plugin version aligned with Nexus CLI 2.10.7. No plugin-level functional changes.

## [2.10.6] - 2026-04-02

### Fixed
- **RDR close bead gate** — `rdr-close` skill and command now hard-gate
  on open beads instead of treating them as advisory. Agent must display
  open beads and get explicit user confirmation before closing.

## [2.10.5] - 2026-04-02

Plugin version aligned with Nexus CLI 2.10.5. No plugin-level functional changes.

## [2.10.4] - 2026-04-01

### Removed
- **PostToolUse prompt hook** — `type: "prompt"` is not valid for
  PostToolUse hooks (only `command` and `http` are supported), causing
  `PostToolUse:Bash hook error` on every Bash tool call. Removed
  entirely; `/conexus:debug` remains available on demand.

## [2.10.3] - 2026-04-01

### Added
- **PostToolUse prompt hook** — enforces `/conexus:debug` invocation on
  repeated test failures. Fires after Bash commands, has full
  conversation context, zero subprocess overhead. One manual retry is
  acceptable; two consecutive failures without `/conexus:debug` triggers
  the enforcement prompt.

## [2.10.2] - 2026-04-01

### Fixed
- **Restore skill routing guardrails** — Skill Directory, Process Flow,
  Storage Tier Protocol, and Red Flags table restored to SessionStart
  injection. Agents were not invoking debugger, architect, and other
  specialized skills after these were trimmed for compactness.

## [2.10.1] - 2026-04-01

### Fixed
- **Both hooks advisory-only** — removed test suite execution from
  Stop and PreToolUse hooks. Stop warns about uncommitted changes and
  open beads. PreToolUse warns about missing review markers. Neither
  blocks.
- **PreToolUse output format** — uses `hookSpecificOutput` /
  `permissionDecision` (correct PreToolUse protocol).
- **Bead ID extraction** — BSD sed compatible on macOS.

## [2.10.0] - 2026-04-01

### Added
- **Post-implementation verification hooks** (RDR-045) — two opt-in
  mechanical enforcement hooks that catch premature session closure and
  premature bead closure.
  - **Stop hook** (`on_stop: true`) — blocks session end on uncommitted
    git changes, open beads, or test failures. On retry, test failures
    are let through with a warning; mechanical issues continue to block.
  - **PreToolUse close gate** (`on_close: true`) — intercepts `bd close`
    and `bd done`, blocks on test failure, emits advisory when no
    review marker found in T1 scratch.
  - Standalone `read_verification_config.py` config reader for hooks
    (no nexus package imports).
  - hooks.json: `Stop` (timeout 180s) and `PreToolUse` (matcher `Bash`,
    timeout 300s) entries registered.

### Changed
- **code-review skill** — writes T1 scratch marker
  (`review-completed bead=<id>`) on successful completion, enabling the
  PreToolUse hook to verify review happened before bead closure.

## [2.9.1] - 2026-03-31

### Added
- **receiving-review skill** — technical evaluation of code review feedback
  with 6-step pattern (READ→UNDERSTAND→VERIFY→EVALUATE→RESPOND→IMPLEMENT),
  YAGNI check via serena-code-nav, pushback correction guidance.
- **git-worktrees skill** — isolated workspace setup with directory selection
  priority, safety verification, auto-detect setup (uv/pip/npm/cargo/go),
  Agent tool `isolation: "worktree"` guidance.
- **finishing-branch skill** — branch completion workflow: verify tests,
  present merge/PR/keep/discard options, typed "discard" confirmation,
  worktree detection, beads close + dolt push integration.
- **using-nx-skills routing** — git workflow section and common mistakes
  table updated for new skills.

### Changed
- **All agents and skills** — migrated `bd` CLI references to `/beads:`
  skill invocations. Skills are native to the Skill tool — more reliable
  for subagents, no shell escaping needed. Only `bd dolt push` retained
  (no skill equivalent). 29 files, 117 replacements.
- **registry.yaml** — 3 new standalone skills registered; removed
  superpowers reference from nx-preflight description.

### Fixed
- **Auto-detect MinerU fallback** — reuses already-computed fast_result
  instead of re-converting PDF through Docling (review finding).
- **test_plugin_structure.py** — consolidated STANDALONE_SKILLS into
  single module-level constant; added 3 new skills to exclusion set.

## [2.8.0] - 2026-03-30

### Added
- **analytical-operator agent** — executes 5 analytical operations (extract,
  summarize, rank, compare, generate) over retrieved content. Dispatched by
  the `/conexus:query` skill. (RDR-042)
- **query-planner agent** — decomposes complex analytical questions into
  step-by-step JSON execution plans with `$step_N` references. (RDR-042)
- **`/conexus:query` skill** — multi-step analytical query execution driver.
  Orchestrates query-planner → analytical-operator dispatch loop with T1
  scratch for step persistence and T2 plan library for reuse. (RDR-042)
- **`plan_save` / `plan_search` MCP tools** — expose T2 plan library to
  agents. Project-scoped FTS5 search over saved query plans. (RDR-042)
- **Orchestrator failure relay protocol** — distinguishes ESCALATION
  sentinels (circuit breaker, route to debugger) from incomplete output
  (retry up to 2x). (RDR-042)
- **SubagentStart hook** — now injects plan library and analytical operator
  guidance so all subagents know about the query pipeline. (RDR-042)

### Fixed
- **Serena hook tool names** — sn plugin SubagentStart hook now uses full
  MCP-prefixed names (`mcp__plugin_sn_serena__*`). Short names were invisible
  to subagents.

## [2.7.1] - 2026-03-28

### Added
- **nx MCP tool guidance injection** — SubagentStart hook now injects three-tier
  storage tool signatures (T1 scratch, T2 memory, T3 search/store) into ALL
  subagents, not just nx agents. Any arbitrary agent can now participate in
  inter-agent communication via T1 scratch and access project knowledge.

## [2.7.0] - 2026-03-28

### Added
- **Sequential thinking MCP injection** — SubagentStart hook now injects usage
  guidance for `sequentialthinking` MCP tool (when to use, parameter patterns
  for `needsMoreThoughts`, `isRevision`, branching).

## [2.6.1] - 2026-03-27

### Fixed
- **rdr-accept planning detection** — broadened section and heading matching,
  flipped default from "no" to "yes". RDRs with `## Approach` / `### Step`
  headings now correctly trigger planning handoff.
- **rdr-gate** — accepts Approach/Steps as valid plan sections (not just
  Implementation Plan).

## [2.6.0] - 2026-03-26

### Added
- **T1 scratch inter-agent context sharing** — tag vocabulary in CONTEXT_PROTOCOL,
  sibling context for relay-reliant agents, developer writes failed approaches,
  reviewer and debugger search scratch for predecessor findings.
- **Escalation relay improvements** — debugger relay includes `nx scratch` field,
  re-dispatch developer relay template with structured artifacts.
- **Escalation guard** — prevents infinite developer→debugger loop.

## [2.5.0] - 2026-03-25

### Added
- **Developer agent circuit breaker** — hard stop after 2 consecutive test
  failures with structured ESCALATION report for debugger dispatch.
- **Debugger escalation section** in development skill with relay template.
- **Developer → debugger escalation edge** in orchestration routing.

## [2.4.2] - 2026-03-25

Plugin version aligned with Nexus CLI 2.4.2. No plugin-level functional changes.

## [2.4.1] - 2026-03-24

Plugin version aligned with Nexus CLI 2.4.1. No plugin-level functional changes.

## [2.4.0] - 2026-03-24

### Bug Fixes (Track C)
- **C1**: Single-chunk CCE documents now use `contextualized_embed()` instead of falling back to `voyage-4`, fixing a model mismatch for short documents
- **C2/C3**: Paginated all unbounded `col.get()` calls in `indexer.py` to handle >300 chunks (ChromaDB Cloud hard cap)
- **C4**: Partial CCE embedding failure now re-embeds entire document with voyage-4 for consistency, preventing mixed-model vectors
- **C5**: MCP server collection cache uses atomic tuple assignment to eliminate race condition

### Post-Mortem Gap Closure (Track A)
- **A1**: Added retrieval quality unit tests that assert semantic rank ordering, not just `len(results) > 0`
- **A2**: Enhanced `nx collection verify --deep` with known-document probe and distance reporting; shared `verify_collection_deep()` function in `db/t3.py`
- **A3**: Added cross-model invariant regression test — fails if CCE index/query models diverge
- **A4**: New `nx collection reindex <name>` command with pre-delete safety check, per-type dispatch, and post-reindex verification
- **A5**: Per-chunk progress callback for pdf/md indexing — `--monitor` now shows tqdm bar during embedding

### MCP Server Enhancement (Track B)
- **B1**: `search` tool default changed from `corpus="knowledge"` to `corpus="knowledge,code,docs"` with `"all"` alias
- **B2**: New `collection_list` tool — lists all collections with document counts and models
- **B3**: New `collection_info` tool — detailed collection metadata
- **B4**: New `collection_verify` tool — known-document retrieval health probe

### Documentation (Track D)
- Updated CLI reference, architecture docs, MCP tool reference, and CLAUDE.md for all changes above

### References
- RDR-040: CCE Post-Mortem Gap Closure & MCP Server Enhancement
- Post-mortem: cce-query-model-mismatch

## [2.3.6] - 2026-03-23

Plugin version aligned with Nexus CLI 2.3.6. No plugin-level functional changes.

## [2.3.5] - 2026-03-23

### Docs
- **Unprefixed skill references** — all `/rdr-create` → `/conexus:rdr-create` etc.
  across documentation and RDR files.
- **Python version** — updated to 3.12–3.13 in plugin README prerequisites.

## [2.3.4] - 2026-03-23

### Fixed
- **Unprefixed skill references** — corrected `/rdr-create` → `/conexus:rdr-create` etc.
  across 11 documentation and RDR files.

## [2.3.3] - 2026-03-23

Plugin version aligned with Nexus CLI 2.3.3. No plugin-level functional changes.

## [2.3.2] - 2026-03-22

### Fixed
- **rdr-accept**: PROHIBITION block prevents orchestrator from bypassing
  planning chain. Chain mandatory for multi-phase RDRs. Subagent failure
  clause blocks "let me finish this directly" compensation. Dead T2
  idempotency code removed; self-healing uses live memory_get results.
  Unbound placeholders fixed with `<ID>` notation.
- **plan-enricher**: `bd update --description` replaced with Write tool →
  `--body-file` pattern. Prevents silent content corruption from shell escaping.
- **enrich-plan skill**: Standalone invocation path updated to match agent fix.

### Added
- **writing-nx-skills**: Known Pitfalls section for `--description` corruption.

## [2.3.1] - 2026-03-22

### Fixed
- StopFailure hook guarded behind `CLAUDECODE` env var — no more junk beads from test runs.

## [2.3.0] - 2026-03-22

### Added
- PostCompact hook (`post_compact_hook.sh`) — re-injects active beads and T1 scratch
  after compaction. Buffers output and only emits header when content exists.
- StopFailure hook (`stop_failure_hook.py`) — logs API failures to beads memory,
  creates blocker bead on rate limits. Python 3.9+ compatible, null-safe.

### Fixed
- PostCompact scratch test adapted for empty-scratch environments (CI).

## [2.2.0] - 2026-03-21

### Added
- `effort` frontmatter on all 15 agents and 28 skills (RDR-039 Phase 1)
- `maxTurns` on 2 haiku agents (knowledge-tidier=20, pdf-chromadb-processor=30)
- `HARD-CONSTRAINT` on pdf-chromadb-processor — must use `nx index pdf`, never manual extraction
- `_rdr_dir()` in rdr_hook.py — reads `.nexus.yml` for RDR path instead of hardcoding `docs/rdr`
- `closed` status to rdr_hook.py `_STATUS_ORDER` (was missing, caused wrong reconciliation)
- Essential MCP Tools section in using-nx-skills (sequential thinking + storage tiers)

### Changed
- Orchestrator upgraded from haiku to sonnet (routing ambiguous requests needs reasoning)
- plan-enricher version 1.0 → 2.0
- plan-auditor routing: substantive-critic added as first successor option
- using-nx-skills rewritten: routing decision tree replaces flat tables, Common Mistakes table
- writing-nx-skills updated for effort field, Agent tool reference, using-nx-skills update reminder
- pdf-process command simplified to delegate to skill (respects quick path for single PDFs)
- rdr_hook.py: terminal conflicts warn instead of auto-reconciling, explicit log messages
- subagent-start.sh: `python3` instead of `uv run python`
- All hooks now have explicit timeouts

### Removed
- `/conexus:orchestrate` command (routing tree in using-nx-skills replaces it)
- `mcp_health_hook.sh` (redundant with `nx hook session-start`)
- `setup.sh` (redundant, Setup event rarely fires)
- `bead_context_hook.py` (broken output format)
- `permission-request-stdin.sh` (dead code — wrong field names, settings bypass)
- Setup, PostToolUse, PermissionRequest hook events from hooks.json
- Duplicate T2 memory output from `session_start()` (session_start_hook.py is single source)

## [2.1.1] - 2026-03-15

### Fixed
- **Fully-qualify all skill slash command references** — all 19 files across agents,
  commands, hooks, skills, and README now use `/conexus:skill-name` instead of `/skill-name`.
  Short-form references were not invocable by users because Claude Code requires the
  `/<plugin>:<skill>` format for plugin-namespaced skills.

## [2.1.0] - 2026-03-15

Plugin version aligned with Nexus CLI 2.1.0. Local T3 backend (RDR-038) enables zero-config semantic search — agents and MCP tools work with local embeddings when no cloud credentials are configured. No plugin-level API changes.

## [2.0.0] - 2026-03-14

Plugin version aligned with Nexus CLI 2.0.0. T3 backend consolidated from 4 databases to 1 (RDR-037). No plugin-level API changes — agents and skills work unchanged.

## [1.12.1] - 2026-03-14

Plugin version aligned with Nexus CLI 1.12.1. No plugin-level functional changes.

## [1.12.0] - 2026-03-13

Plugin version aligned with Nexus CLI 1.12.0. No plugin-level functional changes.

## [1.11.1] - 2026-03-13

### Fixed
- **rdr-accept chain orchestration** — skill now explicitly dispatches all three
  agents sequentially (strategic-planner → plan-auditor → plan-enricher) instead
  of relying on agent-to-agent relay, which was impossible (subagents cannot spawn
  subagents)
- **Agent handoff model rewrite** — all 15 agents: "Successor Enforcement" →
  "Recommended Next Step" output blocks. Shared templates (`RELAY_TEMPLATE.md`,
  `CONTEXT_PROTOCOL.md`, `MAINTENANCE.md`, `README.md`) and 2 skills updated
  to match
- **Template variable mismatches** — `{rdr_file_path}`/`{path}` → `{rdr_file}`
  in rdr-accept command and skill
- **Stale "spawn" imperatives** in architect-planner, developer, orchestrator
  rewritten to output-oriented language
- **enrich-plan skill** added to using-nx-skills directory table

## [1.11.0] - 2026-03-12

### Added
- **plan-enricher agent** — enriches beads with audit findings, execution context, and
  codebase alignment after plan-auditor validates (sonnet, emerald)
- **enrich-plan skill + `/conexus:enrich-plan` command** — invoke plan-enricher standalone or
  via RDR planning chain
- **Planning handoff in `/conexus:rdr-accept`** — Step 7 auto-detects multi-phase RDRs and
  offers to dispatch strategic-planner → plan-auditor → plan-enricher chain
- **Conditional successor routing in plan-auditor** — T1 `rdr-planning-context` tag
  with RDR ID correlation routes to plan-enricher only in RDR planning context

### Changed
- **`/conexus:rdr-close` bead decomposition → bead status advisory** — close no longer creates
  beads; shows read-only status table, human decides which to close
- **strategic-planner Phase 3** renamed "Audit Handoff"; removed "iterate" instruction
- Registered plan-enricher in `registry.yaml` (agents, feature pipeline, model summary)
- Updated `rdr-accept` description in registry to mention planning dispatch
- Updated `rdr-close` description in registry, using-nx-skills, workflow docs
- Agent count: 14 → 15; Skill count: 27 → 28

## [1.10.3] - 2026-03-12

Plugin version aligned with Nexus CLI 1.10.3. No plugin-level functional changes.

## [1.10.2] - 2026-03-12

### Fixed
- **Remove `tools:` frontmatter from all 14 agents** (RDR-035) — Claude Code bug
  where explicit `tools:` in plugin agents filters out MCP tools. Agents now inherit
  all tools from the parent session. PermissionRequest hook remains as enforcement.

## [1.10.1] - 2026-03-11

### Fixed
- Removed `SessionEnd` hook — cancelled by Claude Code during process teardown,
  producing spurious error on every exit. T1 server stops with process tree; hook
  was a no-op.

## [1.10.0] - 2026-03-11

### Added
- **Nexus MCP server** (RDR-034) — bundled FastMCP server (`nx-mcp`) exposing 8
  structured tools for direct T1/T2/T3 storage access. Agents no longer depend on
  Bash for storage operations. Declared in `.mcp.json` alongside sequential-thinking.
- **Plugin-wide MCP migration** — all 14 agents, `_shared/` protocols
  (`CONTEXT_PROTOCOL.md`, `ERROR_HANDLING.md`), and 9 skills updated from CLI syntax
  to MCP tool syntax (`mcp__plugin_conexus_nexus__*`). Human-facing docs retain CLI syntax.
- **Permission auto-approval** for all `mcp__plugin_conexus_nexus__*` tools in the
  PermissionRequest hook.

### Changed
- `id` parameter renamed to `entry_id` in scratch tool calls across all agent and
  skill files (avoids Python builtin shadow).
- Plugin README rewritten: MCP Servers section expanded with full tool documentation,
  prerequisites table updated, permission section updated.

## [1.9.1] - 2026-03-10

### Changed
- Plugin version aligned with Nexus CLI 1.9.1. No plugin-level functional changes.

## [1.9.0] - 2026-03-10

### Changed
- **PDF agent rewrite** (RDR-033) — `pdf-chromadb-processor` agent v3.0 now delegates
  entirely to `nx index pdf` instead of reimplementing extraction in bash. Eliminates
  sandbox permission failures and context limit issues.

### Added
- **`nx store export`/`import` in pdf-processing skill** — agent can now suggest
  backup workflows using the new export/import commands.

## [1.8.0] - 2026-03-08

### Changed
- **Language-agnostic agents** (RDR-025) — renamed `java-developer` → `developer`,
  `java-debugger` → `debugger`, `java-architect-planner` → `architect-planner`.
  Agents use CLAUDE.md delegation for language/build/test detection at runtime.
- **Skill and command renames** — `java-development/` → `development/`,
  `java-debugging/` → `debugging/`, `java-architecture/` → `architecture/`.
  Commands: `/java-implement` → `/conexus:implement`, `/java-debug` → `/conexus:debug`,
  `/java-architecture` → `/conexus:architecture`.
- **Registry updated** — all pipelines, predecessor/successor chains, naming aliases,
  and model summary reflect new agent names.
- **18 cross-reference files updated** — orchestrator, strategic-planner, test-validator,
  plan-auditor, deep-analyst, deep-research-synthesizer, codebase-deep-analyzer,
  shared protocols, 6 skill files, and orchestrate command.

### Added
- **CLAUDE.md preflight check** in `/conexus:nx-preflight` — validates language, build system,
  and test command presence. Warnings only, not errors.

## [1.7.1] - 2026-03-07

### Added
- Project-local `/release` skill to enforce release checklist.

## [1.7.0] - 2026-03-07

### Added
- **Agent tool permissions** (RDR-023) — explicit `tools` frontmatter on all 14 agents
  with least-privilege assignments and sequential thinking MCP tool.
- **PermissionRequest hook expansion** (RDR-023) — auto-approve Read, Grep, Glob, Write,
  Edit, WebSearch, WebFetch, Agent, and sequential thinking for subagents. Expanded Bash
  allowlist with `uv run pytest`, additional `bd` subcommands, read-only `git branch`/`git tag`.
- **RDR process guardrails** (RDR-024) — soft-warning pre-checks in brainstorming-gate
  skill, strategic-planner relay validation, and bead context hook to catch implementation
  on ungated RDRs.

### Fixed
- **git branch/tag hook patterns** — restricted to read-only forms only.

## [1.6.1] - 2026-03-06

### Changed
- Plugin version aligned with Nexus CLI 1.6.1. PermissionRequest hook now auto-approves
  all nx subcommands with a deny guard on nx collection delete.

## [1.6.0] - 2026-03-06

### Changed
- Plugin version aligned with Nexus CLI 1.6.0. No plugin-level functional changes.

## [1.5.3] - 2026-03-05

### Changed
- Plugin version aligned with Nexus CLI 1.5.3. No plugin-level functional changes.

## [1.5.2] - 2026-03-05

### Changed
- Plugin version aligned with Nexus CLI 1.5.2. No plugin-level functional changes
  this release; all changes (retry helpers moved to `nexus.retry` leaf module) are
  in the CLI.

## [1.5.1] - 2026-03-04

### Changed
- Plugin version aligned with Nexus CLI 1.5.1. No plugin-level functional changes
  this release; all changes (ChromaDB transient retry, release process improvements)
  are in the CLI.

## [1.5.0] - 2026-03-04

### Changed
- Plugin version aligned with Nexus CLI 1.5.0. No plugin-level functional changes
  this release; all changes (auto-provision T3 databases, nx migrate removal, UX polish)
  are in the CLI.

## [1.4.0] - 2026-03-03

### Changed
- Plugin version aligned with Nexus CLI 1.4.0. No plugin-level functional changes
  this release; all changes (file lock, git hooks, `nx serve` removal) are in the CLI.

## [1.3.0] - 2026-03-03

### Changed
- Plugin version aligned with Nexus CLI 1.3.0. No plugin-level functional changes
  this release; all changes (`--force`, `--monitor`, auto-TTY, byte cap, AST line
  ranges) are in the CLI.

## [1.2.0] - 2026-03-03

### Changed
- Plugin version aligned with Nexus CLI 1.2.0. No plugin-level functional changes
  this release; all changes (SKIP class, context prefix, AST expansion) are in the CLI.

## [1.1.1] - 2026-03-02

### Fixed
- **`rdr-close` pre-check** — status check now correctly accepts `"accepted"` (or
  `"final"`) matching actual command behaviour; warning message shows `{current_status}`
  instead of the hardcoded `"Draft"`.

### Changed
- **Agent and skill counts** corrected throughout plugin docs after PM removal
  (14 agents, 27 skills).
- **`nexus` skill description** — "project management" replaced with "indexing".

## [1.1.0] - 2026-03-02

### Removed
- **`nx pm` command layer** — six slash commands (`/pm-archive`, `/pm-close`, `/pm-list`,
  `/pm-new`, `/pm-restore`, `/pm-status`), the `project-management-setup` agent, and
  the `project-setup` command and skill. T2 memory (`nx memory`) replaces all PM
  functionality directly; the layer added overhead without benefit.
- **`--mxbai` reference** removed from `nexus/reference.md` (Mixedbread integration
  removed from CLI).
- **Superpowers check** removed from `mcp_health_hook.sh` — superpowers is an optional
  plugin and should not produce session-start warnings.

### Changed
- `mcp_health_hook.sh`: `bd` not-found message now includes the install URL.
- `setup.sh`: prints a warning (rather than silently skipping) when `bd` is absent.
- `nx-preflight.md`: added `uv` prerequisite check as section 5.

## [1.0.0] - 2026-03-01

### Changed
- Plugin version aligned with Nexus CLI 1.0.0 release.
- Package name corrected in hook scripts.
- Skill count updated in README.
- Free-tier callout added to prerequisite table.

## [0.7.0] - 2026-03-01

### Added
- **Storage Tier Protocol** in `using-nx-skills` SKILL.md: T3→T2→T1 read-widest-first
  table and T1→persist→knowledge-tidy write path — gives every agent an explicit data
  discipline so they don't re-research what siblings already found.

## [0.6.0] - 2026-03-01

### Added
- **`serena-code-nav` skill**: navigate code by symbol — definitions, callers, type
  hierarchies, safe renames — without reading whole files.
- **SubagentStart T1 injection**: `subagent-start.sh` now injects live T1 scratch entries
  into every spawned agent's context; agents see session-wide discoveries immediately.
- **`using-nx-skills` polish**: 29-skill directory table with 5 categories, Announce step
  in process flow, 12 red flags (restored from 7), `brainstorming-gate` replaces
  `verification-before-completion` in Skill Priority.
- Registry trigger conditions sharpened: knowledge-tidier, orchestrator, substantive-critic.

### Fixed
- SessionStart hook matcher tightened to `startup|resume|clear|compact` (was match-all `""`).
- Wrong comment in `subagent-start.sh` claiming T1 is per-agent-scoped corrected; actual
  behavior (PPID-chain shared) documented inline.

## [0.5.0] - 2026-02-28

### Added (RDR-007: Claude Adoption — Session Context and Search Guidance)
- T2 multi-namespace prefix scan (`t2_prefix_scan.py`) — SubagentStart hook now surfaces all `{repo}*` namespaces, not just the bare project namespace
- `get_projects_with_prefix()` on T2Database with LIKE metacharacter escaping
- Cap algorithm: 5 entries with snippet + 3 with title-only + remainder as count per namespace; 15-entry cross-namespace hard cap
- `nx index repo --chunk-size N` flag — configurable lines-per-chunk for code files (default 150, min 1)
- `nx index repo --no-chunk-warning` flag — suppress large-file pre-scan warning
- Large-file pre-scan warning: detects code files exceeding 30× chunk size lines before indexing and suggests `--chunk-size 80`
- `chunk_lines` parameter threaded through `index_repository` → `_run_index` → `_index_code_file` → `chunk_file`
- Nexus skill `reference.md` updated: T2 namespace naming table, T2 Search Constraints section (FTS5 literal token rules, title-search caveat), Code Search guidance (nx vs Grep), RDR-006 precision note

### Changed
- `AST_EXTENSIONS` in `chunker.py` renamed from `_AST_EXTENSIONS` to public constant
- Warning suggestion is adaptive: recommends `--chunk-size 80` when no chunk size specified, or `max(10, current // 2)` when already set

## [0.4.0] - 2026-02-24

### Added
- brainstorming-gate skill: design gate before implementation (S1)
- verification-before-completion skill: evidence before claims (S2)
- receiving-code-review skill: technical rigor for review feedback (S3)
- using-nx-skills skill: skill invocation discipline (S4)
- dispatching-parallel-agents skill: parallel agent coordination (O3)
- writing-nx-skills meta-skill: plugin authorship guide (O5)
- Graphviz flowcharts in decision-heavy skills (O2)
- REQUIRED SUB-SKILL cross-reference markers (O4)
- Companion reference.md for nexus skill (O6)
- CHANGELOG.md
- SessionStart hook for using-nx-skills injection

### Changed
- All skill descriptions rewritten to CSO "Use when [condition]" pattern (C1, C2)
- Removed non-standard frontmatter fields from all skills (S6)
- Removed YAML comments from description block scalars (S5)
- Replaced inline relay templates with hybrid cross-reference to RELAY_TEMPLATE.md (O6)
- Simplified agent-delegating commands with pre-filled relay parts (C3)
- Added disable-model-invocation to pure-bash pm commands (O1)
- PostToolUse hook now has matcher for bd create commands only (S7)
- Nexus skill split into quick-ref SKILL.md + detailed reference.md

### Fixed
- PostToolUse hook performance (was firing Python on every tool use)

## [0.3.2] - 2026-02-23

### Added
- RDR workflow skills (rdr-create, rdr-list, rdr-show, rdr-research, rdr-gate, rdr-close)
- cli-controller skill with raw tmux commands
