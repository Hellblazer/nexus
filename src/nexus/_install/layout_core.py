# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The generation layout: where installs live, what they record, how they bind.

nexus-utpuw.1 (P0). THE LOGIC LIVES HERE. ``nexus.install_layout`` re-exports
this module unchanged and is the name the installed world imports;
``src/nexus/_install/layout.sh`` is the shell statement of the same contract,
pinned to it by ``tests/test_install_layout_twins_agree.py``.

WHY THIS FILE SITS IN ``_install/`` AND IMPORTS NOTHING FROM NEXUS

Two implementations exist because the callers have incompatible import
constraints. The generation builder and the shim writer run from
``scripts/reinstall-tool.sh``, which may run with NOTHING installed;
``health.py`` and ``upgrade_finish.py`` run after the install and can import
nexus. The constraint is importing NEXUS, not running Python -- the shell half
already calls ``python3`` constantly -- so a stdlib-only module that imports
nothing from nexus satisfies both callers, which is what this file is.

That makes the no-nexus-imports rule load-bearing rather than tidy. It is
pinned by ``tests/test_install_layout_core_is_bootstrap_safe.py``, which
imports this module with ``nexus`` made unimportable. The two couplings that
had to go were ``structlog`` and ``NexusError``; see :func:`_warn` and
:func:`_error_base` for how each is now deferred, and why deferring beats the
obvious alternative in the second case.

It sits beside ``layout.sh`` rather than at ``src/nexus/layout_core.py``
because the shell half must reach it by a relative path next to itself, with
no nexus on ``sys.path``.

THE LAYOUT

    <tools>/gen-<stamp>/                a venv, BUILT AT this path
    <tools>/gen-<stamp>/nexus-install.json   nexus-owned receipt
    <tools>/current -> <tools>/gen-<stamp>   absolute symlink
    <bin>/<command>                     a nexus-owned regular file, not a link

An install builds a new generation beside the old ones and repoints
``current``. It never writes into a tree a live process is running from,
which is what makes an install safe under any number of live sessions --
the property nexus-utpuw exists to buy.

WHY THE DEFAULTS MUST STAY $HOME-DERIVED, AND MUST NOT BE CACHED

``tests/e2e/release-sandbox.sh`` and ``tests/e2e/run.sh`` isolate themselves
ONLY by redirecting ``$HOME``. If these defaults were resolved once at import
time, or hardcoded, those harnesses would silently start writing into the
operator's live install. ``tools_dir()`` and ``bin_dir()`` therefore consult
``Path.home()`` on every call, and
``test_defaults_are_recomputed_when_home_moves`` is what keeps them doing so.

WHY THE SHIM READS THE POINTER BEFORE IT EXECS

``Modules/getpath.py`` looks for ``pyvenv.cfg`` next to the executable AS
INVOKED, before it resolves symlinks; realpath happens later and only feeds
the base-interpreter and stdlib search. A shim that exec'd
``<tools>/current/bin/nx`` directly would therefore leak the ``current``
component into ``sys.prefix`` and ``sys.path``, and the next flip would
retarget every not-yet-imported module inside an already-running process --
reproducing nexus-q3xrx by way of the mechanism meant to prevent it. So the
shim resolves the pointer into ``NX_GEN`` first and execs the real path. It
is one line of difference and it is the whole design.

``nexus-utpuw.9`` (P5a) extends this module with the runtime half: resolving
the current generation, enumerating generations, reading receipts off disk,
and answering "am I stale?" -- :func:`current_generation`, :func:`is_stale`,
:func:`list_generations`, :func:`read_receipt`.

STALENESS IS EXACT

    stale  <=>  Path(sys.prefix) != os.readlink(<tools>/current)

One readlink. No filesystem-clock inference, no false positives or false
negatives. Today ``upgrade_finish.py``'s ``self_staleness()`` infers
staleness from dist-info mtime, which only worked because uv replaced
site-packages IN PLACE; under side-by-side generations the old tree is never
replaced, so that detector would report ``stale=False`` forever. This is a
deliberate replacement, not a casualty (see nexus-utpuw.12).

:func:`is_stale` is ALSO design point 6 of nexus-utpuw: a new spawn LOGS
(never fails) when its own baked generation differs from current. The
detector and the tripwire are the same call -- a caller either lets the
baseline default to its own ``sys.prefix`` (a short-lived spawn) or supplies
one captured earlier (a long-lived host comparing against its startup
state) -- so the rule is implemented once, not twice.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nexus.bounded_subprocess import run_bounded

#: Override the generation root. Absolute paths only; see ``_resolve_dir``.
TOOLS_DIR_ENV = "NX_TOOLS_DIR"

#: Override the directory the shims are written into.
BIN_DIR_ENV = "NX_BIN_DIR"

#: ``<tools>/gen-<stamp>``. A prefix rather than a bare stamp so that a GC
#: pass can tell a generation from anything else that lands in the root.
GENERATION_PREFIX = "gen-"

#: The one pseudo-generation: ``<tools>/gen-legacy-uv-tool``, the GC-ledger
#: pointer ``legacy.sh`` writes for a legacy ``uv tool install conexus`` tree.
#: Permanently receipt-less by design, so :func:`list_generations` never
#: returns it -- which is why anything that needs the legacy tree in its
#: population must ask for it by this name (``install_census``). Pinned
#: against the shell half's ``NX_LEGACY_GENERATION_NAME`` by
#: ``tests/test_install_layout_legacy_name_pin.py``.
LEGACY_GENERATION_NAME = "legacy-uv-tool"

