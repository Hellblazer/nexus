# RC1 Readiness Design

## Goal

Prepare Nexus for a `1.0.0-rc1` release: published to PyPI, tagged on GitHub, with all pre-release artifacts (old accidental PyPI versions) cleaned up and the codebase in a state that reflects a stable, well-documented public release.

## Context

- Current version: `0.3.2` (local), `0.3.1` (PyPI latest)
- All `0.1.x`–`0.3.x` PyPI versions were published accidentally and should be yanked
- CI exists (pytest on push/PR, Python 3.12 + 3.13)
- No release workflow, no CHANGELOG, `pyproject.toml` missing PyPI metadata
- `nx/.claude-plugin/` is a legacy artifact from the old plugin discovery format — the correct location is `.claude-plugin/marketplace.json` at repo root

## Approach

Three sequential phases, each delivered as a PR. The release workflow (Phase 3) does not fire until a `v*` tag is pushed, so there is no risk of accidental publish during preparation.

---

## Phase 1: Packaging & Hygiene

**Deliverables:**

1. **Yank old PyPI versions** — yank all `0.1.x`, `0.2.x`, `0.3.x` releases via PyPI UI. Yanked versions remain downloadable for pinned users but are excluded from `pip install nexus` resolution.

2. **`pyproject.toml` metadata** — add:
   - `license = "AGPL-3.0-or-later"` (SPDX field)
   - `authors = [{name = "Hal Hildebrand", email = "..."}]`
   - `keywords = ["semantic-search", "knowledge-management", "cli", "llm", "rag"]`
   - `readme = "README.md"` with `content-type = "text/markdown"`
   - `[project.urls]` — Homepage, Repository, Documentation, Bug Tracker
   - `classifiers` — development status (Beta), audience, topic, license, Python versions

3. **`CHANGELOG.md`** at repo root — curated version history from `0.1.0` → `1.0.0-rc1`, [Keep a Changelog](https://keepachangelog.com/) format with Added/Changed/Fixed/Removed sections per version. Entries derived from `git log`.

4. **`docs/contributing.md`** — add Release Process section documenting: version bump, changelog update, tag format (`v1.0.0-rc1`, annotated), CI gate, PyPI publish via GitHub Actions.

5. **Remove `nx/.claude-plugin/`** — legacy plugin discovery directory, superseded by `.claude-plugin/marketplace.json` at repo root.

---

## Phase 2: Quality Pass

**Deliverables:**

1. **CLI help text audit** — run every `nx <command> --help`, verify accuracy and consistency against `docs/cli-reference.md`. Fix mismatches in Click decorators and docs together.

2. **Error message review** — walk error paths (missing credentials, bad collection names, network failures, invalid args). Ensure messages are actionable: state what failed and how to fix it. Priority: first-run experience (`nx config init`, `nx doctor`).

3. **`nx doctor` output quality** — verify all meaningful prerequisites are checked (API keys, ripgrep, Python version, ChromaDB reachability) with clear, specific remediation steps.

4. **Docs completeness check** — cross-reference the full `nx --help` command tree against `docs/cli-reference.md` and `docs/getting-started.md`. No orphaned docs for removed commands, no undocumented flags. Verify `nx/README.md` plugin docs accuracy.

---

## Phase 3: Release Infrastructure

**Deliverables:**

1. **Version bump** — `pyproject.toml` `0.3.2` → `1.0.0-rc1`. Update `nx/CHANGELOG.md` and `.claude-plugin/marketplace.json` plugin version accordingly.

2. **GitHub Actions release workflow** — `.github/workflows/release.yml` triggered on `push: tags: ["v*"]`:
   - Run full test suite
   - Build wheel + sdist with `hatch build`
   - Publish to PyPI via `pypi-publish` action using OIDC trusted publisher (no stored API key)
   - Create GitHub release with tag annotation as release notes

3. **PyPI trusted publisher** — configure PyPI to trust the GitHub Actions OIDC token. One-time setup in PyPI project settings.

4. **Tagging convention** — document in `docs/contributing.md`: annotated tags only (`git tag -a v1.0.0-rc1 -m "..."`), tag message becomes GitHub release title and body.

5. **Branch protection note** — document that `main` requires CI green before merge.

---

## Success Criteria

- [ ] All `0.1.x`–`0.3.x` PyPI versions yanked
- [ ] `pyproject.toml` renders correctly on PyPI (all metadata fields populated)
- [ ] `CHANGELOG.md` covers full version history
- [ ] `nx/.claude-plugin/` removed
- [ ] Every `nx <command> --help` matches `docs/cli-reference.md`
- [ ] `nx doctor` gives actionable output for all failure modes
- [ ] Release workflow triggers on tag push and publishes to PyPI + GitHub
- [ ] `1.0.0-rc1` installable via `pip install nexus==1.0.0-rc1`
