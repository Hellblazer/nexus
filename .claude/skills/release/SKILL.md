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
0, 0c, 1, 2, 3, 4, 4a, 5, 6d, 6e, 7, 8, 8a, 9, 10, 11, 11b, 11c, 12, 12a.

**RDR-237 prerequisite (the release that first carries MinerU-only extraction).**
That release (every PDF through MinerU, the old extractor removed, no default-on MinerU
end-to-end check in PR CI) has one more prerequisite, checked before Step 1 and not
discovered at Step 11c: the RDR-237 Minimum Viable Validation (nexus-tr1rj.6, real
MinerU on the laptop and chas, numbers in T2 `nexus_rdr/237-mvv-<date>`) must be done
and its bead closed before the tag. After the publish, Step 11c's formula-PDF check on
the published bytes is required as well (default-on; a skip needs a recorded reason).
Later releases need only Step 11c's check.

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
  on develop, which no develop run checks), and when it is below
  `SENSITIVITY_EXCLUDE_MIN_ENGINE_VERSION` (RDR-243 Phase 2: a release must pin an
  engine with vectors-055, or every `nx_answer` search step is refused), and when it
  is below `RESOLVE_SENSITIVE_MIN_ENGINE_VERSION` (RDR-193 Step 6: a release must pin
  an engine whose `resolve_or_register` accepts `sensitive`, or every sensitive titled
  put and curator register is refused with a 400);
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

**The first-run gate (Sam 2026-11-10, nexus-f3yqd.6): no cut while a known
first-run or upgrade UX problem is untriaged.** Every finding lives in T2
`nexus/first-run-ux-triage`, one line each: `F<n> | <status> | <owner> | <reason>`.
The cut may proceed only when every line reads `fixed <sha>` or `accepted (Sam
<date>[, where he gave it])`. A `fix-<version>` line is open work for this release, and a
`proposed-accept` line is waiting on Sam; either blocks. A first-run finding
that turns up during the cut is added there before the cut goes on, never
noted elsewhere. Run it with the preflight:

```bash
t=$(nx memory get -p nexus -t first-run-ux-triage) || { echo "UNVERIFIED: the triage record could not be read"; exit 2; }
n=$(printf '%s\n' "$t" | grep -cE '^F[0-9]+ \| ')
[ "$n" -gt 0 ] || { echo "UNVERIFIED: no finding lines in the triage record"; exit 2; }
open=$(printf '%s\n' "$t" | grep -E '^F[0-9]+ \| ' | grep -vE '^F[0-9]+ \| (fixed [0-9a-f]{7,}|accepted \(Sam [0-9]{4}-[0-9]{2}-[0-9]{2}(, [^)]*)?\)) \|')
[ -z "$open" ] && echo "first-run gate clear ($n findings)" || { printf 'CUT BLOCKED, first-run findings not fixed or accepted:\n%s\n' "$open"; exit 1; }
```

The check passes only on the two closed statuses, so a misspelt or new status
blocks too.

**When this release changes the cut or release machinery itself** (`release.yml`,
the cut and floor scripts, the battery), rehearse the whole flow end to end in
isolation before the real run: a fake target (bare origin, fork, container) with
the real scripts and CI's real checkout shape (depth-1, explicit tag fetches,
event payloads, `GITHUB_ACTIONS` set), with the source's uncommitted work
committed into the sandbox so fixes are rehearsed before they land. A rehearsal
that passed on a full clone has not rehearsed CI. When you find the gap, build
the rehearsal now; do not file it (Sam, 2026-08-30, after the RDR-197 cut
refused three times). Source: memory `feedback_release_and_develop_ci`
§ release_and_ci_mechanics.

