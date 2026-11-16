# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The checks of ``nx aura check`` (RDR-249 Technical Design 2; bead nexus-mdbds.10.2).

The parser (:mod:`nexus.auras.aura`) refuses what a file can say wrongly by itself. These checks
need more than the file:

* ``procedure``: the procedure resolves through RDR-240's library and its header parses;
* ``arguments``: every required argument of the header is mapped, and no mapping names an
  argument the header does not declare (the reserved ``now`` and ``repo`` are the host's);
* ``clear``: a ``[clear]`` that a post could meet together with ``trigger.match`` is dead or
  ambiguous and is refused (below);
* ``threads``: two auras that write one ``display.threads`` block would overwrite each other;
* ``dictionary`` (needs the engine's registry): the trigger subspace resolves to a template, and
  every field of ``trigger.match``, ``clear.match`` and each ``dims.<f>`` source is a dimension
  of it; a ``body.<f>`` source is checked against the template's ``doc.body`` where it serves one.

A check that cannot run is :class:`Skipped` by name, with its reason, and never counts as passed.

The ``[clear]`` rule. A post meets a match when it carries every pair. A post meets both
``trigger.match`` and ``clear.match`` unless some key is in both with different values, so the
file is refused when trigger.match is absent (every post on the subspace is a trigger) or when no
key is in both tables with different values. ``ci_develop_red`` is clean: its two tables differ
on ``outcome``.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nexus.auras.aura import Aura, AuraError
from nexus.auras.loader import AuraEntry
from nexus.lua_runtime import library
from nexus.lua_runtime.errors import ScriptError
from nexus.lua_runtime.header import RESERVED, Header
from nexus.lua_runtime.header import parse as parse_header
from nexus.lua_runtime.library import LibraryError
from nexus.tuple_templates import resolve_tuple_template

#: The text a skipped dictionary check carries when the caller gives no reason.
NO_REGISTRY: str = "the registry is unavailable"


@dataclass(frozen=True)
class Finding:
    """A refusal: the check that made it, the dotted key at fault, and why."""

    check: str
    key: str
    detail: str


@dataclass(frozen=True)
class Skipped:
    """A check that did not run, by name, with the reason. Never a pass."""

    check: str
    reason: str


@dataclass(frozen=True)
class AuraCheck:
    """The outcome for one aura file: what was refused and what was skipped."""

    name: str
    root: str
    findings: tuple[Finding, ...]
    skipped: tuple[Skipped, ...]

    @property
    def ok(self) -> bool:
        """Nothing was refused. A skipped check is not a refusal and is not a pass either:
        read :attr:`skipped`."""
        return not self.findings


def check_arguments(aura: Aura, header: Header) -> list[Finding]:
    """The argument mapping against *header*: one finding per required argument left unmapped and
    per mapping of an argument the header does not declare."""
    declared = {a.name: a for a in header.args}
    mapped = {name for name, _ in aura.args}
    out = [
        Finding("arguments", f"args.{a.name}", f"the procedure requires {a.name!r}; map it")
        for a in header.args
        if not a.optional and a.name not in RESERVED and a.name not in mapped
    ]
    out += [
        Finding("arguments", f"args.{name}", f"the procedure {aura.procedure!r} declares no argument {name!r}")
        for name, _ in aura.args
        if name not in declared
    ]
    return out


def check_clear(aura: Aura) -> list[Finding]:
    """A ``[clear]`` a post could meet together with ``trigger.match`` is refused (module docstring)."""
    if aura.clear_match is None:
        return []
    clear = dict(aura.clear_match)
    trigger = dict(aura.match)
    if not trigger:
        return [Finding("clear", "clear.match", "trigger.match is absent, so every post is a trigger and none could clear")]
    if not any(k in clear and clear[k] != v for k, v in trigger.items()):
        return [
            Finding(
                "clear", "clear.match",
                "a post could meet both trigger.match and clear.match: no key is in both with different values",
            )
        ]
    return []


def _dimension_findings(aura: Aura, dims: set[str]) -> list[Finding]:
    out = [
        Finding("dictionary", f"trigger.match.{k}", f"{k!r} is not a dimension of the subspace's template")
        for k, _ in aura.match
        if k not in dims
    ]
    out += [
        Finding("dictionary", f"clear.match.{k}", f"{k!r} is not a dimension of the subspace's template")
        for k, _ in aura.clear_match or ()
        if k not in dims
    ]
    out += [
        Finding("dictionary", f"args.{arg}", f"dims.{src.value} is not a dimension of the subspace's template")
        for arg, src in aura.args
        if src.kind == "dims" and src.value not in dims
    ]
    return out


def check_dictionary(aura: Aura, templates: Sequence[dict[str, Any]]) -> tuple[list[Finding], list[Skipped]]:
    """The trigger's fields against the registry's *templates* (``registry(doc=True)['templates']``)."""
    template = resolve_tuple_template(list(templates), aura.subspace)
    if template is None:
        return [Finding("dictionary", "trigger.subspace", f"{aura.subspace!r} matches no template in the registry")], []
    dims = set((template.get("dimensions") or {}).keys())
    findings = _dimension_findings(aura, dims)
    skipped: list[Skipped] = []
    body_args = [(arg, src) for arg, src in aura.args if src.kind == "body"]
    if body_args:
        body = (template.get("doc") or {}).get("body")
        if not isinstance(body, dict) or not body:
            skipped.append(
                Skipped(
                    "dictionary.body",
                    f"template {template.get('name')!r} serves no doc.body, so body.<field> sources are unchecked",
                )
            )
        else:
            findings += [
                Finding("dictionary", f"args.{arg}", f"body.{src.value} is not a field of the template's doc.body")
                for arg, src in body_args
                if src.value not in body
            ]
    return findings, skipped


def _check_procedure(
    aura: Aura, cwd: Path | None, probe: library.ProbeArg
) -> tuple[list[Finding], Header | None]:
    try:
        _, data = library.find_file(aura.procedure, cwd=cwd, probe=probe)
        return [], parse_header(data, name=aura.procedure)
    except LibraryError as exc:
        return [Finding("procedure", "procedure", f"{exc.reason}: {exc.detail}")], None
    except ScriptError as exc:
        return [Finding("procedure", "procedure", f"the header is refused: {exc.detail}")], None


def _threads_findings(entries: Iterable[AuraEntry]) -> dict[str, Finding]:
    """For each aura that shares its ``display.threads`` value, a finding naming the others."""
    by_threads: dict[str, list[str]] = defaultdict(list)
    for e in entries:
        if isinstance(e.result, Aura):
            by_threads[e.result.threads].append(e.name)
    out: dict[str, Finding] = {}
    for threads, names in by_threads.items():
        if len(names) > 1:
            for name in names:
                others = ", ".join(sorted(n for n in names if n != name))
                out[name] = Finding(
                    "threads", "display.threads",
                    f"{threads!r} is also written by {others}; one would overwrite the other's block",
                )
    return out


def check_auras(
    entries: Sequence[AuraEntry],
    *,
    cwd: Path | None,
    templates: Sequence[dict[str, Any]] | None,
    unavailable: str = NO_REGISTRY,
    only: str | None = None,
    probe: library.ProbeArg = library.UNPROBED,
) -> list[AuraCheck]:
    """Check *entries* (from :func:`~nexus.auras.enumerate_auras`), or only the one named *only*.

    *templates* is the registry's template list, or ``None`` when it could not be read, with
    *unavailable* saying why: the dictionary checks are then skipped by name and the rest run.
    An empty list is skipped too: a registry that lists no template cannot vouch for a field.
    The ``threads`` check always looks across every entry, so a filtered run still sees the
    aura that shares its block. A :class:`~nexus.lua_runtime.header.RepoProbeError` from the
    library propagates.
    """
    if templates is not None and not templates:
        templates, unavailable = None, "the registry lists no templates"
    threads = _threads_findings(entries)
    out: list[AuraCheck] = []
    for e in entries:
        if only is not None and e.name != only:
            continue
        findings: list[Finding] = []
        skipped: list[Skipped] = []
        result = e.result
        if isinstance(result, LibraryError):
            findings.append(Finding("library", "(file)", f"{result.reason}: {result.detail}"))
        elif isinstance(result, AuraError):
            findings.append(Finding("parse", result.key, result.detail))
        else:
            proc_findings, header = _check_procedure(result, cwd, probe)
            findings += proc_findings
            if header is not None:
                findings += check_arguments(result, header)
            findings += check_clear(result)
            if e.name in threads:
                findings.append(threads[e.name])
            if templates is None:
                skipped.append(Skipped("dictionary", unavailable))
            else:
                dict_findings, dict_skipped = check_dictionary(result, templates)
                findings += dict_findings
                skipped += dict_skipped
        out.append(AuraCheck(e.name, e.root, tuple(findings), tuple(skipped)))
    return out
