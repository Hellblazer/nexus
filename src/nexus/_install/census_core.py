# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Which live processes are still running from which generation.

nexus-utpuw.10. THE LOGIC LIVES HERE. ``nexus.install_census`` re-exports this
module and is the name the installed world imports (``upgrade_finish.py``,
``health.py``); ``src/nexus/_install/census.sh`` dispatches to it.

WHY THIS FILE SITS IN ``_install/`` AND IMPORTS NOTHING FROM NEXUS

The shell half is sourced by GC and the installer, which run with nothing
installed and cannot import nexus. The constraint is importing NEXUS, not
running Python, so a stdlib-only module serves both callers. Pinned by
``tests/test_install_census_core_is_bootstrap_safe.py``.

This half was ALREADY stdlib-only at module scope; the only couplings were two
deferred ``from nexus import install_layout`` calls, which now reach
``layout_core`` beside this file instead of going back out through the package.

ONE SNAPSHOT IS A PROPERTY OF THE CALLER, AND THE DISPATCH PRESERVES IT

``ps`` runs once per census and every generation is attributed from that single
view (see below). That is why :func:`census_report` does the whole loop here
rather than being called per generation from shell: a per-generation dispatch
would start a fresh process each time and take a fresh snapshot with it,
re-introducing the exact bug the shell half already fixed once. Where a caller
does hold its own snapshot, it arrives on STDIN, never argv -- ``ps axww`` on a
busy box runs to hundreds of kilobytes and argv has a hard limit.

WHAT THIS REPLACES, AND WHY IT IS NOT A REFACTOR. ``upgrade_finish`` decided
which processes were stale by matching hardcoded substrings —
``_PROC_MARKERS = ('uv/tools/conexus', '.local/bin/nx')``. Under the generation
layout nothing lives at ``uv/tools/conexus`` any more, so the markers matched
nothing, and NOTHING FAILED: the pass reported success having examined an empty
set. That is the failure class this whole arc keeps removing — an inventory
somebody has to maintain, whose staleness is silent.

Attribution here is STRUCTURAL. A holder is a process whose argv names the
generation directory followed by a path separator. There is no class list, no
vocabulary of daemon names, and therefore nothing to keep in sync: a daemon
class invented tomorrow is attributed correctly on the day it ships, because
the question asked is "did you exec out of this tree", not "are you one of the
things I know about".

ONE SNAPSHOT PER CENSUS. ``ps`` runs once and every generation is attributed
from that single view. Per-generation calls would let a process exit between
them and appear to hold two trees or none, and a caller acting on that would be
acting on a state that never existed at any instant.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_LAYOUT: object | None = None


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


def _sibling_layout():
    """``layout_core``, cached. Reached once per ``generation_match_pairs``
    call and the census loop calls that per generation."""
    global _LAYOUT
    if _LAYOUT is None:
        _LAYOUT = _sibling("layout_core")
    return _LAYOUT


__all__ = [
    "ps_snapshot",
    "generation_holder_pids",
    "generation_match_pairs",
    "generation_match_prefixes",
    "legacy_tree_candidates",
    "PS_COMMAND",
]

#: The snapshot command, byte-identical to the shell half's ``_nx_ps_snapshot``.
PS_COMMAND = ("ps", "axww", "-o", "pid=,command=")


def ps_snapshot() -> str:
    """One process snapshot. Empty string when ``ps`` is unavailable.

    A ps-less box (minimal container, stripped host) yields no holders rather
    than an exception: ``nexus-p78a0`` is the record of what happens when this
    leg raises and takes unrelated work down with it.
    """
    try:
        r = subprocess.run(PS_COMMAND, capture_output=True, text=True, timeout=10)  # noqa: S603
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout if r.returncode == 0 else ""


