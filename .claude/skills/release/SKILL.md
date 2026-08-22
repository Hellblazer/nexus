---
name: release
description: Use when cutting a release, bumping version, tagging, or publishing to PyPI. Enforces the full release checklist from AGENTS.md § Cutting a release. Also surfaces as /conexus:release.
---

# Release Checklist

Follow every step in order. Do not skip or reorder. Authority: AGENTS.md § Cutting a release (`docs/contributing.md#release-process` is the long form; T2 [22511] gap 11 — this line previously cited a "CLAUDE.md § Release Process" heading that does not exist in CLAUDE.md/AGENTS.md, only in `docs/contributing.md`).

## Steps

### 0. Engine-freshness gate (PREREQUISITE — the two-lifecycle check)

The Java **engine-service** is a SEPARATE release artifact from this PyPI release: its own `engine-service-vX.Y.Z` tag fires `engine-service-release.yml`, version is tag-stamped (no manifest bump), and it is **decoupled from the luxe6 / RDR-155-P4a develop release boundary**. This PyPI release pins ONE engine IDENTITY, `REQUIRED_ENGINE_VERSION` (`src/nexus/engine_version.py`) — the engine the release was built and gated with, installed on EVERY path (fresh init AND upgrade). It is NOT a compatibility minimum and NOT a range (Hal directive 2026-07-15). `PINNED_SERVICE_TAG` (`src/nexus/daemon/binary_install.py`, the exact tag a fresh local `nx init --service` install downloads) is DERIVED from it, not an independently hand-typed literal — there is no floor/exact split to reason about, bumping the one constant moves both together, by construction.

**This is a BLOCKING command, not a prose eyeball-check** (nexus-i5c2u — the prior prose-only version of this step was routinely skipped, letting the cloud engine sit at v0.1.17 for 9+ days across releases while the floor moved to v0.1.34; the pin then independently drifted the identical way in 2026-07-12, sitting at v0.1.36 two tags behind a verified, cloud-deployed fix — which is why the pin is no longer a second hand-typed constant at all):

```bash
uv run python scripts/check_engine_release_floor.py
```

If it exits non-zero, STOP — do not proceed with the PyPI release. The gate fails in TWO directions and the remedy differs:

- **cloud BEHIND the pinned identity** → EITHER conexus has not deployed it yet (surface the deploy relay and wait), OR this is a **PAIRED release** (Hal directive 2026-08-02: the floor bump rides the SAME release as the engine's deploy, and the deploy relay fires at client-tag push, parallel with the PyPI publish — see AGENTS.md § Engine-service release). For a paired release, cloud-behind is the EXPECTED pre-tag state — mechanized via `--paired-deploy` (nexus-k1c08):
  ```bash
  uv run python scripts/check_engine_release_floor.py --paired-deploy engine-service-vX.Y.Z
  ```
  Name the EXACT tag this release pairs with — never a guess. The flag accepts a below-floor cloud solely where that tag independently verifies as (a) a published, non-draft GH release with assets (`gh release view` — a missing/failing `gh` fails the gate, it does not pass it), (b) exactly equal to `REQUIRED_ENGINE_VERSION`, and (c) the newest published `engine-service-v*` tag. Any single miss stays red with a named reason — this is a stricter, mechanized check, not a looser one. On acceptance it prints an explicit "PAIRED MODE" acknowledgment naming both versions and the POST-TAG VERIFY obligation. Do that verify: once the deploy lands, re-run the SAME command WITHOUT `--paired-deploy` — a still-behind cloud at that point is real drift, escalate loudly, do not re-arm the flag to silence it.
- **a gated engine tag was never pinned** (`REQUIRED_ENGINE_VERSION` behind the newest published tag) → bump `REQUIRED_ENGINE_VERSION` to that tag (this alone also moves `PINNED_SERVICE_TAG`). This is the local-install delivery failure: cloud users get the deployed engine regardless, local-mode users get ONLY what this constant names, so an unpinned tag reaches nobody. When the unpinned tag carries engine halves of features whose CLIENT halves ship in THIS release, the bump into this release is mandatory, not optional — floor-lag ships a client whose pinned engine lacks the engine halves of its own features (the 7.1.0/v0.1.62 inversion).

Re-run (without `--paired-deploy`) until it exits 0 — including the post-tag verify for a paired release.

`release.yml`'s own copy of this gate runs `--paired-deploy-auto` (nexus-gc9ir) instead of bare — it derives the candidate tag from `REQUIRED_ENGINE_VERSION` itself and only engages the paired-acceptance path when the cloud is actually below floor, so the workflow no longer red's during a paired release's expected parallel-deploy window. This pre-tag human invocation still uses the explicit `--paired-deploy <tag>` form above — name the tag deliberately here, where you already know it.

**The core tradeoff:** paired acceptance (either flag) proves the engine TAG is legitimate — it does not prove the deploy has actually landed. That's an accepted, bounded gap: the daily `engine-floor-verify` scheduled job (`.github/workflows/scheduled-failure-watch.yml`) re-probes the real endpoint bare and surfaces a still-stale cloud within ≤24h via the tracked "Scheduled workflows are failing silently" GH issue. See `check_engine_release_floor.py`'s module docstring for the full statement.

**Known CI-side failure mode, no bypass by design:** the workflow's `--paired-deploy-auto` invocation has no `--ack-client-lag` flag (no human present at tag-push to name a bead), so an unacknowledged `docs/wire-contract-pending.md` `## Unshipped` entry fails that step CLOSED during a paired release, before the tag/cloud checks even run — correctly, do not ask for a CI-side bypass. **Re-running the SAME tag via `workflow_dispatch` does NOT fix this** (nexus-55r6o) — the checkout pins the tag's immutable tree, and the ledger check reads the ledger off that same frozen tree, so a same-tag retry re-reads the identical unacknowledged entry and fails identically. Real remedy: (a) cut a FRESH client tag whose tree carries the ledger fix (a new release, not a retry of the failed one), or (b) implemented (nexus-55r6o): ci.yml's `release-ledger-gate` job runs the identical ledger-only check (`check_engine_release_floor.py --ledger-only`) on EVERY PR targeting main, before any tag exists — not narrowed to `release/*`-named branches, so a hand-named branch promoting to main is covered too. **This is mechanically MERGE-BLOCKING, not advisory:** the job is wired into `pytest-gate`'s `needs:`, and `pytest-gate` is main's actual required branch-protection check — a release PR with an unacknowledged entry fails CI on Step 7 and cannot merge, no separate GitHub repo-settings change needed. Because the ledger this job reads is repo-GLOBAL and the CI invocation carries no `--ack-client-lag` path, this is deliberately broad friction: a single unacknowledged `## Unshipped` entry blocks EVERY PR to main, not just the eventual release PR, until it is acknowledged (client half shipped) or a human edits the ledger — intended, matching the ledger's own philosophy of surfacing an unshipped both-halves commit as a standing risk, not a tag-time surprise.

Supplementary context (useful when deciding whether recent `service/` work is cloud-relevant, but the script above is the actual gate):

```bash
git tag -l "engine-service-v*" | sort -V | tail -1          # last engine tag
git log --oneline <last-engine-tag>..HEAD -- service/        # cloud-relevant drift?
```

1. Confirm the pinned engine tag is (a) cloud-DEPLOYED and (b) cloud-GATED (recall + hybrid parity, xr7.8.9-style) — read the authoritative bead + conexus bus, **not memory** (cross-repo gate state goes stale fast: 2026-06-26 a `luxe6` condition had been cleared a week earlier than memory implied).
2. If `service/` has drifted with cloud-relevant changes (pooler/RLS, pgvector, catalog conformance, aspect queue, batch endpoints), cut a fresh engine FIRST — see **AGENTS.md § Engine-service release** — bump `REQUIRED_ENGINE_VERSION` to it IN THIS release (this alone also moves `PINNED_SERVICE_TAG` — nothing else to bump), gate the release battery against that engine, and arm the deploy relay to fire at client-tag push, parallel with the PyPI publish (paired-release choreography; passive bus: the relay itself goes through Hal, never autonomous). The engine cut is NOT luxe6-gated, so refreshing it never blocks on the develop boundary, and it is never blocked by client-release preconditions either — those gate the DEPLOY, and pairing satisfies them the instant the client tag exists.
3. The engine cut itself: full `service/` suite green on the tagged commit (confirm the `service/` tree equals a green-`service-ci` commit — the Java CI is advisory and does not block auto-merge, so verify), then the **human** pushes `engine-service-vX.Y.Z`.

This gate exists because the engine silently drifted 22 `service/` commits / 4 days behind the cloud (2026-06-26); the PyPI checklist had no step that would have caught it.

### 0b. Remediation-commit gate (open beads whose fix must ride this release)

nexus-fix9t: nexus-3n7pr's remediation was sequenced "after the client release ships" — 7.7.0 shipped, but the commit its plan depended on (5f59ede70, nexus-gvmbo / nexus-b91tv) was NOT an ancestor of v7.7.0, so the installed `nx` at 7.7.0 carried the pre-fix DESTRUCTIVE `manifest_backfill` module the plan assumed was already safe. Nothing checked this at release time.

```bash
uv run python scripts/check_remediation_commits_ride_release.py --release-ref vX.Y.Z
```

Run it against the **release tag** (or the branch tip about to be tagged). Non-zero exit = **STOP** — a red line names the bead and the missing commit; the remedy is one of:

- re-sequence the named bead to run after a release that DOES carry the commit, or
- include the commit in this release before cutting it.

**Bead-authoring convention this gate reads**: when sequencing a remediation bead behind a specific commit ("do not run this until commit X has shipped"), write a line anywhere in the bead's description or a comment:

```
requires-commit: <sha>
```

one sha per line (7-40 hex chars). This is the structured form the gate scans for first. It also nets two free-text phrasings ("requires commit `<sha>`", "must include `<sha>`") for beads written before this convention existed, but the marker is the reliable form — prefer it. Closed beads are never scanned.

**Pre-tag snapshot for the CI replay (nexus-fehi3, MANDATORY, do BEFORE Step 7's commit).** This repo's `bd` backend is Dolt with no credentials on the CI runner, so `release.yml` cannot run `bd export` live — it replays this gate against a cut-time snapshot instead. Write it now:

```bash
uv run python scripts/check_remediation_commits_ride_release.py --write-snapshot .release-gates/remediation-snapshot.json
```

Stage `.release-gates/remediation-snapshot.json` into Step 7's release-branch commit (alongside the seven version-bump manifests). `release.yml` reruns the gate against that exact committed file at tag-publish time via `--verify-snapshot`, which fails the release closed if the file is missing (the pre-tag step didn't run), present but not committed on the tagged ref, or stale (its newest bead `updated_at` predates the commit immediately preceding this release). A stale/missing snapshot from a prior release does NOT carry forward — write a fresh one every release.

### 0c. PREFLIGHT — run the cheap blockers FIRST, all of them (32s)

```bash
./tests/e2e/release-preflight.sh
```

Run this BEFORE step 1 and before any expensive leg. It evaluates every
seconds-scale, deterministic, release-BLOCKING check in one pass and does NOT
abort on the first red -- it reports every failure it finds, so one cycle
surfaces the whole fix list.

WHY (the 7.15.0 cut, 2026-08-22). The battery was ordered expensive-first and
abort-on-first-red. Two blockers that day were each sub-second assertions:
`REQUIRED_CHECK_CONTEXTS` drift against main's live branch protection (found
13.5 minutes into `local-service-gate.sh`, at the very end of its run) and the
`--package-upgrade` `PREV_ENGINE_TAG` staleness guard (a guard clause in the
first seconds of `run.sh`, but only reached after ~20 minutes of smoke +
shakeout + LSG + fresh-install-mvv). Each was a one-line fix behind an hour of
waiting, and each masked the other. Sequential abort-on-red turns N cheap
blockers into N hours.

