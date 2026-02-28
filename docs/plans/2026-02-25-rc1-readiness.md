# RC1 Readiness Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Prepare Nexus for a `1.0.0-rc1` release on PyPI and GitHub, with all accidental pre-release versions yanked, full packaging metadata, curated CHANGELOG, quality-reviewed CLI and docs, and a tag-triggered release workflow.

**Architecture:** Three sequential phases delivered as PRs. Phase 1 is pure hygiene. Phase 2 is a quality pass on CLI UX and docs accuracy. Phase 3 wires the release pipeline. The release workflow does not fire until a `v*` tag is pushed, so there is no risk of accidental publish.

**Tech Stack:** Python 3.12+, `uv`, `hatchling`, GitHub Actions, PyPI trusted publisher (OIDC), `twine` (for yank)

---

## Phase 1: Packaging & Hygiene

### Task 1: Yank old PyPI versions (manual — no code)

**Files:** none (PyPI web UI action)

**Step 1: Log into PyPI**

Go to https://pypi.org/manage/project/nexus/releases/ and yank every version:
`0.1.0`, `0.1.1`, `0.1.3`, `0.1.4`, `0.1.5`, `0.1.6`, `0.1.7`, `0.2.0`, `0.2.1`, `0.2.2`, `0.2.3`, `0.3.0`, `0.3.1`, `0.3.2`

For each: click the version → "Yank release" → enter reason: "Pre-release development version; use 1.0.0-rc1 or later"

**Step 2: Verify**

```bash
pip index versions nexus
```
Expected: only yanked versions shown, `pip install nexus` will fail until 1.0.0-rc1 is published.

---

### Task 2: Remove `nx/.claude-plugin/`

**Files:**
- Delete: `nx/.claude-plugin/plugin.json`
- Delete directory: `nx/.claude-plugin/`

**Step 1: Remove the directory**

```bash
rm -rf nx/.claude-plugin
```

**Step 2: Verify it's gone**

```bash
ls nx/
```
Expected: `.claude-plugin` no longer appears in the listing.

**Step 3: Commit**

```bash
git add -A
git commit -m "Remove nx/.claude-plugin: legacy plugin discovery format replaced by .claude-plugin/marketplace.json at repo root"
```

---

### Task 3: Add metadata to `pyproject.toml`

**Files:**
- Modify: `pyproject.toml`

**Step 1: Read the current file**

Read `pyproject.toml` to confirm current content before editing.

**Step 2: Replace the `[project]` table**

Replace the existing `[project]` section with:

```toml
[project]
name = "nexus"
version = "0.3.2"
description = "Self-hosted semantic search and knowledge management for LLM-driven development"
readme = { file = "README.md", content-type = "text/markdown" }
requires-python = ">=3.12"
license = "AGPL-3.0-or-later"
authors = [
    { name = "Hal Hildebrand", email = "hellblazer@me.com" }
]
keywords = [
    "semantic-search",
    "knowledge-management",
    "cli",
    "llm",
    "rag",
    "code-search",
    "chromadb",
    "voyage-ai",
]
classifiers = [
    "Development Status :: 4 - Beta",
    "Intended Audience :: Developers",
    "Topic :: Software Development :: Libraries :: Application Frameworks",
    "Topic :: Scientific/Engineering :: Artificial Intelligence",
    "License :: OSI Approved :: GNU Affero General Public License v3 or later (AGPLv3+)",
    "Programming Language :: Python :: 3",
    "Programming Language :: Python :: 3.12",
    "Programming Language :: Python :: 3.13",
    "Operating System :: OS Independent",
]
dependencies = [
    "anthropic>=0.39",
    "chromadb>=0.6",
    "click>=8.1",
    "flask>=3.0",
    "llama-index-core==0.12.7",
    "markdown-it-py>=4.0.0",
    "pymupdf>=1.26.6",
    "pymupdf4llm>=0.2.2",
    "pyyaml>=6.0",
    "structlog>=24.0",
    "tree-sitter-language-pack==0.7.1",
    "voyageai>=0.2",
    "waitress>=3.0",
]

[project.urls]
Homepage = "https://github.com/Hellblazer/nexus"
Repository = "https://github.com/Hellblazer/nexus"
Documentation = "https://github.com/Hellblazer/nexus/tree/main/docs"
"Bug Tracker" = "https://github.com/Hellblazer/nexus/issues"
```

