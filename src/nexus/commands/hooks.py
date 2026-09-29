# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx hooks — git hook management for automatic repo indexing."""
import re
import shlex
import stat
from pathlib import Path

import click

# nexus-8g79.10 (V2): sentinels + git helpers live in
# ``nexus._git_hooks_meta`` so library-layer probes
# (``nexus.health``) don't reach up into this CLI module. The
# lower-layer ``git_common_dir`` raises ``RuntimeError`` for
# non-git-repo; this CLI module translates to ``ClickException``
# at the boundary.
#
# Sentinels are constants (value-bound is fine). For
# ``effective_hooks_dir`` we use a thin wrapper so test
# monkeypatches on ``nexus._git_hooks_meta.effective_hooks_dir``
# reach the live binding at call time (a bare ``from … import …``
# captures the function at import time and bypasses patches).
from nexus import _git_hooks_meta as _ghm
from nexus._git_hooks_meta import SENTINEL_BEGIN, SENTINEL_END


def _effective_hooks_dir(repo):
    """Delegate to ``nexus._git_hooks_meta.effective_hooks_dir``.

    The lower layer raises ``RuntimeError`` for a non-git path; every
    ``nx hooks`` verb resolves the directory through here, so translate
    once at the CLI boundary (nexus-sis0m.6: ``status``, ``install``,
    ``uninstall`` and ``update`` printed a traceback outside a repo).
    ``ClickException`` is not a ``RuntimeError``;
    ``refresh_all_managed_hooks`` catches both, the ``RuntimeError`` arm
    being a defensive guard now. A nonexistent path is NOT refused here
    (``nx doctor`` resolves registered repos through this and renders a
    vanished one as ``unknown``); the verbs that take a path use
    ``_verb_hooks_dir``.
    """
    try:
        return _ghm.effective_hooks_dir(repo)
    except RuntimeError as exc:
        raise click.ClickException(str(exc))


def _verb_hooks_dir(repo: Path) -> Path:
    """Hooks directory for a path a user typed to ``status`` / ``install`` /
    ``uninstall`` / ``update``. ``git`` runs with the path as its cwd, so a
    missing path would otherwise escape as a ``FileNotFoundError``
    traceback (nexus-sis0m.6)."""
    if not repo.is_dir():
        raise click.ClickException(f"Not a directory: {repo}")
    return _effective_hooks_dir(repo)


def _git_common_dir_raw(repo):
    """Delegate to ``nexus._git_hooks_meta.git_common_dir``."""
    return _ghm.git_common_dir(repo)

_HOOK_NAMES = ("post-commit", "post-merge", "post-rewrite")

