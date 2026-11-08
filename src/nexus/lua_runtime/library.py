# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The two procedure library roots (RDR-242 Technical Design 4; RDR-240 Technical Design 1).

``find(name, cwd=...)`` is the lookup ``run_script`` makes; ``procedures(cwd=...)`` is the
listing behind ``nx run --list`` and the MCP tool's description. Source is never a
parameter of either: a procedure is a file in one of two roots.

* the **shipped** root is the packaged ``nexus/procedures`` directory, reached through
  :mod:`importlib.resources` the way :func:`nexus.tables.load.load_packaged_table` reaches
  its tables, so an installed wheel finds it;
* the **repo** root is ``<repo top>/.nexus/procedures``, with the repo top from
  :func:`nexus.lua_runtime.header.probe_repo` (``None`` outside a repo, or for a missing
  ``cwd``, which means no repo root and not a failure).

A name matches ``[a-z][a-z0-9_]{0,63}``, so it carries no separator, no ``..`` and no
absolute path, and it is not a Windows device name (``con``, ``prn``, ``aux``, ``nul``,
``com1``-``com9``, ``lpt1``-``lpt9``), which a file system there would not open as a file.
The file is ``<root>/<name>.lua``, a directory entry of exactly that name (a case-insensitive
file system does not find ``Foo.lua`` as ``foo``), and its resolved path (symbolic links
followed) must lie inside the resolved root. The resolved repo root must also lie inside the
resolved repo top: a ``.nexus`` or ``.nexus/procedures`` that is a link out of the checkout is
refused with the reason ``outside_root``. RDR-240 leaves a root that is itself a link open;
this is the safe side, so a cloned repo cannot point its root at files outside the clone. The
shipped root has no such bound and is judged by where it resolves. A repo file whose name is
also a shipped procedure's is refused, never preferred. Every refusal is a
:class:`LibraryError` whose ``reason`` is one of :data:`REASONS`.

The repo root is OFF unless the environment variable :data:`REPO_ROOT_ENV` is exactly
``"1"`` (RDR-242 Open Question S5, answered on its safe side: a repo's own procedures do
not run by default). The variable is read at lookup time, never at import. While it is off
the repo root is not listed, and a name found only there is refused with the reason
``repo_root_off``; the shipped root always runs.

