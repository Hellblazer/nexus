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

### 3. Bump version in ALL FIVE bump targets

CI enforces parity. Missing any one of these fails the marketplace-version-matches-pyproject test or the marketplace-source-ref-matches-pyproject test.

- `pyproject.toml`: `version = "X.Y.Z"` (canonical source of truth)
- `.claude-plugin/marketplace.json`: **both `version` fields** (one for conexus, one for sn)
- `.claude-plugin/marketplace.json`: **both `plugins[].source.ref` fields** — must be `"vX.Y.Z"` (the tag form). Easy to forget. This is what decouples installed users from main HEAD: plugin installs follow the pinned tag, not whatever main currently is. **CRITICAL: nexus-mkj6u 2026-05-23**
- `conexus/.claude-plugin/plugin.json`: `version`
- `sn/.claude-plugin/plugin.json`: `version`

Optional but recommended: also bump `plugins[].source.sha` to the 40-char SHA of the release commit, for protection against tag force-push. Add post-commit (Step 8a, see below).

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

### 7. Commit on a release branch + PR to main (nexus-mkj6u: replaces direct-to-main)

Per the marketplace-pinned-source playbook (also used by `Hellblazer/palinex`), release commits go through a PR. CI gates the bump before it lands on main. No more direct-to-main exception.

```bash
git checkout main && git pull
git checkout -b release/vX.Y.Z

git add pyproject.toml uv.lock CHANGELOG.md conexus/CHANGELOG.md \
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
