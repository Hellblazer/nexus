# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Reap old generations. THE ONLY CODE IN THIS ARC THAT DELETES ANYTHING.

nexus-utpuw.6 (P2b). Everything else in ``_install/`` builds beside, points at,
or reports on. Read the refusals before the logic.

Stdlib-only and importing nothing from nexus, for the reason
``layout_core`` and ``census_core`` are: ``gc.sh`` dispatches here and is
sourced by the installer, which runs with nothing installed. Pinned by
``tests/test_install_gc_core_is_bootstrap_safe.py``.

FOUR NEVER-DELETE RULES

    (a) the generation ``current`` points at
    (b) the PREVIOUS current, which is rollback for free, recorded by .3
    (c) any generation with a live holder, from .5's census
    (d) the generation hosting the RUNNING INSTALLER. Under ``nx self install``
        the installer is exec'd from its own generation. keep-last-N usually
        covers this; the plan is explicit that "usually" is not a rule, so it
        is passed in and checked.

They are ABSOLUTE, not tiebreaks: a held generation far outside keep-last-N is
still retained.

THE DATA-LOSS HAZARD IS THE PARENT DIRECTORY

``~/.local/share/nexus/`` also holds ``chroma/`` and ``fastembed_cache/``, user
data ``nx uninstall`` deliberately does not remove. This sweep is scoped to
``<tools>/gen-*`` and touches nothing else, not even the pointers beside them.
A glob that walked the parent would delete someone's vector store.

THE BASE INTERPRETER IS NEVER OURS