Exit codes: `0` PREFLIGHT PASSED, `1` PREFLIGHT FAILED (fix everything listed,
then re-run -- it is 32 seconds, so re-running costs nothing), `2` PREFLIGHT
UNVERIFIED (a dependency such as Docker was absent; "could not check" is never
"fine").

Covered: engine-release-floor, wire-contract ledger (`--ledger-only`, which is
MERGE-BLOCKING on every PR to main, not just at tag time), remediation-commits,
the remediation-snapshot replay against the base a merge commit really has
(`--release-base-ref origin/main`, NOT the local `HEAD^`), the seven version
surfaces + both `source.ref` fields + the emptied ledger, the ci-evidence
required-context drift tests run with `-m ""` so the integration-marked live
check actually executes, the `--package-upgrade` staleness predicate evaluated
without provisioning anything, and Docker availability.

Do not let this file grow into a second battery. Admission requires all three:
seconds-scale, deterministic (no Docker/service/sandbox/network install), and
genuinely release-blocking. Its whole value is that it finishes before you look
away.

### 1. Run unit + integration suite

```bash
uv run pytest                        # unit suite (no API keys)
tests/e2e/local-service-gate.sh      # integration incl. the local-service functional gate
tests/e2e/migration-rehearsal/run.sh --package-upgrade   # ONE-engine convergence MVV (nexus-cfgo9)
tests/e2e/fresh-install-mvv.sh       # VIRGIN-journey gate (nexus-nolqs) — see below
```

All must pass. Integration is excluded from CI and is the last line of defense before tag-push.

**`fresh-install-mvv.sh` — the virgin-journey gate (nexus-nolqs, 2026-07-21).**
Every other E2E gate starts from a POPULATED install and tests the upgrade
axis; the unit suite pins the SQLite opt-out backend — which is how the
f1itv/e9ru2/kmo9h/r5f3c/9xfx5 fresh-box defect class shipped through the full
release process unseen. This gate builds the wheel under test, then on a
scrubbed-env virgin HOME: local init (engine sha256+sig-verified, portable PG,
bge-768), ladder converged at init, store put + index md with ENGINE-CATALOG
registration asserted, semantic search returns both sentinels, doctor with
zero ✗ / zero ⚠ / warnings checked against the script's allowlist. Must end
`FRESH-INSTALL MVV PASSED — ... (LOCAL WHEEL, release-battery layer)`.
`FRESH_MVV_CACHE=/tmp/fresh-mvv-cache` reuses the 416MB model download across
runs. Every new fresh-box warning is a decision: fix it or allowlist it in
the script WITH a rationale + bead reference.

This step's plain invocation is the LOCAL WHEEL layer only (dependencies
resolve from this checkout's `uv.lock`/wheel metadata). It cannot reproduce a
defect living in dependency RESOLUTION at a fresh `uv tool install` — that
was nexus-l2ku5 (`mcp>=1.0` unbounded resolved `mcp` 2.0.0 straight off PyPI,
killing both MCP servers for 4 days while every gate ran pinned to the dev
venv). `tests/e2e/fresh-install-mvv.sh --published [X.Y.Z]` (nexus-796zn)
installs the real PyPI artifact via `uv tool install conexus[==X.Y.Z]` in the
identical scrubbed sandbox and belongs to the POST-publish shakedown, not
this pre-tag battery (nothing is on PyPI yet at this point in the checklist)
— see T2 `nexus/shakedown-playbook` §2 S1.

