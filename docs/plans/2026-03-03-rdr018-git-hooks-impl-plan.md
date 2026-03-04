# RDR-018 Implementation Plan: Replace nx serve with Git Hooks

**Epic**: nexus-cas3 (RDR-018: Replace nx serve polling server with git hooks)
**RDR**: docs/rdr/rdr-018-replace-serve-with-git-hooks.md
**Date**: 2026-03-03
**Status**: Ready for implementation

## Executive Summary

Replace the always-on Flask + Waitress polling server (`nx serve`) with event-driven
git hooks (`nx hooks`). This eliminates ~411 lines of daemon/polling code and replaces
it with ~250 lines of hook management + file locking + doctor updates. The result is
zero background processes, event-driven reindexing, and better diagnostics.

## Dependency Graph

```
Phase 1 (nexus-63w4) ──┐
  file lock + head_hash │
                        ├──> Phase 3 (nexus-q5ig) ──┐
Phase 2 (nexus-h68f) ──┤     CLI + reminder         │
  hooks install/        │                            ├──> Phase 5 (nexus-wt8m) ──> Phase 6 (nexus-4y7u)
  uninstall/status      └──> Phase 4 (nexus-8k3s)  ──┘     delete serve            docs update
                              doctor update
```

**Critical path**: Phase 1 or 2 (whichever finishes last) -> Phase 3 -> Phase 5 -> Phase 6

**Parallelization**: Phase 1 and Phase 2 are independent roots -- SPAWN parallel agents.
Phase 3 and Phase 4 can also run in parallel once dependencies resolve, but Phase 3
requires BOTH Phase 1 and Phase 2.

## Phase 1: index_repository file lock + head_hash update + --on-locked flag

**Bead**: nexus-63w4 (P1, root task, blocks nexus-q5ig)
**Estimate**: 60-90 minutes

### Files to modify

| File | Change |
|------|--------|
| `src/nexus/indexer.py` | Add `_lock_path()`, `on_locked` param, file lock, head_hash update |
| `src/nexus/commands/index.py` | Add `--on-locked` CLI option |

### Test file to create

`tests/test_index_lock.py`

### TDD Steps

**RED** -- Write failing tests first:

```python
# tests/test_index_lock.py
# Test functions to implement:

def test_lock_path_uses_repo_hash(tmp_path):
    """_lock_path() returns ~/.config/nexus/locks/<repo-hash>.lock"""
    # Verify lock path includes the 8-char hash from _repo_identity

def test_lock_skip_exits_immediately(tmp_path, monkeypatch):
    """on_locked='skip' returns without indexing when lock already held."""
    # Hold the lock in a separate thread, call index_repository(on_locked="skip")
    # Verify it returns immediately without running _run_index

def test_lock_wait_blocks_then_runs(tmp_path, monkeypatch):
    """on_locked='wait' blocks until lock released, then indexes."""
    # Hold lock briefly in a thread, release after 0.5s
    # Verify index_repository(on_locked="wait") waits and then completes

def test_frecency_only_bypasses_lock(tmp_path, monkeypatch):
    """frecency_only=True does not acquire the lock at all."""
    # Hold the lock, call index_repository(frecency_only=True)
    # Verify it runs successfully despite lock being held

def test_head_hash_updated_on_success(tmp_path, monkeypatch):
    """After full index, registry.head_hash == current HEAD."""
    # Mock _run_index, mock git rev-parse HEAD to return known hash
    # Verify registry.update called with head_hash=<that hash>

def test_head_hash_not_updated_on_frecency(tmp_path, monkeypatch):
    """Frecency-only run leaves head_hash unchanged."""
    # Run with frecency_only=True
    # Verify head_hash NOT updated in registry

def test_on_locked_cli_option(tmp_path):
    """nx index repo accepts --on-locked=skip and --on-locked=wait."""
    # Use CliRunner to invoke with --on-locked=skip
    # Verify option is parsed correctly
```

Run: `uv run pytest tests/test_index_lock.py -x` (expect failures)

**GREEN** -- Implement:

