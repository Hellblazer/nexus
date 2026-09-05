# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-axwpn: multi-model repeatability diff of an RDR's design text."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

import nexus.commands.rdr as rdr_mod
from nexus.commands.rdr import rdr as rdr_cli
from nexus.rdr_repeat import (
    PLAN_SCHEMA,
    Plan,
    RepeatError,
    Step,
    build_prompt,
    diff_plans,
    extract_design_section,
    parse_plan,
    render_report,
    _same_decision,
    _same_file,
    _same_step,
)

RDR_TEXT = """---
id: 999
status: draft
---
# RDR-999: Example

## Problem

Something.

## Solution

### Technical Design

Add a checker module under src/nexus/tables/ and wire it into load().

#### Details

More.

### Alternatives

Nope.
"""


def test_extracts_technical_design_to_the_next_same_level_heading() -> None:
    body = extract_design_section(RDR_TEXT)
    assert body.startswith("Add a checker module")
    assert "#### Details" in body, "deeper headings stay inside the section"
    assert "Alternatives" not in body and "Nope" not in body


def test_falls_back_to_design_synonyms_and_reports_none() -> None:
    assert extract_design_section("## Proposed Design\n\nbody\n\n## Next\n") == "body"
    assert extract_design_section("### Design\n\nbody\n") == "body"
    assert extract_design_section("## Design Principles\n\nx\n") == "", "word-anchored: Design Principles is not Design"
    assert extract_design_section("## Problem\n\nx\n") == ""


def test_parse_plan_rejects_empty_and_malformed() -> None:
    with pytest.raises(RepeatError):
        parse_plan("m", {"steps": []})
    with pytest.raises(RepeatError):
        parse_plan("m", {"nope": 1})
    plan = parse_plan("m", {"steps": [{"title": " A ", "files": ["x.py", ""], "decisions": []}]})
    assert plan.steps == (Step(title="A", files=("x.py",)),)


def _plan(model: str, *steps: tuple[str, list[str], list[str]]) -> Plan:
    return Plan(model=model, steps=tuple(Step(t, tuple(f), tuple(d)) for t, f, d in steps))


def test_identical_plans_diverge_nowhere() -> None:
    a = _plan("a", ("Add the checker", ["src/nexus/tables/check.py"], ["fail on overlap"]))
    b = _plan("b", ("Add checker module", ["./src/nexus/tables/check.py"], ["fail on overlap"]))
    d = diff_plans(a, b)
    assert d.count == 0 and d.matched_steps == 1
    assert "No divergences" in render_report("rdr-999", a, b, d)


def test_divergent_plans_are_reported_per_axis() -> None:
    a = _plan(
        "a",
        ("Add the checker", ["src/nexus/tables/check.py"], ["fail on overlap"]),
        ("Write a migration", ["service/db/changelog/x.xml"], ["one changeset"]),
    )
    b = _plan(
        "b",
        ("Add checker module", ["src/nexus/tables/check.py", "tests/test_check.py"], ["warn on overlap"]),
    )
    d = diff_plans(a, b)
    assert d.steps_only_a == ["Write a migration"] and d.steps_only_b == []
    assert d.files_only_a == ["service/db/changelog/x.xml"]
    assert d.files_only_b == ["tests/test_check.py"]
    assert d.decisions_only_a == ["fail on overlap", "one changeset"]
    assert d.decisions_only_b == ["warn on overlap"]
    assert d.count == 6
    report = render_report("rdr-999", a, b, d)
    assert "Write a migration" in report and "warn on overlap" in report
    assert "6 divergence(s)" in report


def test_step_matching_survives_the_first_live_run_phrasings() -> None:
    """Pairs a reader makes at a glance from the RDR-180 live run
    (2026-09-04), which plain Jaccard over raw tokens matched 0 of."""
    a = _plan(
        "a",
        ("Implement migration ETL: old\u2192new mapping and rehash", [], []),
        ("Create Liquibase schema changeset", ["src/main/resources/db/changelog/x.xml"], []),
        ("Define Chash record type in Java", ["src/main/java/dev/nexus/service/db/Chash.java"], []),
    )
    b = _plan(
        "b",
        ("Migration ETL: rehash / remap / orphan disposition", [], []),
        ("Schema: chash text\u2192bytea, checks, staged validate", ["service/src/main/resources/db/changelog/x.xml"], []),
        ("Java: Chash boundary type and jOOQ codec", ["service/src/main/java/dev/nexus/service/db/Chash.java"], []),
    )
    d = diff_plans(a, b)
    assert d.matched_steps >= 2, (d.steps_only_a, d.steps_only_b)
    assert d.files_only_a == [] and d.files_only_b == [], "same basename, different root: not a divergence"


