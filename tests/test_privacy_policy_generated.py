# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-243 Phase 1 Step 5c (nexus-rfvyy.6.18): the privacy-policy section 2 table and the ``--sensitive``
help are generated from the registry rows enforced in the release.

The source is ``src/nexus/tables/llm-surfaces.toml`` through ``nexus.tables.llm_surfaces``. The generated
text lists only rows whose enforcing phase is at or below ``RELEASE_ENFORCED_PHASE`` (RDR-243 Technical
Design item 7), so no release describes protection it does not give. The pins:

- the text between the markers in ``docs/privacy-policy.md`` equals what the generator renders now;
- the table and the help list exactly the enforced rows, and a row of a later phase does not appear;
- the generator is a pure function of its inputs, so each pin runs at phase 1 and at phase 2 without
  patching a constant, and on a fixture registry.
"""
from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import gen_privacy_policy_table as gen  # scripts/ is on the test pythonpath (pyproject)
import pytest

from nexus.tables import llm_surfaces as ls

REPO_ROOT = Path(__file__).resolve().parents[1]
POLICY = REPO_ROOT / "docs" / "privacy-policy.md"
SENSITIVITY = REPO_ROOT / "docs" / "sensitivity.md"


def _listed(phase: int) -> list[ls.SurfaceRow]:
    return [r for r in ls.rows() if r.phase <= phase and r.endpoints != ("none",)]


def _generated_region(text: str) -> str:
    m = re.search(
        re.escape(gen.BEGIN) + r"\n(.*?)\n" + re.escape(gen.END), text, re.DOTALL,
    )
    assert m, "docs/privacy-policy.md has no generated region between the BEGIN and END markers"
    return m.group(1)


def _section2(text: str) -> str:
    start = text.index("## 2. What Conexus sends to third parties")
    return text[start:text.index("## 3. ", start)]


# --------------------------------------------------------------------------- #
# The release constant and the rows it selects from.
# --------------------------------------------------------------------------- #


def test_the_release_constant_is_one_until_phase_two_lands() -> None:
    """Phase 1 ships alone until the Phase 2 read filter and its canary rows land (RDR-243 MVV). The generated
    text says "excluded" for a Phase 2 row only when the release enforces it, so the Phase 2 commit raises this
    constant to 2 together with its canary rows and regenerates the docs; raising it earlier over-claims
    (fix round 1, nexus-rfvyy.6.19 / .6.20 ship-blocker).

    This is a bare equality and nothing more: it fails when the constant moves, and says nothing about whether
    the text is honest. The honesty pins are the pending-line test below
    (``test_the_line_names_every_pending_row_that_reaches_outward_or_the_session_model``) and the render checks:
    ``test_the_table_lists_exactly_the_enforced_rows``, ``test_the_help_names_exactly_the_enforced_rows`` and
    ``test_the_real_policy_and_the_real_help_carry_the_line_once``, which read the generated help and docs against
    the registry rows."""
    assert ls.RELEASE_ENFORCED_PHASE == 1


def _pending_outward(phase: int) -> list[ls.SurfaceRow]:
    return [r for r in ls.rows() if r.phase > phase and r.endpoints != ("none",)]


@pytest.mark.parametrize("phase", [1, 2])
def test_the_line_names_every_pending_row_that_reaches_outward_or_the_session_model(phase: int) -> None:
    """Whatever the row's behaviour: a pending Knowledge Map (session-model, exclude) and the Phase 2
    anthropic rows are named, not only the withhold and Voyage rows."""
    line = ls.not_yet_enforced_line(phase)
    pending = _pending_outward(phase)
    assert pending, "non-vacuity: some row is pending"
    for r in pending:
        assert r.label in line, f"{r.name} (phase {r.phase}, {r.behaviour}, {r.endpoints}) is pending and not named"


def test_the_pending_rows_include_a_non_withhold_session_model_row_and_anthropic_rows() -> None:
    """Non-vacuity of the pin above: it covers rows the old filter (withhold, voyage) skipped."""
    kinds = {(e, r.behaviour) for r in _pending_outward(1) for e in r.endpoints}
    assert ("session-model", "exclude") in kinds and ("anthropic", "exclude") in kinds


def test_the_table_has_rows_of_every_phase_so_the_phase_pins_are_not_vacuous() -> None:
    assert {r.phase for r in ls.rows()} == {1, 2, 3}


# --------------------------------------------------------------------------- #
# The policy table.
# --------------------------------------------------------------------------- #


def test_the_policy_region_equals_what_the_generator_renders_now() -> None:
    region = _generated_region(POLICY.read_text(encoding="utf-8"))
    assert region == ls.render_policy_table(ls.RELEASE_ENFORCED_PHASE), (
        "docs/privacy-policy.md is stale; run `uv run python scripts/gen_privacy_policy_table.py --write`"
    )


@pytest.mark.parametrize("phase", [1, 2])
def test_the_table_lists_exactly_the_enforced_rows(phase: int) -> None:
    table = ls.render_policy_table(phase)
    data_lines = [ln for ln in table.splitlines() if ln.startswith("| ") and not ln.startswith("| Surface")]
    data_lines = [ln for ln in data_lines if not set(ln) <= set("| -")]
    listed = _listed(phase)
    assert len(data_lines) == len(listed)
    for row in listed:
        assert f"| {row.label} |" in table, row.name
    for row in ls.rows():
        if row.phase > phase and row.endpoints != ("none",):
            assert f"| {row.label} |" not in table, f"{row.name} is phase {row.phase}, above {phase}"


def test_phase_two_rows_appear_at_two_and_not_at_one() -> None:
    p2 = [r for r in ls.rows() if r.phase == 2]
    assert p2
    at_one, at_two = ls.render_policy_table(1), ls.render_policy_table(2)
    for r in p2:
        assert f"| {r.label} |" not in at_one, "a table ROW; the pending line may name it"
        assert f"| {r.label} |" in at_two


def test_phase_three_rows_never_appear_in_the_release_table() -> None:
    table = ls.render_policy_table(ls.RELEASE_ENFORCED_PHASE)
    p3 = [r for r in ls.rows() if r.phase == 3]
    assert p3
    for r in p3:
        assert f"| {r.label} |" not in table


def test_the_table_names_the_endpoints_the_policy_lacked() -> None:
    table = ls.render_policy_table(ls.RELEASE_ENFORCED_PHASE)
    assert "OpenAlex" in table
    assert "CrossRef" in table
    assert "pdf.mineru_server_url" in table


def test_every_endpoint_class_has_a_destination_text() -> None:
    assert set(ls.ENDPOINT_DESTINATIONS) == set(ls.ENDPOINT_CLASSES)


def test_the_anthropic_bullet_is_not_under_the_managed_cloud_heading() -> None:
    section = _section2(POLICY.read_text(encoding="utf-8"))
    managed_start = section.index("**Managed-cloud mode**")
    managed = section[managed_start:section.index("**In every mode", managed_start)]
    assert "Anthropic Claude" not in managed, "the Anthropic bullet applies in every mode"
    every_mode = section[section.index("**In every mode"):]
    assert "Anthropic Claude" in every_mode


# --------------------------------------------------------------------------- #
# The --sensitive help.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("phase", [1, 2])
def test_the_help_names_exactly_the_enforced_rows(phase: int) -> None:
    text = ls.render_sensitive_help(phase).replace(ls.not_yet_enforced_line(phase), "")
    for row in _listed(phase):
        assert row.label in text, row.name
    for row in ls.rows():
        if row.phase > phase and row.endpoints != ("none",):
            assert row.label not in text, f"{row.name} is phase {row.phase}, above {phase}"


def test_the_help_is_a_function_of_the_registry_rows() -> None:
    """Blank one row's label in a fixture registry: its absence from the help follows, nothing else changes."""
    base = list(ls.rows())
    victim = next(r for r in base if r.phase == 1 and r.endpoints != ("none",))
    fixture = [r for r in base if r.name != victim.name]
    text = ls.render_sensitive_help(2, fixture)
    assert victim.label not in text
    assert ls.render_sensitive_help(2, base) != text