**Step 3: Verify hatchling can build**

```bash
uv run hatch build --clean
```
Expected: Creates `dist/nexus-0.3.2-py3-none-any.whl` and `dist/nexus-0.3.2.tar.gz` without errors.

**Step 4: Check PyPI rendering locally**

```bash
uv run python -m twine check dist/*
```
Expected: `PASSED` for both files. If twine is not installed: `uv add --dev twine` first.

**Step 5: Commit**

```bash
git add pyproject.toml
git commit -m "Add PyPI metadata: license, authors, classifiers, URLs, readme content-type"
```

---

### Task 4: Write `CHANGELOG.md`

**Files:**
- Create: `CHANGELOG.md`

**Step 1: Create the file**

Use the git log history to write the changelog. The version → content mapping derived from the git log:

```markdown
# Changelog

All notable changes to Nexus are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0-rc1] - TBD

### Added
- Smart repository indexing: code routed to `code__` collections, prose to `docs__`, PDFs to `docs__`
- 12-language AST chunking via tree-sitter (Python, JS, TS, Java, Go, Rust, C, C++, Ruby, C#, Bash, TSX)
- Semantic markdown chunking via markdown-it-py with section-boundary awareness
- RDR (Research-Design-Review) document indexing into dedicated `rdr__` collections
- `nx index rdr` command for manual RDR indexing
- Frecency scoring: git commit history decay weighting for hybrid search ranking
- `--frecency-only` reindex flag: update scores without re-embedding
- Hybrid search: semantic + ripgrep keyword scoring with `--hybrid` flag
- Agentic search mode: multi-step Haiku query refinement with `--agentic` flag
- Answer synthesis mode: cited answers via Haiku with `--answer`/`-a` flag
- Reranking via Voyage AI `rerank-2.5` with automatic fallback
- Path-scoped search with `[path]` positional argument
- `--where` filter support for metadata queries
- `-A`/`-B`/`-C` context lines flags for `nx search`
- `--vimgrep` and `--files` output formats
- `nx pm` full lifecycle: init, status, resume, search, archive, restore
- `nx store list` subcommand
- `nx collection verify --deep` deep verification
- Background server HEAD polling for auto-reindex on commit
- Claude Code plugin (`nx/`): 15 agents, 26 skills, session hooks, slash commands
- RDR workflow skills: rdr-create, rdr-list, rdr-show, rdr-research, rdr-gate, rdr-close
- E2E test suite requiring no API keys (1258 tests)
- Integration test suite with real API keys (`-m integration`)

### Changed
- Renamed `nx index code` → `nx index repo`
- Collection names use `__` separator (never `:`)
- Session ID scoped by `os.getsid(0)` (terminal group leader PID) for worktree isolation
- Stable collection names across git worktrees via `git rev-parse --git-common-dir`
- Embedding models: `voyage-code-3` for code indexing, `voyage-context-3` (CCE) for docs/knowledge, `voyage-4` for all queries
- T1 session architecture: shared EphemeralClient store + `getsid(0)` anchor
- Plugin discovery: `.claude-plugin/marketplace.json` at repo root (replaces `nx/.claude-plugin/plugin.json`)
- `nx pm` namespace collapsed; session hooks simplified
- Plugin slash commands: `/plan` → `/create-plan`, `/code-review` → `/review-code`

### Fixed
- CCE fallback metadata bug
- Search round-robin interleaving
- Collection name collision on overflow
- Registry resilience under concurrent access
- Credential TOCTOU race condition
- `nx serve stop` dead code removed
- Indexer ignorePatterns filtering
- Upsert idempotency in doc pipeline
- T1/T2 thread-safe reads

### Removed
- `nx install` / `nx uninstall` legacy commands
- `nx pm migrate` command
- Homebrew tap formula (superseded by `uv tool install`)
- `nx/.claude-plugin/` legacy plugin discovery directory

## [0.4.0] - 2026-02-24

### Added
- nx plugin v0.4.0: brainstorming-gate, verification-before-completion, receiving-code-review, using-nx-skills, dispatching-parallel-agents, writing-nx-skills skills
- Graphviz flowcharts in decision-heavy skills
- REQUIRED SUB-SKILL cross-reference markers
- Companion reference.md for nexus skill
- SessionStart hook for using-nx-skills injection
- PostToolUse hook with bd create matcher

### Changed
- All skill descriptions rewritten to CSO "Use when [condition]" pattern
- Relay templates deduplicated: hybrid cross-reference to RELAY_TEMPLATE.md
- Agent-delegating commands simplified with pre-filled relay parts
- Nexus skill split into quick-ref SKILL.md + detailed reference.md

### Fixed
- PostToolUse hook performance: now fires only on bd create, not every tool use
- Removed non-standard frontmatter fields from all skills

## [0.3.2] - 2026-02-22

### Added
- E2E tests for indexer pipeline and HEAD-polling logic

### Fixed
- `nx serve stop` dead code path

## [0.3.1] - 2026-02-22

### Added
- `nx store list` subcommand
- Integration test improvements: knowledge corpus scoping

### Changed
- README full readability pass: clearer setup path, optional vs required deps

## [0.3.0] - 2026-02-22

### Added
- Voyage AI CCE (`voyage-context-3`) for docs and knowledge collections at index time
- Ripgrep hybrid search: `rg` cache wired to `--hybrid` retrieval
- `--content` flag and `[path]` path-scoping for `nx search`
- `--where` metadata filter, `-A`/`-B`/`-C` context flags, `--reverse`, `-m` alias
- P0 regression test suite
- T3 factory extraction (`make_t3()`) with `_client`/`_ef_override` injection for tests
- `nx pm promote` and `NX_ANSWER` env override
- `nx collection verify --deep` and info enhancements
- Frecency-only reindex flag

### Changed
- Removed pdfplumber in favour of pymupdf4llm
- `search_engine.py` refactored into focused modules (`scoring.py`, `search_engine.py`, `answer.py`, `types.py`, `errors.py`)
- structlog migration

### Fixed
- 10 P0 bugs, 10 P1 bugs, 10 P2 bugs, 5 P3 observations
- CCE fallback metadata bug; `batch_size` dead parameter removed
- `serve` status/stop lifecycle, collection collision, registry resilience
- Credential TOCTOU, env override error handling
- T1 session architecture (getsid anchor, thread-safe reads)

## [0.2.0] - 2026-02-21

### Added
- `nx config` command with credential management and `config init` wizard
- Integration test suite (requires real API keys)
- E2E test suite (no API keys, 505 tests at release)
- T1 session architecture overhaul: shared EphemeralClient + getsid(0) anchor
- Scratch tier fix for CLI use outside Claude Code

### Changed
- Full README rewrite: installation, quickstart, command reference, architecture

### Fixed
- Scratch tier session isolation
- 5-stream global code review: 15 critical/significant fixes (mxbai chunk ID, security, resilience)

## [0.1.0] - 2026-02-21

### Added
- Project scaffold: `src/nexus/` package, `nx` CLI entry point via Click
- T1: `chromadb.EphemeralClient` + ONNX MiniLM, session-scoped scratch (`nx scratch`)
- T2: SQLite + FTS5 WAL, per-project persistent memory (`nx memory`)
- T3: `chromadb.CloudClient` + Voyage AI, permanent knowledge store (`nx store`, `nx search`)
- `nx index repo` (originally `nx index code`): git-aware code indexing with tree-sitter AST
- `nx serve`: Flask/Waitress background daemon with HEAD polling for auto-reindex
- `nx pm`: project management lifecycle (init, status, resume, search, archive, restore)
- `nx doctor`: prerequisite health check
- Claude Code plugin (`nx/`): initial agents, skills, hooks, registry
- Config system: 4-level precedence (defaults → global → per-repo → env vars)
- Hybrid search: semantic + ripgrep keyword scoring
- Answer synthesis: Haiku with cited `<cite i="N">` references
- Agentic search: multi-step Haiku query refinement
- Phase 1–8 implementations covering all CLI surface

[Unreleased]: https://github.com/Hellblazer/nexus/compare/v1.0.0-rc1...HEAD
[1.0.0-rc1]: https://github.com/Hellblazer/nexus/compare/v0.4.0...v1.0.0-rc1
[0.4.0]: https://github.com/Hellblazer/nexus/compare/v0.3.2...v0.4.0
[0.3.2]: https://github.com/Hellblazer/nexus/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/Hellblazer/nexus/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/Hellblazer/nexus/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Hellblazer/nexus/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Hellblazer/nexus/releases/tag/v0.1.0
```