_STANZA = """\
{begin}
REPO_TOP="$(git rev-parse --show-toplevel)"
# LINKED-WORKTREE GUARD (nexus-ws67k, 2026-08-23). A worktree is a transient
# VIEW of a repository, not a thing worth indexing:
#   * it is ephemeral by design -- created for a branch, deleted when done, so
#     any index of it describes a path that will not exist tomorrow;
#   * N worktrees of one repo hold byte-identical content, so each one is a
#     full re-index of a tree already indexed;
#   * the pgrep guard below compares RESOLVED PATHS, so it cannot see a sibling
#     worktree indexing the same repo -- three views run three concurrent full
#     indexes, each believing it is alone;
#   * hooks live in the COMMON git dir, so installing once in the primary arms
#     every worktree automatically. Nobody opts in.
# Measured 2026-08-23 over ~/.config/nexus/index.log: 433 runs in 14 days, 9 of
# them worktree-targeted in a single day, each re-embedding hundreds of files of
# a ~2151-file tree, several projected at 64-131 minutes, all detached so the
# cost never lands on the committing session's clock.
# --git-dir equals --git-common-dir in a primary checkout and differs in a
# linked worktree; both are normalised to absolute physical paths first because
# git returns a bare ".git" for the primary and an absolute path for a worktree.
# INTERIM, AND DELIBERATELY NARROWER THAN THE FIX. It suppresses indexing of
# feature-branch views, which is the safer default while the catalog carries NO
# branch dimension at all (head_hash is per-OWNER, so whichever branch indexes
# last overwrites the one shared corpus; on 2026-08-23 that corpus briefly
# carried a head_hash for a commit that had been rebased away and existed in no
# branch).
# The durable model, Sam 2026-08-23, is its own RDR and this guard is NOT it:
#   * index BRANCHES, not checkouts -- a branch is the durable unit, a checkout
#     path is a transient view of one;
#   * OPT IN to what is indexed -- nexus has two stable branches (main,
#     develop); everything else should be silent by default rather than indexed
#     by default;
#   * a directory changing is NOT a complete reindex -- measured over the last
#     12 develop commits, each touched 1-5 files while the indexer re-processed
#     182-441 per run, roughly 100x amplification. Git already knows the exact
#     changed set; the staleness scan walks the whole tree and ignores it.
# Do not extend this guard toward that design in place. It is a stopgap.
_NX_GIT_DIR="$(cd "$(git rev-parse --git-dir 2>/dev/null)" 2>/dev/null && pwd -P)"
_NX_GIT_COMMON="$(cd "$(git rev-parse --git-common-dir 2>/dev/null)" 2>/dev/null && pwd -P)"
if [ -n "$_NX_GIT_DIR" ] && [ -n "$_NX_GIT_COMMON" ] && [ "$_NX_GIT_DIR" != "$_NX_GIT_COMMON" ]; then
  echo "=== nx index post-commit SKIPPED (linked worktree, nexus-ws67k) $REPO_TOP $(date '+%Y-%m-%dT%H:%M:%S%z') ===" \\
    >> "$HOME/.config/nexus/index.log"
  exit 0
fi
# pgrep guard (nexus-mkj6u 2026-05-23): skip if an indexer for THIS
# repo is already running. Belt-and-suspenders with --on-locked=skip,
# which races on lock acquisition under burst-commit workloads. The
# race fires when 2+ commits happen before the first indexer finishes
# its open()+truncate+write+flock sequence; the second indexer can
# truncate the lock file out from under the first and still get past
# its own flock if the timing aligns. pgrep at the hook layer catches
# 99%+ of pile-ups before they fork.
if pgrep -f "nx index repo $REPO_TOP" > /dev/null 2>&1; then
  exit 0
fi
# nexus-q3xrx: one stamped header per hook run — crash tracebacks (Python
# default excepthook -> stderr -> this redirect) land RAW and undatable
# without it; the header bounds every entry to a dated run window.
# The dispatch below is DETACHED, so this redirect is the only sink its
# stdout/stderr has -- it cannot be dropped. It also never rotated, and
# reached 44MB / 725k lines over two months on a working box, which is how
# 1528 aspect_source_path_uncanonical warnings and 49 manifest write
# failures accumulated unread. Bound it here, at the only layer that sees
# the whole process output: one generation, 4MiB.
NX_INDEX_LOG="$HOME/.config/nexus/index.log"
if [ -f "$NX_INDEX_LOG" ] && [ "$(wc -c < "$NX_INDEX_LOG" 2>/dev/null || echo 0)" -gt 4194304 ]; then
  mv -f "$NX_INDEX_LOG" "$NX_INDEX_LOG.1" 2>/dev/null || :
fi
echo "=== nx index post-commit $REPO_TOP $(date '+%Y-%m-%dT%H:%M:%S%z') ===" \\
  >> "$NX_INDEX_LOG"
nx index repo "$REPO_TOP" --since-head --on-locked=skip \\
  >> "$NX_INDEX_LOG" 2>&1 &
disown
{end}""".format(begin=SENTINEL_BEGIN, end=SENTINEL_END)


def _stanza_for(hook_name: str) -> str:
    """The stanza body installed for *hook_name*: the indexing stanza,
    identical for every hook. (The per-commit review stanza that
    ``post-commit`` carried from nexus-jh86x to 2026-09-05 was deleted
    with the reviewer; the parameter stays so the two callers that resolve
    a hook's body by name keep one call shape.)"""
    del hook_name
    return _STANZA


# ── git helpers ───────────────────────────────────────────────────────────────


def _git_common_dir(repo: Path) -> Path:
    """CLI-layer wrapper: translate RuntimeError → ClickException."""
    try:
        return _git_common_dir_raw(repo)
    except RuntimeError as exc:
        raise click.ClickException(str(exc))


# ── stanza helpers ────────────────────────────────────────────────────────────


def _remove_stanza(content: str) -> str:
    """Remove the nexus sentinel stanza from *content*."""
    return re.sub(
        rf"\n?{re.escape(SENTINEL_BEGIN)}.*?{re.escape(SENTINEL_END)}\n?",
        "",
        content,
        flags=re.DOTALL,
    )


