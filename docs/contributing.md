# Contributing

## Development Setup

```bash
git clone https://github.com/Hellblazer/nexus.git
cd nexus
uv sync
scripts/reinstall-tool.sh           # install nx CLI (preserves optional extras)
nx init                              # provision + start the local service backend (RDR-174 collapsed flow)
nx hooks install                     # auto-index this repo on every commit
```

The unit suite is self-contained — `uv run pytest` uses an in-process
`InMemoryVectorClient` (chromadb is not a dependency) over the suite's
self-provisioned engine substrate, so it needs **no** running daemon or
service. `nx init` is only required for shell CLI usage
(`nx memory`, `nx index`, `nx search`) against persistent state; it provisions
and starts the nexus-service that serves every tier in the default config, and
offers to register the OS autostart unit (accept it, or use `--no-autostart`
for a session-only supervisor).

**Which `nx` a Claude Code session uses.** The plugin's servers and hooks look
for the CLI in the install directory `scripts/reinstall-tool.sh` writes
(`~/.local/share/nexus/tools/current`) BEFORE they look on `PATH` (RDR-228).
So a session runs the generation you last built with `reinstall-tool.sh`, even
when a shell with an activated `.venv` started it; run the script again to get
your edits into sessions. The `/conexus:*` commands still run the `nx` that
`PATH` finds. A test or harness that isolates itself by setting `PATH` must
also set `NX_TOOLS_DIR` to an empty directory, or it finds your install.

The SQLite T2 daemon is retired (nexus-i711w), so there is no daemon to hack
on, restart after a version bump, or run in the foreground. T2 is served by
the nexus-service; for that stack use `nx daemon service start|stop|status`.

## Running Tests

```bash
uv run pytest -n auto                 # full unit suite in parallel (no API keys needed)
uv run pytest                         # serial fallback, for debugging
uv run pytest -m lint                 # repo-structure meta-tests, PR-gated in CI
uv run pytest -m integration          # E2E tests (requires real API keys)
uv run pytest --cov=nexus             # with coverage
uv run pytest tests/test_indexer.py   # single file
uv run pytest -k "test_frecency"      # by name pattern
```

Unit tests use `InMemoryVectorClient` + bundled ONNX MiniLM model — no accounts needed. The default selection (`-m 'not integration and not slow and not lint'`) leaves out the integration, slow and lint buckets, so a bare `uv run pytest` is not everything CI runs. After a pull, a rebase, or any edit under `service/`, run `scripts/build-gate-jar.sh` before the first substrate-backed test; a session refuses to start on a stale jar (exit 75). [test-running](agents/test-running.md) has the hand-run rules and the engine-substrate details.

For integration tests: copy `.env.example` to `.env`, fill in your keys, then:

```bash
set -a && source .env && set +a
uv run pytest -m integration
```

### Storage-stack sandbox gate (T1 + T2 + T3)

The HTTP storage-tier suites (`tests/db/test_http_*_integration.py` and the Java
serving contract tests) run entirely in-sandbox: each spins up its own ephemeral
PG17 + a fresh service JAR with an isolated bearer — no production data, no live
daemon, no API keys. Because they are `@pytest.mark.integration` (excluded from the
default CI/unit run), storage-stack regressions can rot unseen. One button-press
runs the whole tier stack:

```bash
scripts/validate/integration-stack.sh               # build jar, run T1+T2+T3
scripts/validate/integration-stack.sh --no-build    # reuse the existing jar
scripts/validate/integration-stack.sh --python-only # T1/T2/catalog suites only
scripts/validate/integration-stack.sh --java-only   # T3 + repo-layer Java tests only
```

Run it after any change to the HTTP stores, the Java service handlers/schema, or
the token/RLS model. Prereqs (dev box): a JDK/GraalVM and pg17 binaries. When the
prereqs are absent the suites self-skip and the gate reports **inconclusive**
(non-zero exit), never a false green.

### Benchmark corpus for local-mode measurements

Indexing the whole repository takes hours in local mode, which is too slow to
repeat for a search-latency survey, a rerank page-quality check or an
index-throughput run. `tests/e2e/benchmark-corpus.sh` builds a small, fixed
corpus in a sandbox instead: a snapshot of `src/nexus` and `docs/` (no
`tests/`, no `docs/rdr/`) at the `v7.77.1` release commit, plus 30 notes in
two subject collections (`distributed-systems`, `vector-search`). The sandbox
has its own HOME, installs the published conexus from PyPI with `uv tool
install` and runs `nx` in local mode under an `env -i` allowlist, the same
way `fresh-install-mvv.sh --published` does.