1. In `src/nexus/indexer.py`:
   - Import `fcntl`, `os` at top
   - Add helper: `_lock_path(repo: Path) -> Path` using `_repo_identity(repo)` hash
   - Add `on_locked: str = "wait"` parameter to `index_repository()`
   - Before the try/except block, if not `frecency_only`:
     - Create lock dir: `lock_path.parent.mkdir(parents=True, exist_ok=True)`
     - Open lock file, use `fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)` for skip
     - Catch `BlockingIOError` -> return empty dict for skip
     - Use `fcntl.flock(fd, fcntl.LOCK_EX)` for wait (blocks)
   - After successful full index (inside try, after `_run_index`):
     - Get current HEAD via `subprocess.run(["git", "rev-parse", "HEAD"], ...)`
     - `registry.update(repo, head_hash=current_head)`
   - Remove the comment referencing polling.py (lines 370-371) -- actually, defer
     this to Phase 5 since polling.py still exists at this point
   - Release lock in finally block

2. In `src/nexus/commands/index.py`:
   - Add `--on-locked` option: `@click.option("--on-locked", type=click.Choice(["skip", "wait"]), default="wait", ...)`
   - Pass `on_locked=on_locked` to `index_repository()`

Run: `uv run pytest tests/test_index_lock.py -v` (all pass)

**REFACTOR** -- Clean up and verify no regressions:

Run: `uv run pytest tests/ -x --ignore=tests/e2e`

### Acceptance Criteria

- [ ] Lock file created at `~/.config/nexus/locks/<repo-hash>.lock` during indexing
- [ ] `on_locked="skip"` returns immediately when lock held by another process
- [ ] `on_locked="wait"` blocks until lock released, then indexes
- [ ] `frecency_only=True` bypasses lock entirely
- [ ] `head_hash` updated in registry after successful full index
- [ ] `head_hash` NOT updated on frecency-only runs
- [ ] `--on-locked` CLI option accepted on `nx index repo`
- [ ] All existing tests pass (no regressions)

---

## Phase 2: nx hooks install/uninstall/status command

**Bead**: nexus-h68f (P1, root task, blocks nexus-8k3s and nexus-q5ig)
**Estimate**: 90-120 minutes

### Files to create

| File | Purpose |
|------|---------|
| `src/nexus/commands/hooks.py` | Click group: `nx hooks install/uninstall/status` |

### Test file to create

`tests/test_hooks.py`

### Constants

```python
SENTINEL_BEGIN = "# >>> nexus managed begin >>>"
SENTINEL_END = "# <<< nexus managed end <<<"
HOOK_NAMES = ("post-commit", "post-merge", "post-rewrite")
STANZA = '''# >>> nexus managed begin >>>
nx index repo "$(git rev-parse --show-toplevel)" --on-locked=skip \\
  >> "$HOME/.config/nexus/index.log" 2>&1 &
disown
# <<< nexus managed end <<<'''
SHEBANG = "#!/bin/sh"
```

### TDD Steps

**RED** -- Write failing tests:

```python
# tests/test_hooks.py
# All tests use tmp_path to create fake .git/hooks/ directories

def test_install_creates_hooks_in_empty_dir(tmp_path):
    """install creates post-commit, post-merge, post-rewrite with shebang + stanza."""

def test_install_sets_executable_bit(tmp_path):
    """Newly created hook files have the executable bit set."""

def test_install_appends_to_existing_hook(tmp_path):
    """When a hook file exists without sentinel, stanza is appended (no extra shebang)."""

def test_install_idempotent(tmp_path):
    """Running install twice does not duplicate the stanza."""

def test_uninstall_removes_owned_file(tmp_path):
    """When hook is owned (only shebang+stanza), file is deleted entirely."""

def test_uninstall_removes_stanza_preserves_rest(tmp_path):
    """When hook has other content + stanza, only stanza lines are removed."""

def test_uninstall_noop_when_not_installed(tmp_path):
    """uninstall on a repo without nexus hooks is a no-op."""

def test_status_reports_owned(tmp_path):
    """status shows 'managed (owned)' for nexus-only hook files."""

def test_status_reports_appended(tmp_path):
    """status shows 'managed (appended)' for hooks with other + nexus content."""

def test_status_reports_not_installed(tmp_path):
    """status shows 'not installed' for absent or sentinel-free hooks."""

def test_install_respects_core_hookspath(tmp_path):
    """When core.hooksPath is set, hooks go there instead of .git/hooks/."""

def test_install_resolves_worktree(tmp_path):
    """Worktree install goes into main repo's hooks dir, not gitlink stub."""

def test_install_warns_nonwritable_hookspath(tmp_path, capsys):
    """If core.hooksPath is non-writable, print warning and skip."""

def test_sentinel_detection_exact(tmp_path):
    """Only exact sentinel string matches, not looser comment patterns."""

def test_stanza_content_matches_rdr(tmp_path):
    """Generated stanza matches the exact text from RDR-018."""
```