def _has_unterminated_sentinel(content: str) -> bool:
    """True when *content* has a nexus begin marker with no end marker after
    it. There is no safe automatic repair: the stanza's extent is unknown,
    so stripping from the begin marker to EOF could delete the user's own
    hook content. Every verb that would rewrite the file refuses instead."""
    start = content.find(SENTINEL_BEGIN)
    return start != -1 and content.find(SENTINEL_END, start) == -1


def _malformed_message(hook_file: Path) -> str:
    return (
        f"Malformed nexus sentinel in {hook_file}: begin marker without an end "
        "marker. Repair it by hand (complete or remove the stanza), then re-run."
    )


def _refuse_malformed(hooks_dir: Path) -> None:
    """Raise ``ClickException`` naming every malformed hook in *hooks_dir*,
    before any verb has rewritten anything, so a refusal never leaves a
    half-updated set behind."""
    bad = [
        hooks_dir / n for n in _HOOK_NAMES
        if (hooks_dir / n).is_file()
        and _has_unterminated_sentinel((hooks_dir / n).read_text())
    ]
    if bad:
        raise click.ClickException("\n".join(_malformed_message(p) for p in bad))


def _hook_status(hooks_dir: Path, hook_name: str) -> str:
    """Return status string: 'not installed' | 'unmanaged' | 'malformed' |
    'owned' | 'appended'."""
    hook_file = hooks_dir / hook_name
    if not hook_file.exists():
        return "not installed"
    content = hook_file.read_text()
    if SENTINEL_BEGIN not in content:
        return "unmanaged"
    if _has_unterminated_sentinel(content):
        return "malformed"
    remainder = _remove_stanza(content).strip()
    if remainder in ("", "#!/bin/sh"):
        return "owned"
    return "appended"


def hook_stanza_state(repo: Path, hook_name: str = "post-commit") -> str:
    """One word for what *repo*'s *hook_name* will do on the next commit.

    ``armed``: the installed stanza equals the current template.
    ``stale``: a nexus stanza is installed but differs from the template
    (a release changed it and ``nx hooks update`` has not run,
    nexus-trwxr). ``malformed``: a begin sentinel with no end sentinel;
    ``nx hooks update`` cannot repair it (nexus-sis0m.6), so it is not
    ``stale``. ``unmanaged``: a hook exists without a nexus sentinel.
    ``not installed``: no hook file. ``unknown``: not a git repository.

    ``nx doctor`` has computed the stale case since nexus-mkj6u, but as a
    line in a long report nobody was reading; doctor now resolves each
    hook's state through this function too, so there is one comparison.
    """
    try:
        # core.hooksPath honoured, like install/update/status and nx doctor
        # (critique [24292]: the first cut used the common dir directly and
        # would have told a core.hooksPath user "not installed" forever).
        return _stanza_state_in(_effective_hooks_dir(repo), hook_name)
    except Exception:  # noqa: BLE001 - a census line must never traceback
        return "unknown"


def _stanza_state_in(hooks_dir: Path, hook_name: str) -> str:
    """``hook_stanza_state`` for an already-resolved hooks directory.

    The single comparison ``nx doctor`` and ``nx hooks status`` both
    resolve through (nexus-sis0m.6: status had its own sentinel-only
    check and said "owned" for a hook doctor called drifted). Raises on
    an unreadable hook file; the public wrapper degrades to ``unknown``.
    """
    status = _hook_status(hooks_dir, hook_name)
    if status in ("not installed", "unmanaged", "malformed"):
        return status
    installed = (hooks_dir / hook_name).read_text()
    return "armed" if stanza_body(installed) == stanza_body(_stanza_for(hook_name)) else "stale"


def stanza_body(text: str) -> str | None:
    """The text between the nexus sentinels, or None when absent. The one
    definition of "the installed stanza" that both hook_stanza_state and
    nx doctor compare through."""
    m = re.search(
        rf"{re.escape(SENTINEL_BEGIN)}\n(.*?)\n{re.escape(SENTINEL_END)}", text, re.DOTALL
    )
    return m.group(1) if m else None