**Step 2: Commit**

```bash
git add CHANGELOG.md
git commit -m "Add CHANGELOG.md: curated version history from 0.1.0 to 1.0.0-rc1"
```

---

### Task 5: Add Release Process to `docs/contributing.md`

**Files:**
- Modify: `docs/contributing.md`

**Step 1: Append the Release Process section**

Add after the existing "Git Workflow" section:

```markdown
## Release Process

1. **Verify tests pass**
   ```bash
   uv run pytest tests/
   ```

2. **Update version in `pyproject.toml`**
   Change the `version` field (e.g. `"1.0.0-rc1"` → `"1.0.0"`).

3. **Update `CHANGELOG.md`**
   - Rename `[Unreleased]` section to `[X.Y.Z] - YYYY-MM-DD`
   - Add a new empty `[Unreleased]` section at the top
   - Update the comparison links at the bottom

4. **Update plugin versions** (if plugin changed)
   - `nx/CHANGELOG.md`: add release entry
   - `.claude-plugin/marketplace.json`: bump `"version"` field

5. **Commit the release**
   ```bash
   git add pyproject.toml CHANGELOG.md
   git commit -m "Release vX.Y.Z"
   ```

6. **Create an annotated tag** (message becomes the GitHub release body)
   ```bash
   git tag -a vX.Y.Z -m "$(cat <<'EOF'
   Release X.Y.Z

   [Paste the CHANGELOG section for this version here]
   EOF
   )"
   ```

7. **Push branch and tag**
   ```bash
   git push origin main
   git push origin vX.Y.Z
   ```

8. **CI publishes automatically**
   The `release.yml` workflow triggers on `v*` tags, runs tests, builds the wheel, publishes to PyPI via OIDC trusted publisher, and creates a GitHub release.

9. **Yank pre-release versions** (if applicable)
   Go to https://pypi.org/manage/project/nexus/releases/ and yank any versions that should not be resolved by `pip install nexus`.
```

