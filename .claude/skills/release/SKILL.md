---
name: release
description: Use when cutting a release, bumping version, tagging, or publishing to PyPI. This skill IS the release checklist and its authority; AGENTS.md points here. Also surfaces as /conexus:release.
---

# Release Checklist

Follow every step in order. Do not skip or reorder.

**This file is the authority for cutting a release.** AGENTS.md § Cutting a
release is a pointer to it, not a parallel copy — the authority was inverted
deliberately (2026-09-20) because the two had drifted and a reader had no way
to tell which was current: AGENTS.md carried a retirement the skill contradicted
57 lines later, a `git add` block here absorbed its own commit, and a rule
stated in both had widened in one. Two copies of a procedure decay until the
stale one wins an argument. `docs/contributing.md#release-process` remains the
long form for rollback and one-time setup. (Historical note, T2 [22511] gap 11:
this line once cited a "CLAUDE.md § Release Process" heading that exists in
neither file.)

## Steps

**Removed in cleanup step 11 (nexus-0r1uz), numbering left as it was:** Step 1c
(the generation-flip live-holder gate), Step 6 (sandbox smoke), Step 6b
(the upgrade shakeout) and Step 6c (sandbox shakedown) went with the scripts they
ran. The battery is now the fresh-install MVV, the package-upgrade convergence
MVV and the local-service gate, behind one preflight. Steps that remain:
0, 0c, 1, 2, 3, 4, 4a, 5, 6d, 7, 8, 8a, 9, 10, 11, 11b, 11c, 12.

### Where to run the battery

In the release worktree, on the release branch — never in the shared primary
checkout. `docs/agents/worktrees.md` (moved from AGENTS.md § "Worktrees: one session, one worktree") rule 4 carries the
reason and the incident; the short version is that rule 9 fast-forwards the
primary on every push to `develop`, and a tree that moves mid-battery changes
what the remaining legs build and gate. Cut the worktree before Step 1's
expensive legs, so they build and gate the tree that actually ships.

### 0. Engine-freshness gate (PREREQUISITE — the two-lifecycle check)

The Java **engine-service** is a SEPARATE release artifact from this PyPI release: its own `engine-service-vX.Y.Z` tag fires `engine-service-release.yml`, version is tag-stamped (no manifest bump), and it is **decoupled from the luxe6 / RDR-155-P4a develop release boundary**. This PyPI release pins ONE engine IDENTITY, `REQUIRED_ENGINE_VERSION` (`src/nexus/engine_version.py`) — the engine the release was built and gated with, installed on EVERY path (fresh init AND upgrade). It is NOT a compatibility minimum and NOT a range (Hal directive 2026-07-15). `PINNED_SERVICE_TAG` (`src/nexus/daemon/binary_install.py`, the exact tag a fresh local `nx init` install downloads) is DERIVED from it, not an independently hand-typed literal — there is no floor/exact split to reason about, bumping the one constant moves both together, by construction.

**This is a BLOCKING command, not a prose eyeball-check** (nexus-i5c2u — the prior prose-only version of this step was routinely skipped, letting the cloud engine sit at v0.1.17 for 9+ days across releases while the floor moved to v0.1.34; the pin then independently drifted the identical way in 2026-07-12, sitting at v0.1.36 two tags behind a verified, cloud-deployed fix — which is why the pin is no longer a second hand-typed constant at all):

```bash
uv run python scripts/check_engine_release_floor.py
```

If it exits non-zero, STOP — do not proceed with the PyPI release. The gate fails in TWO directions and the remedy differs:

- **cloud BEHIND the pinned identity** → EITHER conexus has not deployed it yet (surface the deploy relay and wait), OR this is a **PAIRED release** (Hal directive 2026-08-02: the floor bump rides the SAME release as the engine's deploy, and the deploy relay fires at client-tag push, parallel with the PyPI publish — see .claude/rules/releases-and-engine.md § Engine-service release). For a paired release, cloud-behind is the EXPECTED pre-tag state ONLY on the non-additive branch — when every wire-ledger `## Unshipped` entry leads with `[additive]`, prefer deploying the engine BEFORE the client tag (nexus-1emxn; Step 9 carries the branch decision) and this state never arises. Otherwise mechanized via `--paired-deploy` (nexus-k1c08):
  ```bash
  uv run python scripts/check_engine_release_floor.py --paired-deploy engine-service-vX.Y.Z
  ```
  Name the EXACT tag this release pairs with — never a guess. The flag accepts a below-floor cloud solely where that tag independently verifies as (a) a published, non-draft GH release carrying the `nexus-service-linux-amd64` asset (`gh release view` — a missing/failing `gh` fails the gate, it does not pass it), (b) exactly equal to `REQUIRED_ENGINE_VERSION`, (c) the newest published `engine-service-v*` tag, and (d) authored within 72h (`--paired-tag-max-age-hours` to override); the wire-contract ledger and the DATA EFFECT relay must also pass. Any single miss stays red with a named reason — this is a stricter, mechanized check, not a looser one. On acceptance it prints an explicit "PAIRED MODE" acknowledgment naming both versions and the POST-TAG VERIFY obligation. Do that verify: once the deploy lands, re-run the SAME command WITHOUT `--paired-deploy`. A still-behind cloud at that point is real drift, escalate loudly, do not re-arm the flag to silence it.
- **a gated engine tag was never pinned** (`REQUIRED_ENGINE_VERSION` behind the newest published tag) → bump `REQUIRED_ENGINE_VERSION` to that tag (this alone also moves `PINNED_SERVICE_TAG`). This is the local-install delivery failure: cloud users get the deployed engine regardless, local-mode users get ONLY what this constant names, so an unpinned tag reaches nobody. When the unpinned tag carries engine halves of features whose CLIENT halves ship in THIS release, the bump into this release is mandatory, not optional — floor-lag ships a client whose pinned engine lacks the engine halves of its own features (the 7.1.0/v0.1.62 inversion).

Re-run (without `--paired-deploy`) until it exits 0 — including the post-tag verify for a paired release.

`release.yml`'s own copy of this gate runs `--paired-deploy-auto` (nexus-gc9ir) instead of bare — it derives the candidate tag from `REQUIRED_ENGINE_VERSION` itself and only engages the paired-acceptance path when the cloud is actually below floor, so the workflow no longer red's during a paired release's expected parallel-deploy window. This pre-tag human invocation still uses the explicit `--paired-deploy <tag>` form above — name the tag deliberately here, where you already know it.

**The core tradeoff:** paired acceptance (either flag) proves the engine TAG is legitimate — it does not prove the deploy has actually landed. That's an accepted gap, and no automation backstops it: the only check that the deploy landed is the human POST-TAG VERIFY (re-run `check_engine_release_floor.py` bare once the deploy lands, as the acceptance message directs). See `check_engine_release_floor.py`'s module docstring for the full statement.

**Known CI-side failure mode, no bypass by design:** the workflow's `--paired-deploy-auto` invocation has no bypass flag, so a non-additive `docs/wire-contract-pending.md` `## Unshipped` entry fails that step CLOSED during a paired release, before the tag/cloud checks even run — correctly, do not ask for a CI-side bypass. **Re-running the SAME tag via `workflow_dispatch` does NOT fix this** (nexus-55r6o) — the checkout pins the tag's immutable tree, and the ledger check reads the ledger off that same frozen tree, so a same-tag retry re-reads the identical blocking entry and fails identically. Real remedy: (a) cut a FRESH client tag whose tree carries the ledger fix (a new release, not a retry of the failed one), or (b) implemented (nexus-55r6o): ci.yml's `release-ledger-gate` job runs the identical ledger-only check (`check_engine_release_floor.py --ledger-only`) on EVERY PR targeting main, before any tag exists — not narrowed to `release/*`-named branches, so a hand-named branch promoting to main is covered too. **This is mechanically MERGE-BLOCKING, not advisory:** the job is wired into `pytest-gate`'s `needs:`, and `pytest-gate` is main's actual required branch-protection check — a release PR with a blocking entry fails CI on Step 7 and cannot merge, no separate GitHub repo-settings change needed. Because the ledger this job reads is repo-GLOBAL, this is deliberately broad friction: a single non-additive `## Unshipped` entry blocks EVERY PR to main, not just the eventual release PR, until its client half ships or a human edits the ledger — intended, matching the ledger's own philosophy of surfacing an unshipped both-halves commit as a standing risk, not a tag-time surprise.

Supplementary context (useful when deciding whether recent `service/` work is cloud-relevant, but the script above is the actual gate):

```bash
git tag -l "engine-service-v*" | sort -V | tail -1          # last engine tag
git log --oneline <last-engine-tag>..HEAD -- service/        # cloud-relevant drift?
```

1. Confirm the pinned engine tag is (a) cloud-DEPLOYED and (b) cloud-GATED (recall + hybrid parity, xr7.8.9-style) — read the authoritative bead + conexus bus, **not memory** (cross-repo gate state goes stale fast: 2026-06-26 a `luxe6` condition had been cleared a week earlier than memory implied).
2. If `service/` has drifted with cloud-relevant changes (pooler/RLS, pgvector, catalog conformance, aspect queue, batch endpoints), cut a fresh engine FIRST — see **`docs/agents/engine-service-release.md`** — bump `REQUIRED_ENGINE_VERSION` to it IN THIS release (this alone also moves `PINNED_SERVICE_TAG` — nothing else to bump), gate the release battery against that engine, and arm the deploy relay to fire at client-tag push, parallel with the PyPI publish (paired-release choreography; send the relay to the conexus instance DIRECTLY — tuple mailbox or cross-session `SendMessage`, both reach it — and never frame the cross-instance deploy as autonomous: a relay carries the tag, the gate evidence and what changed, never authorization. See `engine-release` SKILL.md's deploy section, which retired the older "the bus is passive, surface the relay to Hal" wording by name on 2026-09-16 for reading as "you cannot talk to conexus", which is false and cost a round trip). The engine cut is NOT luxe6-gated, so refreshing it never blocks on the develop boundary, and it is never blocked by client-release preconditions either — those gate the DEPLOY, and pairing satisfies them the instant the client tag exists.
3. The engine cut itself: full `service/` suite green on the tagged commit (confirm the `service/` tree equals a green-`service-ci` commit — the Java CI `Java tests + jOOQ codegen drift guard` is a required check on `main` and `develop`, so a PR into either is gated on it, but a direct push to `develop` is not and a run can be cancelled or time out, so verify the tree), then the **human** pushes `engine-service-vX.Y.Z`.

This gate exists because the engine silently drifted 22 `service/` commits / 4 days behind the cloud (2026-06-26); the PyPI checklist had no step that would have caught it.

<!-- RETIRED 2026-08-24 (nexus-2zmfw). The remediation-commit gate and its committed bead snapshot are gone. It gated nothing — 0 of 428 non-closed beads named a required commit — while asserting a repo-wide invariant from ONE developer's local clone, dumped and committed by hand. It could report confident green over a view that never matched reality, and its staleness check keyed on max(updated_at) across every bead (closed included), so it failed a release on wall-clock rather than on content. It blocked v7.16.2 for exactly that reason. The real requirement it stood for — sequence a remediation behind a commit — is a bead-authoring convention, not a release gate. -->

### 0c. PREFLIGHT — run the cheap blockers FIRST, all of them

> Budget MINUTES (the lint bucket alone is ~2 minutes). Build the gate jar first
> (`scripts/build-gate-jar.sh`) or the lint bucket's substrate-backed tests fail
> for that reason alone.

```bash
./tests/e2e/release-preflight.sh
```

Run this BEFORE step 1 and before any expensive leg. It is the ONE preflight
(the former pin-sweep script is folded into it) and it evaluates every
seconds-scale, deterministic, release-BLOCKING check in one pass without
aborting on the first red -- it reports every failure it finds, so one cycle
surfaces the whole fix list. The checks:

- `wire-contract-ledger` -- `check_engine_release_floor.py --ledger-only`, which
  is MERGE-BLOCKING on every PR to main, not just at tag time. It also fails when
  `REQUIRED_ENGINE_VERSION` is below `NOTE_URI_MIN_ENGINE_VERSION` (RDR-231: a
  release must pin an engine that can hold `nx-note://`; the floor leads the pin
  on develop, which no develop run checks);
- `ci-evidence-contexts` -- the required-context drift tests, run with `-m ""` so
  the integration-marked live branch-protection check actually executes;
- `mandatory-pins` -- the GitHub-backed `mandatory_regression_pin` tests under
  the operator's REAL HOME (they need `gh` auth, so they cannot run inside the
  local-service gate's fenced HOME), exact count asserted, zero skip budget;
- the pin sweep -- the `-m lint` bucket with its non-vacuity floor, the pin tests
  that are not lint-marked, `check_wire_contract_pairing.py`, and `ruff check src`.

WHY (the 7.15.0 cut, 2026-08-22). The battery was ordered expensive-first and
abort-on-first-red, so each blocker cost a full replay and hid every other one.
`REQUIRED_CHECK_CONTEXTS` drift against main's live branch protection was a
sub-second assertion found 13.5 minutes into `local-service-gate.sh`. Sequential
abort-on-red turns N cheap blockers into N hours.

Exit codes: `0` PREFLIGHT PASSED, `1` PREFLIGHT FAILED (fix everything listed,
then re-run), `2` PREFLIGHT UNVERIFIED (a dependency was absent; "could not
check" is never "fine").

The engine-identity check (`check_engine_release_floor.py` bare form) is NOT in
the preflight; Step 0 above runs it by hand.

Do not let this file grow into a second battery. Admission requires: genuinely
release-blocking and deterministic (no service, no sandbox HOME).

### 1. Run unit + integration suite

```bash
tests/e2e/release-battery.sh         # preflight, then the three legs below in ONE parallel run: per-leg logs, verdict table, exit 1 on any red; the unit suite still runs SERIAL to it
uv run pytest -n auto && uv run pytest -m lint   # unit suite and the lint bucket (two runs)
tests/e2e/local-service-gate.sh      # leg lsg: integration incl. the local-service functional gate
tests/e2e/migration-rehearsal/run.sh --package-upgrade   # leg pkgup: ONE-engine convergence MVV (nexus-cfgo9)
tests/e2e/fresh-install-mvv.sh       # leg mvv: VIRGIN-journey gate (nexus-nolqs) — see below
tests/e2e/constraints-resolve-gate.sh   # leg resolve: the shipped constraints.txt + overrides.txt RESOLVE (uv pip compile, no install): 22 compiles on Linux x86_64/aarch64 py3.12/3.13 (cpu; [pdf] also cu130 and cu126), Windows x64 and macOS 14 arm64, default and [pdf], plus a macOS 13 negative control (nexus-2k66t.4.4); network; needs a uv that knows --torch-backend cu130 and cu126 (the gate probes and says so); offline self-test tests/e2e/constraints_resolve_gate_test.sh
tests/e2e/hook-cli-skew/run.sh       # every hooks.json entry vs every published CLI from 7.55.0 (nexus-rcoze); standalone since cleanup step 11, no longer a battery leg
```

All must pass. Integration is excluded from CI and is the last line of defense before tag-push.

**Reading the battery's table, which is where a vacuous pass hides.** The
driver runs about 30 minutes on this box. Reds never stop it and it prints one
table at the end, so the verdict you act on is the table, not the exit. **A leg
with no verdict line is MISSING, never passed** — implemented in
`release-battery.sh` (`LEG_STATUS[$leg]="MISSING"`), and it is the rule that
stops a silently-empty leg reading as green. Each leg builds its own wheel or
jar (the gate jar is cached on `service/` content), so a dirtied tree rebuilds.

Count the advisories too. A gate that passed on a fallback prints one
`GATE PASSED-BY-DEFAULT: <gate> <reason>` line and still exits 0
(nexus-1c7oq; producers: the choreography rows marked `advisory`, which
include the four paired-acceptance rows where a below-floor cloud passes
on the paired tag rather than a live engine at floor (Sam, 2026-09-04),
the evidence gate's borrowed-parent path, `local-service-gate.sh` under
`NEXUS_GATE_NO_VOYAGE=1`). `grep -c 'GATE PASSED-BY-DEFAULT:'` over the
battery output; a non-zero count is not a failure, it is the list of greens
that rest on a default, and each one is named in the ship record.

**`fresh-install-mvv.sh` — the virgin-journey gate (nexus-nolqs, 2026-07-21).**
Every other E2E gate starts from a POPULATED install and tests the upgrade
axis; the unit suite THEN pinned the SQLite opt-out backend — since retired at
RDR-158, and today the suite pins the engine substrate instead — which is how
the f1itv/e9ru2/kmo9h/r5f3c/9xfx5 fresh-box defect class shipped through the
full release process unseen. The virgin journey is still covered by no unit
test, which is why this gate exists. It builds the wheel under test, then on a
scrubbed-env virgin HOME: local init (engine sha256+sig-verified, portable PG,
bge-768), ladder converged at init, store put + index md with ENGINE-CATALOG
registration asserted, semantic search returns both sentinels, doctor with
zero ✗ and warnings checked against the script's allowlist. Must end
`FRESH-INSTALL MVV PASSED — ... (LOCAL WHEEL, release-battery layer)`.
`FRESH_MVV_CACHE=/tmp/fresh-mvv-cache` reuses the 416MB model download across
runs. The work tree (about 2.5 GB) is created under `$TMPDIR` and removed on
exit. On failure the script prints `FAILURE EVIDENCE PRESERVED: <dir>`, a small
`$TMPDIR/fresh-mvv-*-logs` dir holding the leg logs plus the engine and Postgres
logs from the sandbox HOME (never config or credentials); `FRESH_MVV_KEEP_WORK=1`
keeps the whole tree on failure instead (nexus-0tqr7). Every new fresh-box warning is a decision: fix it or allowlist it in
the script WITH a rationale + bead reference.

**Leg 9/10 (nexus-utpuw.19) — a generation install on that same virgin HOME.**
Worth naming separately because it is load-bearing twice over: it is the only
fresh-journey coverage of the `<tools>/gen-<stamp>` + shim path, and the only
proof the built WHEEL actually ships `nexus/_install/*.sh`. Every other test of
`packaged_install_dir()` runs against an editable checkout, where that path
exists because the repo does — so those tests would pass against a wheel that
ships none of it.

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

The gate runs the local service **keyless** since nexus-w6h2m (2026-07-28): no
`NX_VOYAGE_API_KEY` reaches it, so it embeds every collection with bge-768 and
`keyed_voyage` tests are deselected — the gate needs no `VOYAGE_API_KEY` for its
own corpus. This is a posture choice, not the engine's ceiling: a keyed local
service embeds with Voyage instead (nexus-umm29 carve-out), but nothing in this
gate, or anywhere else in the release battery, boots the local service keyed —
that combination is untested by any gate. Two markers carve tests out, each
with an exact-count guard: `lived_in` (excludes tests that dispatch real
`claude -p` or need seeded lived-in corpora) and `keyed_voyage`. A guard trip or
any new hard failure is real signal — compensate with live validation of the
release's changed paths (the v6.3.5/v6.3.6 pattern: exercise the advertised
claims against the real deployment).

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

### 3. Bump version in ALL EIGHT bump targets

CI enforces parity on all but one of these (`conexus/PENDING_RELEASE.md` is a drift
ledger, not a version field — see `.claude/rules/releases-and-engine.md` § Release cadence policy rule 6 and
Step 3 below for the exact enumeration these
agree on, nexus-smsau 2026-09-27). Missing any one of the seven CI-enforced ones
fails the marketplace-version-matches-pyproject test, the
marketplace-source-ref-matches-pyproject test, the
mcpb-manifest-version-matches-pyproject test, the
every-plugins-own-plugin-json-version-matches-pyproject test (covers BOTH conexus
and sn, plus any plugin added later — no longer sn-only), or the
uv-lock-version-matches-pyproject test.

- `pyproject.toml`: `version = "X.Y.Z"` (canonical source of truth)
- `mcpb/pyproject.toml`: `version` **and** the `conexus>=X.Y.Z` dependency pin (bare `conexus`, no extra, the same distribution the plugin's `nx-mcp` runs from: the engine embeds T3, so the `[local]` extra the bundle once pinned only fetched a second bge copy; `tests/test_plugin_structure.py::test_mcpb_pins_bare_conexus` enforces the bare pin and that it tracks the version.)
- `mcpb/manifest.json`: `version`
- `.claude-plugin/marketplace.json`: **`plugins[].version` for every plugin the file lists** (today conexus, sn, conexus-tool-spill and conexus-prose-edit; a loop, not a fixed set)
- `.claude-plugin/marketplace.json`: **`plugins[].source.ref` for every plugin the file lists** — must be `"vX.Y.Z"` (the tag form). Easy to forget. This is what decouples installed users from main HEAD: plugin installs follow the pinned tag, not whatever main currently is. **CRITICAL: nexus-mkj6u 2026-05-23**
- `uv.lock`: `version` — pinned exact versions the release publishes from
- every plugin's own `<plugin>/.claude-plugin/plugin.json`: `version` (today `conexus/.claude-plugin/plugin.json`, `sn/.claude-plugin/plugin.json`, `tool-spill/.claude-plugin/plugin.json` **and** `prose-edit/.claude-plugin/plugin.json`; the directories are `tool-spill/` and `prose-edit/`, the plugins `conexus-tool-spill` and `conexus-prose-edit`; bump each file the marketplace lists; `test_every_plugins_own_plugin_json_version_matches_pyproject` loops over all of them, so a plugin added there is covered with no test edit)
- `conexus/PENDING_RELEASE.md`: **empty the pending list.** Not a version field (nothing here equals `X.Y.Z`) — a drift LEDGER. Advancing `source.ref` is exactly what makes those plugin changes live, so the ledger's entries stop being pending at this step. `tests/test_plugin_release_drift_ledger.py` FAILS on a stale entry, so a forgotten clear blocks the release rather than rotting silently. The list you are deleting is also the honest "what becomes active in this release" note for the CHANGELOG (nexus-mk3tw / the 2026-07-25 inert-guard incident: three guards were merged, closed as "mechanized", and protecting nothing because the pin had not moved).

Optional but recommended: also bump `plugins[].source.sha` to the 40-char SHA of the release commit, for protection against tag force-push. Add post-commit (Step 8a, see below).

**Engine-service pin (conditional 8th target — nexus-3rq00).** The Python/Java boundary rides one more hand-edited constant that sits OUTSIDE the seven-manifest parity gate: `PINNED_SERVICE_TAG` in `src/nexus/daemon/binary_install.py`, the `engine-service-vX.Y.Z` release this build auto-installs. It is DERIVED from `REQUIRED_ENGINE_VERSION`, so it is never hand-edited: moving the engine identity moves the pin by construction. Two invariants the `TestEnginePinParity` test enforces: (1) `PINNED_SERVICE_TAG`'s numeric version must be `>= REQUIRED_ENGINE_VERSION` (`src/nexus/engine_version.py`) — never ship a client that auto-installs an engine it then refuses as too old; (2) at the 6.0 release boundary the pin must be non-None (it is intentionally `None` pre-6.0). A release that bumps pyproject to 6.x without setting a real pin trips CI. **The pin's VALUE is asserted in exactly one test** — `tests/test_engine_version.py::TestRequiredEngineVersion::test_pinned_floor_is_current` — and since nexus-9gggv (2026-09-08) it is DERIVED, not typed: the floor must equal the first `engine-service-vX.Y.Z` the newest released `CHANGELOG.md` section names. So a `REQUIRED_ENGINE_VERSION` bump needs no test edit; it needs the release's CHANGELOG section to name its engine pairing as the FIRST `engine-service-v` mention in that section (a `was engine-service-vX` aside goes after the pairing, never before it; every released section to date satisfies this, 358 checked), and a bump without that line, or that line without the bump, is the red. Add the bump's reason to the test's comment trail for provenance. Before this derivation the tuple was hand-edited per release and missed on 7.23.0: the unit leg of the battery red'd on it after the paired engine gate was already green.

Semver: MAJOR for breaking, MINOR for new features, PATCH for bug fixes.

### 4. Update both changelogs

- `CHANGELOG.md` (root): move `## [Unreleased]` content into a new `## [X.Y.Z] - YYYY-MM-DD` section. Leave a fresh empty `## [Unreleased]` at the top.
- `conexus/CHANGELOG.md` (plugin changelog): always update, even if no plugin changes (note: "Plugin version aligned with conexus X.Y.Z. No plugin-side changes." is acceptable).

### 4b. Move this release's wire-ledger entries to Shipped (IN the release commit)

Every `docs/wire-contract-pending.md` `## Unshipped` entry whose commit this
release carries moves to `## Shipped` in the release commit itself, in the
Shipped form the lint enforces:
`` - `<sha>` -- bead <id> -- shipped in `vX.Y.Z` -- engine half engine-service-vA.B.C (<when it deployed>) -- [additive] <note> ``.
Keep the note and its direction-safety prose; rewrite only the head.

Do it here, not after the tag. `scripts/check_wire_contract_pairing.py` calls
an Unshipped entry STALE as soon as a `v*` tag containing its commit exists,
and the ledger lint (`test_live_repo_ledger_is_clean`) is in `-m lint`. An
entry left in Unshipped therefore turns `develop` red the moment the tag is
pushed. That happened at 7.74.2, where four entries reddened develop's lint
until a follow-up commit (a5d59f08b). Moving them in the release commit means
the tagged tree and the back-merge are both clean. Doing it before the tag is
safe: "declared" accepts either section, so the commits are still declared.
The engine deploy that needed the Unshipped entries
(`--client-precondition`) has already been decided by this point (Step 0).

List the entries this release carries:
```bash
git merge-base --is-ancestor <sha> HEAD   # for each ## Unshipped sha; true = it ships in this release
uv run python scripts/check_wire_contract_pairing.py   # must report clean after the move
```
`tests/e2e/release-preflight.sh` enforces this: its `wire-ledger-shipped` leg
(`check_wire_contract_pairing.py --release-tree`, nexus-mm3u5) fails a release
tree (pyproject newer than the newest `v*` tag) that still carries an
`## Unshipped` entry whose commit is an ancestor of HEAD, naming each sha and
bead. It reports NOT-APPLICABLE on any other tree.

### 4a. Set the privacy-policy effective date (nexus-5zv4j)

`docs/privacy-policy.md` line 3 carries the effective date. If it reads
`_Effective: RELEASE-DATE (set when the release carrying nexus-5zv4j is
cut)_` (or, on a later release, any line starting `_Effective:
RELEASE-DATE`), replace it with `_Effective: <release date YYYY-MM-DD>_`
using the SAME `YYYY-MM-DD` the CHANGELOG section above just used — the
placeholder exists precisely because nexus-5zv4j's own bead landed with no
known release date yet. If the line already carries a real date (a prior
release already stamped it), leave it alone; this step is a no-op past the
release that first stamps it. `tests/test_privacy_policy_release_date.py`
fails a tagged release tree (pyproject version == the newest `v*` tag) that
still carries the placeholder.

### 5. Refresh `uv.lock`

```bash
uv sync
```

The lock file MUST be committed. CI also checks this.

Then regenerate the constraints the wheel ships (nexus-2k66t.4.1) and commit them
with the lock:

```bash
scripts/regen-install-constraints.sh   # rewrites src/nexus/_install/constraints.txt
```

Every install path hands `src/nexus/_install/constraints.txt` to `uv pip install
--constraints`, so a stale file means users install versions the release did not
lock. A bare version bump leaves it unchanged (the project itself is not
constrained); a dependency change in the cycle does not.
`tests/test_install_constraints_pin.py` fails the default suite, and so the
release battery, while the file and `uv.lock` disagree.

### 6d. Migration-release branch (CONDITIONAL — this release ships a data migration)

Trigger: this release carries a schema or data migration — a new `upgrade_ladder` rung (`src/nexus/upgrade_ladder/registry.py`), or any client-side change to a shape data already has to conform to (T2 [22511] gap 9). Skip this step entirely when the release carries no such change.

1. **Representative-scale rehearsal.** Run the populated-store upgrade rehearsal against a corpus seeded ABOVE a stated floor, not the harness's default toy seed (10-30 docs in `rehearse_package_upgrade.sh` / `rehearse_acquire.sh`). Pre-tag, that is the worktree `--package-upgrade` run in Step 1; post-publish, close the loop with Step 11c's published-bytes run:
   ```bash
   NEXUS_TARGET_RELEASE=X.Y.Z tests/e2e/migration-rehearsal/run.sh --package-upgrade
   ```
   Name the floor and the actual seed count used in the release relay. RDR-191's cloud 385,484-row unify-chunks migration (T2 [22485]) remains the only at-scale proof this project has produced for a comparable change — and it ran in PRODUCTION. If the rehearsal cannot be brought to a genuinely representative scale before tagging, say so explicitly rather than letting a toy-scale pass stand in for one.

2. **Rollback decision point, settled before the release branch is committed.** Determine whether this migration can be rolled back on an already-upgraded install. When the answer is no, write **IRREVERSIBLE** in the release notes / relay verbatim, and have the substitute in hand: a written rollback/abort runbook with exact statements and named abort criteria — the RDR-191 `nexus-o8dil.22` pattern.

3. **Freeze-window derivation.** Only applies when this migration is coupled to a server-side schema change (an engine tag deploying alongside this release). If so, do not re-derive the window math here — it lives in `.claude/skills/engine-release/SKILL.md` Step 5b; confirm that step ran and its threshold/abort condition are recorded before this release's tag pushes.

4. **Post-deploy data-integrity verification, beyond version identity.** Once Step 11c's published-bytes rehearsal lands, verify data integrity beyond a version match: exact row-count reconciliation, not just `/version` naming X.Y.Z — the T2 [22485] pattern ("ROW INVARIANT EXACT: 385,484 pre == post ... ANALYZE fired").

Full rationale and evidence citations: `docs/contributing.md` § Schema/data-migration releases.

### 7. Commit on a release branch + PR to main (nexus-mkj6u: replaces direct-to-main)

**When this release pins a NEW engine tag, open the PR only after that tag is
published** (draft=false, every asset attached):
```bash
uv run python scripts/check_engine_release_floor.py --require-windows engine-service-vA.B.C   # exit 0 before `gh pr create` or any push to the open release branch
```
PR CI, the hellmini suite and the battery all boot the engine substrate,
which downloads the PINNED tag's PG bundle. Before the tag is published,
every substrate-backed test fails at setup. On 2026-10-08, 7.75.0's PR CI
(12 shards) and a hellmini full suite (25,636 setup errors) both ran while
engine-service-v0.1.154 was still building. The suite's session-start
preflight now refuses such a run in seconds, but a refused CI run is still a
wasted round: check first.

Per the marketplace-pinned-source playbook (also used by `Hellblazer/palinex`), release commits go through a PR. CI gates the bump before it lands on main. No more direct-to-main exception.

```bash
# Base the release branch on DEVELOP, not main: a release PROMOTES develop's
# accumulated state to main (CLAUDE.md: "releases promote develop to main via
# merge"). Branching off main would omit develop's unmerged fixes, so the
# release PR must carry develop's commits + the version-bump commit.
git checkout develop && git pull
git checkout -b release/vX.Y.Z

# PRE-MERGE MAIN FIRST (added 2026-07-04, learned on v6.3.1): a release branch
# based on develop ALWAYS conflicts with main's release-only files (the eight
# bump targets from Step 3, both changelogs, the engine pin) because release
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

# Stage ALL EIGHT bump targets from Step 3 (uv.lock among them), both
# changelogs and the cleared PENDING_RELEASE.md ledger. mcpb/pyproject.toml and
# mcpb/manifest.json are the easy-to-miss pair here, and omitting either fails
# CI's mcpb-manifest-version parity check.
git add pyproject.toml uv.lock CHANGELOG.md conexus/CHANGELOG.md \
        mcpb/pyproject.toml mcpb/manifest.json \
        .claude-plugin/marketplace.json \
        conexus/.claude-plugin/plugin.json \
        sn/.claude-plugin/plugin.json \
        tool-spill/.claude-plugin/plugin.json \
        prose-edit/.claude-plugin/plugin.json \
        conexus/PENDING_RELEASE.md
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
grep '"version"' .claude-plugin/marketplace.json    # every plugin entry must equal X.Y.Z
grep '"version"' conexus/.claude-plugin/plugin.json # must equal X.Y.Z
grep '"version"' sn/.claude-plugin/plugin.json      # must equal X.Y.Z
grep '"version"' tool-spill/.claude-plugin/plugin.json  # must equal X.Y.Z
grep '"version"' prose-edit/.claude-plugin/plugin.json  # must equal X.Y.Z
grep '"ref"' .claude-plugin/marketplace.json        # every plugin entry must equal "vX.Y.Z"
```

The version+ref strings must all line up. CI's `TestMarketplaceVersion` parity checks (version field AND `source.ref` field, looped over EVERY plugin marketplace.json lists — not a fixed count) fail the build if any mismatch. Add one more `grep '"version"' <plugin>/.claude-plugin/plugin.json` line here for each plugin marketplace.json gains; the four shown are today's set (conexus, sn, tool-spill, prose-edit), not a ceiling.

### 8a. Optional: bump source.sha post-merge (defends against tag force-push)

After Step 7's merge lands on main, the release commit has a known SHA. Optionally add it to marketplace.json's `plugins[].source.sha`:

```bash
git checkout main && git pull
RELEASE_SHA=$(git rev-parse HEAD)        # the merge commit (or the chore(release) commit if merged via merge-commit)
# Edit .claude-plugin/marketplace.json: add "sha": "$RELEASE_SHA" alongside "ref": "vX.Y.Z" for every plugin the file lists
git add .claude-plugin/marketplace.json
git commit -m "chore(release): pin sha for vX.Y.Z"
git push
```

Tradeoff: extra commit on main, but guards against the case where someone could force-push the `vX.Y.Z` tag. For solo / small-team projects the `ref` alone is usually fine; skip if so.

### 9. Tag and push IMMEDIATELY after merge (triggers Release workflow + PyPI publish via OIDC)

**PAIRED-RELEASE DEPLOY ORDER (nexus-1emxn — measured twice 2026-08-29: the
"deploy fires at tag push" relay is human-mediated, PyPI publishes in ~90 s,
and the v7.23.0 refusal window sat open 48+ minutes on the releaser's own
box).** Before pushing the client tag of a PAIRED release, settle which
branch applies:

- **All wire-ledger `## Unshipped` entries carry `[additive]`** (old client +
  new engine safe — `check_engine_release_floor.py --client-precondition`
  accepts this shape by name): have the engine DEPLOYED before this step. The
  client tag then cannot open a window at all. This is the preferred branch
  whenever the ledger allows it.
- **Any entry is not additive**: do NOT push the tag until the redeploy is
  staged with conexus — image built, a named trigger ("fires when vX.Y.Z
  appears on origin") — and that is CONFIRMED back. An unstaged non-additive
  pairing does not tag; "I'll send the relay right after" is the exact shape
  that opened both measured windows. This is a human confirmation; no script
  checks it.

Authority for the protocol: `docs/agents/engine-service-release.md`
(nexus-1emxn refinement paragraph) — this step carries the operational
form; on any wording conflict, that file wins.

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
gh api repos/Hellblazer/nexus/commits/<merge-sha>/check-runs --jq '.check_runs[] | [.name, .status, .conclusion] | @tsv'   # read once; re-read later, never `gh run watch` or a polling loop (AGENTS.md worktree rule 7)
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
scripts/git-push-develop.sh HEAD
# the merge commit vouches for what it merged in (nexus-9wxu6)
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

**Formula-PDF / MinerU coverage (opt-in; nexus-gqrg0, GH #1533).** The
pre-tag battery's resolver-bound lint (`tests/test_install_source_wiring_pins.py`)
covers the LOCAL wheel's dependency resolution only. To also prove the
PUBLISHED dependency resolution indexes a real formula PDF through MinerU
and keeps its LaTeX, set `NX_MVV_FORMULA_PDF_CHECK=1` on the fresh-install-mvv
invocation above:

```bash
NX_MVV_FORMULA_PDF_CHECK=1 tests/e2e/fresh-install-mvv.sh --published X.Y.Z
```

Off by default (including in the plain command above) because it pays a
MinerU pipeline model download (~2-3 GB) that would dominate every default
MVV run. Run it whenever a release touches `mineru`/`pdftext`/`pypdfium2`
pins, or on a cadence of your choosing. The sandbox shakedown that used to
exercise MinerU end-to-end against the LOCAL wheel was deleted in cleanup
step 11, so this opt-in run is now the only MinerU formula-PDF coverage.

### 12. Reinstall local tool and verify

**CLOUD-MODE BOX GATE (nexus-1emxn (c)):** on a cloud-mode box, run the
post-tag floor verify FIRST (the bare form):

```bash
uv run python scripts/check_engine_release_floor.py
```

Exit codes, precisely:
- **0** — cloud current; reinstall now.
- **1** — cloud still BEHIND the new floor: new spawns after this
  reinstall would REFUSE the managed service (the GH #1402 class, on your
  own box). Wait for the deploy to land, re-verify, then reinstall.
- **2** — cannot verify (network/API): fix connectivity and re-run; not
  evidence either way.

Local-mode boxes skip this gate — their engine converges from
`PINNED_SERVICE_TAG` at reinstall.

```bash
scripts/reinstall-tool.sh    # preserves [local] and other extras; the PDF stack (mineru, torch) is the on-demand [pdf] extra: NX_REINSTALL_EXTRAS=pdf scripts/reinstall-tool.sh keeps it
nx --version                 # must print X.Y.Z
```

`pyproject.toml` bumps the project version, but the local `nx` shim does not point at a wheel: it resolves `<tools>/current` at spawn time and execs the generation that pointer names, so until this step flips `current` every new spawn still lands in the old generation (and existing holders keep running from theirs afterwards). Caught on v4.9.11: `nx --version` reported 4.9.10 even after PyPI showed 4.9.11.

## Plugin-only fast lane

A patch release that ships plugin-surface content (`conexus/`, `sn/`,
`tool-spill/`, `prose-edit/`, `.claude-plugin/marketplace.json`) while `develop` carries `src/`
work that must not ship yet. It is a client release: same tag form, same
`release.yml` publish, same version surfaces. This section lists only what
differs; every step it points at runs exactly as written above. Design record:
T2 `nexus/design-plugin-only-fast-lane.md`.

Base tag is vX.Y.Z (the last client tag), the new release is vX.Y.N with N = Z+1.
Steps 0 (beyond the floor check in item 4 below) and 6d do not apply: the engine
pin does not move and no migration ships.

**1. When to use it.** The plugin fixes waiting in `conexus/PENDING_RELEASE.md`
are worth shipping now, and there is no open release PR (a release PR already in
flight is the normal release). If `src/`, `service/`, a dependency file or the
engine pin has to move, cut a normal release instead. The lane is open only
while the newest published `engine-service-v*` tag is the one the base tag pins:
`release.yml` runs `check_engine_release_floor.py` at tag time, its pin-currency
check goes red when a newer engine tag exists, and the only remedy (bumping
`REQUIRED_ENGINE_VERSION`) is a `src/` change this lane refuses. A red there
closes the lane: cut a normal release. The lane runs the bare form, not the
workflow's `--paired-deploy-auto`: they agree when the cloud meets the floor, and
when it does not the bare form goes red where the workflow could pass on a fresh
paired engine tag. A paired engine tag means a normal paired release, so the
stricter form is the right one here.

The base tag must itself carry the lane's machinery, because the gate refuses
`scripts/` and the harness in the diff and there is no exemption. The releaser
checks it, instead of trusting a version number:

```bash
git cat-file -e vX.Y.Z:scripts/check_plugin_only_eligibility.py
git cat-file -e vX.Y.Z:tests/e2e/plugin-delivery-proof.sh
git show vX.Y.Z:tests/e2e/release-battery.sh | grep -q -- '--plugin-only'
```

Any non-zero exit means the base tag predates the lane: normal release.

**2. Branch off the tag, never off `develop`.** In a sibling worktree of the primary
checkout (see "Where to run the battery"):

```bash
git fetch origin --tags
git worktree add ../nexus-wt/release-vX.Y.N -b release/vX.Y.N vX.Y.Z
# the ledger names the entries to ship (each entry names its bead); skip those
# under "Deferred to the next client release". This log only helps you FIND
# their commits: pick the ledger's commits, not everything the range lists.
git log --reverse --format='%h %s' vX.Y.Z..origin/develop -- conexus sn tool-spill prose-edit .claude-plugin/marketplace.json
git cherry-pick -x <sha> [<sha> ...]     # -x records the develop sha in each message
```

The nx code under test is the tag's own, so there is no main/develop skew and
Step 7's pre-merge of `main` has nothing to do. A conflict in `CHANGELOG.md` or
`conexus/PENDING_RELEASE.md` is resolved with the release branch's side, because
the bump commit writes both:

```bash
git checkout --ours CHANGELOG.md conexus/PENDING_RELEASE.md   # whichever conflicted
git add CHANGELOG.md conexus/PENDING_RELEASE.md
git cherry-pick --continue
```

A conflict in a plugin file means the pick depends on a commit you skipped:
`git cherry-pick --abort`, and defer that entry and every entry that builds on
it to the next normal release. A commit that also touches `src/` or `service/`
cannot be cherry-picked whole either: that entry waits. So does a bead whose
other commits do: the eligibility gate (item 4) reads every bead id named in
the picked commits and refuses the release when another commit naming that bead
on `origin/develop` since the tag touches a path the lane does not ship. It
matches the whole bead id in commit messages, so a commit that does not name its
bead is not seen by that check; the per-path rules still bind the diff. A change that names an
nx verb the tag lacks fails the existing verb-rot test in the preflight; it is
not eligible.

**3. Bump (Steps 3, 4, 5).** The same surfaces as Step 3 (`pyproject.toml`,
`mcpb/pyproject.toml`, `mcpb/manifest.json`, `.claude-plugin/marketplace.json`
version and `source.ref` for every plugin, every `plugin.json`, `uv.lock`), both
changelogs, and `conexus/PENDING_RELEASE.md` emptied of the entries that shipped.
In `uv.lock` change only the `version` line under `name = "conexus"` and confirm
with `uv lock --check`; do not run Step 5's `uv sync`. A local uv newer than the
one that wrote the tag's lock rewrites the whole file (on 7.87.1 it added
`revision = 2` and changed platform markers on a dozen dependencies), and that is
churn the base tag's tested wheel never had. The engine pin does not move, and a
diff that touches `src/` or `scripts/` is refused, so do not run
`scripts/regen-install-constraints.sh` either. Steps 4a and 4b do not apply. The new `CHANGELOG.md` section must name
its engine pairing as its first `engine-service-v` mention, for example `Pairs
with engine-service-vA.B.C (REQUIRED_ENGINE_VERSION unchanged from X.Y.Z)`
(A.B.C is the engine tag the base tag's own CHANGELOG section names),
or the preflight's pin-tests fail (`tests/test_engine_version.py`, nexus-9gggv).
Commit it all as one `chore(release): conexus X.Y.N` commit.

**4. Eligibility and floor gates.** On the committed HEAD:

```bash
uv run python scripts/check_plugin_only_eligibility.py --base vX.Y.Z   # last line must be PLUGIN-ONLY ELIGIBILITY PASSED
uv run python scripts/check_engine_release_floor.py                    # must exit 0 before the PR (item 1: red closes the lane)
```

For the eligibility script, exit 1 lists each offending path or straddling bead
(naming the commit) and why; exit 2 means it could not check, including a
`--develop-ref` (default `origin/develop`, so fetch first) that does not resolve.
Neither is a pass.

**5. Gates.** From the release worktree with a clean tree (the mode refuses
uncommitted or untracked paths, and refuses `--skip-preflight`):

```bash
lab-remote -c lab-l -- python3 scripts/watched_pytest.py --stall 180 --status /tmp/fastlane-X.Y.N.json -- uv run pytest -n auto   # the unit suite: a hand step, SERIAL to the battery (Common Mistakes), not a leg; the status file stays outside the worktree
tests/e2e/release-battery.sh --plugin-only vX.Y.Z
```

The battery runs the eligibility gate again, then the preflight (which holds the
lint bucket and the pin tests, so Step 1's separate lint run is covered), the
hook-CLI skew leg and the plugin-lockstep leg. `lsg`, `pkgup`, `mvv`, `dual` and
`resolve` are shown as `SKIPPED(plugin-only: ...)`, never omitted, and the closing
line names the mode and the skipped count; a plain `RELEASE BATTERY PASSED` is
not what this lane prints. **Accepted risk:** those five legs, and Step 1's
integration suite, are skipped because the wheel and the engine are identical to
the base tag's but for the version, not because their inputs are identical:
`resolve`, `mvv`, `pkgup` and `dual` also depend on package indexes and keys that
change with time, so a dependency or index break since the base tag goes
undetected by a plugin-only run. The evidence for skipping is one measured
identical-code bump (2026-10-26, all six legs passed). Step 11c below stays in the
lane as the post-publish control for exactly this risk. Then Step 8's pre-push
greps, unchanged.

**6. Publish.** The normal flow, with the same human gates, except at Step 7: the
branch from item 2 already exists and is built off the tag, so skip Step 7's
branch-creation and pre-merge lines and run only its push and
`gh pr create --base main` from that branch. Before Step 9, re-run the
eligibility script on the exact PR head (`git rev-parse HEAD` equals
`git rev-parse origin/release/vX.Y.N`; a later push re-opens items 4 and 5).
Then merge (never squash), Sam's "tag and push" (Step 9; its paired-release
deploy paragraph does not apply, because the engine pin is unchanged), Step 10
(release.yml publishes the identical nx code under the new version), Step 11, and
Step 11b's back-merge of `main` into `develop`.

The back-merge is not trivially clean in this lane (Step 11b's remark assumes the
release branch contained develop; this one did not). Expect conflicts in both
changelogs and the ledger, and resolve them by hand; there is no helper script.
On 7.87.1 only `CHANGELOG.md` conflicted: the ledger merged cleanly to develop's
side, because each cherry-pick's ledger conflict was resolved to the tag's side, so
main never changed the file. Clean is not done: delete the shipped entries and
move the `## Awaiting the next release (pinned: ...)` heading to vX.Y.N by hand.
Changelogs, in this order: develop's `## [Unreleased]` first, with the entries that
shipped in X.Y.N deleted from it, then main's `## [X.Y.N]` section, then the older
sections. `conexus/PENDING_RELEASE.md`: develop's side minus the entries that
shipped (the deferred ones and any plugin commit not picked stay pending). A
later develop edit to a file whose entry you removed still differs from the new
pin, so keep or re-add an entry for it; `tests/test_plugin_release_drift_ledger.py`
fails on an undeclared file or a stale entry. Commit the merge before running that
test: it compares committed HEAD with the pin, so before the commit it reports the
version surfaces as undeclared drift.

**7. Post-publish.** Once Step 11 shows PyPI at X.Y.N, on the laptop (it needs the
`claude` CLI), run Step 11c exactly as written (`tests/e2e/fresh-install-mvv.sh
--published X.Y.N` and the `--package-upgrade` leg with `NEXUS_TARGET_RELEASE`;
they are the only check of the published bytes), and in addition:

```bash
tests/e2e/plugin-delivery-proof.sh --prev X.Y.Z --new X.Y.N   # must print PLUGIN DELIVERY PROOF PASSED; exit 2 (UNVERIFIED) is not a pass
```

It installs the previous release in a scrubbed HOME, runs Claude Code's normal
plugin update and checks that the new plugin files arrived; with no `--source` it
also adds the real GitHub marketplace and checks every plugin's version and
`source.ref`. What it does not cover is a running session's in-session
auto-update. Then Step 12 (the `nx --version` check), and restart sessions
together, as after any release: a plugin update says "Restart to apply" and only a
restarted session loads it. No Windows guest leg.

For the first real use of this lane, paste the eligibility output, the battery
table and the delivery-proof verdict into bead nexus-rbmja.13.

The releaser is Sam: the AI prepares the branch and runs the gates; Sam says
"cut the release" and pushes the tag, or authorizes the AI in-session to push one
named tag (engine-release skill Step 4 states the rule).

## Common Mistakes

- **Bumping only `pyproject.toml` and missing the four plugin manifests.** CI parity check catches this late. Run the Step 8 pre-push check.
- **Skipping the integration suite.** Unit-only is what CI runs; integration is your last gate against keyed-API regressions before tag-push.
- **Running the full unit suite CONCURRENTLY with the E2E gates.** (2026-08-17, 7.9.0 battery.) The E2E gates parallelize safely among THEMSELVES, but their teardown phases (`nx daemon service stop --with-pg`, gate cleanup, the engine-substrate orphan sweep) share a machine-wide blast radius with the unit suite's self-provisioned engine substrates — the suite ran clean to ~75% then block-failed the moment a sandbox teardown fired. Sequence: E2E gates in parallel, the unit suite SERIAL (before or after). A block-shaped mass failure under concurrency is contention-shaped, but the quiet re-run of the failed set is mandatory evidence either way.
- **Using `gh release create` after `git push origin vX.Y.Z`.** Duplicate release. The Release workflow already creates one.
- **Forgetting `uv sync`.** `uv.lock` not updated; CI fails or local install resolves differently.
- **Forgetting `scripts/reinstall-tool.sh` after tag-push.** Local `nx` stays on old version; the post-merge "verify locally" step lies.
- **Pushing the tag before the version-bump commit.** Tag points at the wrong commit; the Release workflow's version-match step fails (`tag != pyproject.toml version`).
- **Running `uv publish` or `twine upload` manually.** The Release workflow handles PyPI via OIDC; manual publish either duplicates or fights the workflow.
- **Declaring "release done" after `git push origin vX.Y.Z`.** Step 11 (PyPI + GitHub release verification) is what closes the loop. The tag-push only TRIGGERS the workflow; if Py3.13 flakes, publish is skipped.
- **Closing follow-on beads as "done" before Step 11 confirms PyPI publication.** Bead-close before publish-verified == the publish-was-skipped class of failure goes undetected.

## See also

- `.claude/rules/releases-and-engine.md` § Cutting a release (a pointer to this file; this file is canonical for the procedure)
- `docs/contributing.md#release-process` (long form: the Break-glass subsection for retry / yank / revert / tag-retraction procedures, and the Schema/data-migration releases subsection Step 6d above draws its rationale from)
- `feedback_invoke_release_skill.md` (memory entry: invoke this skill, do not freehand)
- `feedback_post_release_reinstall.md` (memory entry: reinstall after tag)
- `feedback_release_discipline.md` (memory entry: full suite before tag, not after)
- `feedback_version_bump_manifests.md` (memory entry: all four manifests, CI enforces parity)
