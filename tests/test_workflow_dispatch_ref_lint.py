# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""A dispatchable workflow must not hard-pin the branch it checks out.

``actions/checkout`` defaults to the ref that triggered the run. Setting
``with.ref`` to a bare branch literal OVERRIDES that unconditionally —
including for ``workflow_dispatch``. So a workflow that offers a
branch picker in the Actions UI, and whose checkout says ``ref: develop``,
will accept any branch you choose and then silently gate ``develop``.

The run reports a verdict. The verdict is about a tree you did not ask
about. That is worse than no verdict, because it is believed.

Found 2026-08-23 on ``local-service-gate-nightly.yml``. A fix for
``nexus-pc15o`` was pushed to a feature branch and dispatched against it
(run 32659044975); the run reported ``573 passed, 51 skipped`` —
byte-identical to the pre-fix run — and was read as the fix having
failed. It had not run at all. Corroborated in-band: the gate script
stamps ``build_ref`` from ``git rev-parse --short HEAD`` and the log
printed the PARENT commit's sha, which was ``develop``'s tip. The
workflow's own comment claimed "dispatch from a feature ref if you need
a different tree" — advice its ``ref:`` line made impossible to follow.

Two of the repo's three explicit-``ref`` checkouts were this defect, with
the same copy-pasted rationale. ``release.yml`` is the counter-example
that shows the correct shape: ``ref: ${{ inputs.tag }}``.

Why no existing lint caught it: ``tests/test_workflow_run_pipefail_lint.py``
reads the same YAML but only inspects ``run:`` bodies, and nothing else
walks ``with.ref``.

Legitimate pins exist (a release job checking out a fixed tag). They are
declared in ``_ALLOWED_LITERAL_REFS`` with a reason, so a new one is a
conscious act rather than a silent regression — the same discipline as
``LIVED_IN_EXPECTED`` in ``tests/e2e/local-service-gate.sh``.
"""
from __future__ import annotations

import pathlib
from typing import Any

import yaml

REPO_ROOT = pathlib.Path(__file__).parent.parent
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

#: Sanity floors. A sweep that inspects nothing passes everything (the
#: house vacuous-gate doctrine). Well below today's real counts — 15
#: workflow files, 11 of them dispatchable, 20+ checkout steps — so
#: ordinary edits do not trip them, but a broken glob or parser does.
_MIN_WORKFLOWS = 8
_MIN_DISPATCHABLE = 5
_MIN_CHECKOUTS = 8

#: ``(workflow filename, job id)`` pairs permitted to pin a bare literal
#: ref despite being dispatchable, each with the reason it is correct.
#: Adding an entry here is a decision; leaving one out is a defect.
_ALLOWED_LITERAL_REFS: dict[tuple[str, str], str] = {}


def _load(path: pathlib.Path) -> dict[str, Any]:
    doc = yaml.safe_load(path.read_text())
    return doc if isinstance(doc, dict) else {}


def _is_dispatchable(doc: dict[str, Any]) -> bool:
    """True when the workflow declares ``workflow_dispatch``.

    PyYAML resolves an unquoted ``on:`` key to the BOOLEAN ``True``
    (YAML 1.1 truthy), so both spellings must be probed — reading only
    ``doc["on"]`` silently sees no triggers at all on every workflow in
    this repo.
    """
    on = doc.get("on", doc.get(True))
    if isinstance(on, dict):
        return "workflow_dispatch" in on
    if isinstance(on, list):
        return "workflow_dispatch" in on
    return on == "workflow_dispatch"


def _iter_checkouts():
    """Yield ``(path, job_id, dispatchable, ref)`` per checkout step.

    ``ref`` is ``None`` when the step does not set one (the correct
    default: checkout takes the triggering ref).
    """
    for path in sorted(WORKFLOW_DIR.glob("*.yml")):
        doc = _load(path)
        dispatchable = _is_dispatchable(doc)
        for job_id, job in (doc.get("jobs") or {}).items():
            for step in (job or {}).get("steps") or []:
                if not isinstance(step, dict):
                    continue
                if "actions/checkout" not in (step.get("uses") or ""):
                    continue
                ref = (step.get("with") or {}).get("ref")
                yield path, job_id, dispatchable, ref


def _is_literal(ref: Any) -> bool:
    """True when ``ref`` is a constant no expression can influence."""
    return ref is not None and "${{" not in str(ref)


def test_the_sweep_is_not_vacuous() -> None:
    """Guards the parser: a sweep that finds nothing proves nothing."""
    rows = list(_iter_checkouts())
    workflows = {p.name for p in WORKFLOW_DIR.glob("*.yml")}
    assert len(workflows) >= _MIN_WORKFLOWS, (
        f"only {len(workflows)} workflow files found in {WORKFLOW_DIR} -- "
        "the glob is broken and this module is proving nothing"
    )
    dispatchable = {p.name for p, _, d, _ in rows if d}
    assert len(dispatchable) >= _MIN_DISPATCHABLE, (
        f"only {len(dispatchable)} dispatchable workflows detected -- the "
        "`on:`/`True:` key probe has stopped working (PyYAML resolves an "
        "unquoted `on:` to the boolean True), so the assertion below "
        "cannot fail"
    )
    assert len(rows) >= _MIN_CHECKOUTS, (
        f"only {len(rows)} actions/checkout steps found -- the step walk "
        "is broken and this module is proving nothing"
    )


def test_no_dispatchable_workflow_hard_pins_its_checkout_ref() -> None:
    offenders: list[str] = []
    for path, job_id, dispatchable, ref in _iter_checkouts():
        if not dispatchable or not _is_literal(ref):
            continue
        if (path.name, job_id) in _ALLOWED_LITERAL_REFS:
            continue
        offenders.append(f"{path.name} :: job {job_id} :: ref: {ref}")
    assert not offenders, (
        "these workflows offer a branch picker (workflow_dispatch) but "
        "hard-pin the tree they check out, so a dispatch against any "
        "other branch silently gates the pinned one and reports a "
        "verdict about the wrong tree:\n    "
        + "\n    ".join(offenders)
        + "\n  Fix: honour the dispatch ref while keeping the "
        "scheduled/push default, e.g.\n"
        "    ref: ${{ github.event_name == 'workflow_dispatch' "
        "&& github.ref_name || 'develop' }}\n"
        "  A pin that is genuinely correct (a release job checking out a "
        "fixed tag) belongs in _ALLOWED_LITERAL_REFS with its reason."
    )


def test_detector_flags_a_literal_and_clears_an_expression() -> None:
    """Falsification control for the detector itself."""
    assert _is_literal("develop")
    assert _is_literal("main")
    assert not _is_literal(None), "an absent ref is the correct default"
    assert not _is_literal("${{ inputs.tag }}")
    assert not _is_literal(
        "${{ github.event_name == 'workflow_dispatch' "
        "&& github.ref_name || 'develop' }}"
    )
    assert _is_dispatchable({True: {"workflow_dispatch": None}}), (
        "unquoted `on:` parses to the boolean True -- the probe must "
        "handle it or every workflow reads as non-dispatchable"
    )
    assert _is_dispatchable({"on": {"workflow_dispatch": None}})
    assert not _is_dispatchable({True: {"schedule": [{"cron": "0 0 * * *"}]}})