**Step 2: Commit**

```bash
git add docs/contributing.md
git commit -m "Add Release Process section to contributing.md"
```

---

### Phase 1 PR

```bash
gh pr create --title "Phase 1: Packaging hygiene for RC1" --body "$(cat <<'EOF'
## Summary

- Remove `nx/.claude-plugin/` (legacy plugin discovery format)
- Add full PyPI metadata to `pyproject.toml` (license, authors, classifiers, URLs, readme)
- Add `CHANGELOG.md` covering full version history 0.1.0 → 1.0.0-rc1
- Add Release Process section to `docs/contributing.md`

## Notes

- Old PyPI versions (0.1.x–0.3.x) must be yanked manually via https://pypi.org/manage/project/nexus/releases/ — documented in CHANGELOG task above.
- Package builds and passes `twine check` locally.
EOF
)"
```

---

## Phase 2: Quality Pass

### Task 6: CLI help text audit

**Files:**
- Modify: `src/nexus/commands/*.py` (as needed)
- Modify: `docs/cli-reference.md` (as needed)

**Step 1: Capture all help text**

```bash
uv run nx --help > /tmp/nx-help.txt
uv run nx search --help >> /tmp/nx-help.txt
uv run nx index --help >> /tmp/nx-help.txt
uv run nx index repo --help >> /tmp/nx-help.txt
uv run nx index rdr --help >> /tmp/nx-help.txt
uv run nx store --help >> /tmp/nx-help.txt
uv run nx store put --help >> /tmp/nx-help.txt
uv run nx store get --help >> /tmp/nx-help.txt
uv run nx store list --help >> /tmp/nx-help.txt
uv run nx store expire --help >> /tmp/nx-help.txt
uv run nx memory --help >> /tmp/nx-help.txt
uv run nx memory put --help >> /tmp/nx-help.txt
uv run nx memory get --help >> /tmp/nx-help.txt
uv run nx memory search --help >> /tmp/nx-help.txt
uv run nx memory list --help >> /tmp/nx-help.txt
uv run nx memory delete --help >> /tmp/nx-help.txt
uv run nx scratch --help >> /tmp/nx-help.txt
uv run nx scratch put --help >> /tmp/nx-help.txt
uv run nx scratch get --help >> /tmp/nx-help.txt
uv run nx scratch search --help >> /tmp/nx-help.txt
uv run nx scratch list --help >> /tmp/nx-help.txt
uv run nx scratch flag --help >> /tmp/nx-help.txt
uv run nx scratch promote --help >> /tmp/nx-help.txt
uv run nx pm --help >> /tmp/nx-help.txt
uv run nx pm init --help >> /tmp/nx-help.txt
uv run nx pm status --help >> /tmp/nx-help.txt
uv run nx pm resume --help >> /tmp/nx-help.txt
uv run nx pm search --help >> /tmp/nx-help.txt
uv run nx pm archive --help >> /tmp/nx-help.txt
uv run nx pm restore --help >> /tmp/nx-help.txt
uv run nx collection --help >> /tmp/nx-help.txt
uv run nx collection list --help >> /tmp/nx-help.txt
uv run nx collection info --help >> /tmp/nx-help.txt
uv run nx collection verify --help >> /tmp/nx-help.txt
uv run nx collection delete --help >> /tmp/nx-help.txt
uv run nx config --help >> /tmp/nx-help.txt
uv run nx config init --help >> /tmp/nx-help.txt
uv run nx config set --help >> /tmp/nx-help.txt
uv run nx config get --help >> /tmp/nx-help.txt
uv run nx doctor --help >> /tmp/nx-help.txt
uv run nx serve --help >> /tmp/nx-help.txt
uv run nx serve start --help >> /tmp/nx-help.txt
uv run nx serve stop --help >> /tmp/nx-help.txt
uv run nx serve status --help >> /tmp/nx-help.txt
```

