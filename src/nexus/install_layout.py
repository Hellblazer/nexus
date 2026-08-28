# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The generation layout: where installs live, what they record, how they bind.

nexus-utpuw.1 (P0). This module is the Python statement of a contract that
also has a shell statement in ``src/nexus/_install/layout.sh``; the two are
pinned to each other by ``tests/test_install_layout_twins_agree.py``. Two
implementations exist because the callers have incompatible import
constraints -- the generation builder runs from ``scripts/reinstall-tool.sh``
and may run with nothing installed, while ``health.py`` and
``upgrade_finish.py`` run after the install and can import nexus.

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
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nexus.errors import NexusError

#: Override the generation root. Absolute paths only; see ``_resolve_dir``.
TOOLS_DIR_ENV = "NX_TOOLS_DIR"

#: Override the directory the shims are written into.
BIN_DIR_ENV = "NX_BIN_DIR"

#: ``<tools>/gen-<stamp>``. A prefix rather than a bare stamp so that a GC
#: pass can tell a generation from anything else that lands in the root.
GENERATION_PREFIX = "gen-"

#: Names that live in a venv's ``bin/`` but are NEVER shimmed into the shared
#: bin dir. The Python twin of ``NX_NEVER_SHIM`` in ``_install/shims.sh``,
#: pinned by ``tests/test_install_layout_twins_agree.py``.
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


class InstallLayoutError(NexusError):
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
        raise InstallLayoutError(
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
        raise InstallLayoutError(
            f"{label} must match {_COMPONENT_RE.pattern} (letters, digits, "
            f"'.', '-' and '_', not leading with '.' or '-'), got {value!r}"
        )
    return value


def _root(tools: Path | None) -> Path:
    return tools if tools is not None else tools_dir()


def generation_dir(stamp: str, *, tools: Path | None = None) -> Path:
    """``<tools>/gen-<stamp>``, the directory one install builds and owns."""
    return _root(tools) / f"{GENERATION_PREFIX}{_require_component('generation stamp', stamp)}"


def current_link(*, tools: Path | None = None) -> Path:
    """``<tools>/current``, the pointer a flip moves and a shim reads."""
    return _root(tools) / CURRENT_LINK_NAME


def previous_link(*, tools: Path | None = None) -> Path:
    """``<tools>/previous``, the pointer a rollback reads."""
    return _root(tools) / PREVIOUS_LINK_NAME


def receipt_path(generation: Path) -> Path:
    """The receipt inside *generation*, which must already be an absolute path."""
    if not generation.is_absolute():
        raise InstallLayoutError(
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
            raise InstallLayoutError(
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
            raise InstallLayoutError(
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
            raise InstallLayoutError(f"{RECEIPT_NAME} is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise InstallLayoutError(f"{RECEIPT_NAME} must be a JSON object")
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
            raise InstallLayoutError(
                f"{RECEIPT_NAME} declares schema {found!r}, this nexus reads "
                f"schema {RECEIPT_SCHEMA}"
            )
        required = ("version", "spec", "source_kind", "source", "python",
                    "base_interpreter", "created_at")
        missing = sorted(name for name in required if name not in payload)
        if missing:
            raise InstallLayoutError(f"{RECEIPT_NAME} is missing {', '.join(missing)}")

        # Field-by-field, never ``cls(**payload)``: index-access makes a
        # truncated receipt fail loud, re-coercion keeps a hand-edited file
        # usable, and an unknown key is dropped by simply not being read.
        # Same shape as ``daemon/service_registry.py``'s LeaseRecord.
        extras = payload.get("extras", [])
        if not isinstance(extras, list):
            raise InstallLayoutError(
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

    Raises :class:`InstallLayoutError` rather than a bare ``OSError`` when
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
        raise InstallLayoutError(f"no current generation at {link}: {exc}") from exc
    target = Path(raw)
    if not target.is_absolute():
        raise InstallLayoutError(
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

    Propagates :class:`InstallLayoutError` when :func:`current_generation`
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

    Raises :class:`InstallLayoutError` when the receipt is missing --
    :meth:`Receipt.from_json` already raises the same error for corrupt or
    unrecognised JSON, so both failure modes reach a caller as one named,
    catchable exception rather than a bare ``OSError`` for one and a
    ``json.JSONDecodeError`` for the other.
    """
    path = receipt_path(generation)
    try:
        text = path.read_text()
    except OSError as exc:
        raise InstallLayoutError(f"no receipt at {path}: {exc}") from exc
    return Receipt.from_json(text)
