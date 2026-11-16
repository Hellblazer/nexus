# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Loading auras from the two library roots (RDR-249 Technical Design 1; bead nexus-mdbds.10.1).

The roots, the name rule, the shadow refusal and the repo-root switch are RDR-242's, in
:mod:`nexus.lua_runtime.library`; this module reaches them with the aura suffix and never
repeats their logic. A repo-root aura loads only while ``NX_PROCEDURES_REPO_ROOT=1``.
"""
from __future__ import annotations

from pathlib import Path

from typing import NamedTuple

from nexus.auras.aura import AURA_SUFFIX, Aura, AuraError, parse_aura
from nexus.lua_runtime import library
from nexus.lua_runtime.library import LibraryError, Procedure, Root

_NOUN = "aura"


def load_aura(name: str, *, cwd: Path | None, probe: library.ProbeArg = library.UNPROBED) -> Aura:
    """The aura *name*, looked up from both roots for a session in *cwd*.

    A lookup the library refuses raises :class:`~nexus.lua_runtime.library.LibraryError` (a name
    that breaks the rule, a name in neither root, a repo aura that shadows a shipped one, a repo
    aura while the repo root is off, a path outside its root, an unreadable file). A file that
    breaks the grammar raises :class:`~nexus.auras.AuraError`.
    """
    _, data = library.find_file(name, cwd=cwd, probe=probe, suffix=AURA_SUFFIX, noun=_NOUN)
    return parse_aura(data, name=name)


def _summary(name: str, data: bytes) -> str:
    try:
        return parse_aura(data, name=name).summary
    except AuraError as exc:
        return f"refused: {exc}"


def list_auras(*, cwd: Path | None) -> list[Procedure]:
    """Every aura file of both roots, sorted by name, with its summary and its root.

    A repo aura named like a shipped one is left out, and the repo root is listed only while
    its switch is on. A file that breaks the grammar is listed with ``refused: <the refusal>``
    as its summary, so its author sees why. The entries are the library's
    :class:`~nexus.lua_runtime.library.Procedure` rows (name, summary, root).
    """
    return library.list_files(cwd=cwd, suffix=AURA_SUFFIX, summarize=_summary)


class AuraEntry(NamedTuple):
    """One aura file :func:`enumerate_auras` saw: its name, its root, and what became of it.

    ``result`` is the parsed :class:`Aura`, or the refusal: an :class:`AuraError` for a file that
    breaks the grammar, a :class:`~nexus.lua_runtime.library.LibraryError` for one the library
    refuses (a name that breaks the rule, a path that leaves its root, a repo file that shadows a
    shipped aura, an unreadable file). A caller handles ``Aura | AuraError | LibraryError``;
    :class:`~nexus.lua_runtime.header.RepoProbeError` propagates when the repo root is on and
    the repo probe cannot answer.
    """

    name: str
    root: Root
    result: Aura | AuraError | LibraryError


def enumerate_auras(*, cwd: Path | None, probe: library.ProbeArg = library.UNPROBED) -> list[AuraEntry]:
    """Every aura file of both roots, sorted by name, each parsed or refused, none dropped.

    Unlike :func:`list_auras`, a repo file that shadows a shipped aura and a file whose name
    breaks the rule are entries (with their :class:`LibraryError`). The repo root is walked only
    while ``NX_PROCEDURES_REPO_ROOT=1``.
    """
    out: list[AuraEntry] = []
    for s in library.scan_files(cwd=cwd, suffix=AURA_SUFFIX, noun=_NOUN, probe=probe):
        result: Aura | AuraError | LibraryError
        if isinstance(s.found, LibraryError):
            result = s.found
        else:
            try:
                result = parse_aura(s.found, name=s.name)
            except AuraError as exc:
                result = exc
        out.append(AuraEntry(s.name, s.root, result))
    return out