Run: `uv run pytest tests/test_hooks.py -x` (expect failures)

**GREEN** -- Implement `src/nexus/commands/hooks.py`:

Core functions to implement:
1. `_effective_hooks_dir(repo: Path) -> Path` -- resolve via git rev-parse --git-common-dir + core.hooksPath
2. `_has_sentinel(hook_path: Path) -> bool` -- check for SENTINEL_BEGIN in file
3. `_is_owned(hook_path: Path) -> bool` -- file contains only shebang + stanza (no other content)
4. `_install_hook(hook_path: Path) -> str` -- returns "created" / "appended" / "already installed"
5. `_uninstall_hook(hook_path: Path) -> str` -- returns "removed" / "stanza removed" / "not installed"
6. `_hook_status(hook_path: Path) -> str` -- returns "managed (owned)" / "managed (appended)" / "not installed"

Click commands:
- `@hooks_group.command("install")` -- iterate HOOK_NAMES, call _install_hook, print per-hook status
- `@hooks_group.command("uninstall")` -- iterate HOOK_NAMES, call _uninstall_hook
- `@hooks_group.command("status")` -- iterate HOOK_NAMES, call _hook_status

Key implementation details:
- Use `subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=repo)` for worktree
- Use `subprocess.run(["git", "config", "core.hooksPath"], cwd=repo)` for custom hooks path
- When creating new files: write shebang + newline + stanza, set `os.chmod(path, 0o755)`
- When appending: add newline + stanza to end of existing file
- Uninstall: read file, remove lines between sentinels (inclusive), write back
  - If only shebang remains (or file is empty), delete the file

Run: `uv run pytest tests/test_hooks.py -v` (all pass)

### Acceptance Criteria

- [ ] `nx hooks install` creates post-commit, post-merge, post-rewrite in effective hooks dir
- [ ] Install appends stanza to existing hooks without destroying content
- [ ] Install is idempotent (no duplicate stanzas)
- [ ] `nx hooks uninstall` removes owned files entirely
- [ ] Uninstall removes only the stanza from appended hooks
- [ ] `nx hooks status` correctly reports owned / appended / not-installed
- [ ] Respects `core.hooksPath` git config
- [ ] Resolves worktree via `git rev-parse --git-common-dir`
- [ ] Sets executable bit (`chmod +x`) on newly created files
- [ ] Warns clearly on non-writable `core.hooksPath`
- [ ] Stanza content matches RDR-018 specification exactly

---

## Phase 3: CLI registration + hooks reminder in nx index repo

**Bead**: nexus-q5ig (P1, depends on nexus-63w4 + nexus-h68f, blocks nexus-wt8m)
**Estimate**: 30-45 minutes

### Files to modify

| File | Change |
|------|--------|
| `src/nexus/cli.py` | Add `from nexus.commands.hooks import hooks_group` and `main.add_command(hooks_group, name="hooks")` |
| `src/nexus/commands/index.py` | Add hooks-not-installed reminder after indexing |

### Test file to create or extend

`tests/test_hooks_reminder.py`

### TDD Steps

**RED** -- Write failing tests:

```python
# tests/test_hooks_reminder.py

def test_hooks_command_registered():
    """'hooks' appears as a subcommand in nx --help."""
    # CliRunner, invoke main with --help, assert "hooks" in output

def test_index_repo_shows_reminder_when_hooks_missing(tmp_path, monkeypatch):
    """nx index repo prints tip when no hooks contain the nexus sentinel."""
    # Mock index_repository, mock _effective_hooks_dir to return a dir without hooks
    # Verify output contains "nx hooks install"

def test_index_repo_no_reminder_when_hooks_installed(tmp_path, monkeypatch):
    """No reminder when at least one hook has the sentinel."""
    # Create a hook file with sentinel in mock hooks dir
    # Verify output does NOT contain "nx hooks install"

def test_reminder_checks_effective_hooks_dir(tmp_path, monkeypatch):
    """Reminder uses _effective_hooks_dir (worktree-aware, core.hooksPath-aware)."""
    # Mock subprocess calls for git rev-parse --git-common-dir
    # Verify correct directory is checked
```

Run: `uv run pytest tests/test_hooks_reminder.py -x` (expect failures)

**GREEN** -- Implement:

1. `src/nexus/cli.py`:
   - Add import: `from nexus.commands.hooks import hooks_group`
   - Add registration: `main.add_command(hooks_group, name="hooks")`
   - Keep serve registration for now (removed in Phase 5)