**`--package-upgrade` — the fix-delivery gate (GH #1402, nexus-cfgo9).** Proves
what 6.10.0 shipped without: that an EXISTING install upgrading the package
actually receives this release's engine. It provisions a real previous-release
box (PyPI + its own cold-acquired engine), upgrades ONLY the conexus package to
the working tree, and asserts — with the harness forbidden from supplying or
touching any engine binary (sha256-verified) — that the product converges the
engine to `REQUIRED_ENGINE_VERSION`, the service boots, the chash probe answers
via the view path, and T1 survives the cycle. Must end
`PACKAGE-UPGRADE CONVERGENCE MVV PASSED`. If `run.sh` aborts with the
PREV_ENGINE_TAG staleness FATAL, bump `NEXUS_PREV_RELEASE`/`NEXUS_PREV_ENGINE_TAG`
in `run.sh` to the release immediately before this one — the scenario must
always start from a genuinely older engine.

**Local-mode functional gate — two separate proofs, self-provisioning**
(2026-07-06 v6.3.6 lesson; nexus-edwlp 2026-07-07; re-scoped honestly by
nexus-x81ks, 2026-08-02): `tests/e2e/local-service-gate.sh` self-provisions a
throwaway PG + service (scratch NEXUS_CONFIG_DIR, isolated from
~/.config/nexus and prod) and auto-rebuilds a stale dev jar, but the pytest
family it runs does **not** exercise that throwaway service — autouse conftest
fixtures (`_isolate_service_endpoint_env`, `_isolate_config_dir`,
`_pin_t2_substrate`) strip `NX_SERVICE_*` from every test body and route tests
at self-provisioned substrates the suite manages itself; `NX_SERVICE_HOST`/
`PORT` is harmless legacy env, not a pin. The two proofs are kept separate:
- The pytest FLOOR/BUDGET family is the functional surface of local mode,
  proven against those self-provisioned substrates. Historically it only ran
  when a dev-box service HAPPENED to be alive against a lived-in install — an
  ambient, irreproducible gate that silently degraded to 74/516 tests the day
  the ambient service died. A vacuity guard now asserts pinned passed/skipped
  FLOOR/BUDGET at the end of the run.
- The **direct smoke leg** (script-driven, outside pytest, immune to the
  autouse isolation) is what proves the shipped-shape throwaway service
  itself boots and serves: health, version identity, catalog round-trip, the
  RUNFENCE index-run fence, one vector round trip — exact-count non-vacuity
  (`SMOKE_EXPECTED`), fail-loud on any mismatch or unreachable service.

The gate is **bge-768-only** since nexus-w6h2m (2026-07-28): a local service
embeds with bge-768 and nothing else, so `cloud_mode` tests are deselected and
the gate no longer needs a `VOYAGE_API_KEY` for its own corpus. Two markers
carve tests out, each with an exact-count guard: `lived_in` (excludes tests
that dispatch real `claude -p` or need seeded lived-in corpora) and
`cloud_mode`. A guard trip or any new hard failure is real signal — compensate
with live validation of the release's changed paths (the v6.3.5/v6.3.6
pattern: exercise the advertised claims against the real deployment).

If unit-suite Py3.13 surfaces a known nexus-9eaz-family flake (`test_migration_guard_*`, `test_concurrent_apply_pending_*`, `test_concurrent_bootstrap`, `test_concurrent_t2database_construction`, `test_stop_claiming_on_running_worker_causes_exit`): these are marked with `@_skip_on_gha_flake` on main, so they shouldn't fire here. If they DO fire locally, that's signal: investigate before proceeding.

### 2. Audit docs against changes since last tag

```bash
git log --oneline v<prev>..HEAD
```

Cross-walk against:

- `docs/cli-reference.md` (CLI flags added / changed / removed)
- `docs/architecture.md` (module map, post-store hook contracts, T2 schema)
- `README.md` (user-visible drift)

Update any drift before bumping version. Doc audit is what catches "we changed the wire format but forgot to document it."

### 3. Bump version in ALL SEVEN bump targets

CI enforces parity. Missing any one of these fails the marketplace-version-matches-pyproject test, the marketplace-source-ref-matches-pyproject test, or the mcpb-manifest-version-matches-pyproject test.

