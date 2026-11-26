# SPDX-License-Identifier: AGPL-3.0-or-later
"""The behaviour leg's Provenance label check (RDR-259 Decisions 2 and 11; nexus-sby49.11), on synthetic output.

``tests/e2e/lib/plugin_behaviour_labels.py`` judges what each probe printed: a settings text appears only after its label, a
project text never carries the user label and the reverse, at least three labelled texts are seen across the two strict
fixtures, and both SubagentStart payload shapes (a worktree dispatch and an implementer type) are driven. This file proves the
check fails in each direction and passes on the correct text; ``test_plugin_behaviour_leg.py`` runs it for real.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parent / "e2e" / "lib"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, LIB / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(LIB))
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(LIB))
    return mod


labels = _load("plugin_behaviour_labels")
leg = _load("plugin_behaviour")


def _outcome(fixture: str, text: str, shape: str = "default", posture: str = "P2", rc: int = 0):
    probe = leg.Probe(f"{posture}/{fixture}/SubagentStart/{shape}", "k", fixture, posture, "module", [], None, None)
    return leg.Outcome(probe, rc, text, "", "advice", text)


def _label(key: str, source: str, value: str = "") -> str:
    return labels._LABELS[(key, source)].format(v=value)


PROJECT = labels.PROJECT_SETTINGS
USER = labels.USER_SETTINGS


def _correct() -> list:
    preflight = _label("preflight", "project", PROJECT["preflight"])
    project_q = _label("brief_questions", "project") + "\n1. " + PROJECT["brief_questions"][0]
    user_q = _label("brief_questions", "user") + "\n1. " + USER["brief_questions"][0]
    return [
        _outcome("project-strict", "| Preflight | " + preflight + " |\n" + project_q, labels.SHAPE_WORKTREE),
        _outcome("project-strict", "x " + _label("ci_wait", "project", PROJECT["ci_wait"]), "ci"),
        _outcome("project-strict", "refusal\n" + _label("credential_helper", "project", PROJECT["credential_helper"]), "cred"),
        _outcome("project-strict", "## Brief check\n" + project_q, labels.SHAPE_IMPLEMENTER),
        _outcome("user-strict", "## Brief check\n" + user_q, labels.SHAPE_IMPLEMENTER),
        _outcome("user-strict", "| Search scope |", labels.SHAPE_WORKTREE),
        _outcome("plain", "| Search scope |", labels.SHAPE_WORKTREE),
    ]


def test_correct_labelled_output_passes_and_counts_every_text() -> None:
    problems, seen = labels.label_problems(_correct())
    assert problems == [] and seen == 5, (problems, seen)
    assert labels.shape_problems(_correct()) == []


def test_a_value_outside_its_label_fails() -> None:
    bare = _outcome("project-strict", "run tools/leg-preflight.sh first\n" + _label("preflight", "project", PROJECT["preflight"]))
    problems, _ = labels.label_problems([*_correct(), bare])
    assert any("preflight text appears 1 time(s) outside its label" in p for p in problems), problems


@pytest.mark.parametrize("key,source", [("preflight", "project"), ("ci_wait", "project"), ("credential_helper", "project")])
def test_a_scalar_text_printed_in_the_plugins_own_voice_fails(key: str, source: str) -> None:
    others = [o for o in _correct() if PROJECT[key] not in o.text]
    bare = _outcome("project-strict", "the plugin says: " + PROJECT[key])
    problems, _ = labels.label_problems([*others, bare])
    assert any(f"the {key} text appears" in p for p in problems), problems
    assert any(f"{key} text was never seen after its label" in p for p in problems), "and the labelled sighting is missing"


def test_a_question_before_its_label_or_with_none_fails() -> None:
    no_label = _outcome("user-strict", "1. " + USER["brief_questions"][0])
    problems, _ = labels.label_problems([*_correct(), no_label])
    assert any("a user brief question appears without its label" in p for p in problems), problems
    wrong_order = _outcome("project-strict", "1. " + PROJECT["brief_questions"][0] + "\n" + _label("brief_questions", "project"))
    problems, _ = labels.label_problems([*_correct(), wrong_order])
    assert any("a project brief question appears without its label" in p for p in problems), problems


def test_a_project_text_never_carries_the_user_label_and_the_reverse() -> None:
    cross = _outcome("project-strict", _label("brief_questions", "user"))
    assert any("carries the user label" in p for p in labels.label_problems([*_correct(), cross])[0])
    cross = _outcome("user-strict", _label("brief_questions", "project"))
    assert any("carries the project label" in p for p in labels.label_problems([*_correct(), cross])[0])
    plain = _outcome("plain", _label("brief_questions", "user"))
    assert any("a fixture that has no user text" in p for p in labels.label_problems([*_correct(), plain])[0])


def test_fewer_than_three_labelled_texts_fails_so_the_check_cannot_pass_on_nothing() -> None:
    problems, seen = labels.label_problems([_outcome("plain", "nothing")])
    assert seen == 0
    assert any("the floor is 3" in p for p in problems), problems
    two = [_correct()[1], _correct()[2]]  # ci_wait and credential_helper only
    problems, seen = labels.label_problems(two)
    assert seen < labels.LABEL_FLOOR and any("the floor is 3" in p for p in problems), (problems, seen)


def test_a_p0_outcome_is_not_judged() -> None:
    problems, seen = labels.label_problems([_outcome("project-strict", PROJECT["preflight"], posture="P0")])
    assert seen == 0 and not any("outside its label" in p for p in problems)


def test_a_shape_that_was_not_driven_or_printed_nothing_fails() -> None:
    without = [o for o in _correct() if not (o.probe.fixture == "user-strict" and o.probe.id.endswith(labels.SHAPE_WORKTREE))]
    assert any("user-strict: the 'worktree dispatch'" in p for p in labels.shape_problems(without))
    silent = [*without, _outcome("user-strict", "", labels.SHAPE_WORKTREE)]
    assert any("printed nothing" in p for p in labels.shape_problems(silent))


def test_the_fixtures_set_the_keys_the_check_covers_and_the_leg_reexports_them() -> None:
    for key in ("preflight", "ci_wait", "credential_helper", "brief_questions"):
        assert key in leg.PROJECT_SETTINGS, key
    assert "brief_questions" in leg.USER_SETTINGS and leg.PROJECT_SETTINGS["version"] == 1 and "schema" not in leg.PROJECT_SETTINGS
    assert {w.key for w in labels.expected_texts()} == {"preflight", "ci_wait", "credential_helper", "brief_questions"}


def test_the_label_wording_is_the_rdrs_table(tmp_path) -> None:
    """The oracle's text equals what the plugin's helper builds, so neither side can drift alone."""
    spec = importlib.util.spec_from_file_location("_audience_labels_probe", LIB.parents[2] / "conexus" / "hooks" / "scripts" / "_audience.py")
    audience = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audience)
    for key in ("preflight", "ci_wait", "credential_helper"):
        assert audience.provenance_label(key, "project", "V") == labels._LABELS[(key, "project")].format(v="V")
    assert audience.provenance_label("brief_questions", "project") == labels._LABELS[("brief_questions", "project")]
    assert audience.provenance_label("brief_questions", "user") == labels._LABELS[("brief_questions", "user")]