#: Names that live in a venv's ``bin/`` but are NEVER shimmed into the shared
#: bin dir. THE only statement of this set: ``shims.sh`` carried a twin
#: ``NX_NEVER_SHIM`` until the writer collapsed into ``shims_core``, which asks
#: here. ``tests/test_install_layout_twins_agree.py`` now pins that the shell
#: does not regrow one.
#:
#: This exists because ``~/.local/bin`` is SHARED. pyenv, asdf and homebrew all
#: leave a ``python`` symlink there, so anything deriving "the names nexus owns"
#: from a generation's ``bin/`` must subtract these or it will mistake another
#: tool's symlink for evidence that uv reclaimed our shims (RG-C, nexus-utpuw.11).
NEVER_SHIM = frozenset({
    "python", "python3", "pip", "pip3",
    "activate", "activate.csh", "activate.fish",
    "uv", "uvx",
})

#: Console scripts of DEPENDENCIES that nexus shims although the ``conexus``
#: distribution's own metadata does not declare them (uv does not link them
#: either). THE only statement of this set, for the same reason and with the
#: same pin as :data:`NEVER_SHIM` above.
DEPENDENCY_SCRIPTS = frozenset({"mineru", "mineru-api"})

#: Ask the generation's OWN interpreter which console scripts the installed
#: distribution declares. Exit 3 on a lookup failure so "declares nothing" and
#: "could not ask" never collapse into the same empty answer (RG-A) -- a
#: dist-name mismatch must not silently unshim every one of the project's own
#: console scripts. ``shims.sh`` ran a verbatim copy of this inside
#: ``_nx_declared_scripts`` until the writer collapsed into ``shims_core``.
_DECLARED_SCRIPTS_QUERY = """\
import sys
try:
    import importlib.metadata as md
    eps = md.distribution(sys.argv[1]).entry_points
except Exception as exc:
    print("NX_LOOKUP_FAILED", exc, file=sys.stderr)
    sys.exit(3)
for ep in eps:
    if getattr(ep, "group", None) == "console_scripts":
        print(ep.name)
"""