def test_matching_false_positives_from_review_24379() -> None:
    """Each of these matched on the first cut; each is a distinct thing."""
    assert not _same_decision("warn on overlap", "explicitly reject warn on overlap as unsafe and always fail instead")
    assert not _same_step("Rehash", "Update every consumer of the manifest so that the rehash path, the remap path and the orphan path all agree on the new width")
    assert _same_step("Migration ETL: rehash / remap / orphan disposition", "Implement migration ETL: old\u2192new mapping and rehash")
    assert not _same_file("a/config.py", "b/config.py"), "same basename, different package"
    assert not _same_file("config.py", "b/config.py"), "a bare basename is not a path"
    assert _same_file("src/x/config.py", "service/src/x/config.py")
    assert not _same_file("y/src/x/config.py", "service/src/x/config.py")


def test_design_heading_shapes_from_the_corpus() -> None:
    """rdr-001 and rdr-061 use ``## Design: <subtitle>``, rdr-074 ``## Design
    (sketch)``; each is that RDR's only design section (review [24379] M4)."""
    assert extract_design_section("## Design: The Two-Layer Model\n\nbody\n\n## Next\n") == "body"
    assert extract_design_section("## Design (sketch)\n\nbody\n") == "body"
    assert extract_design_section("## Design Decisions\n\nx\n") == ""


def test_build_prompt_carries_the_design_and_nothing_else() -> None:
    p = build_prompt("rdr-999", "the design body")
    assert "the design body" in p and "rdr-999" in p
    assert "Problem" not in p


# ── the verb ─────────────────────────────────────────────────────────────────


def _install_fake_dispatch(monkeypatch: pytest.MonkeyPatch, by_model: dict[str, dict]):
    calls: list[dict] = []

    async def fake(prompt, json_schema, **kwargs):
        assert json_schema is PLAN_SCHEMA
        assert kwargs["isolated"] is True
        calls.append({"model": kwargs["model"], "prompt": prompt})
        return by_model[kwargs["model"]]

    monkeypatch.setattr(rdr_mod, "_repeat_dispatch", fake)
    return calls


def test_repeat_dispatches_both_tiers_and_prints_the_diff(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "rdr-999-example.md").write_text(RDR_TEXT)
    cheap, strong = "haiku", "sonnet"
    calls = _install_fake_dispatch(
        monkeypatch,
        {
            cheap: {"steps": [{"title": "Add checker", "files": ["a.py"], "decisions": ["x"]}]},
            strong: {"steps": [{"title": "Add checker", "files": ["a.py", "b.py"], "decisions": ["x"]}]},
        },
    )
    res = CliRunner().invoke(rdr_cli, ["repeat", "999", "--root", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert {c["model"] for c in calls} == {cheap, strong}
    assert "Add a checker module" in calls[0]["prompt"], "the design text reached the model"
    assert "b.py" in res.output and "1 divergence(s)" in res.output


def test_repeat_json_output(tmp_path: Path, monkeypatch) -> None:
    f = tmp_path / "rdr-999-example.md"
    f.write_text(RDR_TEXT)
    same = {"steps": [{"title": "Add checker", "files": ["a.py"], "decisions": []}]}
    _install_fake_dispatch(
        monkeypatch,
        {"haiku": same, "sonnet": same},
    )
    res = CliRunner().invoke(rdr_cli, ["repeat", str(f), "--json"])
    assert res.exit_code == 0, res.output
    doc = json.loads(res.stdout)
    assert doc["rdr"] == "rdr-999-example" and doc["divergence"]["count"] == 0
    assert len(doc["plans"]) == 2


def test_repeat_refuses_an_rdr_with_no_design_section(tmp_path: Path, monkeypatch) -> None:
    f = tmp_path / "rdr-998-nodesign.md"
    f.write_text("# RDR-998\n\n## Problem\n\nx\n")
    calls = _install_fake_dispatch(monkeypatch, {})
    res = CliRunner().invoke(rdr_cli, ["repeat", str(f)])
    assert res.exit_code == 2 and "nothing to repeat" in res.output
    assert calls == [], "no dispatch without a design section"


def test_repeat_reports_a_dispatch_failure_without_a_traceback(tmp_path: Path, monkeypatch) -> None:
    f = tmp_path / "rdr-999-example.md"
    f.write_text(RDR_TEXT)

    async def boom(prompt, json_schema, **kwargs):
        raise RuntimeError("claude -p exploded")

    monkeypatch.setattr(rdr_mod, "_repeat_dispatch", boom)
    res = CliRunner().invoke(rdr_cli, ["repeat", str(f)])
    assert res.exit_code == 1 and "claude -p exploded" in res.output
    assert "Traceback" not in res.output


def test_repeat_refuses_two_models_that_are_one(tmp_path: Path, monkeypatch) -> None:
    """Per-commit reviewer FILE finding on 4b65a7ffa: one model twice is a
    self-diff, not a repeatability check."""
    f = tmp_path / "rdr-999-example.md"
    f.write_text(RDR_TEXT)
    calls = _install_fake_dispatch(monkeypatch, {})
    res = CliRunner().invoke(rdr_cli, ["repeat", str(f), "--models", "haiku,haiku"])
    assert res.exit_code == 2 and "two readers" in res.output and calls == []