def _install_hook(hooks_dir: Path, hook_name: str) -> str:
    """Install or append nexus stanza. Returns 'created' | 'appended' | 'already installed'."""
    hook_file = hooks_dir / hook_name
    if not hook_file.exists():
        hook_file.write_text(f"#!/bin/sh\n{_stanza_for(hook_name)}\n")
        hook_file.chmod(0o755)
        return "created"

    content = hook_file.read_text()
    if _has_unterminated_sentinel(content):
        # "already installed" would be a false success: the stanza is not
        # in a runnable state and nothing here can fix it.
        raise click.ClickException(_malformed_message(hook_file))
    if SENTINEL_BEGIN in content:
        return "already installed"

    # Append to existing hook
    hook_file.write_text(content.rstrip("\n") + "\n" + _stanza_for(hook_name) + "\n")
    return "appended"


def _uninstall_hook(hooks_dir: Path, hook_name: str) -> str:
    """Remove nexus stanza. Returns 'removed' | 'stanza removed' | 'not installed'."""
    hook_file = hooks_dir / hook_name
    if not hook_file.exists():
        return "not installed"
    content = hook_file.read_text()
    if SENTINEL_BEGIN not in content:
        return "not installed"
    if _has_unterminated_sentinel(content):
        # _remove_stanza matches nothing here, so "stanza removed" would
        # rewrite the file unchanged and claim success.
        raise click.ClickException(_malformed_message(hook_file))

    new_content = _remove_stanza(content)
    if new_content.strip() in ("", "#!/bin/sh"):
        hook_file.unlink()
        return "removed"

    hook_file.write_text(new_content)
    return "stanza removed"


# ── CLI ───────────────────────────────────────────────────────────────────────


@click.group()
def hooks() -> None:
    """Manage git hooks for automatic repo indexing.

    Distinct from ``nx hook`` (singular) which handles Claude Code session hooks.
    """


@hooks.command("install")
@click.argument("path", type=click.Path(file_okay=False, path_type=Path), default=".")
def hooks_install(path: Path) -> None:
    """Install nexus git hooks into PATH (default: current directory).

    Installs post-commit, post-merge, and post-rewrite hooks that run
    ``nx index repo`` in the background after each qualifying git operation.
    Appends a sentinel-bounded stanza to existing hook files without
    overwriting them.
    """
    repo = path.resolve()

    hooks_dir = _verb_hooks_dir(repo)
    _refuse_malformed(hooks_dir)

    # Check writeability
    if hooks_dir.exists() and not _is_writable(hooks_dir):
        raise click.ClickException(
            f"Hooks directory is not writable: {hooks_dir}\n"
            "Check core.hooksPath or directory permissions."
        )

    hooks_dir.mkdir(parents=True, exist_ok=True)
    click.echo(f"Installing hooks for {repo}…")

    for name in _HOOK_NAMES:
        action = _install_hook(hooks_dir, name)
        symbol = "✓" if action != "already installed" else "·"
        click.echo(f"  {symbol} {name}  ({action})")

    click.echo("Done. Indexing will run in the background after each commit.")


@hooks.command("uninstall")
@click.argument("path", type=click.Path(file_okay=False, path_type=Path), default=".")
def hooks_uninstall(path: Path) -> None:
    """Remove nexus git hooks from PATH (default: current directory).

    Removes the nexus-managed sentinel stanza; leaves any unrelated hook
    content intact.
    """
    repo = path.resolve()
    hooks_dir = _verb_hooks_dir(repo)
    _refuse_malformed(hooks_dir)

    click.echo(f"Removing nexus hooks from {repo}…")

    for name in _HOOK_NAMES:
        action = _uninstall_hook(hooks_dir, name)
        symbol = "✓" if action != "not installed" else "·"
        click.echo(f"  {symbol} {name}  ({action})")

    click.echo("Done.")


@hooks.command("update")
@click.argument("path", type=click.Path(file_okay=False, path_type=Path), default=".")
def hooks_update(path: Path) -> None:
    """Refresh nexus git hooks to the current stanza (nexus-mkj6u shakeout).

    Equivalent to ``nx hooks uninstall && nx hooks install`` in one step.
    Use this when ``nx doctor`` reports stanza drift — typically after a
    conexus upgrade that changed the stanza (e.g. the 2026-05-23 pgrep
    guard for the multi-indexer pile-up race).

    Only rewrites hooks that are currently nexus-managed (have the
    sentinel block); never touches unmanaged hook files.
    """
    repo = path.resolve()
    hooks_dir = _verb_hooks_dir(repo)
    _refuse_malformed(hooks_dir)

    if hooks_dir.exists() and not _is_writable(hooks_dir):
        raise click.ClickException(
            f"Hooks directory is not writable: {hooks_dir}\n"
            "Check core.hooksPath or directory permissions."
        )

    hooks_dir.mkdir(parents=True, exist_ok=True)
    click.echo(f"Updating nexus hooks in {repo}…")

    for name in _HOOK_NAMES:
        hook_file = hooks_dir / name
        if not hook_file.exists():
            click.echo(f"  · {name}  (not installed; skipped)")
            continue
        content = hook_file.read_text()
        if SENTINEL_BEGIN not in content:
            click.echo(f"  · {name}  (unmanaged; skipped)")
            continue
        # Rewrite: remove old stanza, install fresh one. The
        # _install_hook path handles both "owned" (file has only the
        # stanza + shebang) and "appended" (other content present)
        # cases correctly.
        _uninstall_hook(hooks_dir, name)
        action = _install_hook(hooks_dir, name)
        click.echo(f"  ✓ {name}  (refreshed: {action})")

    click.echo("Done. New stanza in effect from the next commit.")