**At the cut announcement, list every finished item a peer is holding back from
this release, and ask Sam in one line whether each ships now (pulled in before
the cut) or waits.** Never accept a hold silently: what ships is Sam's call, and
a peer's caution about merge conflicts is a reason to sequence the work, not to
drop it from the release. On the 7.86.2 cut (2026-10-25) a peer held the
finished tool-spill plugin out of the window because it touched
`marketplace.json` and the cut tooling; the hold was accepted and Sam heard only
after the tag, so the plugin landed on develop pointing at v7.86.2, a tag that
does not contain it, and could not be installed (Sam: "wtf did the plugin just
sit there"). A plugin-only item that waits can still ship after the client tag
through the Plugin-only fast lane below: offer that. The retired
`plugin-vX.Y.Z-n` tags are not an option; do not revive them. Source: memory
`feedback_release_and_develop_ci` § peer_holdouts_from_a_release_go_to_sam.

### 1. Run unit + integration suite

```bash
tests/e2e/release-battery.sh         # preflight, then the legs below in ONE parallel run: per-leg logs, verdict table, exit 1 on any red; the unit suite still runs SERIAL to it
uv run pytest -n auto && uv run pytest -m lint   # unit suite and the lint bucket (two runs)
tests/e2e/local-service-gate.sh      # leg lsg: integration incl. the local-service functional gate
tests/e2e/migration-rehearsal/run.sh --package-upgrade   # leg pkgup: ONE-engine convergence MVV (nexus-cfgo9)
tests/e2e/fresh-install-mvv.sh       # leg mvv: VIRGIN-journey gate (nexus-nolqs) — see below
tests/e2e/constraints-resolve-gate.sh   # leg resolve: the shipped constraints.txt + overrides.txt RESOLVE (uv pip compile, no install): 22 compiles on Linux x86_64/aarch64 py3.12/3.13 (cpu; [pdf] also cu130 and cu126), Windows x64 and macOS 14 arm64, default and [pdf], plus a macOS 13 negative control (nexus-2k66t.4.4); network; needs a uv that knows --torch-backend cu130 and cu126 (the gate probes and says so); offline self-test tests/e2e/constraints_resolve_gate_test.sh
tests/e2e/plugin-out-of-repo-gate.sh --all   # leg plugins: the four plugin dirs run from a project outside any nexus checkout (nexus-z7ddd): the static pass plus a canary (a fixture plugin with random nonces that measures what Claude Code substitutes, judged on events not prose; five live runs, so UNVERIFIED is red: rerun, never waive); no per-target model sweep; where claude is absent the leg is --static-only, and its row AND the battery's closing line say "static pass only": run it again on the laptop; offline self-test tests/e2e/plugin_out_of_repo_gate_test.sh
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

**Battery rounds** (Sam, 2026-09-06, after a day of one stale pin per hour-long
gate; source: T2 `nexus/qo7ie.36-census-feedback_test_runs_before_push`). Run the
whole battery to completion and collect every red, fix once, then rerun only the
reds. Each leg is its own background job, so a red at minute 25 is triaged while
the rest run.

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

- Changelog fragments first: `python3 scripts/changelog_fold.py fold` moves every `changelog/unreleased/*.md` fragment under `CHANGELOG.md`'s `## [Unreleased]` (grouped by section, oldest first) and deletes the fragments; stage the deletions with `git add -- changelog/unreleased` (nexus-3ceda.2). On a release branch that cherry-picks rather than merges develop, fold only the fragments it carries.
- `CHANGELOG.md` (root): move `## [Unreleased]` content into a new `## [X.Y.Z] - YYYY-MM-DD` section. Leave a fresh empty `## [Unreleased]` at the top.
- `conexus/CHANGELOG.md` (plugin changelog): always update, even if no plugin changes (note: "Plugin version aligned with conexus X.Y.Z. No plugin-side changes." is acceptable).

**Resolving a `CHANGELOG.md` or `docs/wire-contract-pending.md` conflict (Step 7's merge of main, Step 11b, a landing's rebase): never keep both sides.** When one side EDITED an existing bullet, the conflict block holds the old and the new version of it, and concatenation ships both. On 2026-10-01 three landings resolved this way left develop with one nexus-z0o2p.24 bullet three times in two variants; on 2026-10-30 a scripted keep-both resolver across four rebases left six superseded bullets on develop's Unreleased section, once three generations of one bullet. Print each side's lines with a same-as marker (the leading `- **title**` or the cited sha), keep ONE copy of an entry present on both sides (the edited one), then check the result by bullet OPENING, because a line-level `uniq -d` passes on a rewritten bullet:

```bash
awk '/^## \[Unreleased\]/{f=1;next} /^## \[/{f=0} f && /^- \*\*/{k=substr($0,1,70); if (seen[k]++) {print "DUPLICATE OPENING: " k; bad=1}; n++} END{print n+0 " bullets examined"; exit bad}' CHANGELOG.md
```

When a pair shares an opening, keep the newest version (the one carrying the latest round's wording). `0 bullets examined` is a pass over nothing, not a clean Unreleased. Since changelog fragments (nexus-3ceda.2) develop's `## [Unreleased]` holds no bullets, so run this after `changelog_fold.py fold`, or on the release section, not on develop's tip.

For `docs/wire-contract-pending.md`, the same keep-both slip duplicates ledger entries and their cited shas. Check the entry lines after the resolution:

```bash
F=docs/wire-contract-pending.md
echo "$(grep -cE '^- `[0-9a-f]{7,40}`' $F) entries examined"
grep -E '^- `[0-9a-f]{7,40}`' $F | sort | uniq -d                                     # must print nothing: an exact duplicate entry
grep -oE '^- `[0-9a-f]{7,40}` -- bead [^ ]+' $F | sort | uniq -c | awk '$1>1'         # a rewritten entry shows here
```

One commit can carry several entries for one bead (a commit that adds four routes has four), so the last line is a list to compare with each side's counts, not a must-be-empty test; the one above it must be empty. Source: memory `feedback_release_and_develop_ci` § changelog_keep_both_duplicates.

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

### 6e. Upgrade rehearsal from the user's seat (BLOCKING, nexus-f3yqd.5)

Why (Sam, 2026-11-10): two upgrades to 8.0.0 each rewrote the local store for about
20 minutes at the first engine boot, with nothing on screen, and no gate looked at
what a user sees. This one takes a real install of the previous release with a
sizeable local store, upgrades it the way a user does, and times what the user
waits through.

Run it after Step 3's version bump, in the release worktree, on chas. It outruns a
10-minute foreground call, so start it as a background command with its output in a
file, and read the file when the notice comes (never poll):

```bash
lab-remote -c lab-m -- tests/e2e/upgrade-rehearsal.sh > /tmp/upgrade-rehearsal-X.Y.Z.out 2>&1   # run_in_background
```

It starts from two or three previous releases, each in its own work dir with its own
step table and its own `UPGRADE REHEARSAL START (from X)` line: the newest published
version below the candidate (8.0.0 for an 8.1.0 cut), the last published 7.x (7.99.0),
and 7.76.0, the last release before per-collection indexes (PCI), whose upgrade boot
builds every `pci_` index. The first two are one start when they are the same release
(an 8.0.0 cut starts from 7.99.0 once), and 7.76.0 is left out when the candidate is
not newer than it. For each start it:

1. installs that release and builds its local store with the f3yqd.1 seeder
   (`scripts/upgrade_boot_writes.py --prepare-only`: 86,000 knowledge rows, about
   2.4 GB; the 7.76.0 start seeds five large collections instead, about 140,000
   rows), then starts its engine;
2. upgrades the package to the candidate wheel built from the worktree
   (`NEXUS_TARGET_RELEASE=X.Y.Z` uses the published version instead), and checks that
   `nx --version` names the candidate;
3. runs `nx upgrade`, `nx doctor` and `nx search` for the seeder's sentinel note.

Each of those steps runs under a pty and FAILS when it prints nothing for 10 s, runs
past its ceiling, or exits non-zero. The 10 s is our chosen reading of Sam's "a few
seconds", not a number he gave; it stands until he confirms it. The ceilings are
per start, measured on 2026-11-10 on chas lab-m:

- `nx upgrade` from the newest release below the candidate: 120 s (2.3 s from 8.0.0).
- `nx upgrade` from the last 7.x: 120 s (13.9 to 14.2 s from 7.99.0).
- `nx upgrade` from 7.76.0: **PENDING**. Measured 460 to 662 s on develop d258282c6
  and 211 s with the rung-scan fix e979863a6 (nexus-mjmjl, in conexus 8.1.1), on
  140,000 rows in five collections (2.6 GB). nexus-f3yqd.5 owns the number and sets
  it from a develop run after that fix lands, with its margin and the store size
  stated; no guessed value stands in until then. Until it is set this start cannot
  pass: it is reported PENDING and the gate line says NOT RUN (exit 2), naming the
  pending ceiling. A release does not go past 6e on that: Sam decides.
- `nx doctor` 60 s (7.5 s), `nx search` 30 s (2.8 s), package upgrade 120 s (0.4 s).

The margins are not uniform. Doctor, search and the last-7x upgrade sit at about 8 to
11 times the slowest measured; the newest-below upgrade ceiling is about 50 times its
2.3 s and the package ceiling is 300 times its 0.4 s, so those two are floors against
a hang, not tight margins. All of them bound this fixture's size and say nothing about
a store many times larger, where a silent stretch grows with the data.

A run also fails when it checked nothing: PG's own row count (read through the
install's psql) under 50,000 before or after `nx upgrade`, or fewer rows after than
before; a step not run; a cross in doctor; the sentinel not found; a `DEFERRED` rung;
or a PCI watcher whose PG probe failed more than ten times.

Around `nx upgrade` the driver reads the install's own PG (`pg_current_wal_lsn`,
`pg_stat_wal`, `pg_stat_io`) and records the WAL bytes, seconds and MB/s per start,
the second reading taken when `nx upgrade` returns (MB = 2**20 bytes), in each table
and `verdict.json`. The bytes an upgrade writes scale with the store, so at the
measured MB/s a store of S GB spends about S*1024*r/MBps seconds on that work, r being
the run's WAL bytes over store bytes (also recorded); the 2.4 GB fixture bounds only
itself.