- `pyproject.toml`: `version = "X.Y.Z"` (canonical source of truth)
- `mcpb/pyproject.toml`: `version` **and** the `conexus[local]>=X.Y.Z` dependency pin (the `[local]` extra is required — without it the .mcpb's venv resolves without `fastembed` and `LocalEmbeddingFunction` silently falls back to the 384-dim ONNX MiniLM against 768/1024-dim collections; `tests/test_plugin_structure.py::test_mcpb_pins_conexus_local_extra` enforces the pin tracks the version. T2 [22511] gap 11 — this line previously said `conexus>=X.Y.Z`, missing the extra.)
- `mcpb/manifest.json`: `version`
- `.claude-plugin/marketplace.json`: **both `version` fields** (one for conexus, one for sn)
- `.claude-plugin/marketplace.json`: **both `plugins[].source.ref` fields** — must be `"vX.Y.Z"` (the tag form). Easy to forget. This is what decouples installed users from main HEAD: plugin installs follow the pinned tag, not whatever main currently is. **CRITICAL: nexus-mkj6u 2026-05-23**
- `conexus/PENDING_RELEASE.md`: **empty the pending list.** Advancing `source.ref` is exactly what makes those plugin changes live, so the ledger's entries stop being pending at this step. `tests/test_plugin_release_drift_ledger.py` FAILS on a stale entry, so a forgotten clear blocks the release rather than rotting silently. The list you are deleting is also the honest "what becomes active in this release" note for the CHANGELOG (nexus-mk3tw / the 2026-07-25 inert-guard incident: three guards were merged, closed as "mechanized", and protecting nothing because the pin had not moved).
- `conexus/.claude-plugin/plugin.json`: `version`
- `sn/.claude-plugin/plugin.json`: `version`

Optional but recommended: also bump `plugins[].source.sha` to the 40-char SHA of the release commit, for protection against tag force-push. Add post-commit (Step 8a, see below).

**Engine-service pin (conditional 8th target — nexus-3rq00).** The Python/Java boundary rides one more hand-edited constant that sits OUTSIDE the seven-manifest parity gate: `PINNED_SERVICE_TAG` in `src/nexus/daemon/binary_install.py`, the `engine-service-vX.Y.Z` release this build auto-installs. It is DERIVED from `REQUIRED_ENGINE_VERSION`, so it is never hand-edited: moving the engine identity moves the pin by construction. Two invariants the `TestEnginePinParity` test enforces: (1) `PINNED_SERVICE_TAG`'s numeric version must be `>= REQUIRED_ENGINE_VERSION` (`src/nexus/engine_version.py`) — never ship a client that auto-installs an engine it then refuses as too old; (2) at the 6.0 release boundary the pin must be non-None (it is intentionally `None` pre-6.0). A release that bumps pyproject to 6.x without setting a real pin trips CI.

Semver: MAJOR for breaking, MINOR for new features, PATCH for bug fixes.

### 4. Update both changelogs

- `CHANGELOG.md` (root): move `## [Unreleased]` content into a new `## [X.Y.Z] - YYYY-MM-DD` section. Leave a fresh empty `## [Unreleased]` at the top.
- `conexus/CHANGELOG.md` (plugin changelog): always update, even if no plugin changes (note: "Plugin version aligned with conexus X.Y.Z. No plugin-side changes." is acceptable).

### 5. Refresh `uv.lock`

```bash
uv sync
```

The lock file MUST be committed. CI also checks this.

### 6. Run sandbox smoke (~2 min)

Required for any change touching `pyproject.toml`, `uv.lock`, `src/nexus/mcp/**`, `conexus/**`, `.claude-plugin/**`, `src/nexus/commands/{doctor,upgrade}.py`.

```bash
./tests/e2e/release-sandbox.sh smoke
```

Must end with `[done]` and confirm the new schema version. Halt on any failure.

This reinstall is genuinely isolated (fixed 2026-07-01, `137d2688`) — safe to run with live Claude Code sessions/MCP servers active, no `--force`/`--cycle-daemons` needed. If it ever refuses with a live-holder error again, suspect a step-ordering regression in `release-sandbox.sh` (sandbox `HOME` must activate *before* the reinstall, since `uv tool install` resolves its install location off `$HOME`) before reaching for `--force`.

### 6b. Run upgrade-shakeout (~3-5 min, conditional)

Required when the release touches the **upgrade path** an installed user traverses: hook stanzas (`src/nexus/commands/hooks.py`), the `nx doctor` drift checks, plugin name / marketplace.json `source.ref` pinning, or any migration touchpoint. `release-sandbox.sh smoke` tests one version in isolation; this tests `FROM_VERSION` to this branch.

```bash
./tests/e2e/upgrade-shakeout.sh run                       # latest stable -> this branch (clean-upgrade path)
./tests/e2e/upgrade-shakeout.sh run --from-version 4.34.6 # pre-pgrep-guard baseline -> exercises drift -> reconcile
```

Runnable from any baseline (nexus-a3nqp): it detects stanza drift at runtime and cross-checks `nx doctor`'s drift claim against the actual stanza byte-diff, so a doctor false-positive/negative fails the run. Must end with `12/12 PASS`. `./tests/e2e/upgrade-shakeout.sh reset` cleans the sandbox.

### 6c. Run sandbox shakedown (~5-10 min warm / +10-15 min cold)

Required on every release (nexus-6xkdu: a diff-based trigger list was rejected — MinerU/docling version drift lands via `uv.lock` alone with no matching pyproject.toml pin to diff, so any trigger list is under-inclusive by construction; see nexus-7g40u).

```bash
./tests/e2e/release-sandbox.sh shakedown
```

Smoke (step 6) never calls `nx index pdf`; this is the only pre-tag gate that exercises MinerU end-to-end through the production indexing path (step 3b of 11, the `bft-to-smr.pdf` formula fixture) — the slow-marked `test_mineru_path_preserves_formulas` pytest test runs in no default or scheduled suite (nexus-6xkdu). Must end `SHAKEDOWN PASSED`; a `SHAKEDOWN FAILED` verdict or non-zero exit halts the release. All four indexing steps (2, 3a, 3b, 4) can now fail the run — the `|| true` that previously made them unable to redden the run was removed at nexus-6xkdu.

### 6d. Migration-release branch (CONDITIONAL — this release ships a data migration)

Trigger: this release carries a schema or data migration — a new `upgrade_ladder` rung (`src/nexus/upgrade_ladder/registry.py`), or any client-side change to a shape data already has to conform to (T2 [22511] gap 9). Skip this step entirely when the release carries no such change.

1. **Representative-scale rehearsal.** Run the populated-store upgrade rehearsal against a corpus seeded ABOVE a stated floor, not the harness's default toy seed (10-30 docs across `rehearse_cold.sh` / `rehearse_acquire.sh` / `rehearse_shakeout.sh` / `rehearse_hole_punch.sh`). Pre-tag, that is the worktree `--package-upgrade` run in Step 1; post-publish, close the loop with Step 11c's published-bytes run:
   ```bash
   NEXUS_TARGET_RELEASE=X.Y.Z tests/e2e/migration-rehearsal/run.sh --package-upgrade
   ```
   Name the floor and the actual seed count used in the release relay. RDR-191's cloud 385,484-row unify-chunks migration (T2 [22485]) remains the only at-scale proof this project has produced for a comparable change — and it ran in PRODUCTION. If the rehearsal cannot be brought to a genuinely representative scale before tagging, say so explicitly rather than letting a toy-scale pass stand in for one.

2. **Rollback decision point, settled before the release branch is committed.** Determine whether this migration can be rolled back on an already-upgraded install. When the answer is no, write **IRREVERSIBLE** in the release notes / relay verbatim, and have the substitute in hand: a written rollback/abort runbook with exact statements and named abort criteria — the RDR-191 `nexus-o8dil.22` pattern.

3. **Freeze-window derivation.** Only applies when this migration is coupled to a server-side schema change (an engine tag deploying alongside this release). If so, do not re-derive the window math here — it lives in `.claude/skills/engine-release/SKILL.md` Step 5b; confirm that step ran and its threshold/abort condition are recorded before this release's tag pushes.

4. **Post-deploy data-integrity verification, beyond version identity.** Once Step 11c's published-bytes rehearsal lands, verify data integrity beyond a version match: exact row-count reconciliation, not just `/version` naming X.Y.Z — the T2 [22485] pattern ("ROW INVARIANT EXACT: 385,484 pre == post ... ANALYZE fired").

Full rationale and evidence citations: `docs/contributing.md` § Schema/data-migration releases.

### 7. Commit on a release branch + PR to main (nexus-mkj6u: replaces direct-to-main)

Per the marketplace-pinned-source playbook (also used by `Hellblazer/palinex`), release commits go through a PR. CI gates the bump before it lands on main. No more direct-to-main exception.

```bash
# Base the release branch on DEVELOP, not main: a release PROMOTES develop's
# accumulated state to main (CLAUDE.md: "releases promote develop to main via
# merge"). Branching off main would omit develop's unmerged fixes, so the
# release PR must carry develop's commits + the version-bump commit.
git checkout develop && git pull
git checkout -b release/vX.Y.Z

# PRE-MERGE MAIN FIRST (added 2026-07-04, learned on v6.3.1): a release branch
# based on develop ALWAYS conflicts with main's release-only files (all seven
# version manifests, both changelogs, the engine pin, uv.lock) because release
# bumps land on main and never merge back to develop. GitHub cannot build the
# PR merge ref while CONFLICTING, so PR checks silently never run ("no checks
# reported") — the conflict must be resolved BEFORE the bumps, or you resolve
# it under pressure post-PR. Resolve by construction:
git fetch origin main
git merge origin/main   # resolve: changelogs = union (fold main's released
                        # sections in, verbatim — verify with a diff of the
                        # section against origin/main, not by eye; a truncated
                        # fold is silent history loss); everything else will be
                        # re-bumped in Step 3 anyway. Then run `uv sync`.
# Also expect develop's manifests/engine-pin to be OLDER than the last release
# (they were never bumped on develop) — Step 3 bumps from whatever is present,
# so bump by pattern, not by exact-previous-version string match.

# Stage ALL SEVEN bump targets from Step 3, plus uv.lock and both changelogs,
# plus Step 0b's pre-tag snapshot and the cleared PENDING_RELEASE.md ledger.
# mcpb/pyproject.toml + mcpb/manifest.json are the easy-to-miss pair here and
# their omission fails CI's mcpb-manifest-version parity check; omitting
# .release-gates/remediation-snapshot.json fails release.yml's
# --verify-snapshot step outright (missing snapshot); omitting
# conexus/PENDING_RELEASE.md leaves a stale entry that fails
# tests/test_plugin_release_drift_ledger.py.
git add pyproject.toml uv.lock CHANGELOG.md conexus/CHANGELOG.md \
        mcpb/pyproject.toml mcpb/manifest.json \
        .claude-plugin/marketplace.json \
        conexus/.claude-plugin/plugin.json \
        sn/.claude-plugin/plugin.json \
        conexus/PENDING_RELEASE.md \
        .release-gates/remediation-snapshot.json
git commit -m "chore(release): conexus X.Y.Z"

git push -u origin release/vX.Y.Z
gh pr create --base main --title "release: conexus X.Y.Z" --body "<release notes>"
```

Wait for CI green, then merge:

```bash
gh pr merge <N> --merge   # NOT --squash — preserves the chore(release) commit verbatim
git checkout main && git pull
```

Why merge-not-squash: tag-push (Step 9) must reference the release commit by SHA. Squash rewrites the SHA; merge preserves it. The optional `source.sha` field in marketplace.json (if you added it pre-tag) would point at the original branch SHA, not the squashed one.

If you forgot something — say you missed the `source.ref` bump — push another commit to the release branch and re-CI. No rebase needed; CI re-runs.

### 8. Pre-push verification (do NOT skip)

Run from the release branch BEFORE pushing:

```bash
git diff --name-only main..HEAD          # all release files must appear
nx --version                             # must NOT yet print X.Y.Z (reinstall happens post-tag)
grep '^version' pyproject.toml           # must equal X.Y.Z
grep '"version"' .claude-plugin/marketplace.json    # both must equal X.Y.Z
grep '"version"' conexus/.claude-plugin/plugin.json # must equal X.Y.Z
grep '"version"' sn/.claude-plugin/plugin.json      # must equal X.Y.Z
grep '"ref"' .claude-plugin/marketplace.json        # both must equal "vX.Y.Z"
```

The version+ref strings must all line up. CI's `TestMarketplaceVersion` parity checks (version field AND `source.ref` field) fail the build if any mismatch.

### 8a. Optional: bump source.sha post-merge (defends against tag force-push)

After Step 7's merge lands on main, the release commit has a known SHA. Optionally add it to marketplace.json's `plugins[].source.sha`:

```bash
git checkout main && git pull
RELEASE_SHA=$(git rev-parse HEAD)        # the merge commit (or the chore(release) commit if merged via merge-commit)
# Edit .claude-plugin/marketplace.json: add "sha": "$RELEASE_SHA" alongside "ref": "vX.Y.Z" for both plugins
git add .claude-plugin/marketplace.json
git commit -m "chore(release): pin sha for vX.Y.Z"
git push
```

Tradeoff: extra commit on main, but guards against the case where someone could force-push the `vX.Y.Z` tag. For solo / small-team projects the `ref` alone is usually fine; skip if so.

### 9. Tag and push IMMEDIATELY after merge (triggers Release workflow + PyPI publish via OIDC)

After Step 7's PR merges, switch to main, fetch, and tag the merge commit:

```bash
git checkout main && git pull
git tag -a vX.Y.Z -m "conexus X.Y.Z" $(git rev-parse HEAD)
git push origin vX.Y.Z
```

Tag-push must follow the commit on origin in tight succession (seconds). marketplace.json's `source.ref` points at `vX.Y.Z`; if any user runs `/plugin install` between commit-push and tag-push, the install would fail.

Tag-immediately is safe even though the merge commit's own check-runs have not arrived yet at that point (main has no push CI; the merge commit only gets its own `pytest-gate` check-run from the develop-push CI fired by step 11b's back-merge, ~15 min later). The nexus-jvhsw evidence gate (`scripts/check_release_ci_evidence.py`) accepts evidence from the merge commit's second parent — the PR head, whose checks already ran and completed at PR-merge time — when the merge commit's own evidence is still missing (nexus-au8zz). It only does this after confirming, via GitHub's own PR-association record, that the parent is genuinely the head of a merged PR whose `merge_commit_sha` is the tagged commit — never on the two-parent shape alone. A publish run that reds citing "no check-run named 'pytest-gate'" on a genuinely fresh tag now indicates a real problem (e.g. a broken/renamed required check, or a merge commit with no PR-head parent), not this race — investigate it rather than assuming a retry will clear it. A run that instead prints `CANNOT VERIFY` (exit 2, distinct from the red `BLOCKED` exit 1) means the gate could not reach the GitHub API to resolve the parent, its check-runs, or its PR association — a transient network/API issue, not evidence the release is broken; retry once connectivity is confirmed.

