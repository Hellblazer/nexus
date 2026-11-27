# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The behaviour leg's settings fixtures and its Provenance label check (RDR-259 Decisions 2 and 11; nexus-sby49.11).

Split out of ``plugin_behaviour.py`` (over its size cap) so the area this bead touches lives in a module of its own.

Every text the plugin prints from a settings file is introduced by its source, never in the plugin's own voice (Decision 2).
The leg drives every hook under the three postures and asks here, of what each probe printed:

* each settings text appears only after its label: the value never occurs outside the label that carries it, and a
  ``brief_questions`` entry follows the list label of its own file;
* a project text never carries the user label and the reverse, and a plain fixture carries neither;
* at least ``LABEL_FLOOR`` labelled texts were seen across the two strict fixtures, and every text the fixtures set was seen,
  so the check cannot pass on nothing.

The wording below is the RDR's Provenance table, written out again so the check is an oracle independent of the helper the
hooks call. The strict fixtures set ``preflight``, ``ci_wait``, ``credential_helper`` and ``brief_questions`` (project) and
one ``brief_questions`` entry (user). The SubagentStart payloads the leg drives are two shapes, named here: a worktree
dispatch (the agent's cwd is a linked worktree of the project) and an implementer-type payload (``conexus:developer``, which
gets the brief check).
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any, Callable

#: What the project-strict fixture's tracked ``.nexus/project.json`` carries: every project key.
PROJECT_SETTINGS: dict[str, Any] = {
    "version": 1,
    "subagent_git": "strict",
    "preflight": "tools/leg-preflight.sh",
    "ci_wait": "leg-ci-wait <sha>",
    "credential_helper": "The project's tools/leg-credentials.sh checks the token and runs a command with it set in the environment.",
    "brief_questions": ["Does it hold on every platform the project supports?"],
    "advice_block": ["whole_disk_scan"],
}
#: What the user-strict fixture's account ``conexus.json`` carries: every user key but telemetry (it needs an engine).
USER_SETTINGS: dict[str, Any] = {
    "version": 1,
    "subagent_git": "strict",
    "t1_nudge": True,
    "advice_block": ["whole_disk_scan"],
    "brief_questions": ["Is the rollback tested in every environment we run?"],
}

#: The two SubagentStart payload shapes the leg drives (it fails when either was not driven and labelled).
SHAPE_WORKTREE = "worktree dispatch"
SHAPE_IMPLEMENTER = "implementer type"
#: The fewest labelled texts the two strict fixtures must show (RDR-259 Decision 2).
LABEL_FLOOR = 3

PROJECT_PREFIX = "This project's `.nexus/project.json`"
USER_PREFIX = "Your `conexus.json`"
_LABELS = {
    ("preflight", "project"): PROJECT_PREFIX + " asks: run `{v}` first. If it exits non-zero, stop and report its output.",
    ("ci_wait", "project"): PROJECT_PREFIX + " names its CI wait command: `{v}`. Run it as a background command; its exit is the wake.",
    ("credential_helper", "project"): PROJECT_PREFIX + " adds (project text, not this guard's): {v}",
    ("brief_questions", "project"): PROJECT_PREFIX + " adds these questions:",
    ("brief_questions", "user"): USER_PREFIX + " adds these questions:",
}


def _norm(text: str) -> str:
    return " ".join(text.split())


@dataclasses.dataclass(frozen=True)
class Labelled:
    """A settings text a strict fixture sets: its fixture, key, source, the label that must introduce it and its value(s)."""

    fixture: str
    key: str
    source: str
    label: str
    values: tuple[str, ...]
    inline: bool            # the value is part of the label (a scalar); else the label heads a list the values follow


def expected_texts() -> list[Labelled]:
    out: list[Labelled] = []
    for key in ("preflight", "ci_wait", "credential_helper"):
        value = str(PROJECT_SETTINGS[key])
        out.append(Labelled("project-strict", key, "project", _norm(_LABELS[(key, "project")].format(v=value)), (_norm(value),), True))
    for fixture, source, settings in (("project-strict", "project", PROJECT_SETTINGS), ("user-strict", "user", USER_SETTINGS)):
        out.append(Labelled(fixture, "brief_questions", source, _norm(_LABELS[("brief_questions", source)]),
                            tuple(_norm(q) for q in settings["brief_questions"]), False))
    return out


