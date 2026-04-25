# release-sandbox: high-fidelity local pre-merge verification

A unified entry point that combines wheel-shape install, isolated `$HOME`, and (optionally) tmux-driven Claude Code, so any change touching the deployment surface can be exercised end-to-end before it lands on main.

Source: `tests/e2e/release-sandbox.sh`. Companion gist: https://gist.github.com/Hellblazer/511a05e1bf79dd6ea20be962d0ca04af

## When you must run this

Required before merging any PR that touches:

- `pyproject.toml`, `uv.lock` (dependency or version surface)
- `src/nexus/db/migrations.py` (T2 schema migrations — version-gated, only fire on real installs)
- `src/nexus/mcp/**` (MCP servers — registration only resolves in installed venv)
- `nx/**` (plugin manifest, hooks, agents, skills — `$CLAUDE_PLUGIN_ROOT` resolution depends on install layout)
- `.claude-plugin/**` (marketplace + plugin descriptors)
- `src/nexus/commands/doctor.py`, `src/nexus/commands/upgrade.py`
- Any code path that reads T2 / T3 state and ships to users

Recommended for any change that "feels like it might ship differently than it tests."

## Why merging-to-test is dangerous

Editable installs (`uv sync` / `pip install -e .`) walk the source tree for package data. A wheel install (`uv tool install`) only sees what `pyproject.toml` declared as package data. Files that exist on disk but are not in the wheel manifest disappear silently.

Same hazard for version-gated migrations: `apply_pending` filters by `pyproject.toml`'s version, so a migration written at `4.14.0` is invisible to a tool venv still on `4.13.0`. CI sees the new migration in the source tree and runs it; users with stale local installs do not.

`release-sandbox.sh` mirrors a fresh PyPI install and runs the canary checks against that, so deployment gaps surface here instead of in user reports.

## Modes

### `smoke`

Reinstall the tool venv, create a fresh isolated `$HOME`, then run from `/tmp`:

- `nx --version` (sanity)
- `nx upgrade --dry-run` (preview migrations)
- `nx upgrade` (apply)
- `nx doctor --check-schema` (T2 schema sanity)
- `nx doctor --check-plan-library` (builtin plan count)
- `nx doctor --check-taxonomy` (topic_links invariant)
- `nx doctor --check-hooks` (slow PostToolUse firings)

Pass / fail per check. Total time ~2 min including reinstall.

```bash
./tests/e2e/release-sandbox.sh smoke
```

**Known fresh-sandbox failures** (not script bugs, expected for a clean sandbox):

- `--check-plan-library`: reports `global-tier builtin count 0 < expected 9` because `nx catalog setup` has not run in the sandbox. To exercise plan-library code paths against the canonical seeded set, drop into `shell` mode and run `nx catalog setup` first.
- `nx upgrade` may report `OldLayoutDetected` if your shell environment has cloud ChromaDB credentials pointing at a legacy four-database tenant. Unset `CHROMA_*` before running, or expect the noise.

### `shell`

Reinstall + drop into a sandbox bash subshell with `HOME=$SANDBOX`. Use for hand-driving `nx index`, `nx search`, `nx catalog`, `nx taxonomy` against a clean state.

```bash
./tests/e2e/release-sandbox.sh shell
(sandbox) nx catalog setup
(sandbox) nx index repo /path/to/test-repo
(sandbox) nx search "..."
(sandbox) exit       # normal exit restores your real $HOME
```

The subshell prompt is `(sandbox) $`. Plain `exit` tears down the env.

### `tmux`

Reinstall + isolated `$HOME` + launch Claude Code in a tmux pane against the sandbox. Use for end-to-end exercises against the real MCP / plugin / hooks surface.

Prerequisites: `tests/e2e/.claude-auth/.credentials.json` must exist. Run `tests/e2e/auth-login.sh` once to cache OAuth from the macOS Keychain.

```bash
./tests/e2e/auth-login.sh         # one-time cache
./tests/e2e/release-sandbox.sh tmux
# tmux attaches automatically; Ctrl-b d to detach
```

Inside tmux, you have a real Claude Code session running against the wheel-installed `nx`, isolated from your live config.

### `reset`

Tear down `~/nexus-sandbox` without reinstalling. Useful when you want to start clean without paying the install cost.

```bash
./tests/e2e/release-sandbox.sh reset
```

## Options

- `--skip-install`: skip the reinstall step. Reuse the current tool venv. Useful when iterating on shell or tmux flow without re-paying the install cost.
- `--keep-existing`: reuse `$HOME/nexus-sandbox` if it exists. Default behaviour blows it away and recreates it for reproducibility.

## The `/tmp` rule

Every `nx` invocation in `smoke` mode runs from `/tmp`. This is load-bearing.

When `nx` runs from inside the source tree, package-data resolution can fall back to walking parent directories until it finds the source layout. From `/tmp`, only the wheel manifest matters. Bugs that pass in-repo and fail in-deployment surface in `/tmp` and not before.

If you write your own ad-hoc verification, run from `/tmp` too.

## Decide: ship or fix

After `smoke`:

- All checks pass → safe to merge / tag / release.
- Any check fails → **stop**. Open a fix-it commit on the same PR (or a separate hotfix PR) before continuing. Do not merge intending to fix on main; the gap was visible here, fix here.

A failing `--check-plan-library` on a fresh sandbox is the only expected failure (see Known above). Anything else is real.

## Cleanup

`shell` mode tears down on subshell exit. `tmux` mode requires `tmux kill-session -t nexus-sandbox` (or detach + let it idle). `smoke` leaves the sandbox in place for inspection; run `reset` when done.

Sandbox lives at `~/nexus-sandbox`. Safe to `rm -rf` directly if anything goes wrong.

## What this does not catch

- Cloud-only behaviour (Voyage AI rate limits, ChromaDB Cloud quota exhaustion at scale). For those, run `pytest -m integration` against real credentials.
- Concurrent-write races in T1 / T3. Worth a separate harness when relevant.
- UI / interactive surface that requires keystrokes. Use `tmux` mode plus the `tests/cc-validation/` scenario harness.