Do NOT use `gh release create`: the Release workflow at `.github/workflows/release.yml` creates the GitHub release automatically from the tag and extracts notes from CHANGELOG.md. Running `gh release create` produces a duplicate.

Do NOT run `uv publish` or `twine upload` manually: the Release workflow handles this via OIDC trusted publisher.

### 10. Watch and verify the Release workflow

```bash
gh run watch                                   # wait for Release workflow green
```

One job must complete: `Build and publish to PyPI`. (2026-07-06 CI-cost
pass, PR #1375: the tag-time pytest matrix was removed — the tag points at a
main merge commit whose identical tree just passed the release PR's required
checks, so the re-run was the same tree's fourth test pass and made publish
hostage to the nexus-9eaz GHA-flake family. The publish job still verifies
tag == pyproject version before building.)

### 11. Verify release landed

```bash
gh release view vX.Y.Z
curl -s "https://pypi.org/pypi/conexus/json" | \
    python3 -c "import sys,json; print(json.load(sys.stdin)['info']['version'])"
```

Both must report `vX.Y.Z` / `X.Y.Z`. **Do not declare done before this check passes.** PyPI publish can be skipped if the Release workflow's pytest fails; the tag alone does not guarantee publication.

### 11b. Back-merge main into develop (MANDATORY — ends the stale-develop class)