**Step 2: Cross-reference against `docs/cli-reference.md`**

Read `docs/cli-reference.md` and compare each command's documented flags against `/tmp/nx-help.txt`.

Look for:
- Flags present in `--help` but missing from docs
- Flags documented but removed from the CLI (orphaned docs)
- Descriptions that don't match what the command actually does
- Missing default values

**Step 3: Fix mismatches**

For each mismatch found:
- If docs are wrong: update `docs/cli-reference.md`
- If help text is wrong/stale: update the `@click.option` decorators in `src/nexus/commands/*.py`
- If a flag is missing docs: add it

**Step 4: Run tests to verify nothing broke**

```bash
uv run pytest tests/ -x -q
```
Expected: all tests pass.

**Step 5: Commit**

```bash
git add src/nexus/commands/ docs/cli-reference.md
git commit -m "Audit and fix CLI help text against docs/cli-reference.md"
```

---

### Task 7: Error message review

**Files:**
- Modify: `src/nexus/errors.py`
- Modify: `src/nexus/commands/*.py` (as needed)
- Modify: `src/nexus/config.py` (as needed)

**Step 1: Find all error/exception raises**

```bash
grep -rn "raise\|click.echo.*[Ee]rror\|sys.exit\|ClickException" src/nexus/ --include="*.py" | grep -v __pycache__
```