def declared_console_scripts_detail(
    generation: Path, dist: str = "conexus"
) -> tuple[frozenset[str], tuple[str, ...]]:
    """The console scripts *generation*'s installed *dist* declares.

    This is THE set of names nexus owns in the shared bin dir: exactly what
    ``nx_write_shims`` writes, derived the same way (the generation's own
    ``bin/python`` answering ``importlib.metadata``). It is deliberately NOT
    "everything in ``<generation>/bin``": a venv's ``bin/`` also holds the
    interpreter it was built from (``python3.12``), and ``~/.local/bin`` is a
    shared directory where uv leaves interpreter links of the same name
    (``uv python install``). Deriving the owned set from the bin listing
    reported those links as reclaimed shims -- a FATAL doctor row on a healthy
    box -- and the takeover repair announced it was "rewriting" them (it never
    did: the shell writer derives its own set, so the message was wrong, not
    the write) (GH #1487, nexus-50hm9). ``NEVER_SHIM`` alone cannot close
    that: it names ``python3``, not every versioned interpreter link a machine
    can carry.

    Raises :class:`LayoutError` when the generation cannot answer --
    no interpreter, a failing query, a distribution that declares nothing --
    so a caller says "could not check" rather than guessing an owned set from
    a listing. Uncertain means say so.
    """
    python = generation / "bin" / "python"
    if not python.is_file():
        raise LayoutError(
            f"{generation} has no bin/python to ask which console scripts it declares"
        )
    try:
        proc = run_bounded(  # noqa: S603 -- the generation's own interpreter, fixed argv
            [str(python), "-c", _DECLARED_SCRIPTS_QUERY, dist],
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LayoutError(
            f"could not run {python} to list {dist}'s console scripts: {exc}"
        ) from exc
    if proc.returncode != 0:
        raise LayoutError(
            f"{python} could not list {dist}'s console scripts "
            f"(exit {proc.returncode}): {proc.stderr.strip() or 'no diagnostic'}"
        )
    names: set[str] = set()
    refused: list[str] = []
    for line in proc.stdout.splitlines():
        name = line.strip()
        if not name:
            continue
        if not _COMPONENT_RE.match(name):
            # COLLECTED, never logged from in here. A declared script the shim
            # template would refuse is a fact about the install and must be
            # said out loud -- but WHERE it is said is the caller's to choose,
            # and one caller cannot choose stdout. shims_core runs as a
            # DISPATCH whose stdout is reserved for results, and _warn routes
            # to structlog whenever structlog imports, which in an installed
            # venv prints to stdout. Measured: the refusal landed on the
            # dispatch's stdout, the one place the layout contract forbids
            # ("a diagnostic on stdout is how a caller ends up installing into
            # it"). So the refusals come back as data and each caller places
            # them: declared_console_scripts warns, shims_core writes stderr.
            refused.append(name)
            continue
        names.add(name)
    if not names:
        raise LayoutError(
            f"{dist} in {generation.name} declares no console scripts -- a "
            "distribution-name mismatch, not an empty product; refusing to derive "
            "the owned shim set from a directory listing instead"
        )
    return frozenset(names) - NEVER_SHIM, tuple(refused)


def declared_console_scripts(generation: Path, dist: str = "conexus") -> frozenset[str]:
    """:func:`declared_console_scripts_detail`'s accepted set, with each refused
    name said out loud through the warning stream.

    The form for IN-PROCESS callers -- ``nx doctor``, ``self_cmd`` -- where
    structlog is configured and a warning belongs in the same structured stream
    as everything else. A dispatch must use the detail form and place the
    refusals itself; see the comment at the refusal site.
    """
    names, refused = declared_console_scripts_detail(generation, dist)
    for name in refused:
        _warn(
            "declared_console_script_refused",
            generation=str(generation), dist=dist, name=name,
        )
    return names


def owned_from_declared(declared: frozenset[str], generation: Path) -> frozenset[str]:
    """The owned shim set, given an ALREADY-FETCHED declared set.

    Split out so that the writer and this module cannot hold two copies of the
    rule. ``shims_core.write_shims`` fetches :func:`declared_console_scripts`
    once -- it needs the set anyway, and each call runs the generation's own
    interpreter -- and derives both its write list and its prune's owned set
    from here. Before the split, ``shims.sh`` recomputed the rule inline in its
    prune loop and the two disagreed on hostile names; see
    ``shims_core``'s module docstring for the measurement.
    """
    candidates = (declared | DEPENDENCY_SCRIPTS) - NEVER_SHIM
    return frozenset(name for name in candidates if (generation / "bin" / name).exists())


def owned_shim_names(generation: Path, dist: str = "conexus") -> frozenset[str]:
    """The shim names the writer WRITES for *generation* -- the exact set nexus
    owns in the shared bin dir, derived the way the writer derives it: the
    distribution's declared console scripts plus :data:`DEPENDENCY_SCRIPTS`,
    minus :data:`NEVER_SHIM`, restricted to names that actually exist in the
    generation's ``bin/`` (a declared-but-not-built entry point -- an optional
    extra -- gets no shim, so a foreign symlink at that name in the bin dir is
    not ours either). Propagates :class:`LayoutError` from
    :func:`declared_console_scripts`.

    The rule itself is :func:`owned_from_declared`, which the writer calls with
    the same declared set, so the two cannot drift apart.
    """
    return owned_from_declared(declared_console_scripts(generation, dist), generation)


def reclaimed_shims(generation: Path, bin_dir: Path) -> list[str]:
    """The owned shim names at *bin_dir* that are symlinks -- uv's, not ours.

    nexus writes every shim as a regular file, so a symlink at an OWNED name
    is a reclaim by construction (a stray ``uv tool install --force conexus``).
    A symlink at any other name -- a uv-managed ``python3.12``, pyenv's
    ``python``, a separately installed ``mineru`` on a generation that never
    shipped one -- is not ours to judge and is never listed. Propagates
    :class:`LayoutError` from :func:`owned_shim_names`.
    """
    return [
        name for name in sorted(owned_shim_names(generation))
        if (bin_dir / name).is_symlink()
    ]


#: The pointer every shim resolves. Always an ABSOLUTE symlink, so that plain
#: ``readlink`` suffices -- ``readlink -f`` is macOS >= 12.3 only.
CURRENT_LINK_NAME = "current"

#: ``<tools>/previous``, the generation a rollback returns to. Written by the
#: flip (nexus-utpuw.3). GC's never-delete rule (b) protects "the previous
#: current", and until .3 that had no on-disk representation — GC would have
#: had to infer it from mtime, the heuristic this arc exists to replace.
PREVIOUS_LINK_NAME = "previous"

#: The nexus-owned receipt, which replaces ``uv-receipt.toml`` as the home of
#: extras. Losing extras re-opens the 768->384 embedder downgrade (README:80).
RECEIPT_NAME = "nexus-install.json"

#: Format version of the receipt. A receipt stamped with a schema this code
#: does not know is refused rather than half-read.
RECEIPT_SCHEMA = 1

#: Provenance of the installer that wrote the receipt. Recorded, not gated:
#: it describes who wrote the file, while ``schema`` describes the format.
INSTALLER_SCHEMA = 1

#: Where an install came from. A closed set, checked at construction.
SOURCE_KINDS = ("directory", "registry")

_DEFAULT_TOOLS_SUBPATH = (".local", "share", "nexus", "tools")
_DEFAULT_BIN_SUBPATH = (".local", "bin")

#: Exit status a shim uses when the pointer cannot be resolved. EX_UNAVAILABLE
#: from sysexits.h -- a specific status, so an operator seeing it in a log can
#: tell "no current generation" from a command that merely failed.
SHIM_NO_CURRENT_EXIT = 70

#: EX_USAGE from sysexits.h: the status every refusal in the shell half exits
#: with. Named here so the core can speak the same status when it is run as a
#: script, and so the two cannot drift -- a refusal that came back as a plain
#: 1 would be indistinguishable from an ordinary failure to a caller writing
#: ``dir=$(nx_tools_dir) || exit 1``.
LAYOUT_USAGE_EXIT = 64

#: ``<tools>/gen-<stamp>/.nx-building``, written by ``install_generation.sh``
#: the instant the directory exists and left in place. The Python twin of
#: ``NX_BUILDING_MARKER_NAME``.
#:
#: This is NOT the completion marker -- the RECEIPT is, and
#: :func:`list_generations` reads it. This is the BUILD CLAIM, the thing that
#: distinguishes "a builder is working here" from "a build died and left
#: wreckage": ``gc.sh`` keeps a receipt-less tree whose marker is younger than
#: its claim window, because a slow resolve or download writes nothing into
#: the tree for minutes at a time (nexus-xn84f).
#:
#: It had no Python twin and therefore no pin, which made it the one layout
#: name the twins test could not have caught drifting. Naming it here is what
#: lets ``tests/test_install_layout_twins_agree.py`` cover all of them.
BUILDING_MARKER_NAME = ".nx-building"


def _warn(event: str, **fields: object) -> None:
    """Say something out loud without requiring nexus, or structlog, to exist.

    This module runs in two worlds. In the installed one it is imported as
    ``nexus._install.layout_core`` and structlog is present, so a warning
    should join the same structured stream as everything else. At bootstrap
    it is run as a script by ``layout.sh`` with NOTHING installed, where
    importing structlog raises. The import is therefore deferred to the call
    rather than taken at module scope: an unconditional ``import structlog``
    here would make the whole module unimportable in exactly the world it
    exists to serve.

    The fallback writes to stderr, not ``logging`` and not ``print``. Not
    ``logging``, because an unconfigured root logger swallows a warning
    silently. Not ``print``, because stdout is RESERVED here -- every layout
    entry point prints its result there and a caller writes
    ``dir=$(nx_tools_dir) || exit 1``, so a diagnostic on stdout is how a
    caller ends up installing into it.
    """
    try:
        import structlog  # noqa: PLC0415 -- deferred; absent at bootstrap
    except ImportError:
        rendered = " ".join(f"{key}={value}" for key, value in sorted(fields.items()))
        sys.stderr.write(f"nexus: {event} {rendered}".rstrip() + "\n")
        return
    structlog.get_logger(__name__).warning(event, **fields)


def _error_base() -> type[Exception]:
    """``NexusError`` where nexus exists, ``Exception`` where it does not.

    :class:`LayoutError` has ONE identity per process and it is re-exported as
    ``nexus.install_layout.InstallLayoutError``, which is documented as a
    member of the nexus hierarchy and is caught by ``health.py`` and
    ``self_cmd.py``. Making it a genuine ``NexusError`` in the installed world
    keeps that true. Making it a plain ``Exception`` at bootstrap is what lets
    the module import at all with nothing installed.

    The alternative -- core raises ``LayoutError``, the wrapper defines a
    SUBCLASS ``InstallLayoutError(NexusError, LayoutError)`` -- is wrong in a
    way that is quiet: every error actually raised by this module would be the
    BASE, so ``except InstallLayoutError`` would stop catching the errors it
    catches today, and no test that only checks the class exists would notice.
    """
    try:
        from nexus.errors import NexusError  # noqa: PLC0415 -- deferred; absent at bootstrap
    except ImportError:
        return Exception
    return NexusError


class LayoutError(_error_base()):  # type: ignore[misc]
    """The layout was asked for something it refuses to name a path for."""


def _resolve_dir(env_var: str, default: Path) -> Path:
    """Apply the one override rule that both directory variables share.

    Five states, because "unset" is only one of them and the other four are
    where silent breakage lives:

    unset       -- the $HOME-derived default
    absolute    -- used verbatim
    empty       -- treated as unset. ``Path("")`` is ``Path(".")``, so
                   honouring an exported-but-empty variable would root the
                   entire install at whatever the caller's CWD happened to be
    relative    -- REFUSED. Same hazard as empty, but stated deliberately
                   enough that guessing an anchor would be worse than saying
                   no. This project has already paid for a moving CWD once
                   (the nexus-yg70j chdir fix)
    leading ~   -- expanded. A shell expands it before we ever see it; a
                   config file, a launchd plist or a systemd unit does not
    """
    raw = os.environ.get(env_var)
    if raw is None or not raw.strip():
        return default

    # Only the two forms a shell expands: bare "~" and a leading "~/".
    # Path.expanduser() also resolves "~someuser" out of the passwd database,
    # which the shell half does not -- and a divergence in the rule that
    # decides WHERE an install lands is the worst place to have one. Anything
    # else falls through to the absolute check below and is refused.
    stripped = raw.strip()
    if stripped == "~":
        stripped = str(Path.home())
    elif stripped.startswith("~/"):
        stripped = str(Path.home() / stripped[2:])

    candidate = Path(stripped)
    if not candidate.is_absolute():
        raise LayoutError(
            f"{env_var}={raw!r} is not an absolute path. The generation layout "
            f"is resolved from processes whose working directory is not stable, "
            f"so a relative override is refused rather than anchored to a guess."
        )
    return candidate


def tools_dir() -> Path:
    """The generation root. Recomputed per call; never cached (see module doc)."""
    return _resolve_dir(TOOLS_DIR_ENV, Path.home().joinpath(*_DEFAULT_TOOLS_SUBPATH))


#: uv's tool directory, and the conexus venv inside it. NOT ``tools_dir()``.
#: ``tools_dir()`` above is the NEXUS GENERATION ROOT (``<tools>/gen-*``);
#: these two are uv's own tree, where a ``uv tool install conexus`` lands.
#: They are different directories answering different questions and a caller
#: that confuses them gets a confidently wrong answer, which is why the names
#: are deliberately not near-homonyms.
def uv_tool_root() -> Path:
    """Where uv keeps its tools, resolved the way UV ITSELF resolves it.

    nexus-orhp5. Four rules in this tree answered this question and they
    disagreed:

      1. ``upgrade_finish.running_from_tool_install`` — substring test for
         ``"uv/tools/conexus"``; wrong whenever ``UV_TOOL_DIR`` points
         somewhere without that literal in it.
      2. ``health._check_orphan_uv_install`` — honoured ``UV_TOOL_DIR`` but
         not ``XDG_DATA_HOME``.
      3. ``legacy.sh`` / ``version_lockstep_action`` — shell out to
         ``uv tool dir``; correct by construction, but needs uv on PATH.
      4. ``upgrade_finish``'s uv-receipt read — a HARDCODED
         ``~/.local/share/uv/tools/conexus/uv-receipt.toml``, wrong for both
         env vars. This one was found by the sweep the bead asked for; three
         was an undercount.

    The precedence below is MEASURED against uv 0.8.0, not inferred::

        default           -> ~/.local/share/uv/tools
        UV_TOOL_DIR=X     -> X
        XDG_DATA_HOME=Y   -> Y/uv/tools
        both              -> X            (UV_TOOL_DIR wins outright)

    Deliberately does NOT shell out to ``uv tool dir``. Rules 3 and 5 already
    do that and are right to — they are shell, and uv is a hard dependency
    there. In Python this is on startup-adjacent paths (doctor, the finish
    pass), a subprocess per call is real cost, and the resolution is three
    lines of env lookup. The tradeoff is that an exotic future uv config
    could drift from this; if that happens, the fix is here, in ONE place,
    which is the entire point of the bead.
    """
    raw = os.environ.get("UV_TOOL_DIR")
    if raw and raw.strip():
        return Path(raw.strip()).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg and xdg.strip():
        return Path(xdg.strip()).expanduser() / "uv" / "tools"
    return Path.home() / ".local" / "share" / "uv" / "tools"


def uv_conexus_venv() -> Path:
    """The legacy ``uv tool install conexus`` venv root."""
    return uv_tool_root() / "conexus"


def is_under_uv_tool_install(path: Path | str) -> bool:
    """True when *path* lies inside the uv-managed conexus tree.

    Replaces the ``"uv/tools/conexus" in str(root)`` substring test. A
    substring is not a containment check: it answers yes for a path that
    merely spells those segments somewhere, and no for the real tree under a
    relocated ``UV_TOOL_DIR``.
    """
    try:
        target = Path(path).expanduser().resolve()
        root = uv_conexus_venv().resolve()
    except (OSError, RuntimeError):
        return False
    return target == root or root in target.parents


def bin_dir() -> Path:
    """The directory shims are written into. Recomputed per call."""
    return _resolve_dir(BIN_DIR_ENV, Path.home().joinpath(*_DEFAULT_BIN_SUBPATH))


#: The only shape a stamp or a command name may have. An ALLOWLIST, and
#: deliberately so: an earlier denylist here rejected separators, traversals
#: and whitespace -- every PATH hazard -- and still admitted
#: ``nx$(touch${IFS}PWNED)``, which reaches a shell double-quoted string in the
#: rendered shim and executes on the next invocation. The sink's hazard
#: alphabet is not the path's, and a denylist for one is not a denylist for
#: the other. This matters concretely rather than theoretically: audit finding
#: F1 has .4 DERIVING the shim set from the installed distribution's
#: entry_points metadata, so these names come from third-party wheels.
_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _require_component(label: str, value: str) -> str:
    """Refuse anything that is not a plain, single path component.

    Both callers interpolate their argument into a path AND into a shell
    script: a stamp names a directory a GC pass will later delete, and a
    command names a file written into the operator's bin directory and
    exec'd by the shim. Console-script names and timestamps are alphanumeric
    with dots, dashes and underscores; nothing legitimate needs more, so
    everything else is refused rather than escaped.
    """
    if not _COMPONENT_RE.match(value):
        raise LayoutError(
            f"{label} must match {_COMPONENT_RE.pattern} (letters, digits, "
            f"'.', '-' and '_', not leading with '.' or '-'), got {value!r}"
        )
    return value


def source_kind(spec: str) -> str:
    """Which KIND of source *spec* names, decided by SHAPE alone.

    The Python twin of ``nx_source_kind``, and until the collapse this rule had
    NO Python statement and therefore no pin -- the one layout rule with a
    recorded incident behind it and nothing watching it.

    This is the one place that question is answered, because it used to be
    answered in two. The generation builder classified by shape while
    ``scripts/reinstall-tool.sh``'s divergent-source guard classified by
    whether ``$SOURCE/pyproject.toml`` existed. They agree on ``.`` and on
    ``conexus`` and disagree on a bare name that happens to match a directory
    in the caller's cwd -- and the guard only fires when it concludes
    "registry", so the disagreement SKIPPED the refusal that stops a PyPI
    install from wiping a dev checkout's unreleased modules (nexus-pk9yt; the
    incident is nexus-q3xrx #2).

    Shape, never existence: a bare distribution name is a registry source
    wherever you happen to be standing. Existence is still worth checking, but
    it answers "can I read this", not "what kind of thing is it".

    The shell half states this as a ``case`` whose directory arm is
    ``.|..|/*|./*|../*|"~"/*|*/*``. Every one of those patterns except ``.``
    and ``..`` requires a slash, so the rule below is that case statement with
    the redundancy removed, and
    ``test_both_halves_classify_every_source_shape_alike`` is what says so
    rather than this comment.
    """
    return "directory" if spec in (".", "..") or "/" in spec else "registry"


def _root(tools: Path | None) -> Path:
    return tools if tools is not None else tools_dir()


def generation_dir(stamp: str, *, tools: Path | None = None) -> Path:
    """``<tools>/gen-<stamp>``, the directory one install builds and owns."""
    return _root(tools) / f"{GENERATION_PREFIX}{_require_component('generation stamp', stamp)}"


def legacy_generation_link(*, tools: Path | None = None) -> Path:
    """Where the legacy uv tree's ledger pointer lives -- whether or not it exists.

    A symlink at this path, targeting the uv venv, means the tree is REGISTERED:
    ``gc.sh`` will reap it once nothing runs from it. No symlink means a
    hybrid box (generation layout beside an unregistered uv tree) that will
    never converge on its own -- the state every checkout-driven box was in
    before nexus-hibpr, because only ``migrate_legacy.sh`` registered and the
    checkout path never ran it.
    """
    return _root(tools) / f"{GENERATION_PREFIX}{LEGACY_GENERATION_NAME}"


def current_link(*, tools: Path | None = None) -> Path:
    """``<tools>/current``, the pointer a flip moves and a shim reads."""
    return _root(tools) / CURRENT_LINK_NAME


def previous_link(*, tools: Path | None = None) -> Path:
    """``<tools>/previous``, the pointer a rollback reads."""
    return _root(tools) / PREVIOUS_LINK_NAME


def receipt_path(generation: Path) -> Path:
    """The receipt inside *generation*, which must already be an absolute path."""
    if not generation.is_absolute():
        raise LayoutError(
            f"a generation path must be absolute, got {str(generation)!r}"
        )
    return generation / RECEIPT_NAME


#: A PEP 508 extras group, anchored so that a source path which merely
#: contains brackets (``/Users/x/my[weird]repo``) is not mistaken for one:
#: a real extras group is followed by a version specifier or ends the spec.
_SPEC_EXTRAS_RE = re.compile(r"\[([^\[\]]*)\](?=$|[=<>!~@;])")


def build_spec(base: str, extras: list[str] | None = None, version: str = "") -> str:
    """The one place a PEP 508 install spec is assembled.

    Extras PRECEDE the version pin -- ``conexus[local]==7.18.0`` is valid and
    ``conexus==7.18.0[local]`` is not. That fixup lived in
    ``scripts/reinstall-tool.sh:157-158`` and .2's bead text re-derives it as
    its own responsibility, which is exactly the "one rule, two
    implementations" shape this contract exists to prevent. The builder calls
    this instead of restating it, and ``Receipt`` validates against the same
    reading, so a spec and its extras cannot disagree by construction.

    *base* is the distribution name for a registry install (``conexus``) or
    the path for a directory install (``.``, or an absolute checkout path).
    *version* is omitted for directory installs, which pin nothing.
    """
    spec = base
    if extras:
        spec += "[" + ",".join(sorted(set(extras))) + "]"
    if version:
        spec += f"=={version}"
    return spec


def _extras_in_spec(spec: str) -> list[str]:
    """The extras a PEP 508 spec asks for, normalised like ``Receipt.extras``."""
    match = _SPEC_EXTRAS_RE.search(spec)
    if match is None:
        return []
    return sorted({part.strip() for part in match.group(1).split(",") if part.strip()})


@dataclass(frozen=True)
class Receipt:
    """What an installed generation records about itself.

    This is the nexus-owned replacement for ``uv-receipt.toml``, and it is the
    only home extras have. ``base_interpreter`` is recorded because a
    generation's ``pyvenv.cfg`` points at a uv-managed CPython that uv itself
    can prune out from under us (the pipx#146 / uv#8028 class). We cannot
    prevent that; recording it is what lets ``nx doctor`` detect it.
    """

    version: str
    spec: str
    source_kind: str
    source: str
    python: str
    base_interpreter: str
    created_at: str
    extras: list[str] = field(default_factory=list)
    schema: int = RECEIPT_SCHEMA
    installer_schema: int = INSTALLER_SCHEMA

    def __post_init__(self) -> None:
        if self.source_kind not in SOURCE_KINDS:
            raise LayoutError(
                f"source_kind must be one of {SOURCE_KINDS}, got {self.source_kind!r}"
            )
        # Sorted and de-duplicated so that a receipt is stable across installs
        # and so that the spec the builder derives from it is deterministic.
        normalised = sorted(set(self.extras))
        object.__setattr__(self, "extras", normalised)

        # extras and spec are two statements of ONE fact, and per-field
        # validation cannot see them disagree. A receipt whose extras say
        # ["local"] over a spec that never asked for it round-trips perfectly
        # and re-opens the 768->384 embedder downgrade this receipt exists to
        # prevent -- the next install reads extras the installed tree does not
        # actually have.
        in_spec = _extras_in_spec(self.spec)
        if in_spec != normalised:
            raise LayoutError(
                f"extras {normalised} disagree with spec {self.spec!r}, which "
                f"asks for {in_spec}. They are one fact, not two."
            )

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def to_json(self) -> str:
        """Key-sorted, indented, newline-terminated: a receipt is read by a
        human during an incident and diffed by tests."""
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_json(cls, text: str) -> Receipt:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LayoutError(f"{RECEIPT_NAME} is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise LayoutError(f"{RECEIPT_NAME} must be a JSON object")
        return cls.from_dict(payload)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Receipt:
        """Strict about the fields it knows, tolerant of the ones it does not.

        Generation GC keeps the previous generation for free rollback, so an
        OLDER nx will read a receipt a NEWER installer wrote. An unknown key
        is therefore not an error; an unknown ``schema`` is, because that
        says the fields we DO recognise may not mean what we think.
        """
        found = payload.get("schema")
        try:
            found = int(found)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            pass
        if found != RECEIPT_SCHEMA:
            raise LayoutError(
                f"{RECEIPT_NAME} declares schema {found!r}, this nexus reads "
                f"schema {RECEIPT_SCHEMA}"
            )
        required = ("version", "spec", "source_kind", "source", "python",
                    "base_interpreter", "created_at")
        missing = sorted(name for name in required if name not in payload)
        if missing:
            raise LayoutError(f"{RECEIPT_NAME} is missing {', '.join(missing)}")

        # Field-by-field, never ``cls(**payload)``: index-access makes a
        # truncated receipt fail loud, re-coercion keeps a hand-edited file
        # usable, and an unknown key is dropped by simply not being read.
        # Same shape as ``daemon/service_registry.py``'s LeaseRecord.
        extras = payload.get("extras", [])
        if not isinstance(extras, list):
            raise LayoutError(
                f"{RECEIPT_NAME} extras must be a list, got {type(extras).__name__}"
            )
        return cls(
            version=str(payload["version"]),
            spec=str(payload["spec"]),
            source_kind=str(payload["source_kind"]),
            source=str(payload["source"]),
            python=str(payload["python"]),
            base_interpreter=str(payload["base_interpreter"]),
            created_at=str(payload["created_at"]),
            extras=[str(extra) for extra in extras],
            schema=int(payload["schema"]),
            installer_schema=int(payload.get("installer_schema", INSTALLER_SCHEMA)),
        )


def render_shim(command: str, *, tools: Path | None = None) -> str:
    """The body of ``<bin>/<command>``: resolve the pointer, then exec.

    The absolute tools path is baked in, which makes a written shim
    $HOME-independent -- and therefore means shims must be REWRITTEN when
    ``NX_TOOLS_DIR`` changes and cannot be shared between sandboxes.

    ``nexus-utpuw.4`` writes these files; the body is fixed here so that the
    phase which writes them cannot quietly restate it.
    """
    _require_component("shim command", command)
    pointer = current_link(tools=tools)
    return "\n".join([
        "#!/bin/sh",
        "# Generated by nexus. Rewritten on every install; edits are lost.",
        "#",
        "# The pointer is resolved BEFORE the exec, and that ordering is",
        "# load-bearing rather than stylistic. CPython looks for pyvenv.cfg next",
        "# to the executable as it was INVOKED, before it resolves symlinks, so",
        "# an exec through the pointer itself would leak that component into",
        "# sys.prefix and sys.path -- and the next flip would retarget every",
        "# not-yet-imported module in a process that was already running",
        "# (nexus-q3xrx).",
        f'NX_GEN="$(readlink "{pointer}")" || {{',
        f'    echo "nexus: {command}: no current generation at {pointer}" >&2',
        f"    exit {SHIM_NO_CURRENT_EXIT}",
        "}",
        f'exec "$NX_GEN/bin/{command}" "$@"',
        "",
    ])


def current_generation(*, tools: Path | None = None) -> Path:
    """The generation ``<tools>/current`` points at, via one ``readlink``.

    Raises :class:`LayoutError` rather than a bare ``OSError`` when
    the pointer is absent or not a symlink -- "no current generation" is a
    named, catchable state, not an accident of how the filesystem call
    happened to fail. Deliberately does NOT stat the target: ``readlink(2)``
    reads the link's content only, so a pointer at a reaped generation (the
    ordinary state between a GC pass and the next flip) resolves cleanly
    rather than raising. The contract (module docstring) is that ``current``
    is always an ABSOLUTE symlink; a relative target is refused rather than
    silently resolved against the reader's cwd.
    """
    link = current_link(tools=tools)
    try:
        raw = os.readlink(link)
    except OSError as exc:
        raise LayoutError(f"no current generation at {link}: {exc}") from exc
    target = Path(raw)
    if not target.is_absolute():
        raise LayoutError(
            f"{link} must be an absolute symlink, got {raw!r}"
        )
    return target


def is_stale(baked: Path | None = None, *, tools: Path | None = None) -> bool:
    """Exact staleness: *baked* (default this process's ``sys.prefix``) is
    compared against the CURRENT generation on disk.

        stale  <=>  Path(sys.prefix) != os.readlink(<tools>/current)

    One readlink. No filesystem-clock inference, no false positives or false
    negatives. This is ALSO nexus-utpuw design point 6's spawn-time tripwire:
    a new spawn logs (never fails) when its own baked generation differs
    from current. Implemented once -- a short-lived spawn calls this with no
    *baked* argument and gets its own live ``sys.prefix``; a long-lived host
    captures ``sys.prefix`` at startup and passes that captured value back in
    later, to compare against what it started with rather than what it is
    running as now.

    Propagates :class:`LayoutError` when :func:`current_generation`
    cannot resolve a pointer at all (no symlink present) -- that is not a
    stale/fresh verdict this formula can make, so it is surfaced rather than
    coerced into an unconditional answer. A DANGLING pointer (symlink
    present, target absent) is not that case: it resolves to a path that can
    never equal a live baseline, so it reports stale rather than raising.
    """
    baseline = Path(sys.prefix) if baked is None else baked
    return baseline != current_generation(tools=tools)


def list_generations(*, tools: Path | None = None) -> list[Path]:
    """Complete generations under *tools*, oldest first.

    A generation is a ``gen-*`` directory CONTAINING a receipt -- the
    completion marker the builder (.2) writes last. A receipt-less ``gen-*``
    directory is wreckage from a build that died before finishing; nothing
    ever pointed ``current`` at it, so it is not a generation at all. This is
    the SAME completeness rule ``nexus-utpuw.6``'s GC pass applies (see
    ``src/nexus/_install/gc.sh``), so the two halves can never disagree
    about what exists. Stamps sort chronologically as plain strings by
    construction (``install_generation.sh``), so a plain name sort is
    creation order.

    A *tools* root that does not exist at all, or exists with nothing built
    in it yet, returns an empty list -- an ordinary state, not the error
    :func:`current_generation` raises for a missing pointer.
    """
    root = _root(tools)
    if not root.is_dir():
        return []
    candidates = [
        entry for entry in root.iterdir()
        if entry.is_dir()
        and entry.name.startswith(GENERATION_PREFIX)
        and (entry / RECEIPT_NAME).is_file()
    ]
    return sorted(candidates, key=lambda p: p.name)


def read_receipt(generation: Path) -> Receipt:
    """Read and parse *generation*'s receipt.

    Raises :class:`LayoutError` when the receipt is missing --
    :meth:`Receipt.from_json` already raises the same error for corrupt or
    unrecognised JSON, so both failure modes reach a caller as one named,
    catchable exception rather than a bare ``OSError`` for one and a
    ``json.JSONDecodeError`` for the other.
    """
    path = receipt_path(generation)
    try:
        text = path.read_text()
    except OSError as exc:
        raise LayoutError(f"no receipt at {path}: {exc}") from exc
    return Receipt.from_json(text)


# ---------------------------------------------------------------------------
# The command-line face, for layout.sh
#
# layout.sh dispatches its behavioural functions here rather than restating
# them, which is what removes the second implementation. The calling contract
# is layout.sh's own, unchanged, because its callers depend on it:
#
#   - a result goes to STDOUT and nothing else does, so that a caller can
#     safely write ``dir=$(nx_tools_dir) || exit 1``
#   - a refusal prints to STDERR, prints NOTHING to stdout, and exits
#     LAYOUT_USAGE_EXIT. A refusal that also emits a path is how a caller ends
#     up installing into it
#   - an unexpected failure is still a refusal, not a traceback on stdout
#
# Argument shapes match the shell functions one for one, positionally, so that
# the dispatchers in layout.sh are a single line each with no reordering. An
# omitted optional argument arrives as the empty string, exactly as ``"${2-}"``
# delivers it, and is read as "not supplied".
# ---------------------------------------------------------------------------


def _opt_path(raw: str) -> Path | None:
    """An optional trailing tools-root argument, in shell's terms.

    ``nx_current_link "${1-}"`` passes an empty string when the caller gave
    nothing, and the shell half reads that as "resolve one for me". ``Path("")``
    is ``Path(".")``, so passing it straight through would silently root the
    layout at the caller's CWD -- the same hazard ``_resolve_dir`` refuses an
    empty override for.
    """
    return Path(raw) if raw else None


def _cli_source_kind(spec: str) -> str:
    return source_kind(spec)


def _cli_tools_dir() -> str:
    return str(tools_dir())


def _cli_bin_dir() -> str:
    return str(bin_dir())


def _cli_generation_dir(stamp: str, tools: str = "") -> str:
    return str(generation_dir(stamp, tools=_opt_path(tools)))


def _cli_current_link(tools: str = "") -> str:
    return str(current_link(tools=_opt_path(tools)))


def _cli_previous_link(tools: str = "") -> str:
    return str(previous_link(tools=_opt_path(tools)))


def _cli_root(tools: str = "") -> str:
    return str(_root(_opt_path(tools)))


def _cli_receipt_path(generation: str) -> str:
    return str(receipt_path(Path(generation)))


def _cli_render_shim(command: str, tools: str = "") -> str:
    return render_shim(command, tools=_opt_path(tools)).rstrip("\n")


def _cli_build_spec(base: str, extras: str = "", version: str = "") -> str:
    return build_spec(base, _split_extras(extras), version)


def _cli_render_receipt(
    version: str,
    spec: str,
    kind: str,
    source: str,
    extras: str,
    python: str,
    base_interpreter: str,
    created_at: str,
) -> str:
    """The receipt, rendered by the same code that reads it.

    The shell half hand-escaped JSON and refused a value carrying a control
    character, because it could not represent one. :meth:`Receipt.to_json`
    escapes correctly by construction, so that refusal is no longer forced by
    the escaper -- but it is KEPT, deliberately. A newline in a generation path
    or a created-at stamp is a sign something is wrong upstream, every caller
    has been refused it since the shell half was written, and quietly starting
    to accept it would be a behaviour change nobody asked for.
    """
    for label, value in (
        ("version", version), ("spec", spec), ("source", source),
        ("extras", extras), ("python", python),
        ("base_interpreter", base_interpreter), ("created_at", created_at),
    ):
        if any(ch.isprintable() is False and ch != " " for ch in value):
            raise LayoutError(
                f"receipt values must not contain control characters, {label} does"
            )
    return Receipt(
        version=version,
        spec=spec,
        source_kind=kind,
        source=source,
        python=python,
        base_interpreter=base_interpreter,
        created_at=created_at,
        extras=_split_extras(extras),
    ).to_json().rstrip("\n")


def _split_extras(raw: str) -> list[str]:
    """A comma-separated extras list as the shell half passes it.

    Empty entries are dropped rather than becoming an empty extra, which is
    what ``grep -v '^$'`` did on the shell side: ``"local,,voyage"`` and a
    trailing comma both arrive from string concatenation upstream.
    """
    return [part.strip() for part in raw.split(",") if part.strip()]


#: The verbs ``layout.sh`` dispatches, mapped to their implementations. The
#: names are the shell function names minus the ``nx_`` prefix, so that a
#: reader can match a dispatcher to its verb without a lookup table.
_VERBS = {
    "source_kind": _cli_source_kind,
    "tools_dir": _cli_tools_dir,
    "bin_dir": _cli_bin_dir,
    "generation_dir": _cli_generation_dir,
    "current_link": _cli_current_link,
    "previous_link": _cli_previous_link,
    "root": _cli_root,
    "receipt_path": _cli_receipt_path,
    "render_shim": _cli_render_shim,
    "build_spec": _cli_build_spec,
    "render_receipt": _cli_render_receipt,
}


def main(argv: list[str]) -> int:
    """Run one verb. Returns the process exit status.

    Every failure path returns :data:`LAYOUT_USAGE_EXIT` and writes nothing to
    stdout, including a wrong arity and an unknown verb -- those reach a user
    as a shell script that mis-called us, which is a usage error, and the
    alternative is a Python traceback arriving where a caller expected a path.
    """
    if not argv:
        sys.stderr.write("nexus: layout_core: no verb given\n")
        return LAYOUT_USAGE_EXIT
    verb, args = argv[0], argv[1:]
    fn = _VERBS.get(verb)
    if fn is None:
        known = ", ".join(sorted(_VERBS))
        sys.stderr.write(f"nexus: layout_core: unknown verb {verb!r}; known: {known}\n")
        return LAYOUT_USAGE_EXIT
    try:
        rendered = fn(*args)
    except LayoutError as exc:
        sys.stderr.write(f"nexus: {exc}\n")
        return LAYOUT_USAGE_EXIT
    except TypeError as exc:
        # Arity. Raised by the call above, never from inside a verb: the verbs
        # are typed and take only strings, so a TypeError here is layout.sh
        # passing the wrong number of arguments.
        sys.stderr.write(f"nexus: layout_core: bad arguments to {verb}: {exc}\n")
        return LAYOUT_USAGE_EXIT
    sys.stdout.write(rendered + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