```bash
git checkout develop && git pull
git merge origin/main --no-edit    # trivially clean right after a release:
                                   # the release branch just CONTAINED develop
git push origin develop
```

Why mandatory (2026-07-23 incident): from 6.12.0 through 6.17.0 no release
was ever merged back, so develop's seven manifests froze at 6.11.0 — all
stale TOGETHER, so the parity tests stayed green on a coherent lie (a
consistency gate cannot detect consistent staleness). Every dev-tree
install self-identified as 6.11.0: doctor nagged, the downgrade guard
misfired on reinstalls, and the RDR-143 lockstep hook was primed to
silently replace a deliberate dogfood install with PyPI. Each release also
re-paid Step 7's conflict tax, whose forgotten-step failure mode is a
release PR with NO CI checks at all. Running this step immediately after
tag-push is the moment the merge is conflict-free by construction; skipping
it re-opens the divergence that Step 7 then has to re-resolve under
pressure. Do it every release, zero-change releases included.

### 11c. Post-publish: published-bytes UPGRADE journey (nexus-86mx2, 2026-08-14)

Both commands below drive the just-published PyPI bytes, not the working
tree — "identical tree" is an argument that the pre-tag battery already ran
this; it is not a run of the actual published artifact. Run them once Step
11 confirms PyPI shows the new version.

