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

* ``serving_group`` (a warning, RDR-252; needs a group reader): an aura triggered on a ``board/ci/<base>--owner``
  subspace should be loaded only in the group that serves ``<base>``, so a missing ``[load]`` table, a ``[load]``
  group with no record, a disbanded one, or one whose ``serves`` lacks ``<base>`` is warned about. A warning does
  not refuse the file. Any other aura is not checked here.

* ``serving_group`` for a ``{serves}`` template (RDR-252 "Aura per base"): the template names no group, so it is checked
  against the groups the session belongs to (``member_groups``). A session in no group, or in groups that are not live
  or serve nothing, leaves the template expanding to no aura; that is a :class:`CheckWarning`, as it is for a concrete
  aura, and never a refusal, so the shipped auras do not turn ``nx aura check`` red in a session outside any serving
  group. A base whose expansion is rejected (its subspace, threads value or branch breaks a rule) is a
  :class:`Finding` naming it. The dictionary check then runs on each expansion and names the base in its findings. With
  no way to read the groups the serving check is :class:`Skipped` and the dictionary check reads one expansion for a
  sample base.

A check that cannot run is :class:`Skipped` by name, with its reason, and never counts as passed.

The ``[clear]`` rule. A post meets a match when it carries every pair. A post meets both
``trigger.match`` and ``clear.match`` unless some key is in both with different values, so the
file is refused when trigger.match is absent (every post on the subspace is a trigger) or when no
key is in both tables with different values. ``ci_develop_red`` is clean: its two tables differ
on ``outcome``.
"""
from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nexus.auras.aura import Aura, AuraError, expand_template
from nexus.auras.load_gate import GroupReader, GroupUnreadable, group_is_live
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
class CheckWarning:
    """A finding that does not refuse the file: the check that made it, the dotted key at fault, and why."""

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
    warnings: tuple[CheckWarning, ...] = ()

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
    out += [
        Finding("arguments", f"args.{name}", f"self.{src.value} fills a string, and {name!r} is declared {declared[name].type}")
        for name, src in aura.args
        if src.kind == "self" and name in declared and declared[name].type != "string"
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


#: ``board/ci/<base>--owner``: the subspace the stamper posts owner rows on (RDR-252).
_OWNER_RE = re.compile(r"board/ci/(?P<base>.+)--owner")


def check_serving_group(
    aura: Aura, read_group: GroupReader | None, *, now: datetime | None = None
) -> tuple[list[CheckWarning], list[Skipped]]:
    """The serving-group warning for an aura triggered on ``board/ci/<base>--owner`` (module docstring).

    No other aura is checked and no read is made for it. With no reader, or a read that raises
    :class:`~nexus.auras.load_gate.GroupUnreadable`, the check is :class:`Skipped` by name and raises no warning.
    An aura with no ``[load]`` group needs no read: it loads in every session, which is the warning."""
    if aura.is_template:  # check_template_serving reads its group: a template is refused, not warned about
        return [], []
    m = _OWNER_RE.fullmatch(aura.subspace)
    if m is None:
        return [], []
    base = m["base"]
    if aura.load_group is None:
        return [
            CheckWarning(
                "serving_group", "load",
                f"{aura.subspace} carries owner rows for {base!r} and the aura has no [load] group, so it loads in every session",
            )
        ], []
    if read_group is None:
        return [], [Skipped("serving_group", "no group reader is available, so the serving group is unchecked")]
    try:
        record = read_group(aura.load_group)
    except GroupUnreadable as exc:
        return [], [Skipped("serving_group", f"the group {aura.load_group!r} could not be read: {exc}")]
    moment = now if now is not None else datetime.now(timezone.utc)
    key = "load.group"
    if record is None:
        return [CheckWarning("serving_group", key, f"the group {aura.load_group!r} has no record, so it serves nothing")], []
    if not group_is_live(record, moment):
        return [CheckWarning("serving_group", key, f"the group {aura.load_group!r} is disbanded, so it serves nothing")], []
    if base not in record.serves:
        return [
            CheckWarning(
                "serving_group", key,
                f"the group {aura.load_group!r} does not serve {base!r} (it serves {sorted(record.serves)!r}), "
                f"so this aura would load in a group that never sees its rows",
            )
        ], []
    return [], []


#: The base a template is expanded for when its group cannot be read, so the dictionary check still has a subspace.
_SAMPLE_BASE = "x-y"


def check_template_serving(
    aura: Aura, read_group: GroupReader | None, member_groups: Callable[[], Iterable[str]] | None, *,
    now: datetime | None = None,
) -> tuple[list[Finding], list[CheckWarning], list[Skipped], list[Aura] | None]:
    """The serving check of a ``{serves}`` template: findings, warnings, skips and its expansions (``None`` when the groups
    were not read, so the caller cannot name a base).

    *member_groups* returns the names of the groups the session belongs to (raising
    :class:`~nexus.auras.load_gate.GroupUnreadable` when it cannot), *read_group* one group's record. With either missing,
    or a read that raises, the check is :class:`Skipped` by name. Warned: the session is in no group, or no group it is in
    is live and serves a base. Refused: each base whose expansion is rejected."""
    key = "load.group"
    if read_group is None or member_groups is None:
        return [], [], [Skipped("serving_group", "the session's groups cannot be read here, so the bases the template expands to are unchecked")], None
    try:
        names = sorted(set(member_groups()))
    except GroupUnreadable as exc:
        return [], [], [Skipped("serving_group", f"the session's groups could not be read: {exc}")], None
    moment = now if now is not None else datetime.now(timezone.utc)
    nothing = "so the template expands to no aura"
    if not names:
        return [], [CheckWarning("serving_group", key, f"the session belongs to no group, {nothing}")], [], []
    findings: list[Finding] = []
    why_not: list[str] = []
    expansions: list[Aura] = []
    unreadable: list[str] = []
    seen: set[str] = set()
    for group in names:
        try:
            record = read_group(group)
        except GroupUnreadable as exc:
            unreadable.append(f"{group!r} ({exc})")
            continue
        if record is None:
            why_not.append(f"the group {group!r} has no record")
        elif not group_is_live(record, moment):
            why_not.append(f"the group {group!r} is disbanded")
        elif not record.serves:
            why_not.append(f"the group {group!r} serves nothing")
        else:
            fresh = [b for b in record.serves if b not in seen]
            seen.update(fresh)
            made, rejected = expand_template(aura, fresh, group=group)
            expansions += made
            findings += [
                Finding("serving_group", "trigger.subspace", f"the template does not expand for the base {base!r} of group {group!r}: {why}")
                for base, why in rejected
            ]
    skipped = [Skipped("serving_group", f"a group of the session could not be read: {', '.join(unreadable)}")] if unreadable else []
    warnings: list[CheckWarning] = []
    if not expansions and not findings and not unreadable:
        warnings.append(CheckWarning("serving_group", key, f"{'; '.join(why_not)}, {nothing}"))
    return findings, warnings, skipped, expansions if (expansions or not unreadable) else None


def _check_template_dictionary(
    aura: Aura, expansions: list[Aura] | None, templates: Sequence[dict[str, Any]]
) -> tuple[list[Finding], list[Skipped]]:
    """The dictionary check of a template: once for each expansion, its findings naming the base; or once for a sample
    expansion when the group's bases are unknown. A sample the expansion rules refuse is a finding on the threads value."""
    if not expansions:
        sample, rejected = expand_template(aura, (_SAMPLE_BASE,))
        if not sample:
            why = rejected[0][1] if rejected else "no base expands"
            return [Finding("threads", "display.threads", f"the template does not expand even for the sample base {_SAMPLE_BASE!r}: {why}")], []
        return check_dictionary(sample[0], templates)
    findings: list[Finding] = []
    skipped: dict[tuple[str, str], Skipped] = {}
    for one in expansions:
        f, sk = check_dictionary(one, templates)
        findings += [Finding(x.check, x.key, f"[{one.base}] {x.detail}") for x in f]
        skipped.update({(x.check, x.reason): x for x in sk})
    return findings, list(skipped.values())


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
    read_group: GroupReader | None = None,
    member_groups: Callable[[], Iterable[str]] | None = None,
    now: datetime | None = None,
) -> list[AuraCheck]:
    """Check *entries* (from :func:`~nexus.auras.enumerate_auras`), or only the one named *only*.

    *templates* is the registry's template list, or ``None`` when it could not be read, with
    *unavailable* saying why: the dictionary checks are then skipped by name and the rest run.
    An empty list is skipped too: a registry that lists no template cannot vouch for a field.
    The ``threads`` check always looks across every entry, so a filtered run still sees the
    aura that shares its block. A :class:`~nexus.lua_runtime.header.RepoProbeError` from the
    library propagates. *read_group* reads a group's record for the serving-group warning (:func:`check_serving_group`);
    *member_groups* names the groups the session belongs to, for a ``{serves}`` template (:func:`check_template_serving`); with
    ``None`` either, that check is skipped for an aura that needs it.
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
        warnings: list[CheckWarning] = []
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
            expansions: list[Aura] | None = None
            if result.is_template:
                serving_findings, serving_warnings, serving_skipped, expansions = check_template_serving(
                    result, read_group, member_groups, now=now,
                )
                findings += serving_findings
                warnings += serving_warnings
            else:
                serving_warnings, serving_skipped = check_serving_group(result, read_group, now=now)
                warnings += serving_warnings
            skipped += serving_skipped
            if e.name in threads:
                findings.append(threads[e.name])
            if templates is None:
                skipped.append(Skipped("dictionary", unavailable))
            else:
                dict_findings, dict_skipped = (
                    _check_template_dictionary(result, expansions, templates) if result.is_template
                    else check_dictionary(result, templates)
                )
                findings += dict_findings
                skipped += dict_skipped
        out.append(AuraCheck(e.name, e.root, tuple(findings), tuple(skipped), tuple(warnings)))
    return out