``find`` runs the repo probe when the repo root is on, and, while it is off, for a name not
in the shipped root (to tell ``repo_root_off`` from ``not_found``); ``procedures`` does when
the root is on. A caller that has probed already passes the result as ``probe=`` so a run
probes once. A git that cannot answer raises :class:`~nexus.lua_runtime.header.RepoProbeError`
when the root is on, and it propagates: a shadow check that cannot see the repo root must not
pass. While the root is off the probe only tells a mistyped name from an off one, so a probe
that fails there reads as ``not_found``.
"""
from __future__ import annotations

import importlib.resources
import os
import stat
from pathlib import Path
from typing import Literal, NamedTuple

from nexus.lua_runtime.errors import ScriptError
from nexus.lua_runtime.header import NAME_RE, RepoProbe, RepoProbeError, parse, probe_repo

#: The one switch for the repo root (RDR-242 Open Question S5): exactly "1" turns it on;
#: unset, "0", "true" or anything else leaves it off.
REPO_ROOT_ENV: str = "NX_PROCEDURES_REPO_ROOT"

#: The repo root's place under the repo top.
REPO_ROOT_PARTS: tuple[str, ...] = (".nexus", "procedures")

#: Why a lookup is refused (``LibraryError.reason``).
REASONS: tuple[str, ...] = (
    "invalid_name", "not_found", "outside_root", "shadows_shipped", "repo_root_off", "unreadable",
)

#: Names a Windows file system reserves for devices, whatever the extension.
RESERVED_NAMES: frozenset[str] = frozenset(
    {"con", "prn", "aux", "nul", *(f"com{n}" for n in range(1, 10)), *(f"lpt{n}" for n in range(1, 10))}
)

#: Bytes read from a procedure file: the static check's source bound plus one, so a file
#: over the bound reaches the check's ``size`` rule without being read whole. Pinned equal
#: to ``static_check.MAX_SOURCE + 1`` by a test (this module does not import ``lupa``).
READ_LIMIT: int = 64 * 1024 + 1

Root = Literal["shipped", "repo"]


class LibraryError(Exception):
    """A name the library refuses; ``reason`` is one of :data:`REASONS`.

    ``invalid_name`` the rule (or a reserved device name), ``not_found`` in neither root,
    ``outside_root`` a resolved path that leaves its root (or a repo root that leaves the
    repo), ``shadows_shipped`` a repo file named like a shipped procedure, ``repo_root_off`` a
    name found only under the repo root while the switch is off, ``unreadable`` a file or
    directory the operating system would not let the library read (permission, vanished, not a
    regular file). ``run_script`` maps it to its own error; it is not a
    :class:`ScriptRunError`.
    """

    def __init__(self, reason: str, detail: str) -> None:
        if reason not in REASONS:
            raise ValueError(f"unknown LibraryError reason {reason!r}")
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


class Procedure(NamedTuple):
    """One library entry: its name, its header summary and the root it came from."""

    name: str
    summary: str
    root: Root


class _Unprobed:
    """The type of :data:`UNPROBED`."""


#: The default of ``find``'s ``probe``: the caller has not probed, so ``find`` does. (``None``
#: is a real probe result, "in no repo", and must stay distinguishable.)
UNPROBED = _Unprobed()


def shipped_root() -> Path:
    """The packaged ``nexus/procedures`` directory."""
    return Path(str(importlib.resources.files("nexus").joinpath("procedures")))


def repo_root_enabled() -> bool:
    """Whether the repo root runs: the environment variable is exactly ``"1"``."""
    return os.environ.get(REPO_ROOT_ENV) == "1"


def _repo_root(
    cwd: Path | None, probe: RepoProbe | None | _Unprobed = UNPROBED
) -> tuple[Path, Path] | None:
    """``(repo root directory, repo top)`` of *cwd*, whether or not the switch is on."""
    if isinstance(probe, _Unprobed):
        probe = probe_repo(cwd)
    return (probe.root.joinpath(*REPO_ROOT_PARTS), probe.root) if probe else None


def _valid_name(name: str) -> bool:
    return NAME_RE.fullmatch(name) is not None and name not in RESERVED_NAMES


def _check_name(name: object) -> str:
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise LibraryError("invalid_name", f"{name!r} breaks the name rule [a-z][a-z0-9_]{{0,63}}")
    if name in RESERVED_NAMES:
        raise LibraryError("invalid_name", f"{name!r} is a reserved device name")
    return name


def _inside(path: Path, root: Path) -> bool:
    return path.is_relative_to(root)


def _unreadable(what: str, exc: BaseException) -> LibraryError:
    return LibraryError("unreadable", f"{what}: {type(exc).__name__}: {exc}")


def _lookup(
    root: Path | None, name: str, label: str, *, top: Path | None = None
) -> tuple[Path, Path] | None:
    """``(file, resolved root)`` for ``<root>/<name>.lua``, or ``None`` when no such regular
    file exists. A file that resolves outside its resolved root is refused, and so is a root
    that resolves outside *top* (the repo top, for the repo root). The directory entry must be
    named exactly ``<name>.lua``. A directory the system will not let us read is
    ``unreadable``."""
    if root is None:
        return None
    entry = f"{name}.lua"
    try:
        with os.scandir(root) as it:
            if not any(e.name == entry for e in it):
                return None
        path = root / entry
        if not path.exists():  # follows links: a dangling link is no file
            return None
        resolved_root = root.resolve()
        resolved = path.resolve()
        if top is not None and not _inside(resolved_root, top.resolve()):
            raise LibraryError("outside_root", f"the {label} root resolves outside the repository")
        if not _inside(resolved, resolved_root):
            raise LibraryError("outside_root", f"{name} in the {label} root resolves outside it")
        if not resolved.is_file():
            return None
    except (FileNotFoundError, NotADirectoryError):
        return None
    except (OSError, RuntimeError) as exc:  # RuntimeError: a link loop, before Python 3.13
        raise _unreadable(f"{name} in the {label} root", exc) from exc
    return resolved, resolved_root


def _read(path: Path) -> bytes:
    """At most :data:`READ_LIMIT` bytes of the regular file *path*.

    The descriptor is opened without following a final link (a swap after the resolve) and
    without blocking (a pipe put in the file's place), then checked to be a regular file.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise LibraryError("unreadable", f"{path.name} is not a regular file")
            with os.fdopen(fd, "rb", closefd=False) as fh:
                return fh.read(READ_LIMIT)
        finally:
            os.close(fd)
    except OSError as exc:
        raise _unreadable(path.name, exc) from exc


def find(
    name: str, *, cwd: Path | None, probe: RepoProbe | None | _Unprobed = UNPROBED
) -> tuple[Path, bytes]:
    """The resolved path and bytes of the procedure *name*, looked up for a run in *cwd*.

    *probe* is the caller's :func:`~nexus.lua_runtime.header.probe_repo` result for *cwd*
    (``None`` meaning no repo); left out, ``find`` probes itself. At most ``READ_LIMIT``
    bytes are read, from a regular file only.

    Raises :class:`LibraryError` for a name that breaks the rule or is a reserved device name
    (``invalid_name``); a name in neither root (``not_found``); a resolved path outside its root, or a
    repo root outside the repo (``outside_root``); a repo file named like a shipped procedure
    (``shadows_shipped``); a name found only under the repo root while it is off
    (``repo_root_off``); and a file or directory the system will not let it read
    (``unreadable``). Raises :class:`~nexus.lua_runtime.header.RepoProbeError` only when the
    repo root is on and git cannot say where the repo is.
    """
    _check_name(name)
    shipped = _lookup(shipped_root(), name, "shipped")
    if not repo_root_enabled():
        if shipped is None:
            # The probe only tells a repo-root name from a mistyped one, so a probe that fails
            # never turns a mistyped name into a repo error.
            try:
                located = _repo_root(cwd, probe)
                in_off_root = located is not None and (located[0] / f"{name}.lua").exists()
            except (RepoProbeError, OSError):
                in_off_root = False
            if in_off_root:
                raise LibraryError(
                    "repo_root_off",
                    f"{name} is in .nexus/procedures/, which is off; set {REPO_ROOT_ENV}=1 "
                    "to run repo procedures (RDR-242 S5)",
                )
            raise LibraryError("not_found", f"no procedure {name} in the shipped root")
        return shipped[0], _read(shipped[0])
    located = _repo_root(cwd, probe)
    repo = _lookup(located[0], name, "repo", top=located[1]) if located else None
    if shipped is not None and repo is not None:
        raise LibraryError("shadows_shipped", f"the repo procedure {name} shadows a shipped one")
    found = shipped or repo
    if found is None:
        raise LibraryError("not_found", f"no procedure {name} in the shipped or repo root")
    return found[0], _read(found[0])


def _entries(root: Path | None, label: Root, *, top: Path | None = None) -> dict[str, Procedure]:
    out: dict[str, Procedure] = {}
    try:
        if root is None or not root.is_dir():
            return out
        paths = sorted(root.glob("*.lua"))
    except (OSError, RuntimeError):
        return out  # a root the system will not let us list lists nothing; find says why
    for path in paths:
        name = path.stem
        if not _valid_name(name):
            continue
        try:
            hit = _lookup(root, name, label, top=top)
        except LibraryError:
            continue
        if hit is None:
            continue
        try:
            summary = parse(_read(hit[0]), name=name).summary
        except ScriptError as exc:
            summary = f"header refused: {exc.detail}"
        except LibraryError as exc:
            summary = f"{exc.reason}: {exc.detail}"
        out[name] = Procedure(name, summary, label)
    return out


def procedures(*, cwd: Path | None) -> list[Procedure]:
    """Every runnable procedure of both roots, sorted by name, with its header summary.

    A repo file named like a shipped procedure is left out (it cannot run). A file whose
    header does not parse, or that cannot be read, is listed with the refusal as its summary,
    so its author sees why. A root the system will not let it list lists nothing.
    """
    shipped = _entries(shipped_root(), "shipped")
    if not repo_root_enabled():
        return sorted(shipped.values(), key=lambda p: p.name)
    located = _repo_root(cwd)
    repo_entries = _entries(located[0], "repo", top=located[1]) if located else {}
    repo = {n: p for n, p in repo_entries.items() if n not in shipped}
    return sorted([*shipped.values(), *repo.values()], key=lambda p: p.name)
