# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx hooks — git hook management for automatic repo indexing."""
import re
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
    """Delegate to ``nexus._git_hooks_meta.effective_hooks_dir``."""
    return _ghm.effective_hooks_dir(repo)


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
nx index repo "$REPO_TOP" --on-locked=skip \\
  >> "$NX_INDEX_LOG" 2>&1 &
disown
{end}""".format(begin=SENTINEL_BEGIN, end=SENTINEL_END)


# ── per-commit review (bead nexus-jh86x) ──────────────────────────────────────

#: Appended INSIDE the sentinel block, for ``post-commit`` ONLY.
#:
#: Not post-merge and not post-rewrite, deliberately. A merge brings in
#: commits already reviewed where they were authored, and post-rewrite
#: fires once per rewritten commit, so a single interactive rebase of
#: twenty commits would dispatch twenty reviews of work that has already
#: been reviewed. Fire where authorship happens, once.
#:
#: BURST SHAPE (raised by the sibling session nexus-13, 2026-09-02, from a
#: live 7.27.0 cut): a release is not one commit. It is a release commit,
#: a back-merge, and any fix-forward, landing in quick succession. The
#: pgrep guard below is the same instrument the indexing stanza above uses
#: for the same reason, and it SERIALISES a burst rather than firing N
#: concurrent ``claude -p`` children. It does not reduce total spend --
#: the per-dispatch cap does that, and the arithmetic is written down at
#: ``config.COMMIT_REVIEW_DEFAULT_BUDGET_USD``.
#:
#: Never blocks: the dispatch is detached and disowned exactly like the
#: indexer, and ``nx review commit`` itself always exits 0. A post-commit
#: hook that can fail is a footgun during a tag-push sequence that has to
#: land in tight succession.
#:
#: PLACED BEFORE THE INDEXING GUARDS, and that position is the whole
#: point (code review of a461db0b7, 2 Critical). The indexing stanza
#: carries two ``exit 0`` guards -- the linked-worktree skip and the
#: "an indexer for this repo is already running" pgrep -- and BOTH sit
#: above the indexer's own dispatch. Appended after them, as this stanza
#: first was, the review inherited both exits and was silently skipped:
#:   * whenever a previous commit's indexer was still running, which on
#:     this repo is measured at 64-131 minutes, so an ordinary burst of
#:     commits would review the first and silently drop the rest -- the
#:     exact release-cut case this stanza's own pgrep guard exists for;
#:   * on every commit made in a linked worktree, which is this
#:     project's standard agent-dispatch workflow.
#: In both cases the only log line written mentioned INDEXING being
#: skipped, so a human reading the log would see a hook that ran fine.
#: A gate that skip-passes without saying so is the nexus-moht0 class.
#:
#: Ordering costs nothing: both dispatches are backgrounded and disowned,
#: so running the review first does not delay the indexer by anything.
#: The earlier ordering rationale ("do not make the cheap local index
#: wait on a network dispatch") was simply wrong about that.
#:
#: Uses ``$HOME/.config/nexus/index.log`` literally rather than
#: ``$NX_INDEX_LOG``: that variable is not assigned until AFTER the
#: guards, i.e. after this block. The linked-worktree guard above writes
#: its own line the same way, for the same reason.
_REVIEW_STANZA = """\
# PER-COMMIT REVIEW (nexus-jh86x). Reviews the commit just made, in the
# background, and records findings in T2. Never blocks: see the bead for
# why this fires per commit rather than on demand.
# Opt out with NX_COMMIT_REVIEW=0 (env beats config), or persistently via
# .nexus.yml#commit_review.enabled. Uninstall removes this with the rest
# of the stanza.
if [ "$NX_COMMIT_REVIEW" != "0" ]; then
  _NX_REVIEW_LOG="$HOME/.config/nexus/index.log"
  # Serialise a burst (release cut = commit + back-merge + fix-forward in
  # quick succession) rather than firing N concurrent children. A hit is
  # QUEUED, not dropped: the sha goes to <git-common-dir>/nx-review-queue
  # (one queue per repository, shared by every linked worktree, read by
  # nexus.commit_review.review_queue_path) and the running reviewer,
  # dispatched with --drain, reviews it before exiting. Until 2026-09-04 a
  # hit only logged SKIPPED and 6 of 9 commits in one push went unreviewed.
  # A hit is also LOGGED: a skipped review that says nothing is
  # indistinguishable from a review that found nothing.
  if pgrep -f "nx review commit .* --repo $REPO_TOP" > /dev/null 2>&1; then
    _NX_REVIEW_COMMON="$(cd "$(git rev-parse --git-common-dir)" 2>/dev/null && pwd -P)"
    if [ -n "$_NX_REVIEW_COMMON" ] && git rev-parse HEAD >> "$_NX_REVIEW_COMMON/nx-review-queue" 2>/dev/null; then
      echo "=== nx review post-commit QUEUED (review already running) $REPO_TOP $(date '+%Y-%m-%dT%H:%M:%S%z') ===" \\
        >> "$_NX_REVIEW_LOG"
    else
      # An empty common dir would have written to /nx-review-queue and the
      # log would still have said QUEUED (review [24406] Major). Say what
      # happened instead: this commit has no record and the census names it.
      echo "=== nx review post-commit NOT QUEUED (queue unwritable; nx census reviews will list this commit) $REPO_TOP $(date '+%Y-%m-%dT%H:%M:%S%z') ===" \\
        >> "$_NX_REVIEW_LOG"
    fi
  else
    echo "=== nx review post-commit $REPO_TOP $(date '+%Y-%m-%dT%H:%M:%S%z') ===" \\
      >> "$_NX_REVIEW_LOG"
    nx review commit "$(git rev-parse HEAD)" --repo "$REPO_TOP" --drain \\
      >> "$_NX_REVIEW_LOG" 2>&1 &
    disown
  fi