def _refresh_managed_hooks(hooks_dir: Path) -> list[tuple[str, str]]:
    """Refresh every nexus-managed hook in *hooks_dir* to the current stanza.

    Only rewrites hooks that already carry the sentinel block; never touches
    unmanaged or absent hook files. Returns a list of ``(hook_name, action)``
    where action is ``refreshed:<install-action>`` | ``unmanaged`` |
    ``malformed`` (begin sentinel without an end; left untouched) |
    ``not installed``.
    """
    results: list[tuple[str, str]] = []
    for name in _HOOK_NAMES:
        hook_file = hooks_dir / name
        if not hook_file.exists():
            results.append((name, "not installed"))
            continue
        content = hook_file.read_text()
        if SENTINEL_BEGIN not in content:
            results.append((name, "unmanaged"))
            continue
        if _has_unterminated_sentinel(content):
            # Not refreshable, and not "refreshed:already installed" either.
            results.append((name, "malformed"))
            continue
        _uninstall_hook(hooks_dir, name)
        action = _install_hook(hooks_dir, name)
        results.append((name, f"refreshed:{action}"))
    return results


def _iter_managed_repo_roots() -> list[Path]:
    """Return existing registered repo working trees (catalog ∪ registry).

    Reuses ``list_repos_dual`` — the same canonical enumeration ``nx doctor``
    uses for its git-hook drift check — so every repo the doctor reports drift
    for is reachable here. Resilient: returns ``[]`` when the catalog is
    uninitialised or unreadable rather than raising, because the caller
    (``nx upgrade``) treats hook refresh as best-effort.
    """
    try:
        from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — command-local import (catalog.factory)
        from nexus.config import nexus_config_dir  # noqa: PLC0415 — command-local import (config)
        from nexus.repos import list_repos_dual  # noqa: PLC0415 — command-local import (repos)

        cat = make_catalog_reader()
        if cat is None:
            return []
        registry_path = nexus_config_dir() / "repos.json"
        repo_strs = list_repos_dual(cat=cat, registry_path=registry_path)
    except Exception:  # noqa: BLE001 — best-effort enumeration
        return []

    seen: set[Path] = set()
    repos: list[Path] = []
    for repo_str in repo_strs:
        repo = Path(repo_str)
        if repo in seen or not repo.is_dir():
            continue
        seen.add(repo)
        repos.append(repo)
    return repos


def refresh_all_managed_hooks(*, echo: bool = False) -> dict[str, int]:
    """Refresh nexus-managed git hooks across every catalog-registered repo.

    Best-effort: a repo that can't be resolved (non-git, hooks dir not
    writable, etc.) is counted under ``errors`` and skipped — one bad repo
    never aborts the sweep. Returns a summary dict with ``repos``,
    ``refreshed``, and ``errors`` counts.
    """
    summary = {"repos": 0, "refreshed": 0, "errors": 0}
    for repo in _iter_managed_repo_roots():
        try:
            # nexus-g76yf: the lower layer raises a raw ``RuntimeError``
            # ("Not a git repository: <path>") when a repo entry no longer
            # resolves as a git checkout -- moved, deleted, or a stale/bad
            # registry entry. Before nexus-sis0m.6 ``_effective_hooks_dir``
            # passed it through untranslated, and catching only
            # ``click.ClickException`` here let it abort the WHOLE sweep
            # (every repo after the bad one silently unrefreshed). It now
            # translates to ``ClickException``, so that arm is the one that
            # fires; the ``RuntimeError`` arm below is kept as a defensive
            # guard for any other path that raises it, because one bad
            # entry must stay a per-repo skip, never a sweep abort.
            hooks_dir = _effective_hooks_dir(repo)
            if hooks_dir.exists() and not _is_writable(hooks_dir):
                summary["errors"] += 1
                if echo:
                    click.echo(f"  ! {repo}  (hooks dir not writable; skipped)")
                continue
            results = _refresh_managed_hooks(hooks_dir)
        except (click.ClickException, RuntimeError) as exc:
            summary["errors"] += 1
            if echo:
                msg = exc.format_message() if isinstance(exc, click.ClickException) else str(exc)
                click.echo(f"  ! {repo}  ({msg})")
            continue

        for n, a in results:
            if a == "malformed":
                summary["errors"] += 1
                if echo:
                    click.echo(
                        f"  ! {hooks_dir / n}  (malformed sentinel, begin without "
                        "end; not refreshed, repair by hand)"
                    )

        refreshed = [n for n, a in results if a.startswith("refreshed")]
        if refreshed:
            summary["repos"] += 1
            summary["refreshed"] += len(refreshed)
            if echo:
                click.echo(f"  ✓ {repo}  ({len(refreshed)} hook(s) refreshed)")
    return summary


