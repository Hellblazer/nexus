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
absolute path. The file is ``<root>/<name>.lua`` and its resolved path (symbolic links
followed) must lie inside the resolved root. A repo file whose name is also a shipped
procedure's is refused, never preferred. Every refusal is a :class:`LibraryError` whose
``reason`` is one of :data:`REASONS`.

The repo root is OFF unless the environment variable :data:`REPO_ROOT_ENV` is exactly
``"1"`` (RDR-242 Open Question S5, answered on its safe side: a repo's own procedures do
not run by default). The variable is read at lookup time, never at import. While it is off
the repo root is not listed, and a name found only there is refused with the reason
``repo_root_off``; the shipped root always runs.

``find`` runs the repo probe when the repo root is on, and, while it is off, for a name not
in the shipped root (to tell ``repo_root_off`` from ``not_found``); ``procedures`` does when
the root is on. A git that cannot answer raises :class:`~nexus.lua_runtime.header.RepoProbeError`,
which propagates: a shadow check that cannot see the repo root must not pass.
"""
from __future__ import annotations

import importlib.resources
import os
from pathlib import Path
from typing import Literal, NamedTuple

from nexus.lua_runtime.errors import ScriptError
from nexus.lua_runtime.header import NAME_RE, parse, probe_repo

#: The one switch for the repo root (RDR-242 Open Question S5): exactly "1" turns it on;
#: unset, "0", "true" or anything else leaves it off.
REPO_ROOT_ENV: str = "NX_PROCEDURES_REPO_ROOT"

#: The repo root's place under the repo top.
REPO_ROOT_PARTS: tuple[str, ...] = (".nexus", "procedures")

#: Why a lookup is refused (``LibraryError.reason``).
REASONS: tuple[str, ...] = ("name", "not_found", "outside_root", "shadows_shipped", "repo_root_off")

#: Bytes read from a procedure file: the static check's source bound plus one, so a file
#: over the bound reaches the check's ``size`` rule without being read whole. Pinned equal
#: to ``static_check.MAX_SOURCE + 1`` by a test (this module does not import ``lupa``).
READ_LIMIT: int = 64 * 1024 + 1

Root = Literal["shipped", "repo"]


class LibraryError(Exception):
    """A name the library refuses; ``reason`` is one of :data:`REASONS`.

    ``name`` the rule, ``not_found`` in neither root, ``outside_root`` a resolved path that
    leaves its root, ``shadows_shipped`` a repo file named like a shipped procedure,
    ``repo_root_off`` a name found only under the repo root while the switch is off.
    ``run_script`` maps it to its own error; it is not a :class:`ScriptRunError`.
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


def shipped_root() -> Path:
    """The packaged ``nexus/procedures`` directory."""
    return Path(str(importlib.resources.files("nexus").joinpath("procedures")))


def repo_root_enabled() -> bool:
    """Whether the repo root runs: the environment variable is exactly ``"1"``."""
    return os.environ.get(REPO_ROOT_ENV) == "1"


def _repo_root(cwd: Path | None) -> Path | None:
    """The repo root directory of *cwd*, whether or not the switch is on."""
    probe = probe_repo(cwd)
    return probe.root.joinpath(*REPO_ROOT_PARTS) if probe else None


def _check_name(name: object) -> str:
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise LibraryError("name", f"{name!r} breaks the name rule [a-z][a-z0-9_]{{0,63}}")
    return name


def _inside(path: Path, root: Path) -> bool:
    return path.is_relative_to(root)


def _lookup(root: Path | None, name: str, label: str) -> tuple[Path, Path] | None:
    """``(file, resolved root)`` for ``<root>/<name>.lua``, or ``None`` when no such regular
    file exists. A file that resolves outside its resolved root is refused."""
    if root is None:
        return None
    path = root / f"{name}.lua"
    if not path.exists():  # follows links: a dangling link is no file
        return None
    resolved_root = root.resolve()
    resolved = path.resolve()
    if not _inside(resolved, resolved_root):
        raise LibraryError("outside_root", f"{name} in the {label} root resolves outside it")
    if not resolved.is_file():
        return None
    return resolved, resolved_root


def _read(path: Path) -> bytes:
    with path.open("rb") as fh:
        return fh.read(READ_LIMIT)


def find(name: str, *, cwd: Path | None) -> tuple[Path, bytes]:
    """The resolved path and bytes of the procedure *name*, looked up for a run in *cwd*.

    Raises :class:`LibraryError` for a name that breaks the rule, a name in neither root,
    a resolved path outside its root, and a repo file named like a shipped procedure.
    At most ``READ_LIMIT`` bytes are read.
    """
    _check_name(name)
    shipped = _lookup(shipped_root(), name, "shipped")
    if not repo_root_enabled():
        if shipped is None:
            root = _repo_root(cwd)
            if root is not None and (root / f"{name}.lua").exists():
                raise LibraryError(
                    "repo_root_off",
                    f"{name} is in .nexus/procedures/, which is off; set {REPO_ROOT_ENV}=1 "
                    "to run repo procedures (RDR-242 S5)",
                )
            raise LibraryError("not_found", f"no procedure {name} in the shipped root")
        return shipped[0], _read(shipped[0])
    repo = _lookup(_repo_root(cwd), name, "repo")
    if shipped is not None and repo is not None:
        raise LibraryError("shadows_shipped", f"the repo procedure {name} shadows a shipped one")
    found = shipped or repo
    if found is None:
        raise LibraryError("not_found", f"no procedure {name} in the shipped or repo root")
    return found[0], _read(found[0])


def _entries(root: Path | None, label: Root) -> dict[str, Procedure]:
    out: dict[str, Procedure] = {}
    if root is None or not root.is_dir():
        return out
    for path in sorted(root.glob("*.lua")):
        name = path.stem
        if not NAME_RE.fullmatch(name):
            continue
        try:
            hit = _lookup(root, name, label)
        except LibraryError:
            continue
        if hit is None:
            continue
        try:
            summary = parse(_read(hit[0]), name=name).summary
        except ScriptError as exc:
            summary = f"header refused: {exc.detail}"
        out[name] = Procedure(name, summary, label)
    return out


def procedures(*, cwd: Path | None) -> list[Procedure]:
    """Every runnable procedure of both roots, sorted by name, with its header summary.

    A repo file named like a shipped procedure is left out (it cannot run). A file whose
    header does not parse is listed with the refusal as its summary, so its author sees why.
    """
    shipped = _entries(shipped_root(), "shipped")
    if not repo_root_enabled():
        return sorted(shipped.values(), key=lambda p: p.name)
    repo = {n: p for n, p in _entries(_repo_root(cwd), "repo").items() if n not in shipped}
    return sorted([*shipped.values(), *repo.values()], key=lambda p: p.name)