The 7.76.0 start adds the PCI keep check, made DURING `nx upgrade` (which waits
for every `pci_` build before it returns, so a restart after it proves nothing).
A watcher polls from before the upgrade; on the first `pci_` build in flight while
`nx upgrade` still runs, a second thread restarts the engine as a user in a second
shell does (`nx daemon service stop`, then `start`, PG left running) and records the
(index, pid, progress), the restart's outputs and exit codes, and what `nx upgrade`
did afterwards (judged by its own step rules). Never seeing a build in flight during
`nx upgrade` is a failure, not a pass. After the search step the driver waits (up to
900 s) for every `pci_` index to be valid and judges by backend pid, from the moment
the restart finished: the in-flight build counts as kept only if the same pid is seen
building after the restart AND the engine log carries a `pci_anchor_adopted` line with
that index AND the index ends valid (a build also leaves the progress view when the
next boot terminates it, so pid continuity alone proves nothing); each index is built
by exactly one pid, every index ends valid, and the engine log carries no
`pci_index_dropped` (failed_build), `pci_index_build_not_done` (CONNECTION_LOST) or
`pci_ddl_failed` (57P01) after the restart. A rebuild under a new pid is the failure
it catches. To run it against an engine newer than the wheel's pin, add
`--engine-jar PATH` (or set `NEXUS_SERVICE_JAR`); that run is not the release gate.

The last line is the gate's own, `UPGRADE REHEARSAL GATE: <word> ...`, and the only
one the wrapper reads (per-start lines say `UPGRADE REHEARSAL START`). The exit code
says the same:

- exit 0, `PASSED`: every start passed and no non-default setting was used.
- exit 1, `FAILED`: a breach, a failed step, a non-vacuity finding, or a postmaster
  that survived the run. A run that ends with no gate line is FAILED too.
- exit 2, `NOT RUN`: the harness could not start, a start's ceiling is PENDING, or the
  candidate wheel's version is already published on PyPI (the bump was skipped); or
  `NOT THE RELEASE GATE`: every start passed but a flag changed what is judged
  (`--standin`, `--rows-floor`, `--silence-limit`, any `--*-ceiling`, `--prepare-cmd`,
  `--no-pci-restart-check`, a single `--from-version`, `--engine-jar`).

Only exit 0 clears 6e. A FAILED is a release blocker, never waived here: a silent or
slow step is what the user sees. NOT RUN and NOT THE RELEASE GATE are not passes
either, and a release does not proceed on them; while the 7.76.0 ceiling is pending,
Sam decides what the release does. A failure keeps its step logs and engine and PG
logs on chas. Paste each start's step table into the cut bead.

A setup failure is not a judged breach. When a start's install, prepare, engine start
or version check fails (the network, PyPI, a stuck download), the start reports a
`setup step ... failed` finding and every later step carries that reason; the gate
line is FAILED (exit 1) but nothing about the candidate was judged. Rerun the gate;
the same setup failure twice is a blocker to run down before the cut. A judged breach
is a `package-upgrade`, `upgrade`, `doctor` or `search` step that says silent, ceiling
or failed, or a non-vacuity or PCI finding: that is the candidate, and rerunning
without a fix proves nothing.

Wall time, from the measured parts: about 50 to 75 minutes. Preparing the store takes
about 13 minutes for each 86,000-row start and about 20 minutes for the five-collection
7.76.0 start; `nx upgrade` takes 2 to 14 s from the other starts and 211 to 660 s from
7.76.0; the PCI end wait adds up to 900 s; installs and the wheel build come on top.