# --------------------------------------------------------------------------- #
# The generator script.
# --------------------------------------------------------------------------- #


def test_a_stale_policy_is_caught(tmp_path, monkeypatch) -> None:
    stale = tmp_path / "privacy-policy.md"
    stale.write_text(POLICY.read_text(encoding="utf-8").replace("OpenAlex", "OpenAlexx", 1), encoding="utf-8")
    monkeypatch.setattr(gen, "POLICY", stale)
    monkeypatch.setattr(gen, "SENSITIVITY", SENSITIVITY)
    assert gen.main(["--check"]) == 1


def test_a_missing_marker_is_caught(tmp_path, monkeypatch) -> None:
    bare = tmp_path / "privacy-policy.md"
    bare.write_text("# Policy\n\nno region here\n", encoding="utf-8")
    monkeypatch.setattr(gen, "POLICY", bare)
    assert gen.main(["--check"]) == 1


def test_write_then_check_is_clean(tmp_path, monkeypatch) -> None:
    doc = tmp_path / "privacy-policy.md"
    doc.write_text(POLICY.read_text(encoding="utf-8").replace("OpenAlex", "OpenAlexx", 1), encoding="utf-8")
    sens = tmp_path / "sensitivity.md"
    sens.write_text(SENSITIVITY.read_text(encoding="utf-8").replace("Voyage AI", "Voyage AIx", 1), encoding="utf-8")
    monkeypatch.setattr(gen, "POLICY", doc)
    monkeypatch.setattr(gen, "SENSITIVITY", sens)
    assert gen.main(["--check"]) == 1
    assert gen.main(["--write"]) == 0
    assert gen.main(["--check"]) == 0
    assert doc.read_text(encoding="utf-8") == POLICY.read_text(encoding="utf-8")
    assert sens.read_text(encoding="utf-8") == SENSITIVITY.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# The --sensitive option on each verb (the flag lands in sibling beads).