```bash
tests/e2e/fresh-install-mvv.sh --published X.Y.Z    # FRESH-install axis, published bytes
NEXUS_TARGET_RELEASE=X.Y.Z tests/e2e/migration-rehearsal/run.sh --package-upgrade   # UPGRADE axis, published bytes
```

The first (nexus-796zn) is the post-publish shakedown for a box that has
never run conexus before — see its description near Step 1 above; it
belongs here, not in the pre-tag battery, because nothing is on PyPI yet at
that point in the checklist.

The second (nexus-86mx2) closes the loop the pre-tag `--package-upgrade` run
(Step 1) cannot: that run always upgrades to the WORKING-TREE wheel, which
proves the code but not the actual bytes PyPI now serves — a difference in
wheel packaging, MANIFEST.in, or dependency resolution at the real
`uv tool install`/`pip install` layer (the exact nexus-l2ku5 shape) is
invisible to a worktree-wheel run by construction. Setting
`NEXUS_TARGET_RELEASE=X.Y.Z` makes `run.sh` download the real published
wheel from PyPI (sha256-verified against PyPI's own JSON API) and upgrade
to THAT instead — same GH #1402 convergence assertions, now against bytes
a real user would actually install. Must end
`PACKAGE-UPGRADE CONVERGENCE MVV PASSED — ... -> published conexus X.Y.Z ->
...` — the verdict line always names which target ran; a plain
`-> working tree` here on a post-publish run means `NEXUS_TARGET_RELEASE`
was not set and the loop was NOT actually closed.

Coordinate with (do not duplicate) `tests/e2e/published-client-write-gate.sh`
(nexus-86mx2, wired into the `engine-release` skill's pre-tag battery): that
gate owns the FRESH-WRITE axis against a CANDIDATE engine, pre-deploy; this
step owns the UPGRADE axis against the REAL published engine identity,
post-publish. See either script's header for the full ownership split.

### 12. Reinstall local tool and verify

```bash
scripts/reinstall-tool.sh    # preserves [local] and other extras (mineru is now a default dep)
nx --version                 # must print X.Y.Z
```

`pyproject.toml` bumps the project version but the local `nx` shim keeps the old wheel until `scripts/reinstall-tool.sh` runs. Caught on v4.9.11: `nx --version` reported 4.9.10 even after PyPI showed 4.9.11.

## Common Mistakes

- **Bumping only `pyproject.toml` and missing the four plugin manifests.** CI parity check catches this late. Run the Step 8 pre-push check.
- **Skipping the integration suite.** Unit-only is what CI runs; integration is your last gate against keyed-API regressions before tag-push.
- **Running the full unit suite CONCURRENTLY with the E2E gates.** (2026-08-17, 7.9.0 battery.) The E2E gates parallelize safely among THEMSELVES, but their teardown phases (`nx daemon service stop --with-pg`, gate cleanup, the engine-substrate orphan sweep) share a machine-wide blast radius with the unit suite's self-provisioned engine substrates — the suite ran clean to ~75% then block-failed the moment a sandbox teardown fired. Sequence: E2E gates in parallel, the unit suite SERIAL (before or after). A block-shaped mass failure under concurrency is contention-shaped, but the quiet re-run of the failed set is mandatory evidence either way.
- **Skipping sandbox smoke when `conexus/**` or `pyproject.toml` changed.** The smoke catches plugin-load + db-migration regressions that unit tests miss.
- **Using `gh release create` after `git push origin vX.Y.Z`.** Duplicate release. The Release workflow already creates one.
- **Forgetting `uv sync`.** `uv.lock` not updated; CI fails or local install resolves differently.
- **Forgetting `scripts/reinstall-tool.sh` after tag-push.** Local `nx` stays on old version; the post-merge "verify locally" step lies.
- **Pushing the tag before the version-bump commit.** Tag points at the wrong commit; the Release workflow's version-match step fails (`tag != pyproject.toml version`).
- **Running `uv publish` or `twine upload` manually.** The Release workflow handles PyPI via OIDC; manual publish either duplicates or fights the workflow.
- **Declaring "release done" after `git push origin vX.Y.Z`.** Step 11 (PyPI + GitHub release verification) is what closes the loop. The tag-push only TRIGGERS the workflow; if Py3.13 flakes, publish is skipped.
- **Closing follow-on beads as "done" before Step 11 confirms PyPI publication.** Bead-close before publish-verified == the publish-was-skipped class of failure goes undetected.

## See also

- `AGENTS.md` § Cutting a release (canonical; defer to it on any discrepancy)
- `docs/contributing.md#release-process` (long form — Step-by-step checklist, and the Break-glass subsection for retry / yank / revert / tag-retraction procedures, and the Schema/data-migration releases subsection Step 6d above draws its rationale from)
- `feedback_invoke_release_skill.md` (memory entry: invoke this skill, do not freehand)
- `feedback_post_release_reinstall.md` (memory entry: reinstall after tag)
- `feedback_release_discipline.md` (memory entry: full suite before tag, not after)
- `feedback_version_bump_manifests.md` (memory entry: all four manifests, CI enforces parity)