**Step 2: Review first-run error paths**

Read `src/nexus/commands/config_cmd.py` and `src/nexus/hooks.py` (doctor). For each error path, verify the message:
- States what failed (not just "Error")
- States how to fix it (e.g. "Run `nx config set chroma_api_key <key>`")
- Is consistent in style: lowercase, no trailing period, imperative fix hint

Example improvements:
- ❌ `"ChromaDB connection failed"`
- ✅ `"ChromaDB unreachable — check chroma_api_url in nx config get"`

**Step 3: Fix any actionless errors**

Update messages that fail the "what + how to fix" test.

**Step 4: Run tests**

```bash
uv run pytest tests/ -x -q
```

**Step 5: Commit**

```bash
git add src/nexus/
git commit -m "Improve error messages: add actionable remediation hints"
```

---

### Task 8: `nx doctor` output quality review

**Files:**
- Modify: `src/nexus/hooks.py` (or wherever doctor is implemented — check via `grep -rn "def doctor\|nx doctor" src/nexus/`)

**Step 1: Find the doctor implementation**

```bash
grep -rn "def.*doctor\|@.*doctor" src/nexus/ --include="*.py"
```

**Step 2: Run doctor in a clean environment**

```bash
uv run nx doctor
```

Review each check:
- Does it check for ripgrep (`rg`)? → `which rg`
- Does it check Python version ≥ 3.12?
- Does it check for each required API key (chroma, voyage, anthropic, mxbai)?
- Does it check ChromaDB cloud reachability?
- Does it give specific fix instructions for each failure?

**Step 3: Add any missing checks; improve remediation text**

For any missing check, add it. For each failure case, ensure the output says exactly what to run or configure.

Example:
```
✗ ripgrep not found — hybrid search disabled
  Fix: brew install ripgrep  (macOS)  |  apt install ripgrep  (Debian/Ubuntu)
```

**Step 4: Run tests**

```bash
uv run pytest tests/ -k "doctor" -v
```
Expected: all doctor tests pass. Update tests if doctor output changed.

**Step 5: Commit**

```bash
git add src/nexus/
git commit -m "Improve nx doctor: complete prerequisite checks with actionable remediation"
```

---

### Task 9: Docs completeness check

**Files:**
- Modify: `docs/getting-started.md` (as needed)
- Modify: `docs/cli-reference.md` (as needed)
- Modify: `nx/README.md` (as needed)

**Step 1: Verify `docs/getting-started.md` reflects current install path**

Read `docs/getting-started.md`. Verify:
- Install instructions use `uv tool install nexus` (not pip, not Homebrew tap)
- Quick start commands match current CLI (`nx index repo`, not `nx index code`)
- API key setup matches `nx config init` flow

**Step 2: Check `nx/README.md` plugin docs**

Read `nx/README.md`. Verify:
- Plugin install command: `/plugin marketplace add Hellblazer/nexus`
- Agent list (15 agents) matches what exists in `nx/agents/`
- Skill list (26 skills) matches what exists in `nx/skills/`
- No references to removed commands (`/plan`, `/code-review` old names)

Count actual agents and skills:
```bash
ls nx/agents/ | wc -l
ls nx/skills/ | wc -l
```

**Step 3: Verify `docs/cli-reference.md` has no orphaned commands**