The plugin-only fast lane does not run it: that lane builds off the base tag, whose
tree has no gate, and ships a wheel and engine that are the base tag's but for the
version.

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
wasted round: check first. Opening a PR, or pushing to a branch that has one, IS
a long run for this reason: PR #1597 (7.75.0) was opened before its pinned
engine-service-v0.1.154 existed and all 12 test shards failed on the PG-bundle
download.

**A paired release whose engine range carries a data-effecting changeset needs
its relay attestation in THIS branch's tree.** Run
`uv run python scripts/list_data_effects.py <prev-engine-tag> <engine-tag> --record-relay-attestation`
(it writes `docs/data-effect-relay/<engine-tag>.json`) and commit the file on
the release branch, in the release commit, before the client tag. `release.yml`
and its `workflow_dispatch` retry both check out the tagged tree, a tag is
immutable, so a file landed on develop or after the tag can never satisfy the
check for that tag. On 2026-10-21 v7.83.0 was tagged with the attestation only
on develop: `--paired-deploy-auto` refused ("no relay attestation"), PyPI got
nothing, the fix was a whole new release (7.83.1, PR #1618), and main's
marketplace pinned an unpublished v7.83.0 in between. v7.97.0 refused the
same way (its tag was re-pointed, per git, at 2026-11-07 00:56 UTC). The battery does not run this check; only the release
workflow does. So before handing over the PR, in the release worktree, re-run
Step 0's `--paired-deploy engine-service-vX.Y.Z` form (it runs the same relay
check; so does the workflow's `--paired-deploy-auto` whenever the cloud is
still below the floor).

**The release tree carries no engine source past the pinned tag.** The floor
gate's source-ancestry arm refuses a tree whose `service/src/main` differs from
the pinned engine tag. A release branch based on develop can carry such source,
so before stacking beads on it run
`git diff --stat engine-service-vX.Y.Z HEAD -- service/src/main`: anything
non-empty waits until after the client tag. (7.45.0, 2026-09-14: two beads
stacked on the release tree were refused by the early preflight and pulled.) A
patch release with no new code is based on main, not develop. The same arm runs
in the post-tag bare verify (Steps 0 and 12), and a develop checkout after the
release has engine source past the pin: run the post-tag verifies from a
worktree at the client tag. A client that ships with engine source its pinned
engine lacks has features its engine does not (the 7.1.0/v0.1.62 inversion).

Per the marketplace-pinned-source playbook (also used by `Hellblazer/palinex`), release commits go through a PR. CI gates the bump before it lands on main. No more direct-to-main exception.

```bash
# Base the release branch on DEVELOP, not main: a release PROMOTES develop's
# accumulated state to main (CLAUDE.md: "releases promote develop to main via
# merge"). Branching off main would omit develop's unmerged fixes, so the
# release PR must carry develop's commits + the version-bump commit.
git checkout develop && git pull
# Read develop's tip CI before cutting on it; a red is diagnosed first, never
# carried into the release PR (7.46.0: fix, rebase, force-push, second PR cycle).
gh api repos/Hellblazer/nexus/commits/$(git rev-parse HEAD)/check-runs --jq '.check_runs[] | [.name, .conclusion] | @tsv'
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
gh pr create --base main --title "release: conexus X.Y.Z" --body-file <body.md>
```

The body follows `docs/contributing.md` § Git Workflow: two or three plain sentences first (what this release changes for a user, and anything a reviewer must check), any `Closes #N` lines, then the full release notes inside `<details><summary>Details</summary> ... </details>`.

