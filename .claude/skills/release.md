---
name: release
description: Use when cutting a release, bumping version, tagging, or publishing to PyPI. Enforces the full release checklist from CLAUDE.md § Release Process. Also surfaces as /nx:release.
---

# Release Checklist

Follow every step in order. Do not skip or reorder. Authority: CLAUDE.md § Release Process (`docs/contributing.md#release-process` is the long form).

## Steps

### 1. Run unit + integration suite

```bash
uv run pytest                # unit suite (no API keys)
uv run pytest -m integration # integration (requires .env from .env.example)
```

Both must pass. Integration is excluded from CI and is the last line of defense before tag-push.

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

### 3. Bump version in ALL FOUR manifests

CI enforces parity. Missing any one of these fails the marketplace-version-matches-pyproject test.

- `pyproject.toml`: `version = "X.Y.Z"`
- `.claude-plugin/marketplace.json`: **both `version` fields** (one for nx, one for sn)
- `nx/.claude-plugin/plugin.json`: `version`
- `sn/.claude-plugin/plugin.json`: `version`

Semver: MAJOR for breaking, MINOR for new features, PATCH for bug fixes.

### 4. Update both changelogs

- `CHANGELOG.md` (root): move `## [Unreleased]` content into a new `## [X.Y.Z] - YYYY-MM-DD` section. Leave a fresh empty `## [Unreleased]` at the top.
- `nx/CHANGELOG.md` (plugin changelog): always update, even if no plugin changes (note: "Plugin version aligned with conexus X.Y.Z. No plugin-side changes." is acceptable).

### 5. Refresh `uv.lock`

```bash
uv sync
```

The lock file MUST be committed. CI also checks this.

### 6. Run sandbox smoke (~2 min)

Required for any change touching `pyproject.toml`, `uv.lock`, `src/nexus/db/migrations.py`, `src/nexus/mcp/**`, `nx/**`, `.claude-plugin/**`, `src/nexus/commands/{doctor,upgrade}.py`.

```bash
./tests/e2e/release-sandbox.sh smoke
```

Must end with `[done]` and confirm the new schema version. Halt on any failure.

### 7. Commit

Two acceptable paths:

**Path A: direct to main** (the one allowed exception per CLAUDE.md "no direct pushes to main"):

```bash
git add pyproject.toml uv.lock CHANGELOG.md nx/CHANGELOG.md \
        .claude-plugin/marketplace.json \
        nx/.claude-plugin/plugin.json \
        sn/.claude-plugin/plugin.json
git commit -m "chore(release): conexus X.Y.Z"
git push
```

**Path B: bundle into a feature PR** (when the release rides on substantive changes that are also in the PR):

The same files as Path A, committed on a feature branch as `chore(release): conexus X.Y.Z`. Open PR with "release" in the title; merge once CI clears. Tag-push happens immediately after merge per Step 9.

### 8. Pre-push verification (do NOT skip)

```bash
git diff --name-only HEAD                # all 5+ release files must appear
nx --version                             # must NOT yet print X.Y.Z (reinstall happens post-tag)
grep '^version' pyproject.toml           # must equal X.Y.Z
grep '"version"' .claude-plugin/marketplace.json   # both must equal X.Y.Z
grep '"version"' nx/.claude-plugin/plugin.json     # must equal X.Y.Z
grep '"version"' sn/.claude-plugin/plugin.json     # must equal X.Y.Z
```

Five version strings must all read X.Y.Z. CI's parity check fails the build if any mismatch.

### 9. Tag and push (triggers Release workflow + PyPI publish via OIDC)

```bash
git tag -a vX.Y.Z -m "conexus X.Y.Z"
git push origin vX.Y.Z
```

Do NOT use `gh release create`: the Release workflow at `.github/workflows/release.yml` creates the GitHub release automatically from the tag and extracts notes from CHANGELOG.md. Running `gh release create` produces a duplicate.

Do NOT run `uv publish` or `twine upload` manually: the Release workflow handles this via OIDC trusted publisher.

### 10. Watch and verify the Release workflow

```bash
gh run watch                                   # wait for Release workflow green
```

Three jobs must complete: `pytest (Python 3.12)`, `pytest (Python 3.13)`, `Build and publish to PyPI`.

If `pytest (Python 3.13)` fails on the known flake set (see Step 1), the failure is GHA-runner pressure (`nexus-9eaz` family). Re-run the failed job:

```bash
gh run rerun <run_id> --failed
```

A second consecutive failure on a non-flake test is real; investigate before retrying.

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
- **Skipping sandbox smoke when `nx/**` or `pyproject.toml` changed.** The smoke catches plugin-load + db-migration regressions that unit tests miss.
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