Cross-check the command list in `docs/cli-reference.md` against the Click groups registered in `src/nexus/cli.py`:

```bash
grep "add_command\|@cli" src/nexus/cli.py
```

Remove any docs for commands that no longer exist.

**Step 4: Commit any fixes**

```bash
git add docs/ nx/README.md
git commit -m "Docs completeness pass: fix stale commands, verify install path, count agents/skills"
```

---

### Phase 2 PR

```bash
gh pr create --title "Phase 2: Quality pass — CLI, errors, doctor, docs" --body "$(cat <<'EOF'
## Summary

- CLI help text audited and aligned with docs/cli-reference.md
- Error messages reviewed for actionability (what failed + how to fix)
- nx doctor output verified and improved
- Docs completeness check: getting-started, cli-reference, nx/README.md

## Test Plan
- [ ] All 1258+ unit tests pass
- [ ] `nx doctor` output reviewed manually
- [ ] Every `nx <command> --help` matches docs/cli-reference.md
EOF
)"
```

---

## Phase 3: Release Infrastructure

### Task 10: Version bump to `1.0.0-rc1`

**Files:**
- Modify: `pyproject.toml`
- Modify: `CHANGELOG.md`
- Modify: `.claude-plugin/marketplace.json`
- Modify: `nx/CHANGELOG.md` (if plugin version tracks CLI version)

**Step 1: Bump version in `pyproject.toml`**

Change:
```toml
version = "0.3.2"
```
To:
```toml
version = "1.0.0-rc1"
```

**Step 2: Update `CHANGELOG.md`**

Add today's date to the `[1.0.0-rc1]` section header:
```markdown
## [1.0.0-rc1] - YYYY-MM-DD
```

**Step 3: Update `.claude-plugin/marketplace.json`**

Read the file and bump the plugin version in the `"version"` field to `"1.0.0-rc1"`.

**Step 4: Run tests to confirm nothing broke**

```bash
uv run pytest tests/ -x -q
```
Expected: all tests pass.

**Step 5: Commit**

```bash
git add pyproject.toml CHANGELOG.md .claude-plugin/marketplace.json
git commit -m "Bump version to 1.0.0-rc1"
```

---

### Task 11: GitHub Actions release workflow

**Files:**
- Create: `.github/workflows/release.yml`

**Step 1: Create the release workflow**

```yaml
name: Release

on:
  push:
    tags:
      - "v*"

jobs:
  test:
    name: pytest (Python ${{ matrix.python-version }})
    runs-on: ubuntu-latest
    strategy:
      fail-fast: true
      matrix:
        python-version: ["3.12", "3.13"]

    steps:
      - uses: actions/checkout@v4

      - uses: astral-sh/setup-uv@v5
        with:
          python-version: ${{ matrix.python-version }}

      - name: Cache ChromaDB ONNX model
        uses: actions/cache@v4
        with:
          path: ~/.cache/chroma
          key: chromadb-onnx-${{ runner.os }}

      - name: Install ripgrep
        run: sudo apt-get install -y ripgrep

      - name: Install project + dev deps
        run: uv sync --group dev

      - name: Run tests
        run: uv run pytest tests/ -v

  publish:
    name: Build and publish to PyPI
    runs-on: ubuntu-latest
    needs: test
    environment: pypi-release
    permissions:
      id-token: write   # required for OIDC trusted publisher
      contents: write   # required to create GitHub release

    steps:
      - uses: actions/checkout@v4

      - uses: astral-sh/setup-uv@v5
        with:
          python-version: "3.12"

      - name: Install project
        run: uv sync

      - name: Build wheel and sdist
        run: uv run hatch build --clean

      - name: Publish to PyPI
        uses: pypa/gh-action-pypi-publish@release/v1
        # No api-token needed — uses OIDC trusted publisher

      - name: Create GitHub Release
        uses: softprops/action-gh-release@v2
        with:
          body: ${{ github.event.head_commit.message }}
          files: dist/*
          prerelease: ${{ contains(github.ref_name, 'rc') || contains(github.ref_name, 'alpha') || contains(github.ref_name, 'beta') }}
```