A PR body closes an issue only with `Closes #N`, `Fixes #N` or `Resolves #N`, one per line; a plain reference leaves the issue open (PR #254). If the release PR is meant to close issues, write the keyword lines in the opening lines of the body file.

Wait for CI green, then merge:

```bash
gh pr merge <N> --merge   # NOT --squash — preserves the chore(release) commit verbatim; NEVER --auto (see below)
git checkout main && git pull
```

Never `gh pr merge --auto` on a release PR: it fires when the required checks pass, including while a non-required check is red, and squashes only the tip at that moment, orphaning later pushes (three times, 2026-05 to 2026-06). Merge by hand, after CI is green.

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

Read completion from the check-run `conclusion` (or the run's own status). The jobs API and `statusCheckRollup` can show a job in progress forever on a completed run, and a wait on them never fires (7.6.0, 2026-08-11). A run that sits at `waiting` is a deployment-environment approval, a person's click, never runner backlog: find the pending environment and tell Sam (`.claude/rules/ci-and-runners.md` § GitHub Actions traps).

One job must complete: `Build and publish to PyPI`. (2026-07-06 CI-cost
pass, PR #1375: the tag-time pytest matrix was removed — the tag points at a
main merge commit whose identical tree just passed the release PR's required
checks, so the re-run was the same tree's fourth test pass and made publish
hostage to the nexus-9eaz GHA-flake family. The publish job still verifies
tag == pyproject version before building.)

**A burned tag means the local battery has a shape gap.** Do not move the tag
yourself (`docs/contributing.md` § Break-glass: a failure in the pipeline retries
through `workflow_dispatch`; a failure in the tagged tree is fixed by the next
version, as 7.83.1 was; re-pointing a tag is an admin decision, as the
re-point of v7.97.0 on 2026-11-07 (00:56 UTC, per git) was, after its run refused for the missing
attestation and published nothing). Before cutting that next version, reproduce
the failed job's exact commands locally, fix everything that shape surfaces, not
only the visible line, then cut once. Each cut that fixes only the visible line hits the next
step of the same job: v0.1.101, v0.1.102 and v0.1.103 burned in one afternoon on
three build-only defects of one class ("It's ALWAYS just one more thing", Sam,
2026-09-05). Then build the shape into a rehearsal leg (nexus-xihsm) rather than
filing it. Source: memory `feedback_release_and_develop_ci`
§ release_and_ci_mechanics.

### 11. Verify release landed

```bash
gh release view vX.Y.Z
curl -s "https://pypi.org/pypi/conexus/json" | \
    python3 -c "import sys,json; print(json.load(sys.stdin)['info']['version'])"
```

Both must report `vX.Y.Z` / `X.Y.Z`. **Do not declare done before this check passes.** PyPI publish can be skipped if the Release workflow's pytest fails; the tag alone does not guarantee publication.

The aggregate JSON above lags the simple index (7.48.0 and 7.49.0 both): a still-old `info.version` is not a pass, and not yet a failure. Re-read after a few minutes; once `https://pypi.org/simple/conexus/` serves the wheel, rerun any check that read the aggregate (`tests/e2e/fresh-install-mvv.sh --published X.Y.Z` is the usual candidate) rather than treating the first red as a publish failure.

### 11b. Back-merge main into develop (MANDATORY — ends the stale-develop class)

```bash
git checkout develop && git pull
git merge origin/main --no-edit    # conflict-free right after a release (the release
                                   # branch just CONTAINED develop), but clean is
                                   # NOT correct: check CHANGELOG.md below first
```

**Check the CHANGELOG section before the push** (7.87.0, 2026-10-26: git
auto-merged `CHANGELOG.md` with no conflict, but the release commit had inserted
`## [X.Y.Z]` right under `## [Unreleased]`, so every entry develop added after
the cut landed under the release heading; the release section then claimed code
the tag does not carry; fixed in ed04993f2 after a peer caught it). Diff develop's
`## [X.Y.Z]` section against the tag's:

```bash
diff <(git show vX.Y.Z:CHANGELOG.md | sed -n '/^## \[X.Y.Z\]/,/^## \[[0-9]/p') \
     <(sed -n '/^## \[X.Y.Z\]/,/^## \[[0-9]/p' CHANGELOG.md)   # must be empty
```

Move every extra entry (with its `###` heading) back under `## [Unreleased]`
in this same merge commit. The fast lane below hand-resolves this merge, but its
CHANGELOG order carries the same risk: run this check there too.

```bash
scripts/git-push-develop.sh --allowed-paths-from-range HEAD
# step 1 prints the files and a digest and pushes nothing; read the list
# (it should be the release's bump files and main's release commits), then:
scripts/git-push-develop.sh --allowed-paths-from-range=<digest> HEAD
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

The first two commands below drive the just-published PyPI bytes, not the working
tree — "identical tree" is an argument that the pre-tag battery already ran
this; it is not a run of the actual published artifact. The third drives the
published plugins as the marketplace installed them. Run them once Step
11 confirms PyPI shows the new version.

```bash
tests/e2e/fresh-install-mvv.sh --published X.Y.Z    # FRESH-install axis, published bytes
NEXUS_TARGET_RELEASE=X.Y.Z tests/e2e/migration-rehearsal/run.sh --package-upgrade   # UPGRADE axis, published bytes
tests/e2e/plugin-out-of-repo-gate.sh --installed X.Y.Z --sweep   # PLUGIN axis, installed marketplace copies: static pass over the installed copies + the fixture canary (it does not load them) + the advisory per-target sweep (laptop: needs claude)
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

**Formula-PDF / MinerU coverage (default-on, required; nexus-gqrg0 GH #1533,
nexus-tr1rj.20 RDR-237).** The pre-tag battery's resolver-bound lint
(`tests/test_install_source_wiring_pins.py`) covers the LOCAL wheel's dependency
resolution only. The `--published` invocation above also proves that the
PUBLISHED dependency resolution indexes a real formula PDF through MinerU
(`tests/fixtures/bft-to-smr.pdf`, `--extractor mineru`) and that the added chunk's
`extraction_method` is literally `mineru`. It installs `conexus[pdf]` for that, so the
plain command above runs it; the summary block above the PASSED sentinel (two lines up,
with the leg 9b line between) reads `formula-PDF check (MinerU, published bytes): ran`.

Since RDR-237 every PDF goes to MinerU, PR CI carries no MinerU end-to-end check by
decision, and the shakedown that used to cover it was deleted in cleanup step 11.
This run is the release's only MinerU end-to-end coverage, so a release is not closed
out without it. It runs against the published bytes, after the tag and publish, so a red
result cannot stop the ship: it means yank or fix forward, and it goes to Sam. It pays a MinerU pipeline model download (~2-3 GB) on a cold cache.

A skip is allowed only with a recorded reason, the way a skipped slim leg is named in
the summary and a pass-by-default gate is named in the ship record:

```bash
NX_MVV_FORMULA_PDF_CHECK=0 NX_MVV_FORMULA_PDF_SKIP_REASON='<why>' \
    tests/e2e/fresh-install-mvv.sh --published X.Y.Z
```

Without the reason the script refuses (exit 2) before it does anything. With it, the
summary line reads `formula-PDF check (MinerU, published bytes): SKIPPED (reason: <why>)`;
copy that line and the reason into the ship record. A skip is a release decision: take
it to Sam. The first release that carries RDR-237 also needs the Minimum Viable
Validation done before the tag (see the prerequisite near the top of this file).

**Plugin out-of-repo gate (nexus-z7ddd).** Every plugin check before it ran inside the nexus
checkout, where a relative `../resources` link, `conexus/registry.yaml` and the repo scripts all
open; a user's project has none of them. Two runs, with different standing:

- **The pre-tag battery's `plugins` leg is the static pass plus a live canary.** `--all` runs the
  static pass over the four plugin directories of the checkout (every file reference classified;
  the allowlist and its stale check; deterministic) plus a canary: a fixture plugin the gate builds
  with random nonces, five short `claude -p` runs judged on the Read events, the text Claude Code
  delivered and each run's init event. The model's prose is not consulted, except that a session
  has to print its nonce. The canary loads its own fixture through `--plugin-dir`; it does not
  measure the plugins as the marketplace installed them, and whether the two substitute alike is
  not measured. It measures what the static classes assume (the plugin-root variable is
  substituted in an agent, a skill's SKILL.md and a command, and is NOT substituted in a file the
  model Reads, in an agent's frontmatter description, or when written without braces; a skill's
  base directory is announced and a relative link resolves against it) and fails when Claude Code
  says otherwise. A model that skips a step leaves a measurement missing: that session is retried
  twice with a pause, and then the leg is UNVERIFIED. UNVERIFIED is red: rerun the leg (an API
  error, a rate limit or an outage looks the same), never waive it. No per-target model sweep is
  in the battery. On chas or in CI (no `claude`) the leg is static only, and both its row and the
  battery's closing line say so: run it again on the laptop.
- **`--installed X.Y.Z --sweep` after publish is advisory.** It runs the static pass over what the
  marketplace delivered, `~/.claude/plugins/cache/nexus-plugins/<plugin>/X.Y.Z` for
  `conexus`, `sn`, `conexus-tool-spill` and `conexus-prose-edit`, plus the same fixture canary
  (the canary does not load the installed copies), then the sweep: one `claude -p`
  per agent and loadable skill, the model quoting and Reading each reference. Update the
  marketplace first (`/plugin marketplace update nexus-plugins`, then update each of the four) so
  all four directories exist at X.Y.Z, with a `plugin.json` that names that plugin and that
  version; a plugin missing there, mislabelled or hollow fails the run, it is never skipped.

Read the verdict as follows:

- Expected shape: `conexus` and `conexus-prose-edit` passed, `sn` (hook-injected sections only) and
  `conexus-tool-spill` NOT-APPLICABLE. The gate pins this: a `conexus` that is NOT-APPLICABLE, or an
  `sn` with no hook markdown, is a hollow plugin and a failure.
- The sweep fails only on definite evidence from events (a Read error on a path that exists, a Read
  given the literal variable or a relative path from an agent, a path opened from a nexus checkout,
  an init event that shows another plugin, an MCP server, other tools or a non-default permission
  mode). What the model only quotes is not evidence. A target it could not probe is UNPROBED:
  counted and named, retried twice, never red. Below its coverage
  floor (at least one opened reference per mechanism) it is UNVERIFIED, exit 2.
- Exit 2 (`UNVERIFIED`) is not a pass. `(static pass only)` in a verdict means `claude` was absent.
- The live runs are isolated from the real install, as far as the gate observes: the plugin under test
  runs from a scratch copy with its hooks emptied and its agents narrowed to Read, `NX_UPGRADE_AUTO=0`,
  scratch tool, cache and config directories and an empty plugin registry, and every run's init event is
  checked for any other plugin, MCP server or tool. The sign-in is read from the real HOME, and
  `~/.claude.json`, the plugin cache and the model's own context (`~/.claude/CLAUDE.md`, rules) are not
  observed. Claude Code files a project directory under `~/.claude/projects` for each run; the gate
  removes its own (only that one, only when it holds nothing but subagent meta files), and
  `plugin-out-of-repo-gate.sh --clean-residue` lists and removes earlier runs'. If a gate run ever leaves
  a `gen-*` directory under `~/.local/share/nexus/tools`, that is a gate defect.
- A red result is a plugin defect in the published copy: fix forward as a plugin-only patch release
  and take it to Sam. A reference that must stay as written goes in
  `tests/e2e/plugin-out-of-repo-allowlist.txt` with a written reason; that file ships empty, and a
  stale entry fails the gate.

Logs for a run you want to keep: add `--out <dir>` (one subdirectory per plugin holds the
stream-json events and `gate.log`).

### 12. Reinstall local tool and verify

**After a release the sessions hold, then move together** (Sam, 2026-10-22: he
wants every session on one version). While other sessions are live, nobody runs
`nx upgrade`, `scripts/reinstall-tool.sh` or `claude plugin update`, and no
session restarts itself, this one included: mixed nx and plugin versions across
sessions hit hook and verb skew, and a floor-bumped client spawned before the
cloud engine is deployed gets the managed service refused (GH #1402). One run
does all of it, so hand Sam that command instead of reinstalling only this
session's nx:

```bash
~/.local/bin/claude-restart-sessions --ask-ready      # asks every idle session whether it can restart; read the replies with --status
~/.local/bin/claude-restart-sessions --nx X.Y.Z --dry-run   # read it, then the same line without --dry-run
```

`~/.local/bin/claude-restart-sessions` is a symlink to `scripts/claude-restart-sessions`;
its docstring is the reference for every mode (`--nx`, `--fresh`, `--ask-ready`,
`--status`, `--revive`). `--fresh` has a cost: the peer-name lease stays held by
both session ids for up to 300 s after each restart, so `mailbox_send` by name is
refused while by id works, and anything keyed on the old id (its mailbox, its T1
board subscriptions, unflagged scratch) is orphaned; the log names each old id
for `claude --resume <id>` back-out. With `--nx` it runs the engine gate (re-reading `/version`
every 30 s up to `--engine-wait`, default 1800 s, so a paired release's redeploy
can land; `--skip-engine-gate` on a local-mode box), then `uv cache clean conexus`
plus `reinstall-tool.sh conexus==X.Y.Z`, then the marketplace and plugin update,
and restarts nothing unless every step holds. The pin and the cache clean exist
because a bare `conexus` spec resolved from uv's cached index and built two
generations of 7.69.0 minutes after 7.70.0 published (2026-10-04): read
`nx --version`, never the "built generation" line. Before handing Sam the
command:

- Before `--ask-ready`, post the upgrade hold on the board and message every
  live peer: hold, stop ALL background shells and agents with TaskStop
  (finished agents' included, not only foreground runs), then reply READY.
  The releaser cannot stop a peer's shells, and `--ask-ready` types only to an
  idle session, so a session pinned at `shell` is never asked and is skipped
  after `--wait` (kimi_nexus_edb, 2026-10-26).

- Read the dry run's `-n` values against ListAgents. A restart keeps each
  session's CURRENT name (Sam, 2026-10-31); before the script dropped the launch
  `-n`, every `/rename`d session would have come back as `nexus_7ff`.
- A session pinned at status `shell` is not restarted (the script waits for idle
  and skips it after `--wait`, 600 s). Live background shells keep that status
  even when the prompt is idle, finished background agents' shells included
  (footer "N shells"): stop stale agents' shells (TaskStop) and check every
  pane footer for "shells". `--force` restarts such a session anyway.
- Afterwards read `~/.claude/restart-sessions.log` for `N of N resumed`, and
  `nx --version` must print X.Y.Z. After a reboot the restart mode finds nothing
  (it needs live pids); `claude-restart-sessions --revive [--dry-run]` resumes
  the sessions whose transcripts were written in the 30 minutes before boot.

Source: memory `feedback_release_and_develop_ci` § coordinated_upgrade_restart.
The rest of this step is what that run does to this box, and the manual form
for a box with no live sessions.

**CLOUD-MODE BOX GATE (nexus-1emxn (c)):** on a cloud-mode box, run the
post-tag floor verify FIRST (the bare form):

```bash
uv run python scripts/check_engine_release_floor.py
```

Exit codes, precisely:
- **0** — cloud current; go ahead with the run above (or the reinstall below, with no other session live).
- **1** — cloud still BEHIND the new floor: new spawns after this
  reinstall would REFUSE the managed service (the GH #1402 class, on your
  own box). Wait for the deploy to land, re-verify, then reinstall.
- **2** — cannot verify (network/API): fix connectivity and re-run; not
  evidence either way.

Local-mode boxes skip this gate — their engine converges from
`PINNED_SERVICE_TAG` at reinstall.

Run this verify from a worktree at the client tag, not a develop checkout: the
source-ancestry arm compares `service/src/main` with the pinned engine tag, and
develop has engine source past it.

The manual form, for a box with no other live session (the coordinated run does
exactly this install):

```bash
scripts/reinstall-tool.sh    # preserves [local] and other extras; the PDF stack (mineru, torch) is the on-demand [pdf] extra: NX_REINSTALL_EXTRAS=pdf scripts/reinstall-tool.sh keeps it
nx --version                 # must print X.Y.Z
```

`pyproject.toml` bumps the project version, but the local `nx` shim does not point at a wheel: it resolves `<tools>/current` at spawn time and execs the generation that pointer names, so until this step flips `current` every new spawn still lands in the old generation (and existing holders keep running from theirs afterwards). Caught on v4.9.11: `nx --version` reported 4.9.10 even after PyPI showed 4.9.11.

### 12a. Memory curation report (RDR-238; report only, never blocks)

Once Step 12's restart has every session on the new version, run the curation report
in one session, with the reinstalled `nx`. With no other session live, run it as soon as
`nx --version` prints X.Y.Z. If Sam defers the restart, skip the step and say so in the
ship record: a long run would hold a session at `shell` through the restart round.

Start the command with the Bash tool's `run_in_background`, with the redirect around the
whole group so the `|| echo` line lands in the file too, and read the file after the
completion notice. Do not run it in the foreground: a foreground Bash call is cut at 10
minutes (2 by default) and the run takes about 5, so the tool would end it or hold the session.

```bash
{ nx memory curate --files --t2 --agents --cold || echo "curation step skipped (exit $?); the release goes on"; } > /tmp/curation-report.txt 2>&1
```

It prints the report and stores it in T2 as `<repo>/curation-report-<date>-files-t2-agents-cold`
(90-day TTL; the `-cold` title keeps a later default run from replacing it): memory files that
are unindexed, oversized or dangling, T2 rows and reviewer
memory to look at, and (`--cold`) memory files, T2 namespaces, MCP tools, CLI verbs
and nested CLAUDE.md files with no write and no use in 30 days. With `--files` and
`--cold` together it reads every transcript of the window four times (`--files` once
for read counts, `--cold` three times), so it takes a while: 305 s on the laptop
on 2026-11-09, over a 30-day window. If the notice has not come
after 30 minutes, stop it (kill the one pid you started and checked with `ps`, never by
pattern), say so in the ship record and finish the release.

This step never blocks the release. The command exits 0 whatever it finds, and a
section that cannot run prints a "not available" line (`--cold` prints one per kind)
while the others still run. A last line "report not stored" means the T2 write failed:
there is nothing to link, so say that in the ship record. A non-zero exit or "No such command" (in the plugin-only lane the local `nx` can
predate the verb) is reported in the ship record, and the release is still done.
The report changes nothing: retiring what it lists is a person's decision and rides
its own change, so link the report from the ship record and leave the list to Sam.

## Plugin-only fast lane

A patch release that ships plugin-surface content (`conexus/`, `sn/`,
`tool-spill/`, `prose-edit/`, `.claude-plugin/marketplace.json`) while `develop` carries `src/`
work that must not ship yet. It is a client release: same tag form, same
`release.yml` publish, same version surfaces. This section lists only what
differs; every step it points at runs exactly as written above. Design record:
T2 `nexus/design-plugin-only-fast-lane.md`.

Base tag is vX.Y.Z (the last client tag), the new release is vX.Y.N with N = Z+1.
Steps 0 (beyond the floor check in item 4 below) and 6d do not apply: the engine
pin does not move and no migration ships. Step 6e does not run either (its last
paragraph says why).

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

A picked commit may carry a root changelog fragment (`changelog/unreleased/`,
nexus-3ceda.2). When the base tag predates fragments (`git cat-file -e
vX.Y.Z:scripts/changelog_fold.py` fails), its eligibility script does not free that
path: pick with `git cherry-pick -n -x <sha>`, `git rm` the fragment, commit, and
write its bullet into the vX.Y.N section by hand at Step 4. When the tag has the
script, Step 4's `changelog_fold.py fold` runs on the branch as written. Either
way the back-merge into develop deletes the shipped fragments (below).

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
`gh pr create --base main` from that branch (body shape as in Step 9: short opening, keywords, then `<details>`). Before Step 9, re-run the
eligibility script on the exact PR head (`git rev-parse HEAD` equals
`git rev-parse origin/release/vX.Y.N`; a later push re-opens items 4 and 5).
Then merge (never squash), Sam's "tag and push" (Step 9; its paired-release
deploy paragraph does not apply, because the engine pin is unchanged), Step 10
(release.yml publishes the identical nx code under the new version), Step 11, and
Step 11b's back-merge of `main` into `develop`.

The back-merge is not conflict-free in this lane (Step 11b's remark assumes the
release branch contained develop; this one did not), and Step 11b's CHANGELOG
section diff against the tag applies here too. Expect conflicts in both
changelogs and the ledger, and resolve them by hand; there is no helper script.
On 7.87.1 only `CHANGELOG.md` conflicted: the ledger merged cleanly to develop's
side, because each cherry-pick's ledger conflict was resolved to the tag's side, so
main never changed the file. Clean is not done: delete the shipped entries and
move the `## Awaiting the next release (pinned: ...)` heading to vX.Y.N by hand.
Changelog fragments: a cherry-picked fragment was added and folded on main inside
this range, so the merge does not delete develop's copy; `git rm` each
`changelog/unreleased/` fragment that shipped in X.Y.N in this merge, or the next
release folds it again. Changelogs, in this order: develop's `## [Unreleased]` first, with the entries that
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
restarted session loads it. Restart with `scripts/claude-restart-sessions`: ask
first with `--ask-ready` and read the table with `--status`. The restart itself
needs that round: it refuses (exit 3) with no round, a malformed or old one, or
any target without a fresh ready reply that is idle now (the session running the
script is exempt from idle only), and checks again after the install and before
each session's exit. Name the mode: after a `--fresh` run a bare restart is
refused until you pass `--fresh` or `--resume`. `--force` overrides the round.
No Windows guest leg.
After the restart, run Step 12a (the curation report); it tolerates a local `nx`
that predates the verb.

For the first real use of this lane, paste the eligibility output, the battery
table and the delivery-proof verdict into bead nexus-rbmja.13.

The releaser is Sam: the AI prepares the branch and runs the gates; Sam says
"cut the release" and pushes the tag, or his go for the named release lets the AI
push its tags once their gates are green (engine-release skill Step 4 states the rule).

## Common Mistakes

- **Holding a release on the curation report (Step 12a).** The report never blocks: a red section, a non-zero exit or a missing verb goes in the ship record and the release is done. Acting on what it lists is Sam's decision, not a release step.
- **Freehanding a version surface or a tag from memory of AGENTS.md instead of invoking this skill.** It skips the pre-push and post-publish verification that catches a failed publish (v4.32.13: the tag's workflow failed, PyPI was skipped, and the release was declared done anyway). Invoke `release` (or `engine-release`) before touching either.
- **Bumping only `pyproject.toml` and missing the four plugin manifests.** CI parity check catches this late. Run the Step 8 pre-push check.
- **Skipping the integration suite.** Unit-only is what CI runs; integration is your last gate against keyed-API regressions before tag-push.
- **Running the full unit suite CONCURRENTLY with the E2E gates.** (2026-08-17, 7.9.0 battery.) The E2E gates parallelize safely among THEMSELVES, but their teardown phases (`nx daemon service stop --with-pg`, gate cleanup, the engine-substrate orphan sweep) share a machine-wide blast radius with the unit suite's self-provisioned engine substrates — the suite ran clean to ~75% then block-failed the moment a sandbox teardown fired. Sequence: E2E gates in parallel, the unit suite SERIAL (before or after). A block-shaped mass failure under concurrency is contention-shaped, but the quiet re-run of the failed set is mandatory evidence either way.
- **Using `gh release create` after `git push origin vX.Y.Z`.** Duplicate release. The Release workflow already creates one.
- **Forgetting `uv sync`.** `uv.lock` not updated; CI fails or local install resolves differently.
- **Forgetting the coordinated install after tag-push (Step 12).** Local `nx` stays on the old version; the post-merge "verify locally" step lies. The opposite slip is also a mistake: running `scripts/reinstall-tool.sh` or `nx upgrade` in one session while others are live.
- **Pushing the tag before the version-bump commit.** Tag points at the wrong commit; the Release workflow's version-match step fails (`tag != pyproject.toml version`).
- **Running `uv publish` or `twine upload` manually.** The Release workflow handles PyPI via OIDC; manual publish either duplicates or fights the workflow.
- **Declaring "release done" after `git push origin vX.Y.Z`.** Step 11 (PyPI + GitHub release verification) is what closes the loop. The tag-push only TRIGGERS the workflow; if Py3.13 flakes, publish is skipped.
- **Closing follow-on beads as "done" before Step 11 confirms PyPI publication.** Bead-close before publish-verified == the publish-was-skipped class of failure goes undetected.

## See also

- `.claude/rules/releases-and-engine.md` § Cutting a release (a pointer to this file; this file is canonical for the procedure)
- `docs/contributing.md#release-process` (long form: the Break-glass subsection for retry / yank / revert / tag-retraction procedures, and the Schema/data-migration releases subsection Step 6d above draws its rationale from)
- `feedback_release_and_develop_ci` (memory entry, merged 2026-10-29: the release and CI mechanics, engine-cut, develop-CI and restart rules cited above, plus the parts that stay Sam's; it replaces the four older `feedback_*` entries this list once named, none of which exist any more)
- `.claude/rules/ci-and-runners.md` § GitHub Actions traps and `docs/agents/worktrees.md` rules 7 and 8 (develop CI reads and push batching)