def _match_prefix(generation: Path | str) -> str:
    """The string an argv must contain to count as running from *generation*.

    Mirrors the shell half exactly, including two properties that were paid for:

    * A generation entry MAY be a symlink — ``.7`` registers the legacy uv tree
      as a ``gen-*`` pointer outside ``tools/``. A live holder's argv names the
      REAL path it exec'd from, never the ledger pointer, so one level of
      readlink happens before matching. One level is enough: everything that
      registers a pseudo-generation writes a direct absolute symlink.
    * A trailing slash is normalised away before the boundary is appended.
      Without that, ``<gen>/`` + ``/`` builds ``<gen>//``, which no ps line can
      contain, and a HELD tree reports zero holders (nexus-qzawu). That is the
      under-reporting direction — the one that lets a caller act as though a
      tree were free.
    """
    path = Path(generation)
    if not path.is_absolute():
        # THE SHELL HALF ALWAYS REFUSED THIS AND THIS HALF NEVER DID. Found by
        # differencing the two during the collapse, not by the 138-line pin
        # that was watching them: generation_holder_pids("rel/path") matched
        # against a bare relative string and returned pids, and the empty
        # string returned "no holders" -- the under-reporting answer, the one
        # that invites a reap. A generation is always named absolutely by both
        # call sites; anything else is a caller bug, and saying so beats
        # answering it.
        raise ValueError(
            f"generation must be an absolute path, got {str(generation)!r}"
        )
    if path.is_symlink():
        resolved = os.readlink(path)
        if resolved:
            path = Path(resolved)

    text = str(path).rstrip("/")
    if not text:
        # "/" normalises to empty, and an empty match makes the boundary "/" —
        # every process on the machine a holder of everything. Refuse instead;
        # answering "no holders" would be worse, being the answer that invites
        # a reap.
        raise ValueError(
            "refusing to census the filesystem root as a generation"
        )
    return text + "/"


def generation_holder_pids(
    generation: Path | str, snapshot: str | None = None
) -> list[int]:
    """PIDs running from *generation*, in snapshot order.

    Pass *snapshot* to attribute several generations from ONE view of the
    process table; omit it and one is taken for this call alone.

    A process that merely NAMES a path inside the tree without running from it
    is counted. That is deliberate and pinned by test: narrowing to argv[0]
    would end the over-attribution and buy under-reporting instead, and
    under-reporting is the direction that lets a live tree look free.
    """
    prefix = _match_prefix(generation)
    text = ps_snapshot() if snapshot is None else snapshot

    # THE CENSUS IS NOT A HOLDER. When census.sh dispatches here, this process
    # is `python3 census_core.py holder_pids <generation>` and its own argv
    # therefore names the tree -- so it counts itself, and a generation nobody
    # is running from reports one holder and is never reaped.
    #
    # The shell half did not need this and its comment says why: the pattern
    # travelled in the ENVIRONMENT rather than argv, so the pipeline could not
    # appear in its own snapshot. Running as a subprocess loses that property,
    # because the argument has to reach us somehow and argv is where it lands.
    #
    # Excluded by PID, which is exact. Not by matching our own argv text --
    # that is the denylist shape this file already paid for once, when a
    # `grep -v grep` inherited from live_venv_processes() censused
    # `nx search grep` as zero holders and GC reaped the tree that process was
    # running from (nexus-qzawu).
    me = os.getpid()

    pids: list[int] = []
    for line in text.splitlines():
        if prefix not in line:
            continue
        head = line.split(maxsplit=1)
        if not head:
            continue
        try:
            pid = int(head[0])
        except ValueError:
            # A ps line whose first field is not a pid is not a process row.
            continue
        if pid == me:
            continue
        pids.append(pid)
    return pids


def generation_match_pairs(
    *, tools: Path | None = None
) -> tuple[tuple[str, Path], ...]:
    """``(marker, generation)`` for every generation on this box.

    The pair form exists because ``upgrade_finish`` needs to know WHICH
    generation a matched process runs from, not merely that it runs from some
    generation. Under the side-by-side layout staleness is an identity
    comparison against ``current`` (nexus-utpuw.9), and a marker alone cannot
    make that comparison -- which is why the verdict half of
    ``detect_stale_processes`` stayed on the pre-generation age heuristic long
    after its enumeration half moved (nexus-ycw67).

    Enumerated, never hardcoded, exactly as
    :func:`generation_match_prefixes` describes; that function is now the
    marker-only PROJECTION of this one, derived rather than restated, so
    there is still one enumeration and one notion of what marks a holder.

    Returns empty when the layout cannot be read, which the caller must treat
    as "cannot tell" rather than "none".
    """
    try:
        install_layout = _sibling_layout()
        generations = install_layout.list_generations(tools=tools)
    except Exception:  # noqa: BLE001 — layout unreadable: say nothing rather than guess
        return ()

    pairs: list[tuple[str, Path]] = []
    for gen in generations:
        try:
            pairs.append((_match_prefix(gen), gen))
        except ValueError:
            continue

    # THE LEGACY uv TREE IS PART OF THE POPULATION. ``list_generations`` requires
    # a receipt, and the legacy tree never has one (legacy.sh: "PERMANENTLY
    # receipt-less"), so the ledger pointer .7 registers -- the very symlink
    # ``_match_prefix`` above was written to resolve -- was filtered out before
    # it ever got here, and an UNREGISTERED tree (every checkout-driven box
    # before nexus-hibpr) was never in scope at all. Measured 2026-08-27
    # (nexus-k52g0): 9 processes running from the 7.19.0 uv tree, 8 MCP servers
    # and the aspect-worker daemon, while doctor reported "nothing is still
    # bound to an older generation" and "all match the installed 7.20.0".
    # The census that exists to notice a stale box reported clean over the
    # exact population that made it stale.
    #
    # Both forms are enumerated here BY STRUCTURE -- the ledger name, and uv's
    # own tool root -- never by a process-name vocabulary, and deduplicated on
    # the real path so a registered tree is not counted twice.
    seen = {gen for _, gen in pairs}
    for candidate in legacy_tree_candidates(tools=tools):
        if candidate in seen:
            continue
        try:
            pairs.append((_match_prefix(candidate), candidate))
        except ValueError:
            continue
        seen.add(candidate)
    return tuple(pairs)


