---
name: release
description: Use when cutting a release, bumping version, tagging, or publishing to PyPI. Enforces the full release checklist from CLAUDE.md § Release Process. Also surfaces as /conexus:release.
---

# Release Checklist

Follow every step in order. Do not skip or reorder. Authority: CLAUDE.md § Release Process (`docs/contributing.md#release-process` is the long form).

## Steps

### 0. Engine-freshness gate (PREREQUISITE — the two-lifecycle check)

The Java **engine-service** is a SEPARATE release artifact from this PyPI release: its own `engine-service-vX.Y.Z` tag fires `engine-service-release.yml`, version is tag-stamped (no manifest bump), and it is **decoupled from the luxe6 / RDR-155-P4a develop release boundary**. This PyPI release pins ONE engine version, `REQUIRED_ENGINE_VERSION` (`src/nexus/engine_version.py`) — not two. `PINNED_SERVICE_TAG` (`src/nexus/daemon/binary_install.py`, the exact tag a fresh local `nx init --service` install downloads) is DERIVED from it, not an independently hand-typed literal — there is no floor/exact split to reason about, bumping the one constant moves both together, by construction.

**This is a BLOCKING command, not a prose eyeball-check** (nexus-i5c2u — the prior prose-only version of this step was routinely skipped, letting the cloud engine sit at v0.1.17 for 9+ days across releases while the floor moved to v0.1.34; the pin then independently drifted the identical way in 2026-07-12, sitting at v0.1.36 two tags behind a verified, cloud-deployed fix — which is why the pin is no longer a second hand-typed constant at all):

```bash
uv run python scripts/check_engine_release_floor.py
```

If it exits non-zero, STOP — do not proceed with the PyPI release. Cut + deploy + cloud-gate a fresh engine first via the `engine-release` skill (see **AGENTS.md § Engine-service release**), bump `REQUIRED_ENGINE_VERSION` to that tag's version (this alone also moves `PINNED_SERVICE_TAG`), then re-run until it exits 0.

Supplementary context (useful when deciding whether recent `service/` work is cloud-relevant, but the script above is the actual gate):

```bash
git tag -l "engine-service-v*" | sort -V | tail -1          # last engine tag
git log --oneline <last-engine-tag>..HEAD -- service/        # cloud-relevant drift?
```

1. Confirm the pinned engine tag is (a) cloud-DEPLOYED and (b) cloud-GATED (recall + hybrid parity, xr7.8.9-style) — read the authoritative bead + conexus bus, **not memory** (cross-repo gate state goes stale fast: 2026-06-26 a `luxe6` condition had been cleared a week earlier than memory implied).
2. If `service/` has drifted with cloud-relevant changes (pooler/RLS, pgvector, catalog conformance, aspect queue, batch endpoints), cut a fresh engine FIRST — see **AGENTS.md § Engine-service release** — have conexus deploy + re-gate it (passive bus: surface an explicit "relay: deploy `engine-service-vX.Y.Z` + re-gate" to Hal), THEN bump `REQUIRED_ENGINE_VERSION` here (this alone also moves `PINNED_SERVICE_TAG` — nothing else to bump) and re-run `scripts/check_engine_release_floor.py` to confirm. The engine cut is NOT luxe6-gated, so refreshing it never blocks on the develop boundary.
3. The engine cut itself: full `service/` suite green on the tagged commit (confirm the `service/` tree equals a green-`service-ci` commit — the Java CI is advisory and does not block auto-merge, so verify), then the **human** pushes `engine-service-vX.Y.Z`.

This gate exists because the engine silently drifted 22 `service/` commits / 4 days behind the cloud (2026-06-26); the PyPI checklist had no step that would have caught it.

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
zero ✗ / zero ⚠ / an EMPTY warnings allowlist. Must end
`FRESH-INSTALL MVV PASSED`. `FRESH_MVV_CACHE=/tmp/fresh-mvv-cache` reuses the
416MB model download across runs. Every new fresh-box warning is a decision:
fix it or allowlist it in the script WITH a rationale + bead reference.

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

**Local-mode functional gate — self-provisioning** (2026-07-06 v6.3.6
lesson; nexus-edwlp, 2026-07-07): the local-service round-trip family
(the functional test of local mode) skip-gates on a reachable local
service and deliberately never resolves the managed cloud.
Historically it only ran when a dev-box service HAPPENED to be alive
against a lived-in install — an ambient, irreproducible gate that
silently degraded to 74/516 tests the day the ambient service died.
`tests/e2e/local-service-gate.sh` self-provisions a throwaway PG +
service (scratch NEXUS_CONFIG_DIR, isolated from ~/.config/nexus and
prod), auto-rebuilds a stale dev jar, and pins the whole family at the
throwaway service via the shared HOST/PORT env leg (the T3 vector
resolver honors it since nexus-edwlp). Infra is hermetic; credentials
are not — the voyage/CCE subset needs a real `VOYAGE_API_KEY`, sourced
explicitly from repo-root `.env` before the service spawns. The
`lived_in` marker excludes the handful of tests that dispatch real
`claude -p` or need seeded lived-in corpora (carve-out size asserted
exactly), and a vacuity guard asserts pinned passed/skipped
FLOOR/BUDGET at the end of the run. A guard trip or any new hard
failure is real signal — compensate with live validation of the
release's changed paths (the v6.3.5/v6.3.6 pattern: exercise the
advertised claims against the real deployment).

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
- `mcpb/pyproject.toml`: `version` **and** the `conexus>=X.Y.Z` dependency pin
- `mcpb/manifest.json`: `version`
- `.claude-plugin/marketplace.json`: **both `version` fields** (one for conexus, one for sn)
- `.claude-plugin/marketplace.json`: **both `plugins[].source.ref` fields** — must be `"vX.Y.Z"` (the tag form). Easy to forget. This is what decouples installed users from main HEAD: plugin installs follow the pinned tag, not whatever main currently is. **CRITICAL: nexus-mkj6u 2026-05-23**
- `conexus/.claude-plugin/plugin.json`: `version`
- `sn/.claude-plugin/plugin.json`: `version`

