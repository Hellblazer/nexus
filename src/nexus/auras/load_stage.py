# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The group gate and the template expansion of an aura's load (RDR-252 "Load by membership" and "Aura per base"; bead
nexus-igea9), split out of :mod:`nexus.auras.session`.

:meth:`LoadStage.gate` decides, for each aura a session has turned on, whether the session's groups let it load:

* an aura with ``[load] group = "<name>"`` loads only when the identity is a member of that group and the group is live;
* a ``{serves}`` template names no group. It loads for every live group the identity belongs to, one expansion for each
  base in that group's ``serves`` (RDR-252:70), and each expansion carries its own group's verdict. A topic is served by
  at most one live group, so the ``<aura>@<base>`` names do not collide; a base two groups both name is expanded once,
  for the first group read.

:meth:`LoadStage.expand` then replaces each loadable template by its expansions. Both touch no loader state, so they are
safe on a worker thread. Every read is injected: one membership read, then one group read for each distinct group a
gated aura names, and for each group of the identity when a template is on.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime

import structlog

from nexus.auras.aura import Aura, expand_template
from nexus.auras.load_gate import GroupReader, group_is_live
from nexus.auras.loaded_state import AuraState

_log = structlog.get_logger(__name__)

#: The ``display.threads`` value each shipped template wrote before it became a template, by aura name. A session that
#: expands the template retires the block under that heading from its own threads entry: the new heading carries the base
#: (``develop CI red (nexus-develop)``), so the old one would otherwise stay in the entry for good.
LEGACY_THREADS: dict[str, str] = {
    "ci_develop_red": "develop CI red",
    "ci_develop_nudge": "develop CI nudge",
}


@dataclasses.dataclass(frozen=True)
class Gate:
    """What the gate read: a verdict for each gated aura and template, by name, and, for the templates, the live groups
    of the identity with the bases each serves (in group-name order)."""

    verdicts: dict[str, AuraState] = dataclasses.field(default_factory=dict)
    served: tuple[tuple[str, tuple[str, ...]], ...] = ()
    #: The groups of the identity whose record could not be read, in name order. A template that still loads from another
    #: group records each one (``<aura>@group:<name>``), so the loss is visible and a reconnect is known to be needed.
    unreadable: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class Expanded:
    """:meth:`LoadStage.expand`'s result: the auras to admit, their verdicts, and the decisions for what no aura carries
    (a base of a template that was refused)."""

    auras: list[Aura]
    verdicts: dict[str, AuraState]
    extra: dict[str, AuraState]


def legacy_blocks(auras: Iterable[Aura]) -> list[tuple[str, str]]:
    """The ``(aura name, threads value)`` pairs of the pre-template blocks of the templates *auras* expanded from."""
    names = {a.expanded_from for a in auras if a.expanded_from is not None}
    return sorted((n, LEGACY_THREADS[n]) for n in names if n in LEGACY_THREADS)


