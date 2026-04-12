---
name: rdr-audit
description: Use when auditing a project's RDR lifecycle for silent-scope-reduction frequency, or when inspecting or managing scheduled periodic audits
effort: high
---

# RDR Audit Skill

Wraps the canonical audit pattern proven in Phase 1b (RDR-067) as a one-command skill. Delegates the classification work to the **deep-research-synthesizer** agent (sonnet). See [registry.yaml](../../registry.yaml).

**Phase 2a scope**: default-mode audit dispatch only. The `list` / `status` / `history` / `schedule` / `unschedule` management subcommands are added in Phase 2b (`nexus-dqp.4`) and are not implemented here.

## When This Skill Activates

- User invokes `/nx:rdr-audit` (no argument → current project) or `/nx:rdr-audit <project>` — runs the audit
- User says "audit this project", "run the silent-scope-reduction audit", "check the base rate on `<project>`"
- Periodic audit trigger fires headlessly: external cron or launchd invokes `claude -p '/nx:rdr-audit <project>'` on the user's local machine (see Phase 4 scheduling templates)
- User invokes a management subcommand: `list` / `status <project>` / `history <project>` / `schedule <project>` / `unschedule <project>` (Phase 2b)

## Inputs

- **First positional argument** is either a subcommand OR a project name:
  - No argument → audit current project (see Current-Project Derivation below)
  - `<project>` → audit the named project (e.g., `nexus`, a public project the user owns)
  - `list` → list scheduled audits (Phase 2b, read-only)
  - `status <project>` → show next-fire timestamp + last-run outcome (Phase 2b, read-only)
  - `history <project>` → list last N audit findings for a project (Phase 2b, read-only)
  - `schedule <project>` → print platform-specific install commands (Phase 2b, print-only)
  - `unschedule <project>` → print uninstall commands (Phase 2b, print-only)
- **Optional audit mode flags** (Phase 2a):
  - Time window (default: last 90 days)
  - Pinpoint incident (specific RDR ID to compare against §Problem Statement)
  - `--no-transcripts` — skip the transcript-mining pre-step entirely (fast path)

## Current-Project Derivation

When invoked with no positional argument, derive the target project **name** via this precedence chain:

1. **`git remote get-url origin`** — parse the repo name from the remote URL; strip the `.git` suffix and take the last path component
2. **Fallback to `pwd` basename** if `git remote` fails, returns empty, or is ambiguous
3. **Final fallback: prompt the user** — ask "Which project should I audit? (no current-project could be derived)"

Derivation happens in the skill body before anything else runs. The derived name is what gets substituted into `{project}` in the canonical prompt.

## Project Worktree Path Resolution

The skill body resolves the **absolute filesystem path** to the target project's worktree so the canonical prompt can Glob files via fully-qualified paths. There is no universal convention for where users keep project worktrees, so the resolution precedence is:

1. **Explicit path argument**: if the positional project argument is already an absolute path (e.g. `/nx:rdr-audit /srv/work/ART`) OR a path containing a `/`, use it directly and derive the project name from its basename.
2. **`NEXUS_PROJECT_ROOTS` env var**: colon-separated list of directories under which the user keeps project worktrees. Example: `NEXUS_PROJECT_ROOTS="$HOME/src:$HOME/work"`. The skill body expands `~` and `$HOME`, probes each root for a child directory matching the project name, and uses the first existing match.
3. **Default candidate roots** (probed in order, used only if `NEXUS_PROJECT_ROOTS` is unset): `$HOME/git`, `$HOME/src`, `$HOME/projects`, `$HOME/code`, `$HOME/work`, `$HOME/dev`, `$HOME/Documents/git`. None of these are authoritative — they are common conventions, not assumptions. Users whose project roots are elsewhere should set `NEXUS_PROJECT_ROOTS`.
4. **Not found**: if no root/project combination resolves, the audit still runs — it skips the file-based evidence layer and relies on T2 `rdr_process` + RDR cross-references. Surface a user-visible note: "No local worktree for `<project>` found under `NEXUS_PROJECT_ROOTS` or default candidates; proceeding with T2 evidence only. Set `NEXUS_PROJECT_ROOTS` to restore file-based evidence."

The resolved absolute path is what gets substituted into `{project_path}` in the canonical prompt. The relative project name goes into `{project}` separately for display purposes. Never let the canonical prompt Glob a relative path — it will resolve against the main session's CWD (not the target project) and silently audit the wrong files, as surfaced during RDR-067 Phase 5a MVV first-attempt anomaly.