def subagent_start_scenarios(fixture: Any, common: Callable[..., dict]) -> list[dict]:
    """The two named payload shapes, for one fixture. *common* builds the shared payload fields (``plugin_behaviour._common``)."""
    shapes: list[dict] = []
    worktree = getattr(fixture, "worktree", None)
    project = fixture.name == "project-strict"
    if worktree is not None:
        # A worktree dispatch: the agent's cwd is a linked worktree of the project, which carries the tracked settings file.
        payload = common(fixture, "SubagentStart", agent_id=f"aleg-{fixture.name}-wt", agent_type="conexus:developer", prompt_id="p3")
        payload["cwd"] = str(worktree)
        must = ["| Preflight |", PROJECT_PREFIX] if project else []
        forbid = [] if project else ["| Preflight |", PROJECT_PREFIX]
        shapes.append({"name": SHAPE_WORKTREE, "payload": payload, "expect": None, "must": tuple(must), "forbid": tuple(forbid)})
    # An implementer-type payload: the agent type that gets the brief check, whose questions follow their labels.
    payload = common(fixture, "SubagentStart", agent_id=f"aleg-{fixture.name}-impl", agent_type="conexus:developer", prompt_id="p4")
    must = ["## Brief check"]
    forbid: list[str] = []
    if project:
        must.append(_LABELS[("brief_questions", "project")])
    elif fixture.name == "user-strict":
        must.append(_LABELS[("brief_questions", "user")])
    else:
        forbid += [PROJECT_PREFIX, USER_PREFIX]
    shapes.append({"name": SHAPE_IMPLEMENTER, "payload": payload, "expect": None, "must": tuple(must), "forbid": tuple(forbid)})
    return shapes


def label_problems(outcomes: list[Any]) -> tuple[list[str], int]:
    """``(problems, labelled texts seen)`` over every P2 outcome: the checks in this module's docstring."""
    problems: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    wanted = expected_texts()
    for o in outcomes:
        p = o.probe
        if p.posture != "P2" or o.rc != 0:
            continue
        text = _norm(o.text)
        for forbidden, who in ((USER_PREFIX, "user"), (PROJECT_PREFIX, "project")):
            sources = {"project-strict": "project", "user-strict": "user"}.get(p.fixture)
            if forbidden in text and sources != who:
                problems.append(f"{p.id}: the output carries the {who} label in a fixture that has no {who} text")
        for w in (w for w in wanted if w.fixture == p.fixture):
            if w.inline:
                value, label = w.values[0], w.label
                outside = text.count(value) - text.count(label)
                if outside > 0:
                    problems.append(f"{p.id}: the {w.key} text appears {outside} time(s) outside its label")
                if label in text:
                    seen.add((w.fixture, w.key, w.source))
                continue
            for q in w.values:
                if q not in text:
                    continue
                at = text.find(w.label)
                if at < 0 or text.find(q) < at:
                    problems.append(f"{p.id}: a {w.source} brief question appears without its label before it")
                else:
                    seen.add((w.fixture, w.key, w.source))
    for w in wanted:
        if (w.fixture, w.key, w.source) not in seen:
            problems.append(f"{w.fixture}: the {w.source} {w.key} text was never seen after its label, so it was not checked")
    if len(seen) < LABEL_FLOOR:
        problems.append(f"only {len(seen)} labelled text(s) were seen across the two strict fixtures; the floor is {LABEL_FLOOR}, "
                        "so this check would pass on nothing")
    return problems, len(seen)


def shape_problems(outcomes: list[Any]) -> list[str]:
    """The leg drives both SubagentStart shapes in each strict fixture, and each exited cleanly with output."""
    out: list[str] = []
    for fixture in ("project-strict", "user-strict"):
        for shape in (SHAPE_WORKTREE, SHAPE_IMPLEMENTER):
            hit = [o for o in outcomes if o.probe.posture == "P2" and o.probe.fixture == fixture and o.probe.id.endswith("/" + shape)]
            if not hit:
                out.append(f"{fixture}: the {shape!r} SubagentStart payload was not driven")
            elif not any(o.rc == 0 and o.text.strip() for o in hit):
                out.append(f"{fixture}: the {shape!r} SubagentStart payload printed nothing")
    return out


def worktree_path(work: Path, name: str) -> Path:
    return work / f"worktree-{name}"