Optional but recommended: also bump `plugins[].source.sha` to the 40-char SHA of the release commit, for protection against tag force-push. Add post-commit (Step 8a, see below).

**Engine-service pin (conditional 8th target — nexus-3rq00).** The Python/Java boundary rides one more hand-edited constant that sits OUTSIDE the seven-manifest parity gate: `PINNED_SERVICE_TAG` in `src/nexus/daemon/binary_install.py`, the `engine-service-vX.Y.Z` release this build auto-installs. It is NOT bumped every release — only when the compatible engine-service version advances. When this release ships a new engine, bump `PINNED_SERVICE_TAG` in lock-step. Two invariants the `TestEnginePinParity` test enforces: (1) `PINNED_SERVICE_TAG`'s numeric version must be `>= REQUIRED_ENGINE_VERSION` (`src/nexus/engine_version.py`) — never ship a client that auto-installs an engine it then refuses as too old; (2) at the 6.0 release boundary the pin must be non-None (it is intentionally `None` pre-6.0). A release that bumps pyproject to 6.x without setting a real pin trips CI.

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

Required for any change touching `pyproject.toml`, `uv.lock`, `src/nexus/db/migrations.py`, `src/nexus/mcp/**`, `conexus/**`, `.claude-plugin/**`, `src/nexus/commands/{doctor,upgrade}.py`.

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

# Stage ALL SEVEN bump targets from Step 3, plus uv.lock and both changelogs.
# mcpb/pyproject.toml + mcpb/manifest.json are the easy-to-miss pair here and
# their omission fails CI's mcpb-manifest-version parity check.
git add pyproject.toml uv.lock CHANGELOG.md conexus/CHANGELOG.md \
        mcpb/pyproject.toml mcpb/manifest.json \
        .claude-plugin/marketplace.json \
        conexus/.claude-plugin/plugin.json \
        sn/.claude-plugin/plugin.json
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

### 12. Reinstall local tool and verify

```bash
scripts/reinstall-tool.sh    # preserves [local] and other extras (mineru is now a default dep)
nx --version                 # must print X.Y.Z
```

`pyproject.toml` bumps the project version but the local `nx` shim keeps the old wheel until `scripts/reinstall-tool.sh` runs. Caught on v4.9.11: `nx --version` reported 4.9.10 even after PyPI showed 4.9.11.

## Common Mistakes

- **Bumping only `pyproject.toml` and missing the four plugin manifests.** CI parity check catches this late. Run the Step 8 pre-push check.
- **Skipping the integration suite.** Unit-only is what CI runs; integration is your last gate against keyed-API regressions before tag-push.
- **Skipping sandbox smoke when `conexus/**` or `pyproject.toml` changed.** The smoke catches plugin-load + db-migration regressions that unit tests miss.
- **Using `gh release create` after `git push origin vX.Y.Z`.** Duplicate release. The Release workflow already creates one.
- **Forgetting `uv sync`.** `uv.lock` not updated; CI fails or local install resolves differently.
- **Forgetting `scripts/reinstall-tool.sh` after tag-push.** Local `nx` stays on old version; the post-merge "verify locally" step lies.
- **Pushing the tag before the version-bump commit.** Tag points at the wrong commit; the Release workflow's version-match step fails (`tag != pyproject.toml version`).
- **Running `uv publish` or `twine upload` manually.** The Release workflow handles PyPI via OIDC; manual publish either duplicates or fights the workflow.
- **Declaring "release done" after `git push origin vX.Y.Z`.** Step 11 (PyPI + GitHub release verification) is what closes the loop. The tag-push only TRIGGERS the workflow; if Py3.13 flakes, publish is skipped.
- **Closing follow-on beads as "done" before Step 11 confirms PyPI publication.** Bead-close before publish-verified == the publish-was-skipped class of failure goes undetected.

## See also

- `CLAUDE.md` § Release Process (canonical; defer to it on any discrepancy)
- `docs/contributing.md#release-process` (long form with rollback / one-time setup)
- `feedback_invoke_release_skill.md` (memory entry: invoke this skill, do not freehand)
- `feedback_post_release_reinstall.md` (memory entry: reinstall after tag)
- `feedback_release_discipline.md` (memory entry: full suite before tag, not after)
- `feedback_version_bump_manifests.md` (memory entry: all four manifests, CI enforces parity)