# RDR-185 P4.1 (nexus-n7u38.28): DEMOTED to an internal primitive — hidden
# from the user-facing surface, still callable + tested for surgical/dev use.
# Its job is the upgrade ladder's now (`nx upgrade` refreshes managed hooks itself).
# NOT deleted: hiding keeps scripts/surgical use working, and RDR-155 P4b
# owns the migration module's actual deletion (standing blocker).
@hooks.command("update-all", hidden=True)
def hooks_update_all() -> None:
    """Refresh nexus-managed git hooks across ALL catalog-registered repos.

    Sweeps every ``repo`` owner in the catalog and refreshes any hook that
    already carries the nexus stanza, so a single command brings every repo
    to the current stanza after a conexus upgrade. Unmanaged and uninstalled
    hooks are left untouched. This is also run automatically by ``nx upgrade``.
    """
    click.echo("Refreshing nexus hooks across all registered repos…")
    summary = refresh_all_managed_hooks(echo=True)
    if summary["repos"] == 0 and summary["errors"] == 0:
        click.echo("No nexus-managed hooks found in any registered repo.")
        return
    click.echo(
        f"Done. {summary['refreshed']} hook(s) refreshed across "
        f"{summary['repos']} repo(s)"
        + (f"; {summary['errors']} repo(s) skipped." if summary["errors"] else ".")
    )


@hooks.command("status")
@click.argument("path", type=click.Path(file_okay=False, path_type=Path), default=".")
def hooks_status(path: Path) -> None:
    """Show nexus git hook status for PATH (default: current directory)."""
    repo = path.resolve()
    hooks_dir = _verb_hooks_dir(repo)

    click.echo(f"Hooks directory: {hooks_dir}")

    stale: list[str] = []
    malformed: list[str] = []
    for name in _HOOK_NAMES:
        s = _hook_status(hooks_dir, name)
        symbol = "✓" if s.startswith(("owned", "appended")) else "·"
        if s == "malformed":
            # A begin sentinel with no end: nothing in nx can repair it
            # safely, so no `nx hooks update` suggestion for this one.
            s = f"malformed sentinel (begin without end) — repair by hand: {hooks_dir / name}"
            symbol = "!"
            malformed.append(name)
        elif s in ("owned", "appended"):
            # Ownership says who wrote the file; it does not say the
            # stanza is current. Same comparison as nx doctor's drift line.
            try:
                drifted = _stanza_state_in(hooks_dir, name) == "stale"
            except OSError:
                drifted = False
            if drifted:
                s = f"{s}, stanza stale (differs from the current template)"
                symbol = "!"
                stale.append(name)
        click.echo(f"  {symbol} {name}: {s}")

    if stale:
        cmd = f"nx hooks update {shlex.quote(str(repo))}"
        if malformed:
            # update refuses while any hook is malformed, so order the repair first.
            click.echo(
                f"Stanza drift in {', '.join(stale)}. Repair the malformed "
                f"hook(s) by hand first, then run: {cmd}"
            )
        else:
            click.echo(f"Stanza drift in {', '.join(stale)}. Run: {cmd}")


# ── internal ──────────────────────────────────────────────────────────────────


def _is_writable(path: Path) -> bool:
    return bool(path.stat().st_mode & stat.S_IWUSR)