## Main-Session PRE-STEP (NOT DELEGATABLE)

**Transcript mining is NOT delegatable to subagents.** The `~/.claude/projects/*.jsonl` tree is not efficiently scannable from a subagent context — subagents do not have the main session's working directory, cannot traverse the directory efficiently with their tool budget, and the JSONL line lengths cause truncation in Grep tool output. The main session must gather transcript excerpts BEFORE dispatching the deep-research-synthesizer agent.

This invariant was verified in RDR-067 Phase 1b (T2 `nexus_rdr/067-spike-disposition` id 754). Violating it means the subagent receives an empty evidence layer and degrades silently.

**Pre-step flow**:

1. If `--no-transcripts` was passed → skip mining, leave the slot empty, proceed to dispatch. The canonical prompt explicitly supports the empty-slot degraded case.
2. Otherwise, scope the transcript directory: `ls ~/.claude/projects/-Users-<username>-git-<project>/` (path-mangled from the project's git worktree location). If the directory doesn't exist → leave the slot empty and proceed.
3. Grep the transcript tree for composition-failure indicator patterns (use the pattern list from the canonical prompt's §Indicator patterns section). Filter files by hit count and recency.
4. **Filter out meta-session noise**: sessions that DISCUSS the failure mode (the session that created this skill, any session working on RDR-065/66/67/68/69) must be excluded. They are self-referential noise, not evidence. The skill heuristic: exclude sessions modified within the last 24h if they contain ≥5 hits on meta-discussion terms (`silent scope reduction`, `composition failure`, `meta-RDR`, `failure mode`).
5. From the filtered candidates, extract up to 3 short decision-point turns (≤20 lines each) — prefer turns showing a retcon framing (`design mistake`, `latent`, `the plan was wrong`) or a reopen decision.
6. Format the excerpts as the `{transcript_excerpts}` substitution block with session ID and line numbers preserved as citations.
7. Budget cap on the pre-step: ≤10 main-session tool calls. Hitting the cap → fall back to empty slot + note the truncation in the dispatch record.

**Fast path**: if the target project has no `~/.claude/projects/*` directory, or `--no-transcripts` was passed, skip the entire pre-step and dispatch with an empty `{transcript_excerpts}` block. Phase 1b validated that the prompt handles the empty case cleanly.

## Canonical Prompt

The skill embeds the pinned canonical prompt from T2 at dispatch time:

1. `mcp__plugin_nx_nexus__memory_get(project="nexus_rdr", title="067-canonical-prompt-v1")` — loads the v1 prompt (ttl=0 permanent; reference by stable title only — do not pass any runtime numeric id as a parameter)
2. Substitute `{project}` with the derived project name (bare name, for display)
3. Substitute `{project_path}` with the resolved absolute worktree path (see §Project Worktree Path Resolution) — this MUST be an absolute path with no tilde, no `$HOME`, no relative components. The canonical prompt uses `{project_path}` wherever it Globs evidence files.
4. Substitute `{transcript_excerpts}` with the pre-step block (empty if fast path)
5. Pass the substituted text as the subagent's task body

Do NOT inline the canonical prompt text into this skill file — the prompt is the single source of truth in T2 and the skill references it by stable title. Updating the prompt is a T2 `memory_put` upsert, not a skill edit.

## Behavior — Audit Dispatch (default mode)

1. **Derive project name** (see Current-Project Derivation)
2. **Seed T1 link context** so the auto-linker ties findings to RDR-067:
   ```
   mcp__plugin_nx_nexus__scratch(
     action="put",
     content='{"targets": [{"tumbler": "1.1.771", "link_type": "cites"}], "source_agent": "deep-research-synthesizer"}',
     tags="link-context"
   )
   ```
3. **Main-session PRE-STEP** (transcript mining, see above — NOT delegatable)
4. **Load canonical prompt** from T2 and substitute `{project}` + `{transcript_excerpts}`
5. **Dispatch the deep-research-synthesizer agent** via the Agent tool with the substituted prompt as the task body (see Agent Invocation section)
6. **Wait for the subagent result**
7. **Parse the output** — extract the verdict category (VERIFIED / PARTIALLY VERIFIED / INCONCLUSIVE / FALSIFIED), incident count, confidence level, and drift-category distribution
8. **Skill body owns persistence** (critical — see Persistence Ownership note below): after the subagent returns, the main session calls `memory_put` to persist the full output. Subagents do NOT reliably self-persist; this was observed in 3/3 Phase 1b spike runs where subagents acknowledged persistence instructions but never called `memory_put`. The skill body itself must own this step.
   ```
   mcp__plugin_nx_nexus__memory_put(
     project="rdr_process",
     title="audit-<project>-<YYYY-MM-DD>",
     ttl=0,
     tags="rdr-audit,<project>,audit",
     content=<full subagent output with next_expected_fire timestamp header>
   )
   ```
9. **Surface a compact summary to the user**: verdict category, rate, confidence, drift-category distribution, and the T2 record id for the full detail
10. **Discrepancy check**: `memory_search(project="rdr_process", query="audit-<project>")` to find prior audits. If this audit contradicts a prior one (different verdict category OR different dominant drift category), flag the discrepancy for user review before returning

## Management Subcommands

Five management subcommands let users and agents inspect and manage scheduled audits from inside Claude Code without shelling out to OS primitives manually. Invoked as the first positional argument: `/nx:rdr-audit list`, `/nx:rdr-audit status <project>`, etc. If the first token is not one of the reserved subcommand words, the argument is treated as a project name and the skill routes to the default audit-dispatch flow instead.

### Safety Split (core user-protection invariant)

The five subcommands split into two disjoint safety classes:

**Read-only**: `list`, `status`, `history`
- Safe to invoke from any session — interactive, headless `claude -p`, CCR remote agent
- The skill body **must not modify OS state** — no file writes, no process spawns, no `launchctl load`, no `crontab -e`
- The skill body **must not modify T2 state** — no `memory_put`, no `memory_delete`, no mutation of any T2 record
- Invariant verification approach: a session snapshot of OS state (`launchctl list` + `crontab -l`) and T2 state (`memory_list` on `rdr_process`) taken before and after a read-only invocation should be byte-identical

**Print-only**: `schedule`, `unschedule`
- Safe to invoke from any session; the output is platform-specific install/uninstall instructions printed to stdout for user review
- The skill body **must not execute any privileged OS command** — specifically must not run `launchctl load`, must not run `launchctl unload`, must not write `.plist` files to `~/Library/LaunchAgents/`, must not edit crontab via `crontab -e`, must not spawn any process that installs or modifies scheduled triggers
- The output is a printed template the user reviews and runs themselves manually
- Invariant verification approach: a dry-run harness that captures stdout and inspects `~/Library/LaunchAgents/`, the user's crontab, and running-process list before and after — confirms no files written, no processes spawned, no scheduled triggers modified

**System-level installs are explicitly the user's step.** The skill never runs them automatically. This split protects users from unauthorized privileged OS changes while still making the management surface inspectable from any session.

### `list` (read-only)

Enumerate all scheduled rdr-audit triggers on the local machine across both macOS launchd and Linux cron.

Behavior:
1. Shell out: `launchctl list | grep rdr-audit` (macOS) — capture lines matching `com.nexus.rdr-audit.*`
2. Shell out: `crontab -l 2>/dev/null | grep rdr-audit` (Linux) — capture matching lines
3. Parse both outputs, aggregate into a single table
4. Format as markdown table with columns: `project`, `platform` (launchd/cron), `schedule expression`, `next-fire timestamp` (when available)
5. If both shell-outs return empty: print `No audits scheduled` and exit 0

This subcommand calls **only** the two read commands above. It does **not** call `launchctl load`, `launchctl unload`, `crontab -e`, or any write-variant command. It does **not** touch T2 at all.

### `status <project>` (read-only)

Show next-fire time + last-run outcome for a specific project's audit.

Behavior:
1. Parse `launchctl print com.nexus.rdr-audit.<project>.90d` (macOS) OR parse the matching `crontab -l` line (Linux) to extract the next-fire timestamp. If neither finds a trigger, the subcommand reports `No audit scheduled for <project>` and exits 0.
2. `mcp__plugin_nx_nexus__memory_search(project="rdr_process", query="audit-<project>")` — find the most recent entry
3. `mcp__plugin_nx_nexus__memory_get(project="rdr_process", title=<found title>)` — read the last-run content
4. Extract the verdict category, rate, confidence, and drift distribution from the T2 record
5. Display side-by-side:
   ```
   Project: <project>
   Scheduled: <platform> — next fire <timestamp>
   Last run:  <date> — <verdict> — <rate> — <confidence>
   Drift:     <distribution>
   ```

Read-only: no `memory_put`, no `launchctl load`, no crontab edit, no file writes. T2 reads via `memory_search` and `memory_get` only. OS reads via `launchctl print` or `crontab -l` only.

### `history <project>` (read-only)

List the last N audit findings for a project from T2.

Behavior:
1. `mcp__plugin_nx_nexus__memory_search(project="rdr_process", query="audit-<project>")` — enumerate matching entries
2. Take the most recent **N entries** (default N=5; accept `--count N` override)
3. For each: `memory_get` the entry content
4. Display a compact list: date, verdict, rate, confidence, drift distribution, T2 id

Read-only: exclusively T2 `memory_search` + `memory_get`. No OS interaction. No T2 writes.

### `schedule <project>` (print-only)

Print the platform-specific install commands for the user to review and run themselves manually. **Does not execute the install.**

Behavior:
1. Detect platform via `uname -s` (Darwin → macOS, Linux → Linux)
2. On macOS: render the plist body (see template below) with `<PROJECT>` substituted, and print together with the `launchctl load` instruction the user will run
3. On Linux: render the crontab line (see template below) with `<PROJECT>` substituted, and print together with the `crontab -e` instruction the user will run
4. Print to stdout only

**Cadence note (macOS vs Linux)**: launchd's `StartCalendarInterval` does not support exact 90-day intervals natively. The macOS plist template below fires on the 1st of each month at 03:00 local time — **approximately 30-day cadence**, not the RDR-067 target 90-day cadence. This is the closest practical approximation launchd supports without manual month-list scheduling. The Linux crontab template (shown after the plist) uses `0 3 1 */3 *` which IS a true 90-day cadence (1st of every 3rd month). macOS users who want true quarterly cadence have three options: (a) accept the monthly approximation (the failure mode occurs ~1-2× per month, so monthly sampling is strictly finer-grained than the target, not coarser), (b) add a month-list `StartCalendarInterval` with explicit Jan/Apr/Jul/Oct entries, or (c) switch to a user-level cron daemon (`pcron`, `gcron`) and use the Linux crontab template instead. When the skill prints this plist via `/nx:rdr-audit schedule <project>`, it prints this cadence note alongside the template so the user is not surprised.

**macOS plist template** (substitute `<PROJECT>` with the target project name; coordinate with `scripts/launchd/com.nexus.rdr-audit.PROJECT.plist` from Phase 4):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTD/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.nexus.rdr-audit.<PROJECT>.90d</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/local/bin/claude</string>
    <string>-p</string>
    <string>/nx:rdr-audit <PROJECT></string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Day</key><integer>1</integer>
    <key>Hour</key><integer>3</integer>
    <key>Minute</key><integer>0</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>/tmp/rdr-audit-<PROJECT>.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/rdr-audit-<PROJECT>.err</string>
</dict>
</plist>
```

**Linux crontab template** (substitute `<PROJECT>`; coordinate with `scripts/cron/rdr-audit.crontab` from Phase 4):

```cron
0 3 1 */3 * /usr/local/bin/claude -p '/nx:rdr-audit <PROJECT>' >> ~/.local/state/rdr-audit-<PROJECT>.log 2>&1
```

**Printed installation instructions** (what the user then runs themselves):

- macOS:
  ```
  1. Save the plist above to ~/Library/LaunchAgents/com.nexus.rdr-audit.<PROJECT>.90d.plist
  2. Run: launchctl load ~/Library/LaunchAgents/com.nexus.rdr-audit.<PROJECT>.90d.plist
  3. Verify: launchctl list | grep rdr-audit
  ```

- Linux:
  ```
  1. Run: crontab -e
  2. Append the line above
  3. Save and exit the editor; cron picks up the change automatically
  4. Verify: crontab -l | grep rdr-audit
  ```

The skill body itself **must not write** the plist file, **must not execute** `launchctl load`, **must not edit** the crontab via `crontab -e`, and **must not spawn** any privileged OS process. The templates are printed text; all state changes are the user's explicit step.

### `unschedule <project>` (print-only)

Print the platform-specific uninstall commands for the user to review and run themselves manually. **Does not execute the uninstall.**

Behavior:
1. Detect platform via `uname -s`
2. On macOS: print the `launchctl unload` + `rm` commands the user will run
3. On Linux: print `crontab -e` instructions and the line to remove
4. Print to stdout only

**macOS uninstall commands** (printed for user to run):
```
launchctl unload ~/Library/LaunchAgents/com.nexus.rdr-audit.<PROJECT>.90d.plist
rm ~/Library/LaunchAgents/com.nexus.rdr-audit.<PROJECT>.90d.plist
```

**Linux uninstall instructions** (printed for user to run):
```
1. Run: crontab -e
2. Remove the line matching: /nx:rdr-audit <PROJECT>
3. Save and exit
4. Verify: crontab -l | grep rdr-audit   # should show nothing
```

The skill body itself **must not execute** `launchctl unload`, **must not write** or delete plist files, **must not edit** crontab via `crontab -e`, and **must not spawn** any privileged OS process. The commands are printed text the user runs manually.

### Subcommand-to-project-name disambiguation

The first positional argument may be either a reserved subcommand word or a project name. Rule: check the reserved subcommand set first (`list`, `status`, `history`, `schedule`, `unschedule`) — if the first token matches any of these exactly, route to the named subcommand. Otherwise treat the first token as a project-name argument and route to the default audit-dispatch flow. This prevents a project literally named `list` from being hijacked to the listing subcommand (the user can use `/nx:rdr-audit <full-qualified-name>` or rename the project).

### Format coordination with Phase 4 scheduling templates

The plist and crontab templates above are the canonical shape. Phase 4 (`nexus-dqp.7`) ships the same templates as sibling files under `scripts/launchd/` and `scripts/cron/`. Both bead paths converge on the same format: the file is the source of truth at install time; the skill's `schedule` subcommand renders the equivalent text from the skill body for immediate user review. Updating the template means updating both locations — the sibling script file and this skill section — in one PR.

## Persistence Ownership (Phase 1b finding)

The skill body MUST own the `memory_put` step, not the subagent. This is an invariant from Phase 1b:

- 3/3 spike runs dispatched the deep-research-synthesizer agent with explicit instructions to call `memory_put` on their findings
- 3/3 runs returned audit output and reported "Findings stored" in their return value
- 0/3 runs actually called `memory_put` — all records had to be persisted by the main session after the dispatch

The root cause is not diagnosed (could be model-level instruction compliance, could be tool routing in subagent context, could be a budget ceiling heuristic). The Phase 2 resolution is structural: persistence is a skill-body responsibility. The subagent returns the audit text; the skill body parses it, formats the T2 record, and calls `memory_put` directly. Do NOT rely on the subagent to self-persist under any circumstances.

## Agent Invocation

Use the Agent tool with standardized relay format.
See [RELAY_TEMPLATE.md](../../agents/_shared/RELAY_TEMPLATE.md) for required fields and examples.

```markdown
## Relay: deep-research-synthesizer

**Task**: Execute the RDR-067 canonical audit prompt against `<project>`. Output must conform to the `## Output format (REQUIRED)` section of the prompt (CA-1 structural criteria a-d).
**Bead**: none (or the invoking bead ID if triggered from planning work)

### Input Artifacts
- nx store: none
- nx memory: rdr_process/* (prior audits for this project), nexus_rdr/067-canonical-prompt-v1 (the canonical prompt)
- Files: `<project-worktree>/docs/rdr/post-mortem/*.md` (primary evidence; the skill body substitutes the absolute worktree path into the prompt via the `{project_path}` parameter before dispatch)

### Deliverable
Full audit output conforming to the canonical prompt's output-format contract:
- Sources consulted
- Sampling caveats
- Confirmed incidents in window (with all required fields) OR explicit VERDICT: INCONCLUSIVE block
- Frequency estimate with HIGH/MEDIUM/LOW confidence
- Drift-category distribution
- (Optional) Near-misses
- Recommendation (exactly one of VERIFIED / PARTIALLY VERIFIED / FALSIFIED / INCONCLUSIVE)

### Quality Criteria
- [ ] All four CA-1 structural criteria satisfied (a confirmed-or-inconclusive, b caveats, c frequency+confidence, d drift-category per incident)
- [ ] ≤25 tool calls (budget ceiling from Phase 1b)
- [ ] No unsolicited trailing relay sections ("Next Step", "Recommended Next Step", etc.)
- [ ] Respects the non-delegatable invariant — do NOT attempt to glob `~/.claude/projects/*` from within the subagent context
```

**Task body**: embed the full substituted canonical prompt (loaded via `memory_get(project="nexus_rdr", title="067-canonical-prompt-v1")`, with `{project}` and `{transcript_excerpts}` filled in) as the task content. The relay envelope above wraps the prompt — the prompt itself is the dispatch payload.

## Success Criteria

- [ ] Target project name derived (git remote → pwd basename → user prompt)
- [ ] T1 link context seeded pointing to RDR-067 tumbler 1.1.771
- [ ] Main-session transcript pre-step completed (or explicitly skipped via `--no-transcripts` / no transcript directory)
- [ ] Canonical prompt loaded from T2 `nexus_rdr/067-canonical-prompt-v1` and substituted
- [ ] deep-research-synthesizer dispatched with the substituted prompt
- [ ] Subagent output parsed for verdict category, incident count, confidence, drift distribution
- [ ] Full output persisted by the skill body to T2 `rdr_process/audit-<project>-<YYYY-MM-DD>` with `ttl=0`
- [ ] Compact summary surfaced to the user with T2 record id
- [ ] Discrepancy check against prior audits completed, any contradiction flagged

## Cross-Project Incident Filings

When the audit subagent reads T2 `rdr_process`, it ingests both prior audit outputs and individual incident filings from sibling projects. Sibling projects file cross-project incidents using the template at `nx/resources/rdr_process/INCIDENT-TEMPLATE.md` (Phase 3). The template has 6 frontmatter fields (`project`, `rdr`, `incident_date`, `drift_class`, `caught_by`, `outcome`) and 8 required narrative sections covering what was meant, what shipped, the gap, decision point, mechanism, what caught it, cost, and lessons.

Filings land in T2 as `rdr_process/<project>-incident-<slug>` with `ttl=0` (permanent). The audit subagent picks them up via `memory_list(project="rdr_process")` + `memory_search(project="rdr_process", query="<project>")`. The template's `drift_class` enum values exactly match the sub-pattern taxonomy in the canonical prompt, so the subagent can classify filed incidents directly without translation.

See the template file for the full schema and `## How to file` instructions.

## Agent-Specific PRODUCE

Outputs generated across the audit flow:

- **T1 scratch**: link-context entry via scratch tool: action="put", content='{"targets": [...], "source_agent": "deep-research-synthesizer"}', tags="link-context" — seeded before dispatch, consumed by the auto-linker when findings are stored. Also: ephemeral transcript excerpt staging during the pre-step, via scratch tool with tags="rdr-audit-transcripts,session-<project>-<date>".
- **T2 memory** (persisted by the skill body, NOT the subagent): full audit output via memory_put tool: project="rdr_process", title="audit-<project>-<YYYY-MM-DD>", ttl=0 (permanent), tags="rdr-audit,<project>,audit". Includes a `next_expected_fire` timestamp header for Phase 2b `status` subcommand use.
- **T3 knowledge** (deferred to Phase 5): cross-project aggregated audit evidence is out of scope for Phase 2a. Phase 5 may promote accumulated T2 audits to T3 as a project-agnostic pattern library. For Phase 2a, all audit output stays in T2 `rdr_process`.

## Phase 1b Signals Folded In

This skill incorporates six signals surfaced by the Phase 1b spike (T2 `nexus_rdr/067-spike-disposition`):

1. **Skill body owns `memory_put`** — subagents do not reliably self-persist (0/3 Phase 1b runs called memory_put despite explicit instruction). Resolved by moving persistence to the skill body.
2. **Transcript-mining fast path** — `--no-transcripts` flag + automatic fast path when no transcript directory exists. Resolves the meta-session noise trap identified in Phase 1b.
3. **No unsolicited trailing sections** — the relay Quality Criteria explicitly forbids trailing "Next Step" blocks (Run 1 emitted one, Runs 2+3 did not after being instructed not to).
4. **≤25 tool budget ceiling** — documented in the relay Quality Criteria and inherited from the canonical prompt's own budget section.
5. **Near-misses section is high-signal** — Run 3 surfaced 3 near-miss catches as positive evidence for the prevention infrastructure. Consider promoting this section from optional to required in a prompt v2.
6. **Output is narrative, not scalar** — LLM classification variance on borderline incidents means the skill surfaces the full subagent output to the user, not a collapsed single-scalar verdict. The compact summary in Step 9 is a pointer to the full T2 record, not a replacement for it.