Old generations' ``pyvenv.cfg`` ``home=`` points at a uv-managed CPython
outside the tools tree. Deleting or pruning it silently breaks every old
generation (the pipx#146 / uv#8028 class). Never reaching outside ``tools/`` is
what makes that true.

WHAT COUNTS AS A GENERATION

A ``gen-*`` directory CONTAINING a receipt, which is .2's completion marker. A
receipt-less ``gen-*`` is wreckage from a build that died before writing one:
it is reaped, and it does NOT count toward keep-last-N, or one crashed install
shields a real generation from retention it is entitled to. Nothing ever
pointed ``current`` at it, which is what makes reaping it safe.
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

#: Minutes a receipt-less ``gen-*`` tree is presumed to be a build in progress.
#: A generation build takes minutes; an hour is well past any of them.
DEFAULT_BUILD_GRACE_MINUTES = 60

#: Minutes a receipt-less tree carrying the builder's claim marker is presumed
#: to be a build in progress even if nothing under it was written since: a slow
#: resolve or download phase lands packages in uv's cache, not the tree. Six
#: hours is past any build; a crashed one is reaped after that.
DEFAULT_BUILD_CLAIM_MINUTES = 360

#: EX_USAGE, matching ``layout_core.LAYOUT_USAGE_EXIT``. Restated rather than
#: imported so a refusal does not depend on the sibling loading; pinned equal.
GC_USAGE_EXIT = 64

_LAYOUT: object | None = None
_CENSUS: object | None = None


def _sibling(name: str):
    """A neighbour module in ``_install/``, as ONE object per process.

    Two worlds, and the rule differs between them:

    * Imported as part of the package, ``__package__`` is ``nexus._install``
      and the package sibling IS the adjacent file, so it is imported
      normally. That keeps ONE module object per process, which matters for
      more than tidiness: ``LayoutError`` must be one class or
      ``except InstallLayoutError`` stops catching errors raised in here, and
      a test patching ``nexus._install.layout_core`` must reach this caller.
    * Run as a script at bootstrap there is no package, so the file is loaded
      by path under a stable key and REUSED from ``sys.modules`` on the next
      ask.

    Measured before the sys.modules check existed: gc_core loaded its own
    layout_core, census_core loaded another under the same key and clobbered
    it, and three distinct layout_core objects coexisted with three distinct
    LayoutError classes. Pinned by
    ``tests/test_install_cores_share_one_module_identity.py``.
    """
    if __package__:
        from importlib import import_module  # noqa: PLC0415 -- package world only

        return import_module(f"{__package__}.{name}")

    key = f"_nx_install_{name}"
    cached = sys.modules.get(key)
    if cached is not None:
        return cached

    import importlib.util  # noqa: PLC0415 -- bootstrap path only

    path = Path(__file__).resolve().parent / f"{name}.py"
    spec = importlib.util.spec_from_file_location(key, path)
    if spec is None or spec.loader is None:  # pragma: no cover -- unreachable
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    # Registered BEFORE exec, so a sibling that reaches back mid-import finds
    # the partially-built module rather than starting a second load of it.
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


def _layout():
    global _LAYOUT
    if _LAYOUT is None:
        _LAYOUT = _sibling("layout_core")
    return _LAYOUT


def _census():
    global _CENSUS
    if _CENSUS is None:
        _CENSUS = _sibling("census_core")
    return _CENSUS


class GCError(Exception):
    """The sweep refuses to proceed. Never raised after a deletion begins."""


def _protected_targets(root: Path) -> list[str]:
    """The generations ``current`` and ``previous`` name, as written.

    Read with ``readlink`` and compared as STRINGS, exactly as the shell half
    does. Not resolved: the contract is that both pointers hold absolute
    targets, and resolving here would start matching a generation reached by a
    different spelling, which is a behaviour change dressed as a tidy-up.
    """
    layout = _layout()
    out: list[str] = []
    for name in (layout.CURRENT_LINK_NAME, layout.PREVIOUS_LINK_NAME):
        link = root / name
        if link.is_symlink():
            target = os.readlink(link)
            if target:
                out.append(target)
    return out


def _recently_written(tree: Path, minutes: int) -> bool:
    """True when anything at or under *tree* was written within *minutes*.

    The shell half is ``find "$tree" -mmin -N -print -quit``, which stops at
    the first hit; this walks until it finds one, for the same reason. The tree
    itself counts, which is what ``find`` does with a bare path argument.
    """
    cutoff = time.time() - minutes * 60
    try:
        if tree.stat().st_mtime > cutoff:
            return True
    except OSError:
        return False
    for dirpath, dirnames, filenames in os.walk(tree, onerror=lambda _e: None):
        for name in list(dirnames) + list(filenames):
            try:
                if (Path(dirpath) / name).lstat().st_mtime > cutoff:
                    return True
            except OSError:
                continue
    return False


def _claimed_recently(tree: Path, marker: str, minutes: int) -> bool:
    """True when *tree*'s build-claim marker is younger than *minutes*.

    The claim is separate from the write check because a slow resolve or
    download writes nothing INTO the tree for minutes at a time: packages land
    in uv's cache. The marker is written the instant the directory exists.
    """
    try:
        return (tree / marker).lstat().st_mtime > time.time() - minutes * 60
    except OSError:
        return False


def plan(
    root: Path,
    *,
    keep: int = 3,
    self_generation: str = "",
    snapshot: str | None = None,
    grace_minutes: int = DEFAULT_BUILD_GRACE_MINUTES,
    claim_minutes: int = DEFAULT_BUILD_CLAIM_MINUTES,
) -> list[tuple[str, Path, str]]:
    """Decide what happens to every ``gen-*`` entry, deleting nothing.

    Returns ``(action, path, detail)`` in directory order, where action is
    ``"reap"``, ``"keep"`` or ``"skip"``. ``skip`` is an entry inside the keep
    window or protected by a pointer, which the shell half reports nothing
    about; ``keep`` is the two cases it prints a line for.

    SEPARATED FROM THE DELETION ON PURPOSE. Every rule that decides a tree's
    fate is here, where a test can ask what the sweep WOULD do over any tree
    shape without a single ``rm`` running. ``--dry-run`` becomes "run the plan
    and print it" rather than a second code path that has to be kept honest
    against the first, which is the shape a dry run has to have to be worth
    trusting.
    """
    layout, census = _layout(), _census()
    prefix, receipt = layout.GENERATION_PREFIX, layout.RECEIPT_NAME
    marker = layout.BUILDING_MARKER_NAME

    protected = _protected_targets(root)
    if self_generation:
        protected.append(self_generation)

    entries = [e for e in sorted(root.iterdir(), key=lambda p: p.name)
               if e.name.startswith(prefix) and e.is_dir()]

    # Complete generations, counted first. Stamps sort chronologically by .2's
    # construction, so lexical order IS creation order.
    total_complete = sum(1 for e in entries if (e / receipt).is_file())

    # ONE census for the whole pass, for the same reason .5 takes one snapshot:
    # a per-generation re-read could reap against a state that never existed at
    # any instant.
    view = census.ps_snapshot() if snapshot is None else snapshot

    out: list[tuple[str, Path, str]] = []
    index = 0
    for entry in entries:
        if (entry / receipt).is_file():
            index += 1
            if total_complete - index < keep:
                out.append(("skip", entry, "inside the keep window"))
                continue
        elif not entry.is_symlink() and (
            _recently_written(entry, grace_minutes)
            or _claimed_recently(entry, marker, claim_minutes)
        ):
            # A receipt-less tree written to within the grace window is a BUILD
            # IN PROGRESS, not wreckage: install_generation.sh writes the
            # receipt last, and its uv argv names the bare directory, which the
            # holder census does not match. With the reap on every session's
            # SessionStart hook, another session's install is the normal case.
            out.append(("keep", entry,
                        f"build in progress (receipt-less; written within "
                        f"{grace_minutes} min or claimed within {claim_minutes} min)"))
            continue
        # A receipt-less directory falls through deliberately: reapable, and
        # never counted toward the keep window.

        if str(entry) in protected:
            out.append(("skip", entry, "protected pointer"))
            continue

        holders = census.generation_holder_pids(entry, snapshot=view)
        if holders:
            # Said on stdout: a held tree outside the keep window is 1.7 GB the
            # operator cannot see go, and a reap that only reported deletions
            # let a box grow one generation per upgrade for as long as its
            # sessions lived.
            out.append(("keep", entry,
                        "held by " + " ".join(str(p) for p in holders)))
            continue

        out.append(("reap", entry, ""))
    return out


def _reap(entry: Path, emit_err) -> list[str]:
    """Delete one planned entry. Returns the lines to print on stdout.

    THE SCOPING GUARD LIVES HERE. Following a ``gen-*`` symlink is the ONLY way
    this sweep can delete anything outside the tools root, so it is fenced
    twice rather than trusted.

    Measured before the guard existed: a ``gen-rogue`` symlink pointing at an
    unrelated directory caused ``rm -rf`` of that directory. The only check was
    that the target was not literally ``/`` -- one value out of infinitely many
    dangerous ones, which is the shape of a guard that reads as protection
    without being any.

    (1) Only the reserved ledger name may be a symlink at all.
    (2) Its target must look like the uv-managed venv it claims to be, a
        directory carrying ``pyvenv.cfg``. A wrong target (a home directory, a
        checkout) has none, so the pointer is unlinked and the tree is left
        alone. Failing that way leaves litter; failing the other way deletes
        data.
    """
    layout = _layout()

    if not entry.is_symlink():
        # rmtree on the directory itself, never through a pointer: the pointers
        # live in this same directory and following one would empty the
        # generation it names rather than removing a link.
        shutil.rmtree(entry, ignore_errors=True)
        return [f"reaped {entry}"]

    ledger_name = f"{layout.GENERATION_PREFIX}{layout.LEGACY_GENERATION_NAME}"
    if entry.name != ledger_name:
        emit_err(f"nexus: refusing to reap through an unrecognised generation symlink: {entry}")
        return []

    # A pseudo-generation (.7's legacy uv-tool bridge): this entry is only our
    # LEDGER pointer, never the tree itself. Unlinking a symlink leaves its
    # target untouched, which is exactly backwards for a reap whose whole job
    # here is deleting the legacy tree. Resolve one level -- registration only
    # ever writes a direct absolute symlink, never a chain -- and remove both:
    # the real tree, then the now-dangling pointer.
    real = os.readlink(entry)
    if not real.startswith("/"):
        emit_err(f"nexus: ledger target is not an absolute path, refusing: '{real}'")
        return []

    target = Path(real)
    if not target.is_dir() or not (target / "pyvenv.cfg").is_file():
        emit_err(f"nexus: ledger target is not a venv, unlinking the pointer only: {real}")
        entry.unlink(missing_ok=True)
        return [f"reaped {entry}"]

    shutil.rmtree(target, ignore_errors=True)
    entry.unlink(missing_ok=True)
    # Reaping the tree IS what closes uv's door: measured against uv 0.8
    # (2026-08-28), with the venv gone `uv tool list` says "No tools installed"
    # and `uv tool upgrade conexus` REFUSES rather than rebuilding. Never
    # advise `uv tool uninstall` here -- also measured: it deletes a
    # nexus-owned regular-file shim sitting at its bin path.
    emit_err(
        f"nexus: reaped the legacy uv tree {real}; uv no longer lists it and "
        f"'uv tool upgrade conexus' will now refuse rather than rebuild it"
    )
    return [f"reaped {entry}"]


def gc_generations(
    root: Path | None = None,
    *,
    keep: int = 3,
    self_generation: str = "",
    dry_run: bool = False,
    snapshot: str | None = None,
    grace_minutes: int = DEFAULT_BUILD_GRACE_MINUTES,
    claim_minutes: int = DEFAULT_BUILD_CLAIM_MINUTES,
    emit_err=None,
) -> list[str]:
    """Run the sweep. Returns the stdout lines; refusals go to *emit_err*.

    Raises :class:`GCError` for a bad ``keep``, BEFORE anything is planned and
    long before anything is deleted.
    """
    if emit_err is None:
        def emit_err(message: str) -> None:
            sys.stderr.write(message + "\n")

    if keep < 0:
        raise GCError(f"--keep must be a non-negative integer, got '{keep}'")
    if keep < 1:
        # --keep 0 means "retain nothing", leaving only the four rules between
        # the operator and an install with no fallback at all. Almost certainly
        # a mistake, and refusing costs one message.
        raise GCError("--keep 0 would retain no generations; refusing")

    resolved = _layout().tools_dir() if root is None else Path(root)
    if not resolved.is_dir():
        return []

    lines: list[str] = []
    for action, entry, detail in plan(
        resolved, keep=keep, self_generation=self_generation, snapshot=snapshot,
        grace_minutes=grace_minutes, claim_minutes=claim_minutes,
    ):
        if action == "skip":
            continue
        if action == "keep":
            lines.append(f"kept {entry}: {detail}")
        elif dry_run:
            lines.append(f"would reap {entry}")
        else:
            lines.extend(_reap(entry, emit_err))
    return lines


def _env_minutes(name: str, default: int) -> int:
    """An operator knob from the environment, ignoring a value we cannot use.

    These reach us from the environment rather than argv because they are
    operator knobs: threading them through arguments would mean every caller of
    ``nx_gc_generations`` had to know about them in order to leave them alone.

    A malformed value falls back to the default rather than refusing. The shell
    half used them unvalidated in a ``find -mmin`` argument, where a bad value
    makes find fail, the test comes back empty, and the tree is treated as not
    recently written -- which is the REAPING direction. Falling back is the
    conservative reading of a typo, and the one that cannot delete anything the
    default would have kept.
    """
    raw = os.environ.get(name, "")
    return int(raw) if raw.isdigit() and int(raw) > 0 else default


def main(argv: list[str]) -> int:
    """``gc.sh``'s calling contract: results on stdout, refusals on stderr with
    an empty stdout and EX_USAGE."""
    keep = 3
    self_generation = ""
    dry_run = False
    root_arg = ""

    args = list(argv)
    while args:
        head = args.pop(0)
        if head == "--keep":
            if not args:
                sys.stderr.write("nexus: --keep needs a value\n")
                return GC_USAGE_EXIT
            raw = args.pop(0)
            if not raw.isdigit():
                sys.stderr.write(
                    f"nexus: --keep must be a non-negative integer, got '{raw}'\n"
                )
                return GC_USAGE_EXIT
            keep = int(raw)
        elif head == "--self":
            self_generation = args.pop(0) if args else ""
        elif head == "--dry-run":
            dry_run = True
        else:
            root_arg = head

    try:
        lines = gc_generations(
            Path(root_arg) if root_arg else None,
            keep=keep, self_generation=self_generation, dry_run=dry_run,
            grace_minutes=_env_minutes(
                "NX_GC_BUILD_GRACE_MINUTES", DEFAULT_BUILD_GRACE_MINUTES),
            claim_minutes=_env_minutes(
                "NX_GC_BUILD_CLAIM_MINUTES", DEFAULT_BUILD_CLAIM_MINUTES),
        )
    except GCError as exc:
        sys.stderr.write(f"nexus: {exc}\n")
        return GC_USAGE_EXIT
    for line in lines:
        sys.stdout.write(line + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