2. `src/nexus/commands/index.py`:
   - Import: `from nexus.commands.hooks import _effective_hooks_dir, _has_sentinel, HOOK_NAMES, SENTINEL_BEGIN`
   - After `index_repository()` call and before "Done." echo, add:
     ```python
     # Check if hooks are installed
     try:
         hooks_dir = _effective_hooks_dir(path)
         has_hooks = any(
             _has_sentinel(hooks_dir / name)
             for name in HOOK_NAMES
         )
         if not has_hooks:
             click.echo("Tip: run `nx hooks install` to auto-index this repo on every commit.")
     except Exception:
         pass  # non-fatal, don't block indexing output
     ```

Run: `uv run pytest tests/test_hooks_reminder.py -v` (all pass)

### Acceptance Criteria

- [ ] `nx hooks` appears in `nx --help` output
- [ ] `nx index repo` prints hooks reminder when hooks not installed
- [ ] No reminder when hooks are installed (sentinel detected)
- [ ] Reminder checks effective hooks dir (worktree-aware, core.hooksPath-aware)
- [ ] Reminder is non-fatal (exception in check does not crash indexing)

---

## Phase 4: nx doctor hooks check + index log check

**Bead**: nexus-8k3s (P1, depends on nexus-h68f, blocks nexus-wt8m)
**Estimate**: 45-60 minutes

### Files to modify

| File | Change |
|------|--------|
| `src/nexus/commands/doctor.py` | Replace lines 163-170 (Nexus server block) with hooks + log checks |

### Test file to create

`tests/test_doctor_hooks.py`

### TDD Steps

**RED** -- Write failing tests:

```python
# tests/test_doctor_hooks.py

def test_doctor_hooks_all_installed(tmp_path, monkeypatch):
    """Doctor shows checkmark for repo with all 3 hooks installed."""

def test_doctor_hooks_not_installed(tmp_path, monkeypatch):
    """Doctor shows warning with Fix: hint for repo without hooks."""

def test_doctor_hooks_no_repos(tmp_path, monkeypatch):
    """Doctor shows 'no repos registered' when registry is empty."""

def test_doctor_hooks_core_hookspath(tmp_path, monkeypatch):
    """Doctor reports core.hooksPath when set."""

def test_doctor_index_log_exists(tmp_path, monkeypatch):
    """Doctor shows index log path and last-modified time."""

def test_doctor_index_log_not_exists(tmp_path, monkeypatch):
    """Doctor shows 'not created yet' when log absent."""

def test_doctor_no_serve_import():
    """doctor.py does not import from commands.serve."""
    # Read doctor.py source and verify no 'commands.serve' import
```

Run: `uv run pytest tests/test_doctor_hooks.py -x` (expect failures)

**GREEN** -- Implement in `src/nexus/commands/doctor.py`:

Replace the Nexus server block (lines 163-170) with:

1. **Hooks check** (non-fatal, always checkmark):
   ```python
   from nexus.commands.hooks import _effective_hooks_dir, _has_sentinel, HOOK_NAMES, SENTINEL_BEGIN
   from nexus.registry import RepoRegistry

   reg = RepoRegistry(Path.home() / ".config" / "nexus" / "repos.json")
   repos = reg.all()
   if not repos:
       lines.append(_check_line("git hooks", True, "no repos registered (run: nx index repo <path>)"))
   else:
       for repo_str in repos:
           repo = Path(repo_str)
           try:
               hooks_dir = _effective_hooks_dir(repo)
               installed = [n for n in HOOK_NAMES if _has_sentinel(hooks_dir / n)]
               if installed:
                   hook_list = ", ".join(installed)
                   # Check for core.hooksPath
                   detail = f"{repo_str} ({hook_list})"
                   lines.append(_check_line("git hooks", True, detail))
               else:
                   lines.append(_check_line("git hooks", True, f"{repo_str} -- not installed"))
                   _fix(lines, f"nx hooks install {repo_str}")
           except Exception:
               lines.append(_check_line("git hooks", True, f"{repo_str} -- check failed"))
   ```

2. **Index log check** (non-fatal, always checkmark):
   ```python
   log_path = Path.home() / ".config" / "nexus" / "index.log"
   if log_path.exists():
       import datetime
       mtime = log_path.stat().st_mtime
       dt = datetime.datetime.fromtimestamp(mtime, tz=datetime.timezone.utc)
       delta = datetime.datetime.now(datetime.timezone.utc) - dt
       if delta.total_seconds() < 60:
           ago = "just now"
       elif delta.total_seconds() < 3600:
           ago = f"{int(delta.total_seconds() / 60)} minutes ago"
       else:
           ago = f"{int(delta.total_seconds() / 3600)} hours ago"
       lines.append(_check_line("index log", True, f"{log_path} (last write: {ago})"))
   else:
       lines.append(_check_line("index log", True, f"{log_path} (not created yet -- hooks have not fired)"))
   ```

