# SPDX-License-Identifier: AGPL-3.0-or-later
"""Multi-model repeatability diff for an RDR's design text (bead nexus-axwpn).

Send the same Technical Design to two models, ask each for an
implementation plan in one fixed shape, and diff the two plans
structurally: steps, files named, decisions taken. Where the plans
diverge, the design text under-specifies; a reader who happens to be a
different model reached a different implementation from the same words.
That divergence is reported as a finding on the record, never as a
verdict on either model.

One verb, one diff, one report. Nothing is written by this module; the
command prints, and a caller who wants the report on the RDR appends it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Final

#: Output contract for each dispatch: a plan as steps, each naming the
#: files it touches and the decisions it makes. Kept flat so two plans
#: can be diffed without interpreting prose.
PLAN_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "files": {"type": "array", "items": {"type": "string"}},
                    "decisions": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title", "files", "decisions"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["steps"],
    "additionalProperties": False,
}

#: Headings accepted as the design section, in preference order; the
#: earliest match of the FIRST shape that matches anywhere wins.
#: The bare ``Design`` form must be the WHOLE heading so ``Design
#: Principles`` / ``Design Decisions`` (differently scoped sections) do
#: not match; the two-word forms tolerate trailing heading text.
_DESIGN_HEADINGS: Final[tuple[str, ...]] = (
    r"Technical Design\b[^\n]*",
    r"Proposed Design\b[^\n]*",
    # Bare ``Design``, ``Design: <subtitle>`` (rdr-001, rdr-061), ``Design
    # (sketch)`` (rdr-074): anything but a following word, which is what
    # ``Design Principles`` / ``Design Decisions`` have (review [24379] M4).
    r"Design(?![ \t]+[A-Za-z])[^\n]*",
)

#: Two step titles are the same step when this share of the smaller
#: title's tokens (stopwords removed) appears in the other. Loose on
#: purpose: "Add the checker" and "Add checker module" are one step;
#: "Add the checker" and "Write the migration" are not.
STEP_MATCH_THRESHOLD: Final = 0.5

#: Decisions are short sentences whose meaning turns on one word ("fail
#: on overlap" vs "warn on overlap"), so they match only when nearly
#: identical.
DECISION_MATCH_THRESHOLD: Final = 0.75

#: Jaccard floor under the containment rule for steps: shared tokens over
#: the union. Keeps a one-word title from matching any long title that
#: contains the word.
STEP_JACCARD_FLOOR: Final = 0.2


class RepeatError(Exception):
    """The RDR text has nothing to repeat, or a model returned no plan."""


@dataclass(frozen=True)
class Step:
    title: str
    files: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()


@dataclass(frozen=True)
class Plan:
    model: str
    steps: tuple[Step, ...]

    @property
    def files(self) -> set[str]:
        return {f for s in self.steps for f in s.files}

    @property
    def decisions(self) -> set[str]:
        return {d for s in self.steps for d in s.decisions}


@dataclass
class Divergence:
    """What one plan has that the other does not."""

    model_a: str
    model_b: str
    steps_only_a: list[str] = field(default_factory=list)
    steps_only_b: list[str] = field(default_factory=list)
    files_only_a: list[str] = field(default_factory=list)
    files_only_b: list[str] = field(default_factory=list)
    decisions_only_a: list[str] = field(default_factory=list)
    decisions_only_b: list[str] = field(default_factory=list)
    matched_steps: int = 0

    @property
    def count(self) -> int:
        return (
            len(self.steps_only_a)
            + len(self.steps_only_b)
            + len(self.files_only_a)
            + len(self.files_only_b)
            + len(self.decisions_only_a)
            + len(self.decisions_only_b)
        )


def extract_design_section(text: str) -> str:
    """The RDR's design section body, or ``""`` when it has none.

    Level-agnostic (``##`` to ``####``) and ends at the next heading of
    the same or a shallower level, the same rule
    ``nexus.commands.rdr._prg_extract_approach_section`` uses for
    §Approach.
    """
    for heading in _DESIGN_HEADINGS:
        padded = "\n" + text
        m = re.search(
            r"\n(#{2,4})[ \t]+" + heading + r"\r?\n", padded, re.IGNORECASE
        )
        if not m:
            continue
        text = padded
        depth = len(m.group(1))
        start = m.end()
        nxt = re.search(r"\n#{1," + str(depth) + r"} ", text[start:])
        return (text[start : start + nxt.start()] if nxt else text[start:]).strip()
    return ""


def build_prompt(rdr_id: str, design: str) -> str:
    return f"""You are implementing a design record. You can see only the design text below.

Produce an implementation plan for it as ordered steps. For each step give a
short title, the files you would create or modify (repository-relative paths
where the text names them, otherwise your best specific guess), and the
decisions you had to make that the text did not settle. Be concrete; do not
pad. Do not restate the design.

--- {rdr_id}: design ---
{design}
"""


def parse_plan(model: str, payload: Any) -> Plan:
    if not isinstance(payload, dict) or not isinstance(payload.get("steps"), list):
        raise RepeatError(f"{model}: payload has no 'steps' list")
    steps: list[Step] = []
    for item in payload["steps"]:
        if not isinstance(item, dict):
            raise RepeatError(f"{model}: step is not an object: {item!r}")
        steps.append(
            Step(
                title=str(item.get("title", "")).strip(),
                files=tuple(str(f).strip() for f in item.get("files", []) or [] if str(f).strip()),
                decisions=tuple(
                    str(d).strip() for d in item.get("decisions", []) or [] if str(d).strip()
                ),
            )
        )
    if not steps:
        raise RepeatError(f"{model}: returned an empty plan")
    return Plan(model=model, steps=tuple(steps))


_TOKEN_RE = re.compile(r"[a-z0-9_]+")

#: Verbs and glue that carry no identity: "Implement migration ETL" and
#: "Migration ETL: rehash / remap" are the same step. Measured on the
#: first live run (RDR-180, 2026-09-04): plain Jaccard over raw tokens
#: matched 0 of 9 vs 5 steps that a reader pairs at a glance.
_STOPWORDS: Final[frozenset[str]] = frozenset(
    "a an and the of to for in on with via from into by or as is be add create "
    "implement write update define build set up new".split()
)


def _tokens(s: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall(s.lower()) if t not in _STOPWORDS and len(t) > 1}


def _same_step(a: str, b: str) -> bool:
    """Containment over the SMALLER token set, so a terse title matches a
    verbose one that contains it, AND a floor on Jaccard so a one-word
    title cannot match every sentence that happens to contain the word
    (review [24379] M2: "Rehash" matched a twenty-word title)."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return a.strip().lower() == b.strip().lower()
    shared = len(ta & tb)
    return (
        shared / min(len(ta), len(tb)) >= STEP_MATCH_THRESHOLD
        and shared / len(ta | tb) >= STEP_JACCARD_FLOOR
    )


def _same_decision(a: str, b: str) -> bool:
    """Symmetric (Jaccard), not containment: a decision is a sentence whose
    meaning turns on one word, and containment called "warn on overlap"
    the same as "reject warn on overlap, always fail" (review [24379] M1)."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return a.strip().lower() == b.strip().lower()
    return len(ta & tb) / len(ta | tb) >= DECISION_MATCH_THRESHOLD


def _normalise_path(p: str) -> str:
    return p.strip().lstrip("./").rstrip("/")


def _same_file(a: str, b: str) -> bool:
    """Equal, or one is a suffix of the other at a ``/`` boundary with at
    least two segments in common (``src/x/config.py`` vs
    ``service/src/x/config.py``, not ``a/config.py`` vs ``b/config.py``)."""
    if a == b:
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return short.count("/") >= 1 and long_.endswith("/" + short)


def diff_plans(a: Plan, b: Plan) -> Divergence:
    """Structural difference between two plans for the same design."""
    d = Divergence(model_a=a.model, model_b=b.model)
    unmatched_b = list(b.steps)
    for sa in a.steps:
        hit = next((sb for sb in unmatched_b if _same_step(sa.title, sb.title)), None)
        if hit is None:
            d.steps_only_a.append(sa.title)
        else:
            unmatched_b.remove(hit)
            d.matched_steps += 1
    d.steps_only_b = [s.title for s in unmatched_b]

    # Files match on the normalised path, or when one path is a
    # segment-boundary suffix of the other (one plan wrote service/src/...,
    # the other src/...; first live run). A bare basename is not enough:
    # two config.py files in different packages are two files (review
    # [24379] M3).
    fa = {_normalise_path(f) for f in a.files}
    fb = {_normalise_path(f) for f in b.files}
    d.files_only_a = sorted(f for f in fa - fb if not any(_same_file(f, g) for g in fb))
    d.files_only_b = sorted(f for f in fb - fa if not any(_same_file(f, g) for g in fa))

    da, db = a.decisions, b.decisions
    d.decisions_only_a = sorted(x for x in da if not any(_same_decision(x, y) for y in db))
    d.decisions_only_b = sorted(y for y in db if not any(_same_decision(y, x) for x in da))
    return d


def render_report(rdr_id: str, a: Plan, b: Plan, d: Divergence) -> str:
    lines = [
        f"Repeatability diff: {rdr_id}",
        f"Models: {a.model} ({len(a.steps)} steps) vs {b.model} ({len(b.steps)} steps); "
        f"{d.matched_steps} step(s) matched, {d.count} divergence(s).",
        "",
    ]
    if d.count == 0:
        lines.append("No divergences. Both plans name the same steps, files and decisions.")
        return "\n".join(lines)

    def block(title: str, items: list[str]) -> None:
        if items:
            lines.append(title)
            lines.extend(f"  - {i}" for i in items)
            lines.append("")

    block(f"Steps only {a.model} planned:", d.steps_only_a)
    block(f"Steps only {b.model} planned:", d.steps_only_b)
    block(f"Files only {a.model} would touch:", d.files_only_a)
    block(f"Files only {b.model} would touch:", d.files_only_b)
    block(f"Decisions only {a.model} had to make:", d.decisions_only_a)
    block(f"Decisions only {b.model} had to make:", d.decisions_only_b)
    lines.append(
        "Each divergence is a place the design text left open; two readers "
        "settled it differently. Treat it as spec ambiguity, not a model verdict."
    )
    return "\n".join(lines).rstrip()