class LoadStage:
    """The reads and decisions of a load that do not touch the loader's state (module docstring)."""

    def __init__(
        self, session_id: str, *, memberships: Callable[[str], Iterable[str]] | None, read_group: GroupReader | None,
        now: Callable[[], datetime],
    ) -> None:
        self.session_id = session_id
        self._memberships = memberships
        self._read_group = read_group
        self._now = now

    def gate(self, auras: Sequence[Aura], identity: str | None) -> Gate:
        """The verdict of each group-gated aura and template in *auras* (an ungated aura has no entry and reads nothing)."""
        gated = [a for a in auras if a.load_group is not None]
        templates = [a for a in auras if a.is_template]
        out: dict[str, AuraState] = {}
        if not gated and not templates:
            return Gate()

        def refuse(reason: str) -> Gate:
            for aura in (*gated, *templates):
                out[aura.name] = AuraState(False, reason, aura.load_group)
            return Gate(out)

        if identity is None:
            return refuse("unbound")
        member_of: set[str] | None = None
        if self._memberships is not None:
            try:
                member_of = set(self._memberships(identity))
            except Exception as exc:  # noqa: BLE001 -- GroupUnreadable, an engine error or a bug: loads nothing gated
                _log.warning("auras_memberships_unreadable", session_id=self.session_id, error=str(exc))
        if member_of is None:
            return refuse("memberships_unreadable")
        records: dict[str, tuple[AuraState, tuple[str, ...]]] = {}

        def record(group: str) -> tuple[AuraState, tuple[str, ...]]:
            if group not in records:
                records[group] = self._group_verdict(group)
            return records[group]

        for aura in gated:
            group = aura.load_group
            assert group is not None
            out[aura.name] = AuraState(False, "not_a_member", group) if group not in member_of else record(group)[0]
        served: list[tuple[str, tuple[str, ...]]] = []
        unreadable: list[str] = []
        if templates:
            reasons: list[str] = []
            for group in sorted(member_of):
                state, serves = record(group)
                if state.loaded and serves:
                    served.append((group, serves))
                else:
                    reasons.append(state.reason if not state.loaded else "serves_nothing")
                    if state.reason == "group_unreadable":
                        unreadable.append(group)
            if served:
                verdict = AuraState(True, "loaded", None)
            elif not member_of:
                verdict = AuraState(False, "not_a_member", None)
            else:
                verdict = AuraState(False, "group_unreadable" if "group_unreadable" in reasons else "serves_nothing", None)
            for aura in templates:
                out[aura.name] = verdict
        return Gate(out, tuple(served), tuple(unreadable))

    def _group_verdict(self, group: str) -> tuple[AuraState, tuple[str, ...]]:
        """The verdict on *group* and its ``serves`` list (empty unless the verdict is loaded)."""
        if self._read_group is None:
            return AuraState(False, "group_unreadable", group), ()
        try:
            record = self._read_group(group)
        except Exception as exc:  # noqa: BLE001 -- GroupUnreadable, an engine error or a bug
            _log.warning("auras_group_unreadable", session_id=self.session_id, group=group, error=str(exc))
            return AuraState(False, "group_unreadable", group), ()
        if record is None:
            return AuraState(False, "no_group_record", group), ()
        if not group_is_live(record, self._now()):
            return AuraState(False, "group_disbanded", group), ()
        return AuraState(True, "loaded", group), tuple(record.serves)

    def expand(self, auras: Sequence[Aura], gate: Gate) -> Expanded:
        """Replace each template the gate loads by its expansions: for each live group of the identity, one aura per base it
        serves, each with that group's verdict. A template the gate refuses stays as it is and is recorded under its own
        name; one whose bases all expand to nothing stays with the reason ``serves_nothing``. A base its expansion refuses
        (the subspace or threads rules, or a branch the procedure would refuse) is a decision of its own (``bad_base``),
        returned apart because no aura carries it."""
        out: list[Aura] = []
        verdicts = dict(gate.verdicts)
        extra: dict[str, AuraState] = {}
        for aura in auras:
            verdict = gate.verdicts.get(aura.name)
            if not aura.is_template or verdict is None or not verdict.loaded:
                out.append(aura)
                continue
            made: list[Aura] = []
            refused = 0
            seen: set[str] = set()
            for group, serves in gate.served:
                fresh = [b for b in serves if b not in seen]
                seen.update(fresh)
                expansions, rejected = expand_template(aura, fresh, group=group)
                for base, why in rejected:
                    extra[f"{aura.name}@{base}"] = AuraState(False, "bad_base", group)
                    _log.warning("aura_base_refused", aura=aura.name, group=group, base=base, reason=why)
                refused += len(rejected)
                made += expansions
            for group in gate.unreadable:
                extra[f"{aura.name}@group:{group}"] = AuraState(False, "group_unreadable", group)
            if not made:
                _log.warning("aura_serves_nothing", aura=aura.name, refused=refused)
                verdicts[aura.name] = AuraState(False, "serves_nothing", None)
                out.append(aura)
                continue
            for expansion in made:
                verdicts[expansion.name] = AuraState(True, "loaded", expansion.load_group)
                out.append(expansion)
        return Expanded(out, verdicts, extra)