fi
"""


def _stanza_for(hook_name: str) -> str:
    """The stanza body installed for *hook_name*.

    ``post-commit`` carries the indexing stanza PLUS the review stanza,
    inside ONE sentinel block so ``nx hooks uninstall`` still removes both
    with a single sentinel match. Every other hook gets the indexing
    stanza unchanged, byte for byte -- which is also what keeps
    :data:`_STANZA` a valid comparison target for the two callers that
    hold it (``nexus.health``'s drift check and the ws67k guard tests).
    """
    if hook_name != "post-commit":
        return _STANZA
    # Insert immediately after REPO_TOP is assigned and BEFORE the two
    # indexing ``exit 0`` guards -- see _REVIEW_STANZA's own note. The
    # anchor is the REPO_TOP line because that is the one thing the review
    # block needs and the last line guaranteed to precede every guard.
    anchor = 'REPO_TOP="$(git rev-parse --show-toplevel)"\n'
    if anchor not in _STANZA:  # pragma: no cover - guarded by test_stanza_anchor_exists
        raise RuntimeError(
            "the post-commit review stanza's anchor line is gone from _STANZA; "
            "re-derive the insertion point rather than appending at the end, "
            "which is what silently disabled the reviewer behind two exit 0 guards"
        )
    return _STANZA.replace(anchor, anchor + _REVIEW_STANZA, 1)


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


def _hook_status(hooks_dir: Path, hook_name: str) -> str:
    """Return status string: 'not installed' | 'unmanaged' | 'owned' | 'appended'."""
    hook_file = hooks_dir / hook_name
    if not hook_file.exists():
        return "not installed"
    content = hook_file.read_text()
    if SENTINEL_BEGIN not in content:
        return "unmanaged"
    remainder = _remove_stanza(content).strip()
    if remainder in ("", "#!/bin/sh"):
        return "owned"
    return "appended"


def hook_stanza_state(repo: Path, hook_name: str = "post-commit") -> str:
    """One word for what *repo*'s *hook_name* will do on the next commit.

    ``armed``: the installed stanza equals the current template.
    ``stale``: a nexus stanza is installed but differs from the template
    (a release changed it and ``nx hooks update`` has not run; the
    per-commit reviewer was silently absent for two days this way,
    nexus-trwxr). ``unmanaged``: a hook exists without a nexus sentinel.
    ``not installed``: no hook file. ``unknown``: not a git repository.

    ``nx doctor`` has computed the stale case since nexus-mkj6u, but as a
    line in a long report nobody was reading; doctor now resolves each
    hook's state through this function too, so there is one comparison,
    and ``nx census reviews`` prints it where someone will act on it.
    """
    try:
        # core.hooksPath honoured, like install/update/status and nx doctor
        # (critique [24292]: the first cut used the common dir directly and
        # would have told a core.hooksPath user "not installed" forever).
        hooks_dir = _effective_hooks_dir(repo)
        status = _hook_status(hooks_dir, hook_name)
        if status in ("not installed", "unmanaged"):
            return status
        installed = (hooks_dir / hook_name).read_text()
    except Exception:  # noqa: BLE001 - a census line must never traceback
        return "unknown"
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

    try:
        hooks_dir = _effective_hooks_dir(repo)
    except click.ClickException as exc:
        raise exc

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
    hooks_dir = _effective_hooks_dir(repo)

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
    hooks_dir = _effective_hooks_dir(repo)

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
    ``not installed``.
    """
    results: list[tuple[str, str]] = []
    for name in _HOOK_NAMES:
        hook_file = hooks_dir / name
        if not hook_file.exists():
            results.append((name, "not installed"))
            continue
        if SENTINEL_BEGIN not in hook_file.read_text():
            results.append((name, "unmanaged"))
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
            hooks_dir = _effective_hooks_dir(repo)
            if hooks_dir.exists() and not _is_writable(hooks_dir):
                summary["errors"] += 1
                if echo:
                    click.echo(f"  ! {repo}  (hooks dir not writable; skipped)")
                continue
            results = _refresh_managed_hooks(hooks_dir)
        except click.ClickException as exc:
            summary["errors"] += 1
            if echo:
                click.echo(f"  ! {repo}  ({exc.format_message()})")
            continue

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
    hooks_dir = _effective_hooks_dir(repo)

    click.echo(f"Hooks directory: {hooks_dir}")

    for name in _HOOK_NAMES:
        s = _hook_status(hooks_dir, name)
        symbol = "✓" if s.startswith(("owned", "appended")) else "·"
        click.echo(f"  {symbol} {name}: {s}")


# ── internal ──────────────────────────────────────────────────────────────────


def _is_writable(path: Path) -> bool:
    return bool(path.stat().st_mode & stat.S_IWUSR)