3. **Remove** the old serve import and check block.

Run: `uv run pytest tests/test_doctor_hooks.py -v` (all pass)

### Acceptance Criteria

- [ ] Doctor shows per-repo hook status with installed hook names
- [ ] Doctor shows `Fix: nx hooks install <path>` for repos without hooks
- [ ] Doctor shows "no repos registered" when registry empty
- [ ] Doctor reports `core.hooksPath` when set
- [ ] Doctor shows index log path and last-modified time
- [ ] Doctor shows "not created yet" when log absent
- [ ] No import from `commands/serve` in doctor.py
- [ ] All checks are non-fatal (always checkmark)

---

## Phase 5: Delete serve/polling/server code and tests

**Bead**: nexus-wt8m (P1, depends on nexus-8k3s + nexus-q5ig, blocks nexus-4y7u)
**Estimate**: 30-45 minutes

### Files to delete

| File | Reason |
|------|--------|
| `src/nexus/server.py` | Flask app + poll thread |
| `src/nexus/server_main.py` | Server entry point |
| `src/nexus/polling.py` | HEAD-hash polling |
| `src/nexus/commands/serve.py` | nx serve CLI |
| `tests/test_server.py` | Server tests |
| `tests/test_server_api.py` | Server API tests |
| `tests/test_serve_cmd.py` | Serve command tests |
| `tests/test_head_polling.py` | Polling tests |

### Files to modify

| File | Change |
|------|--------|
| `src/nexus/cli.py` | Remove `from nexus.commands.serve import serve` and `main.add_command(serve)` |
| `src/nexus/indexer.py` | Remove lines 370-371 (polling.py comment) |
| `tests/test_p0_regressions.py` | Remove `test_serve_stop_returns_nonzero_when_process_never_dies` function |
| `src/nexus/config.py` | Remove `"server"` key from `_DEFAULTS` dict |
| `pyproject.toml` | Remove `"flask>=3.0"` and `"waitress>=3.0"` from dependencies |

### TDD approach

For deletion phases, the "test" is: all remaining tests pass, and the deleted modules
are not importable.

```python
# tests/test_serve_deleted.py (quick verification)

def test_server_module_deleted():
    """server.py no longer importable."""
    import importlib
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("nexus.server")

def test_polling_module_deleted():
    """polling.py no longer importable."""
    import importlib
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("nexus.polling")

def test_serve_command_deleted():
    """commands/serve.py no longer importable."""
    import importlib
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("nexus.commands.serve")

def test_nx_help_no_serve():
    """'serve' does not appear in nx --help."""
    from click.testing import CliRunner
    from nexus.cli import main
    result = CliRunner().invoke(main, ["--help"])
    assert "serve" not in result.output
```

### Execution Steps

1. Delete the 8 files listed above
2. Modify `src/nexus/cli.py`:
   - Remove line: `from nexus.commands.serve import serve`
   - Remove line: `main.add_command(serve)`
3. Modify `src/nexus/indexer.py`:
   - Remove comment on lines 370-371: `# Re-raise so the polling loop skips recording head_hash for this repo.` / `# A clean return would incorrectly signal success (see polling.py).`
   - Replace with: `# Re-raise so callers know indexing failed.`
4. Modify `tests/test_p0_regressions.py`:
   - Remove `test_serve_stop_returns_nonzero_when_process_never_dies` and its imports
5. Modify `src/nexus/config.py`:
   - Remove `"server": {"port": 7890, "headPollInterval": 10, "ignorePatterns": []}` from `_DEFAULTS`
6. Modify `pyproject.toml`:
   - Remove `"flask>=3.0"` from dependencies
   - Remove `"waitress>=3.0"` from dependencies
7. Run `uv sync` to update lock file
8. Write `tests/test_serve_deleted.py` verification test
9. Run: `uv run pytest tests/ -x --ignore=tests/e2e`

### Acceptance Criteria