# --------------------------------------------------------------------------- #

_VERBS = (
    ("index", "pdf"), ("index", "md"), ("dt", "index"), ("dt", "capture"), ("store", "put"),
)


def _sensitive_option(group: str, cmd: str):
    import click

    from nexus.cli import main

    grp = main.commands.get(group)
    sub = grp.commands.get(cmd) if isinstance(grp, click.Group) else None
    if sub is None:
        return None
    return next((p for p in sub.params if isinstance(p, click.Option) and "--sensitive" in p.opts), None)


@pytest.mark.parametrize(("group", "cmd"), _VERBS)
def test_the_verb_help_is_the_generated_help(group: str, cmd: str) -> None:
    opt = _sensitive_option(group, cmd)
    if opt is None:
        pytest.skip(f"`nx {group} {cmd}` has no --sensitive option yet (RDR-243 Phase 1 Step 3 beads add it)")
    assert opt.help == ls.render_sensitive_help(ls.RELEASE_ENFORCED_PHASE)


# --------------------------------------------------------------------------- #
# The row fields the text is built from.
# --------------------------------------------------------------------------- #


def test_every_row_carries_a_label_and_sends_text_without_table_breaking_characters() -> None:
    for r in ls.rows():
        assert r.label.strip() and r.sends.strip(), r.name
        assert "|" not in r.label and "|" not in r.sends, r.name
        assert "\n" not in r.label and "\n" not in r.sends, r.name
    assert len({r.label for r in ls.rows()}) == len(ls.rows()), "labels are unique"


def test_a_row_edit_changes_the_rendered_table() -> None:
    base = list(ls.rows())
    edited = [dataclasses.replace(r, sends=r.sends + " (edited)") if r.census_row == 7 else r for r in base]
    assert ls.render_policy_table(2, edited) != ls.render_policy_table(2, base)


# --------------------------------------------------------------------------- #
# The line about rows not yet enforced (RDR-243 accept residual S-E, nexus-rfvyy.5), and docs/sensitivity.md.
# --------------------------------------------------------------------------- #


def _enforce(rows: list[ls.SurfaceRow], *, predicate) -> list[ls.SurfaceRow]:
    """A fixture registry in which the rows *predicate* picks are enforced in phase 1."""
    return [dataclasses.replace(r, phase=1) if predicate(r) else r for r in rows]


def _session_model_withhold(r: ls.SurfaceRow) -> bool:
    return "session-model" in r.endpoints and r.behaviour == "withhold"