**Step 2: Run existing CI to make sure it still passes**

```bash
# Verify the existing ci.yml still runs on current branch (no conflict)
cat .github/workflows/ci.yml
```

**Step 3: Commit**

```bash
git add .github/workflows/release.yml
git commit -m "Add GitHub Actions release workflow: tag-triggered PyPI publish + GitHub release"
```

---

### Task 12: Document PyPI trusted publisher setup

**Files:**
- Modify: `docs/contributing.md`

**Step 1: Add PyPI trusted publisher instructions**

In the "Release Process" section added in Task 5, insert before step 7 (push tag):

```markdown
### One-time PyPI Trusted Publisher Setup

Before the first release, configure PyPI to trust the GitHub Actions OIDC token:

1. Go to https://pypi.org/manage/project/nexus/settings/publishing/
2. Click "Add a new publisher"
3. Fill in:
   - **Owner**: `Hellblazer`
   - **Repository**: `nexus`
   - **Workflow filename**: `release.yml`
   - **Environment name**: `pypi-release`
4. Click "Add"

This eliminates the need for a `PYPI_API_TOKEN` secret. GitHub Actions authenticates directly via OIDC.

**GitHub Environment setup** (one-time):
1. Go to https://github.com/Hellblazer/nexus/settings/environments
2. Create environment named `pypi-release`
3. Add protection rules as desired (e.g., require review before publish)
```

**Step 2: Commit**

```bash
git add docs/contributing.md
git commit -m "Document PyPI OIDC trusted publisher one-time setup"
```

---

### Task 13: Update CI to enforce PR checks

**Files:**
- Modify: `.github/workflows/ci.yml`

**Step 1: Add branch protection note to contributing.md**

Append to the Git Workflow section in `docs/contributing.md`:

```markdown
The `main` branch requires CI to pass before merging. Configure branch protection at
https://github.com/Hellblazer/nexus/settings/branches:
- Require status checks: `pytest (3.12)` and `pytest (3.13)`
- Require branches to be up to date before merging
```

**Step 2: Commit**

```bash
git add docs/contributing.md
git commit -m "Document branch protection requirements for main"
```

---

### Phase 3 PR

```bash
gh pr create --title "Phase 3: Release infrastructure — version bump, release workflow, OIDC docs" --body "$(cat <<'EOF'
## Summary

- Bump version to 1.0.0-rc1 in pyproject.toml, CHANGELOG.md, marketplace.json
- Add .github/workflows/release.yml: tag-triggered, test-gated, OIDC PyPI publish + GitHub release
- Document PyPI trusted publisher one-time setup in contributing.md
- Document branch protection requirements

## Before Tagging

- [ ] Configure PyPI trusted publisher (one-time setup in PyPI UI)
- [ ] Create `pypi-release` GitHub environment
- [ ] Yank all 0.1.x–0.3.x PyPI versions (if not done in Phase 1)

## To Trigger Release

```bash
git tag -a v1.0.0-rc1 -m "1.0.0-rc1: First public release candidate"
git push origin v1.0.0-rc1
```
EOF
)"
```

---

## Final Verification Checklist

Before pushing the `v1.0.0-rc1` tag:

- [ ] All PyPI 0.x versions yanked
- [ ] `uv run pytest tests/` passes (all ~1258 tests)
- [ ] `uv run hatch build && uv run twine check dist/*` passes
- [ ] `nx doctor` output is clean and actionable
- [ ] Every `nx <command> --help` matches `docs/cli-reference.md`
- [ ] `CHANGELOG.md` has correct date for 1.0.0-rc1 entry
- [ ] PyPI trusted publisher configured
- [ ] `pypi-release` GitHub environment created
- [ ] `nx/.claude-plugin/` removed