- [ ] All 8 files deleted
- [ ] `serve` removed from `cli.py` imports and registration
- [ ] `polling.py` comment removed from `indexer.py`
- [ ] Serve test removed from `test_p0_regressions.py`
- [ ] `server` config defaults removed from `config.py`
- [ ] `flask` and `waitress` removed from `pyproject.toml` dependencies
- [ ] `nx serve` no longer appears in `nx --help`
- [ ] Verification tests pass (deleted modules not importable)
- [ ] All remaining tests pass

---

## Phase 6: Documentation updates

**Bead**: nexus-4y7u (P2, depends on nexus-wt8m)
**Estimate**: 30-45 minutes

### Files to modify

| File | Change |
|------|--------|
| `docs/cli-reference.md` | Remove `## nx serve` section (lines ~194-207); add `## nx hooks` section; update doctor description |
| `docs/repo-indexing.md` | Replace line 189 (HEAD polling reference) with hooks explanation |
| `docs/architecture.md` | Update line 46 (`Server` row) to `Hooks` with new file list |
| `docs/configuration.md` | Remove `server.port` row (line 31) |

### Execution Steps

1. `docs/cli-reference.md`:
   - Delete the `## nx serve` section
   - Add new section:
     ```markdown
     ## nx hooks

     Manage git hooks for automatic background reindexing.

     | Subcommand | Description |
     |------------|-------------|
     | `install [PATH]` | Install nexus hooks into the repo at PATH (default: cwd) |
     | `uninstall [PATH]` | Remove nexus hooks from the repo |
     | `status [PATH]` | Show which hooks are installed |
     ```
   - Update doctor description to mention hooks check instead of server check

2. `docs/repo-indexing.md`:
   - Replace "HEAD polling via `nx serve`..." with:
     "Git hooks installed via `nx hooks install` trigger automatic background
     reindexing after each commit, merge, or rebase."

3. `docs/architecture.md`:
   - Update the Server row in the module table:
     From: `| **Server** | server.py, server_main.py, polling.py | Daemon, HEAD polling, auto-reindex |`
     To: `| **Hooks** | commands/hooks.py | Git hook install/uninstall/status, auto-reindex |`

4. `docs/configuration.md`:
   - Remove the `server.port` row from the configuration table

### Acceptance Criteria

- [ ] `cli-reference.md` has `nx hooks` section, no `nx serve` section
- [ ] `repo-indexing.md` references hooks, not polling
- [ ] `architecture.md` module table updated (Server -> Hooks)
- [ ] `configuration.md` `server.port` row removed
- [ ] No stale `nx serve` or `polling` references in `docs/` (outside historical/ and rdr/)

---

## Risk Factors and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| `fcntl.flock` not available on target platform | Lock mechanism fails | Project is Unix-only (shell scripts, disown); fcntl is stdlib on all Unix |
| Existing hook files have unexpected formats | Install/uninstall corrupts hooks | Sentinel-bounded stanza is self-contained; only exact sentinel triggers detection |
| `git rev-parse --git-common-dir` fails on unusual git configs | Wrong hooks directory | Fallback to `.git/hooks/` when git command fails |
| Flask/waitress removal breaks downstream package | Import errors | Both are only imported by deleted modules; grep verification in Phase 5 |
| `head_hash` update race with concurrent hooks | Stale hash | File lock prevents concurrent index runs; hash written after lock-protected index |

## Summary of Beads

| Bead | Phase | Title | Priority | Dependencies |
|------|-------|-------|----------|--------------|
| nexus-cas3 | Epic | RDR-018: Replace nx serve polling server with git hooks | P1 | -- |
| nexus-63w4 | 1 | index_repository file lock + head_hash + --on-locked | P1 | none |
| nexus-h68f | 2 | nx hooks install/uninstall/status command | P1 | none |
| nexus-q5ig | 3 | CLI registration + hooks reminder in nx index repo | P1 | nexus-63w4, nexus-h68f |
| nexus-8k3s | 4 | nx doctor hooks check + index log check | P1 | nexus-h68f |
| nexus-wt8m | 5 | Delete serve/polling/server code and tests | P1 | nexus-8k3s, nexus-q5ig |
| nexus-4y7u | 6 | Documentation updates | P2 | nexus-wt8m |

## Net Code Change Estimate

- **Deleted**: ~411 lines (server.py 125 + polling.py 77 + serve.py 209) + ~400 lines tests
- **Added**: ~250 lines (hooks.py ~180 + indexer lock ~40 + doctor ~30) + ~300 lines tests
- **Modified**: ~20 lines across cli.py, config.py, indexer.py, pyproject.toml
- **Net**: ~160 fewer production lines, simpler architecture