def legacy_tree_candidates(*, tools: Path | None = None) -> list[Path]:
    """The legacy ``uv tool install`` tree(s) live processes may run from.

    Two structural locations, in ledger-first order: the target of the
    registered pseudo-generation pointer (``install_layout.legacy_generation_link``),
    and uv's own ``<tool root>/conexus`` whether or not anyone registered it.
    A path appears once. A location whose ``bin/`` is missing is not a tree
    anything can be running from and is not returned.

    Returns empty when nothing legacy exists OR when the layout cannot be
    read; callers that need to tell those apart should ask ``install_layout``
    directly, exactly as :func:`generation_match_pairs` documents.
    """
    install_layout = _sibling_layout()

    out: list[Path] = []
    try:
        link = install_layout.legacy_generation_link(tools=tools)
        if link.is_symlink():
            target = Path(os.readlink(link))
            if target.is_absolute() and (target / "bin").is_dir():
                out.append(target)
    except Exception:  # noqa: BLE001 — an unreadable ledger is "cannot tell", not "none"
        pass
    try:
        venv = install_layout.uv_conexus_venv()
        if (venv / "bin").is_dir() and venv not in out:
            out.append(venv)
    except Exception:  # noqa: BLE001 — same posture
        pass
    return out


def generation_match_prefixes(*, tools: Path | None = None) -> tuple[str, ...]:
    """Every string that marks a process as running from SOME generation.

    The plural of :func:`_match_prefix`, and deliberately the only other way to
    ask the question. ``upgrade_finish`` needs to enumerate holders of ANY
    generation rather than one, and giving it its own notion of what a marker
    looks like is how the markers it already had drifted out of matching
    anything at all.

    Enumerated, never hardcoded: the generations that exist ARE the marker set,
    so a generation created tomorrow is matched the day it appears and there is
    nothing to keep in sync. Returns empty when the layout cannot be read, which
    the caller must treat as "cannot tell" rather than "none".
    """
    return tuple(prefix for prefix, _ in generation_match_pairs(tools=tools))


# ---------------------------------------------------------------------------
# The command-line face, for census.sh
#
# census.sh dispatches here rather than restating any of this. The calling
# contract is the shell half's own: a result on STDOUT and nothing else there,
# refusals on STDERR with an empty stdout and LAYOUT_USAGE_EXIT.
#
# THE SNAPSHOT IS WHY THE BOUNDARY SITS WHERE IT DOES. `report` runs the whole
# per-generation loop in ONE process, so exactly one `ps` is taken and every
# generation is attributed from that single view. A dispatch per generation
# would start a fresh process each time and take a fresh snapshot with it,
# which is the bug the shell half already fixed once by testing argument COUNT
# instead of the snapshot's value (an empty snapshot is falsy, so a census with
# no holders quietly ran ps N+1 times). Getting it back through a different
# mechanism would be worse, because the comment warning about it would still be
# sitting there looking satisfied.
#
# Where a caller genuinely holds its own snapshot -- gc.sh and
# reinstall-tool.sh both take one and pass it down their reap loops -- it
# arrives on STDIN. Not argv and not the environment: `ps axww -o pid=,command=`
# on a busy box runs to hundreds of kilobytes and both of those have a hard
# size limit.
# ---------------------------------------------------------------------------

