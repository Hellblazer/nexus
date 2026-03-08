---
name: release
description: Use when cutting a release, bumping version, tagging, or publishing to PyPI. Enforces the full release checklist from docs/contributing.md.
---

# Release Checklist

Follow every step in order. Do not skip or reorder.

## Steps

1. **Run full test suite**
   ```bash
   uv run pytest tests/
   ```
   Do not proceed if any test fails.

2. **Update `docs/cli-reference.md`** if any CLI flags were added, changed, or removed.

3. **Bump version in `pyproject.toml`**
   Semver: MAJOR for breaking, MINOR for new features, PATCH for bug fixes.

4. **Regenerate lock and reinstall local tool**
   ```bash
   uv sync
   uv tool install --reinstall .
   nx --version
   ```
   Verify the printed version matches what you set in step 3. Do not proceed otherwise.

5. **Update `CHANGELOG.md`**
   Move `## [Unreleased]` content into a new `## [X.Y.Z] - YYYY-MM-DD` section.
   Leave a fresh empty `## [Unreleased]` at the top.

6. **Update `nx/CHANGELOG.md`** (plugin changelog — always, even if no plugin changes).

7. **Update `.claude-plugin/marketplace.json`** — bump the `"version"` field.

8. **Commit directly to main**
   ```bash
   git add pyproject.toml uv.lock CHANGELOG.md nx/CHANGELOG.md .claude-plugin/marketplace.json
   git commit -m "chore: bump version to X.Y.Z"
   git push
   ```

9. **Pre-push verification** (do NOT skip)
   ```bash
   git diff --name-only HEAD    # uv.lock must appear
   nx --version                 # must print X.Y.Z
   grep "^version" pyproject.toml  # must match tag
   ```

10. **Tag and push — triggers CI release pipeline**
    ```bash
    git tag vX.Y.Z
    git push origin vX.Y.Z
    ```
    Do NOT use `gh release create` — the CI workflow creates the GitHub release
    automatically from the tag and extracts notes from CHANGELOG.md.

11. **Verify release**
    ```bash
    gh run watch                    # wait for CI green
    gh release view vX.Y.Z
    curl -s "https://pypi.org/pypi/conexus/json" | python3 -c "import sys,json; print(json.load(sys.stdin)['info']['version'])"
    ```

## Common Mistakes

- Using `gh release create` instead of `git tag` + `git push origin vX.Y.Z` (creates duplicate release)
- Forgetting `uv tool install --reinstall .` (local nx stays on old version)
- Forgetting `uv sync` (uv.lock not updated)
- Pushing tag before version-bump commit (tag points to wrong commit)
- Running `uv publish` manually (CI handles this via OIDC trusted publisher)