```bash
tests/e2e/benchmark-corpus.sh run                 # build, assert, print the numbers, tear down
tests/e2e/benchmark-corpus.sh up                  # same, then leave the engine and Postgres up
tests/e2e/benchmark-corpus.sh env  <sandbox>      # the sandbox environment as KEY=VALUE lines
tests/e2e/benchmark-corpus.sh exec <sandbox> -- <command>
tests/e2e/benchmark-corpus.sh down <sandbox>      # stop, assert no process names it, delete
tests/e2e/benchmark-corpus.sh run --exec '<command>'   # measure inside a run
```

A follow-on command sees `HOME`, `PATH` (the sandbox's `nx` first),
`NX_LOCAL=1`, `NX_BENCH_SANDBOX`, `NX_BENCH_SNAPSHOT` (the indexed
repository) and `NX_BENCH_PYTHON`, and nothing else. The run fails, and cannot
skip, when the chunk counts leave the pinned ranges, the fixture is not
exactly 30 notes in 2 subjects, a phrase from the snapshot or from a note does
not come back from a search, or a process still names the sandbox after
teardown. It prints the chunk count per collection, the wall time of the
first index and the chunks per second. Measured on chas (8 CPUs, shared with
CI, conexus 7.77.1): 10,883 chunks (code 9,263, docs 1,620), first index 1,557 s,
about 7 chunks per second, 27 minutes for the whole run.

The pins (commit, conexus version, chunk ranges, the snapshot's file count and
digest) live in `tests/e2e/benchmark-corpus/benchmark_corpus.py`, and
`test_benchmark_corpus.py` beside it checks them. Moving to a later release
means changing the pins together and re-measuring the ranges; run it on a
test host (on chas, through `lab-run`), not on a busy laptop.

## Code Conventions

- **Python 3.12–3.13**: use `match/case`, `tomllib`, `typing.Protocol`, walrus operator
- **Type hints everywhere**: all public functions, methods, module-level variables
- **No ORM**: raw SQL. T2 is HTTP stores over the engine's Postgres tables (`db/t2/`); legacy `sqlite3` code is migration-source/maintenance-only, never a destination for new state — see AGENTS.md's NO-SQLITE hot rule
- **Logging**: `structlog` — never `print()` in library code
- **TDD**: write tests before implementation
- **Package manager**: `uv` (not pip directly)

## Project Structure

```
src/nexus/           # Core Python package
  commands/          # Click CLI commands (one file per group)
  db/                # Storage tier implementations (t1, t2, t3)
conexus/                  # Claude Code plugin (skills, agents, hooks)
tests/               # pytest test suite
docs/                # Documentation
```

See [architecture.md](architecture.md) for the full module map.

## Adding a CLI Command

1. Create `src/nexus/commands/your_cmd.py` with a Click group or command
2. Register it in `src/nexus/cli.py` via `cli.add_command()`
3. Add tests in `tests/test_your_cmd.py`
4. Document in `docs/cli-reference.md`

## Adding a T2 Domain Feature

T2 is split into nine domain stores under `src/nexus/db/t2/`, each an HTTP
client against the engine's Postgres (the SQLite twins were deleted in
RDR-158 P4, nexus-i711w): `http_memory_store.py`,
`http_plan_library.py`, `http_taxonomy_store.py`,
`http_telemetry_store.py`, `http_chash_index.py`,
`http_document_aspects_store.py`, `http_aspect_queue.py`,
`http_document_highlights_store.py`, and `http_tuple_store.py` (the Linda
tuple space, RDR-205). See
[architecture.md § T2 Domain Stores](architecture.md#t2-domain-stores)
for the map (note: `chash_index`, `taxonomy`, `document_aspects`,
`aspect_queue`, `document_highlights`, and `tuples` are reached directly
via their attributes, not through facade delegates — `memory`, `plans`,
and `telemetry` are the three with facade delegate methods). Two more
`Http*Store` modules live in the same directory but are not part of this
nine-store facade set: `http_centroid_store.py` and `http_token_store.py`
back other subsystems (taxonomy clustering and data-token minting
respectively) and are constructed independently of `T2Database`.

**Adding a method to an existing store** (the common case):

1. Add the method to the store's class in its own module — use the
   store's own HTTP session / engine routes via its internal methods;
   do not reach out to the facade.
2. If the feature needs a new table or column, that is a Liquibase
   changeset in the engine — the client-side migration chain is
   DELETED (RDR-158 P4 Stage 4; there are no per-store SQLite
   migrations in any mode).
   - **Substrate boundary (RDR-120 §A8, restated for the engine
     era):** the changeset ships DDL only. Any work beyond DDL
     (per-row backfills, sweeps, content seeding) belongs in a
     consumer verb under the matching `nx <area>` command group or
     an upgrade-ladder rung. The narrow set of exceptions lives in
     RDR-120 §Research Findings ("§A8-exempt substrate-owned
     writes"); if your change is not on that list, it ships
     DDL-only and the data work moves to a
     consumer verb.
3. If external callers should be able to use the method via the
   `T2Database` facade for backward compatibility, add a one-line
   delegate on `T2Database` in `src/nexus/db/t2/__init__.py`.
   Otherwise prefer the domain call style: `db.memory.your_method(...)`.
4. Tests go in the matching file — `tests/test_memory.py`,
   `tests/test_plan_library.py`, `tests/test_taxonomy.py`,
   or `tests/test_t2.py` for cross-domain cases.

**Adding a whole new domain store** (rare):

1. Schema first: the store's tables are Liquibase changesets in the
   engine (`service/`), DDL-only per the substrate boundary above.
   There is no client-side DDL in any mode (NO-SQLITE directive,
   2026-07-18).
2. Add the engine routes (Java) and gate them with the engine's own
   test suite.
3. Create `src/nexus/db/t2/http_<your_domain>_store.py` following the
   existing `Http*Store` twins (bearer auth, tenant header,
   `_refreshable_client` session handling).
4. Add the store to `T2Database.__init__` in construction order and
   tear it down in `T2Database.close()`.
5. If your store registers cross-domain expiry work, add it to
   `T2Database.expire()`.

**Concurrency rules**:

- Concurrency is arbitered by Postgres in the engine — the client
  stores are stateless HTTP clients and need no cross-store locking.
- Do not add a global T2 lock client-side. If two domains genuinely
  need to coordinate (rare), that coordination belongs in the engine
  (one transaction, one route), not in Python.

## Adding an Agent or Skill

See `conexus/README.md` for the plugin structure. Skills live in `conexus/skills/<name>/SKILL.md`, agents in `conexus/agents/<name>.md`, and both are registered in `conexus/registry.yaml`.

**MCP tools in agents**: Agents do NOT declare a `tools:` or `disallowedTools:` field in frontmatter — Claude Code has confirmed bugs where these fields in plugin-defined agents filter out MCP tools or are silently ignored (see RDR-035, RDR-039). Agents inherit all tools from the parent session; the `settings.json` permissions list provides runtime enforcement. Agent body text references MCP tool syntax (not CLI commands) for storage tier operations. See `conexus/README.md` § MCP Servers for tool names and parameters.

## Version Pinning

Two packages have known breaking incompatibilities and must be pinned to exact versions in `pyproject.toml`:

- `llama-index-core` (AST chunking dependency)
- `tree-sitter-language-pack` (parser compatibility)

Do not bump these without testing the full chunking pipeline.

## Git Workflow

- Branch naming: `feature/<bead-id>-<short-description>`
- **Integration branch is `develop`.** Open PRs against `develop`, not `main`. `main` carries the plugin marketplace surface; the develop split protects it from in-flight churn. Releases promote `develop` to `main` via merge (or merge-then-tag).
- `main` takes changes through PRs only, release version-bumps included (nexus-mkj6u replaced the prior direct-to-main carve-out). That is policy, held by the push guard and the release skill, not by the branch protection (below). See Release Process below.
- **Pushing `develop` from the shared checkout goes through `scripts/git-push-develop.sh <sha>...`** (nexus-9wxu6). Every session commits as the same git user, so the outbound range cannot be attributed by author; the script fetches, lists `origin/develop..develop`, and pushes only when that range equals the commits you vouch for on the command line. An unvouched commit, a vouched commit that is not in the range, or a diverged branch refuses with a `PUSH_REFUSED_*` line and pushes nothing. From a detached worktree based on `origin/develop`, `NX_PUSH_SOURCE=HEAD` pushes your worktree's commits and leaves the local `develop` (a peer's in-flight tree) alone. `git commit --amend` in the primary checkout is refused by the user-level policy hook when HEAD is not a commit this session made (`tests/fixtures/hal_git_policy_hook.py` rule 3); amend in a worktree you own, or make a new commit. Rule 5 below applies to an amend too, so even on your own tip it must name its paths. The same hook (rule 4) denies a bare `git push` to `develop` in any repo whose toplevel carries the script, so the script is the only unblocked path; `# routing-allow: <reason>` is the audited escape. The script also takes the `lock/ci-develop-push` tuple-space lock immediately before the push and releases it right after (nexus-agctp), refusing with `PUSH_REFUSED_LOCK_HELD` (naming the holder and lease expiry) or `PUSH_REFUSED_LOCK_UNREACHABLE`; `NX_PUSH_SKIP_LOCK='<reason>'` is the named escape.

- **Commits in the shared primary must name their paths, and the push audits what they contain** (nexus-bbriq). Vouching is by SHA: it proves you made the commit and says nothing about what is in it. On 2026-09-17 a peer had staged a 740-line `docs/rdr/rdr-212-*.md` draft in the shared index; an accept commit ran `git add <two paths>` then a bare `git commit`, which commits the whole INDEX, so the peer's draft rode `0249b0c98` to `origin/develop` past every check. Two guards now:
  - **Commit time** — the user-level policy hook (rule 5) refuses a `git commit` that does not name its paths, and refuses `git commit -a` outright, in a primary checkout that has linked worktrees — the observable signature of a checkout several sessions share, so a throwaway single-checkout repo is untouched. A pathspec must name specific FILES: a bare directory, `.`, or a glob is `-a` scoped to a subtree, and reproduces the incident exactly (measured 2026-09-18 — with `docs/rdr/mine.md` and a peer's `docs/rdr/rdr-212-peer.md` both staged, `git commit -m x -- docs/rdr/` committed both). Use `git commit -m "..." -- path/a.py path/b.py`. An amend must clear this too: rule 3 asks whether HEAD is yours to rewrite, rule 5 asks whether you are naming what you commit. A *bare* amend re-commits the whole index exactly as a bare fresh commit does; a scoped one does not — measured 2026-09-18, `git commit --amend --no-edit -- a.txt` kept everything already in HEAD, took the working-tree content of `a.txt`, and left a foreign staged `c.txt` uncommitted. Exempt: linked worktrees, `--allow-empty`, and any merge/cherry-pick/revert/rebase in progress, where git itself refuses a partial commit.
  - **Push time** — `git-push-develop.sh` audits every outbound commit's file set against `NX_PUSH_ALLOWED_PATHS` (space-separated prefixes or globs) and refuses with `PUSH_REFUSED_SCOPE` on a file outside it. The variable is required rather than optional, because an audit with no allowlist passes everything and a gate that skip-passes when its input is absent proves nothing. On a refusal with it unset the script prints the outbound file set and a pasteable line — always the literal files, never coarsened to directory prefixes, because a `docs/rdr/` allowlist would pass the very file the audit exists to catch and collapse both layers from one habit. A wildcard allowlist (`*`, `.`, `/`) is refused outright: it satisfies the letter of a required variable while auditing nothing, and unlike the named skip it leaves no trace saying so. `NX_PUSH_SKIP_SCOPE_AUDIT='<reason>'` is the one escape, and it prints its reason; there is no silent skip.

  Neither guard catches a commit whose named path carries ANOTHER session's uncommitted edit of that same file: `git commit -- <path>` commits the WORKING TREE version, so on a co-edited file it quietly takes their text under your message (measured 2026-09-18). A pathspec protects the paths you did not name; it guarantees nothing about one you did. Check `git diff --staged -- <paths>` before committing a file someone else is in.
- Use `bd` (beads, **≥ 1.0.0**: `brew install beads` or `brew upgrade beads`) for task tracking. Earlier 0.x versions reject the comma-separated `--status` flag the close-skill preamble uses; the bead advisory will silently report no open beads on stale installs.
- **`nx init` / `nx upgrade` install a machine-wide `bd prime` file** (nexus-cnzei.8) when `bd` or the beads Claude Code plugin is detected: a short, generic, conexus-managed `PRIME.md` at the OS user config directory `bd` itself falls back to when no repo-level `.beads/PRIME.md` exists (macOS `~/Library/Application Support/beads/PRIME.md`; Linux `$XDG_CONFIG_HOME/beads/PRIME.md` or `~/.config/beads/PRIME.md`; Windows `%AppData%/beads/PRIME.md`). This is separate from — and applies to every beads repo on the machine, not only — this repo's own tracked `.beads/PRIME.md`, which still wins when present. beads 1.2.x still appends its own `bd remember` memories after this file; installing it does not stop that. The installed marker records a hash of its body, so a hand edit under an intact marker is detected and never overwritten (same protection as an unmarked hand-authored file); `nx uninstall` removes the file only when that hash still matches. Opt out per-run with `--no-beads-prime`, or persistently with `nx config set beads_prime.manage false`; deleting the file restores `bd`'s own default. `nx doctor`'s "Beads PRIME.md (user-level)" row reports its state and the same undo text.
- **Code review**: Plans include review tasks after implementation phases. Use `/conexus:review-code` or dispatch `code-review-expert` at the designated plan steps.

Both `main` and `develop` carry branch protection. Read the live settings with
`gh api repos/Hellblazer/nexus/branches/develop/protection` (and `.../main/...`);
the settings page is https://github.com/Hellblazer/nexus/settings/branches. As of
2026-10-24 they are:

- **Required status checks** (the same three on both branches):
  - `pytest-gate` (it fans in over the sharded pytest matrix, on the lab pool or hosted runners, plus the
    lint and census legs; it replaced the two `pytest (Python 3.12)` /
    `pytest (Python 3.13)` checks at nexus-n0ful)
  - `service change detection`
  - `Java tests + jOOQ codegen drift guard`
- **Up to date before merging:** required on `develop`, not on `main`.
- **Pull request required:** not at the protection level, on either branch.
  `develop` takes direct pushes through `scripts/git-push-develop.sh`, and the
  PR-only rule for `main` is policy plus the push guard.
- **Force-pushes and deletions:** not allowed on either branch (the develop reset
  on 2026-05-21 was a one-time bypass via the API; routine resets are not permitted).
- **Administrators:** the rules are not enforced for them.

## License

AGPL-3.0-or-later, dual-licensed with a commercial option (see
[LICENSING.md](../LICENSING.md)). For Python source files, use the SPDX header:

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
```

Agent files, skill files, config files: no header needed — the LICENSE file covers them.

Contributions are accepted under the terms in
[LICENSING.md § Contributions](../LICENSING.md#contributions): contributed
code is AGPL-3.0-or-later and may also be included in commercially licensed
editions. This is what keeps the dual-license offer viable.

## Release Process

The `release` skill (`.claude/skills/release/SKILL.md`, also `/conexus:release`) is the executable checklist and the authority for a client release; `.claude/rules/releases-and-engine.md` has the short rules and the engine-service cut has its own skill (`engine-release`). This page keeps what those do not: the pointer to the plugin-only fast lane, the schema/data-migration rationale, break-glass, the file quick reference and the one-time setup. A step-by-step copy used to live here and drifted from the skill, so it was removed; do not rebuild one.

Three rules hold for every release:

- **The releaser is human.** AI prepares the release branch; the explicit "cut the release" and "tag and push" come from Sam.
- **Work in a release worktree off `origin/develop`**, never in the shared primary checkout, which keeps `develop` checked out (AGENTS.md worktree rules 2 to 4). The release branch is cut from `develop`, not `main`, because a release promotes `develop`'s state; `origin/main` is merged into the branch first so the release-only files resolve there and not in the PR.
- **Nothing lands on `main` except through the release PR** (merged with `--merge`, never `--squash`, so the release commit keeps its SHA). The tag points at the merge commit.

What the skill walks, in order: the engine-freshness gate (`scripts/check_engine_release_floor.py`, Step 0, blocking) and the preflight (0c); the release battery (`tests/e2e/release-battery.sh` and its legs, Step 1); the docs audit against changes since the last tag (2); the version bump across every manifest, the changelogs, the wire ledger, the privacy-policy date and `uv.lock` (3 to 5); the release branch and PR (7); the pre-push verification (8); tag and push (9), which fires `release.yml`; verification that the workflow, GitHub and PyPI all show the release (10, 11); the mandatory back-merge of `main` into `develop` (11b); the published-bytes journeys (11c); and the local reinstall (12). The checks that gate a tag read GitHub's check-run record and do not re-run the suite on a tree CI already tested.

### Plugin-only releases

Plugin-surface content (`conexus/`, `sn/`, `tool-spill/`, `.claude-plugin/marketplace.json`) that must ship while `develop` carries `src/` work goes out as a patch client release cut off the last client tag, with the plugin commits cherry-picked from `develop`. The procedure is the `release` skill's "Plugin-only fast lane" section; this page does not repeat it. The anchored `plugin-vX.Y.Z-n` channel of RDR-197 was retired on 2026-10-26 (nexus-rbmja.8).

### Schema/data-migration releases (conditional)

Trigger: this release's tag (client `vX.Y.Z` or engine `engine-service-vX.Y.Z`) carries a schema or data migration — a new Liquibase changeset, a new `upgrade_ladder` rung, or any change to a shape data already has to conform to. Four requirements, none of which the release checklist enforced before this section existed (T2 [22511] gap 9 — no schema-migration protocol existed in any release document; every prior trigger for "is this release safe" reduced to version identity and the standard functional gates, none of which speak to migration risk specifically). Operational checklist form: `.claude/skills/release/SKILL.md` Step 6d (client-side data migrations) and `.claude/skills/engine-release/SKILL.md` Step 5b (engine-side schema DDL). This section is their shared rationale and evidence citations.

1. **Populated-store upgrade rehearsal at a stated, representative scale.** The mechanism is `NEXUS_TARGET_RELEASE=X.Y.Z tests/e2e/migration-rehearsal/run.sh --package-upgrade` (published-bytes mode; see the release skill's Step 11c) run against a corpus seeded above a named floor, not the harness's default toy seed (10-30 documents in `rehearse_package_upgrade.sh` and `rehearse_acquire.sh`). State the floor and the actual seed count used in the release relay. If the seed cannot be brought to a genuinely representative scale before deploy, say so explicitly — do not let a toy-scale pass stand in for an at-scale one. RDR-191 is the standing evidence for why this matters: the cloud 385,484-row unify-chunks migration (T2 [22485]) is the only at-scale proof this project has produced for a chunk-table DDL change, and it ran in PRODUCTION — no pre-production rehearsal at that scale has ever happened. Treat that as a named, accepted gap until a representative-scale pre-production rehearsal exists, not a silently inherited one.

2. **A rollback decision point, settled before the deploy relay fires.** Determine explicitly whether the migration can be rolled back after it commits. Non-transactional DDL (`CREATE INDEX CONCURRENTLY`, any Liquibase changeset that cannot run inside a transaction) forfeits the free atomic rollback a transactional migration gets — RDR-191's `nexus-o8dil.22` names this exactly: "CIC, non-blocking, +11%, cannot run in a transaction, and therefore forfeits the free atomic rollback that the local path gets." When the answer is no, write **IRREVERSIBLE** in the relay verbatim, and attach its substitute: a written rollback/abort runbook (exact statements, abort criteria) that exists and is in the operator's hand before the window opens, not improvised mid-window.

3. **A freeze window derived from measurement, not a guess.** Generalizing the `nexus-o8dil.22` window pre-flight (T2 [22420] / [22427] / [22485]): (a) copy-peak — the largest transient storage footprint the migration needs mid-flight; (b) WAL budget — retained WAL under the deployment's replication settings during the window, checked against `max_wal_size`; (c) disk floor — abort unless available disk clears (steady-state floor + copy-peak) with a stated margin, re-measured immediately before executing (corpus growth between planning and execution shrinks the margin). Record the threshold and the abort condition in the operator runbook, not just the target duration.

4. **Post-deploy data-integrity verification beyond version identity.** A `/version` match proves the binary shipped, not that the data survived. Verify, per T2 [22485]'s pattern: exact row-count reconciliation pre vs. post (per dimension/table, not an aggregate), that `ANALYZE` actually fired, and that the standing functional gates (`tests/e2e/cloud-client-path-gate.sh`, run with `NX_EXPECTED_OWNERLESS_WRITE_MODE=<the live mode>` once the engine carries the RDR-223 ownerless-write refusal, because the plain form fails leg B3 on any engine that reports a mode; the deploy gate's parity/recall legs) are green post-migration. [22485]'s own verdict is the bar: "ROW INVARIANT EXACT: 385,484 pre == post ... ANALYZE fired ... STEP-6 green (112/113 parity, recall 12/12 pools identical), cloud client-path gate PASSED all four legs."

### Break-glass: retry, yank, revert, and tag retraction

Two prior pointers to this content — `.claude/skills/release/SKILL.md`'s "See also" list and AGENTS.md § Cutting a release, both saying "rollback / one-time setup steps live in `docs/contributing.md` § Release Process" — named nothing until this subsection existed (T2 [22511] Explore sweep, dangling-pointer finding). This is what they were pointing at.

**Publish failed after the tag already pushed.** Never move, delete, or force-push a published release tag (`vX.Y.Z` or `engine-service-vX.Y.Z`) to retry a failed publish — see "Tag retraction" below for why. Use the `release.yml` `workflow_dispatch` retry path instead: it checks out the ALREADY-PUSHED tag but runs the CURRENT workflow file from `main`, so a bug in the pipeline itself (not the artifact) can be fixed on `main` and re-run against the same immutable tag.
```bash
gh workflow run release.yml --ref main -f tag=vX.Y.Z
```
This is exactly the v7.7.0 sequence (2026-08-14, commit `62da4273b`): the first tag-push run failed because `GITHUB_REF_NAME` resolves to the triggering BRANCH on `workflow_dispatch`, not the tag, so the version-verify step compared `main` against `pyproject.toml`'s `7.7.0` and red'd on the retry path's first real exercise. The fix landed on `main` (`RELEASE_TAG` now resolves from the dispatch's `tag` input when present, mirroring the checkout step), and the SAME `v7.7.0` tag was re-dispatched without ever being moved. Before this path existed, `v7.6.0`'s publish failure (a `twine`/metadata-version mismatch, unrelated to the artifact — `gh-action-pypi-publish`'s bundled twine predated `hatchling`'s Metadata-Version 2.5) had no such recourse and the tag was moved twice to retry — see "Tag retraction" for why that was the wrong fix and is now forbidden.

**PyPI yank for a bad full release** (distinct from yanking a pre-release version such as an `rcN`, `alpha` or `beta` at https://pypi.org/manage/project/conexus/releases/ so `pip install conexus` does not resolve it). PyPI yanking marks a release as yanked; it does not delete it. Effect: `pip install conexus` (unpinned, or a range) stops resolving to the yanked version; an exact pin (`conexus==X.Y.Z`) still installs it, with a warning. It does **not** un-install it from any box that already ran `pip install`/`uv tool install` before the yank, and it does **not** touch `.claude-plugin/marketplace.json`'s `source.ref` on `main` — if the bad release's plugin-side commit already landed and got tagged, `source.ref` still points new `/plugin install` users at that content until a NEW release supersedes it. To yank: https://pypi.org/manage/project/conexus/release/X.Y.Z/ → "Yank release", with a reason. A yank is a signal, not a recall; the actual fix is always a new release.

**Engine-deploy revert.** The engine-service cloud deployment is conexus-managed infrastructure this repo does not drive directly (`docs/agents/t1-sub-agent-contract.md`'s "Hal relays the cross-instance bus" convention applies here too). A revert is **relay-mediated**, with real limits:
- There is no button here to press; surface an explicit relay naming the target tag to redeploy (normally the previous `engine-service-v*` identity), never frame it as autonomous.
- For a migration-carrying tag, redeploying an older BINARY does not undo an already-committed schema change — the schema and the binary are two different things to revert, and a `CREATE INDEX CONCURRENTLY`-style migration (see "Schema/data-migration releases" above) has no free rollback once committed. "Revert the engine" without "revert the schema" can leave an old binary talking to a new schema shape.
- Local-mode installs are unaffected by any cloud revert — they pull whatever `REQUIRED_ENGINE_VERSION` (`src/nexus/engine_version.py`) pins on the client side. Walking local installs back to a prior engine identity requires a NEW client release that moves the pin backward, not a cloud-side action.
- There is no automated post-revert verification today (T2 [22511] gap 2): confirm the revert landed with `nx service probe` (reads `/version` live) rather than any recorded value. The `deployed-engine-version` T2 tracker and `nx service record-deploy` were deleted (cleanup step 10b); `/version` is the only source of truth.

**Tag-retraction policy.** Moving, deleting, or force-pushing a published `vX.Y.Z` or `engine-service-vX.Y.Z` tag is **forbidden** except as an explicit, admin-only (Hal) decision — never a default remedy for a failed publish. Why: `.claude-plugin/marketplace.json`'s `source.ref` pins installed Claude Code plugin users to a specific, named tag as an IMMUTABLE identity; moving what a tag name resolves to after the fact is a supply-chain integrity violation, not a convenience — a user (or CI cache) that already resolved the old tag can silently diverge from one that resolves it after the move, with no way to tell from the tag name alone that this happened. `v7.6.0` is the standing lesson (2026-08-11): before the `workflow_dispatch` retry path existed, a publish failure (the twine/metadata mismatch above) was "fixed" by moving the tag, twice — `.github/workflows/release.yml`'s own header comment records it verbatim ("7.6.0 moved twice for exactly that reason") as the incident that justified building the retry path in the first place. If a tag genuinely must be retracted (e.g. a published artifact is actively harmful), it requires Hal's explicit authorization and: (a) a NEW tag carries the fix — never a reused or re-pointed old identity; (b) the bad PyPI release is yanked per the procedure above; (c) if `main`'s `source.ref` already points at the bad tag, a new release supersedes it — retracting the old tag alone does not move already-tagged users off it.

### Quick reference — files that change every release

| File | What to update |
|------|----------------|
| `pyproject.toml` | `version` field |
| `mcpb/pyproject.toml` | `version` field |
| `mcpb/manifest.json` | `version` field |
| `uv.lock` | auto-updated by `uv sync` — **must be committed** |
| `CHANGELOG.md` | move Unreleased → `[X.Y.Z]`, add empty Unreleased |
| `conexus/CHANGELOG.md` | add `[X.Y.Z]` entry |
| `.claude-plugin/marketplace.json` | bump `plugins[].version` for every plugin listed (conexus, sn, conexus-tool-spill) **and every `plugins[].source.ref` to `vX.Y.Z`** (parity-tested) |
| `conexus/.claude-plugin/plugin.json` | bump `"version"` to match — **controls nx cache refresh** |
| `sn/.claude-plugin/plugin.json` | bump `"version"` to match — **controls sn cache refresh** |
| `tool-spill/.claude-plugin/plugin.json` | bump `"version"` to match — **controls conexus-tool-spill cache refresh** |
| `docs/cli-reference.md` | new/changed CLI flags and subcommands |
| `docs/architecture.md` | new/changed modules |
| `docs/repo-indexing.md` | indexing pipeline changes |
| `docs/configuration.md` | new config keys or tuning parameters |
| `docs/storage-tiers.md` | new storage capabilities |
| `README.md` | high-level feature descriptions |
| `src/nexus/upgrade_ladder/` | new DATA-convergence axes land as rungs registered in `registry.py` (the client-side T2 migration chain is deleted — RDR-158 P4 Stage 4; schema is Liquibase in the engine) |
| `conexus/PENDING_RELEASE.md` | empty the pending-drift list for every entry this release ships — advancing `source.ref` is what makes the declared plugin changes live; a stale entry fails `tests/test_plugin_release_drift_ledger.py` |

### Pre-push release checklist

Before pushing the version-bump commit, verify:

```bash
git diff --name-only HEAD          # uv.lock must appear here
nx --version                       # must print the new X.Y.Z
grep "^version" pyproject.toml    # must match the tag you'll push
```

If `uv.lock` is not in the diff, you forgot to run `uv sync` or forgot to stage it.
**Do not push the tag until `uv.lock` is committed.**

### One-time Release Infrastructure Setup

Two things to configure before the first automated release:

#### 1. GitHub `pypi-release` Environment

The release workflow uses a GitHub Actions environment named `pypi-release` to gate PyPI publishing. Create it at https://github.com/Hellblazer/nexus/settings/environments:

1. Click "New environment"
2. Name: `pypi-release`
3. Optionally add required reviewers (manual approval gate before publish)
4. Save

#### 2. PyPI Trusted Publisher

Configure PyPI to accept OIDC tokens from the `pypi-release` environment:

1. Go to https://pypi.org/manage/project/conexus/settings/publishing/
2. Click "Add a new publisher"
3. Fill in:
   - **Owner**: `Hellblazer`
   - **Repository**: `nexus`
   - **Workflow filename**: `release.yml`
   - **Environment name**: `pypi-release`
4. Click "Add"

The environment name in PyPI must match exactly — `pypi-release` — or OIDC authentication will fail. This eliminates the need for a `PYPI_API_TOKEN` secret.