def _voyage(r: ls.SurfaceRow) -> bool:
    return "voyage" in r.endpoints


def test_the_session_model_rows_the_line_derives_from_exist_above_the_release_phase() -> None:
    """Non-vacuity: the line has something to be derived from."""
    pending = [r for r in ls.rows() if r.phase > ls.RELEASE_ENFORCED_PHASE]
    assert any(_session_model_withhold(r) for r in pending)
    assert any(_voyage(r) for r in pending)


@pytest.mark.parametrize("phase", [1, 2])
def test_the_help_and_the_table_each_carry_the_line_exactly_once(phase: int) -> None:
    line = ls.not_yet_enforced_line(phase)
    assert line and "\n" not in line
    assert "still shown to the session model" in line
    help_text, table = ls.render_sensitive_help(phase), ls.render_policy_table(phase)
    assert help_text.count(line) == 1
    assert table.count(line) == 1


def test_the_real_policy_and_the_real_help_carry_the_line_once() -> None:
    line = ls.not_yet_enforced_line(ls.RELEASE_ENFORCED_PHASE)
    assert POLICY.read_text(encoding="utf-8").count(line) == 1
    assert ls.render_sensitive_help().count(line) == 1


def test_the_line_is_absent_once_every_row_it_derives_from_is_enforced() -> None:
    fixture = _enforce(list(ls.rows()), predicate=lambda r: r.endpoints != ("none",))
    assert ls.not_yet_enforced_line(2, fixture) == ""
    assert "Not yet enforced" not in ls.render_sensitive_help(2, fixture)
    assert "Not yet enforced" not in ls.render_policy_table(2, fixture)


def test_the_line_is_absent_when_the_release_phase_is_three() -> None:
    assert ls.not_yet_enforced_line(3) == ""


def test_the_clauses_leave_independently() -> None:
    base = list(ls.rows())
    assert "Anthropic" in ls.not_yet_enforced_line(1), "non-vacuity: the anthropic clause exists to leave"
    shown_enforced = ls.not_yet_enforced_line(2, _enforce(base, predicate=_session_model_withhold))
    assert "still shown to the session model" not in shown_enforced and "Voyage AI" in shown_enforced
    voyage_enforced = ls.not_yet_enforced_line(2, _enforce(base, predicate=_voyage))
    assert "still shown to the session model" in voyage_enforced and "Voyage AI" not in voyage_enforced
    anthropic_enforced = ls.not_yet_enforced_line(1, _enforce(base, predicate=lambda r: "anthropic" in r.endpoints))
    assert "Anthropic" not in anthropic_enforced and "Voyage AI" in anthropic_enforced


def test_the_line_does_not_depend_on_the_text_of_a_neighbouring_row() -> None:
    """A row that is not session-model/withhold or voyage, edited, leaves the line unchanged."""
    base = list(ls.rows())
    edited = [dataclasses.replace(r, label=r.label + " x") if r.census_row == 7 else r for r in base]
    assert ls.not_yet_enforced_line(2, edited) == ls.not_yet_enforced_line(2, base)


def test_sensitivity_doc_regions_equal_what_the_generator_renders_now() -> None:
    text = SENSITIVITY.read_text(encoding="utf-8")
    for path, name, region in gen.regions():
        if path != gen.SENSITIVITY:
            continue
        begin, end = gen.begin_marker(name), gen.end_marker(name)
        m = re.search(re.escape(begin) + r"\n(.*?)\n" + re.escape(end), text, re.DOTALL)
        assert m, f"docs/sensitivity.md has no {name} region"
        assert m.group(1) == region, "docs/sensitivity.md is stale; run scripts/gen_privacy_policy_table.py --write"


def test_sensitivity_stub_has_its_sections_and_points_at_the_rdr() -> None:
    text = SENSITIVITY.read_text(encoding="utf-8")
    assert "## What `--sensitive` enforces in this release" in text
    assert "## What it does not enforce" in text
    assert "rdr/rdr-243-first-class-document-sensitivity.md" in text
    assert "T2 note" in text
    assert ls.not_yet_enforced_line(ls.RELEASE_ENFORCED_PHASE) in text