#: EX_USAGE. The same status layout_core refuses with, and the shell half's
#: NX_LAYOUT_USAGE_EXIT. Restated rather than imported from the sibling so that
#: a refusal path does not depend on the sibling loading; pinned equal by
#: tests/test_install_census_twins_agree.py.
CENSUS_USAGE_EXIT = 64


def census_report(tools: Path | None = None, snapshot: str | None = None) -> list[str]:
    """One line per generation: its path, its holder count, its holder pids.

    Informational. Never reports a failure by exit status, because a holder is
    a fact about a tree rather than an obstacle -- the arc this belongs to
    removed the refusal that used to live here, and an exit status meaning
    "occupied" would smuggle it back in wearing a different hat.

    Takes ONE snapshot for the whole loop unless the caller supplies one.
    """
    layout = _sibling_layout()
    root = layout.tools_dir() if tools is None else tools
    if not root.is_dir():
        return []

    view = ps_snapshot() if snapshot is None else snapshot
    lines: list[str] = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        # Only gen-* directories. The tools root also holds documented user
        # data, and enumerating past the prefix is how a sweep turns into a
        # data-loss bug.
        if not entry.name.startswith(layout.GENERATION_PREFIX) or not entry.is_dir():
            continue
        pids = generation_holder_pids(entry, snapshot=view)
        lines.append(f"{entry} holders={len(pids)} {','.join(str(p) for p in pids)}")
    return lines


def _read_snapshot_from_stdin() -> str | None:
    """The caller's snapshot, or None when it did not send one.

    An empty stdin and a closed stdin both mean "no snapshot supplied", and an
    empty SNAPSHOT means "ps returned nothing", which is a real answer. The two
    are distinguished by whether the caller passed the `-` marker at all, never
    by the emptiness of what arrives -- the same distinction, for the same
    reason, that the shell half draws by testing argument count.
    """
    import sys  # noqa: PLC0415 -- see the module docstring on bootstrap imports

    if sys.stdin is None or sys.stdin.isatty():
        return ""
    return sys.stdin.read()


def _cli_holder_pids(generation: str, stdin_marker: str = "") -> str:
    snapshot = _read_snapshot_from_stdin() if stdin_marker == "-" else None
    pids = generation_holder_pids(generation, snapshot=snapshot)
    return "\n".join(str(p) for p in pids)


def _cli_report(tools: str = "", stdin_marker: str = "") -> str:
    snapshot = _read_snapshot_from_stdin() if stdin_marker == "-" else None
    return "\n".join(census_report(Path(tools) if tools else None, snapshot=snapshot))


def _cli_ps_snapshot() -> str:
    return ps_snapshot().rstrip("\n")


def _cli_match_prefixes(tools: str = "") -> str:
    return "\n".join(generation_match_prefixes(tools=Path(tools) if tools else None))


_VERBS = {
    "holder_pids": _cli_holder_pids,
    "report": _cli_report,
    "ps_snapshot": _cli_ps_snapshot,
    "match_prefixes": _cli_match_prefixes,
}


def main(argv: list[str]) -> int:
    """Run one verb. Returns the process exit status.

    Same shape as ``layout_core.main``, and same reasoning: every failure is a
    refusal with an empty stdout, because callers write
    ``pids=$(nx_generation_holder_pids "$gen") || return 1`` and a refusal that
    also prints something is a refusal a caller can mistake for an answer.
    """
    import sys  # noqa: PLC0415 -- see the module docstring on bootstrap imports

    if not argv:
        sys.stderr.write("nexus: census_core: no verb given\n")
        return CENSUS_USAGE_EXIT
    verb, args = argv[0], argv[1:]
    fn = _VERBS.get(verb)
    if fn is None:
        known = ", ".join(sorted(_VERBS))
        sys.stderr.write(f"nexus: census_core: unknown verb {verb!r}; known: {known}\n")
        return CENSUS_USAGE_EXIT
    try:
        rendered = fn(*args)
    except ValueError as exc:
        # _match_prefix raises this for the filesystem root, which the shell
        # half refuses with the same status and the same message.
        sys.stderr.write(f"nexus: {exc}\n")
        return CENSUS_USAGE_EXIT
    except TypeError as exc:
        sys.stderr.write(f"nexus: census_core: bad arguments to {verb}: {exc}\n")
        return CENSUS_USAGE_EXIT
    if rendered:
        sys.stdout.write(rendered + "\n")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
