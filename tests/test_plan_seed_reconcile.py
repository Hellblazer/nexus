# SPDX-License-Identifier: AGPL-3.0-or-later
"""Seed-loader reconcile leg and its doctor parity assert (nexus-f1mbo).

Before this work the loader was insert-only: an edited template on disk
could never reach a library that already held a row for its dimensions.
The live library on this project's own install froze at its April 2026
seed — two templates never landed at all, three descriptions drifted —
while ``nx doctor --check-plan-library`` reported green, because its only
gate was a count floor of 9 against a live count of 15.

Two things are pinned here:

* the reconcile leg updates drifted rows, skips unchanged ones, and
  refuses to overwrite user-grown plans; and
* the parity assert actually REDS on a missing or drifted template. A
  parity check that has never been seen to fail is the same vacuous gate
  one level up (the nexus-moht0 doctrine), so both failure directions get
  their own test rather than being inferred from a green run.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from nexus.plans.schema import canonical_dimensions_json
from nexus.plans.seed_loader import desired_row_for_template, load_seed_directory

_BUILTIN_DIR = Path(__file__).parent.parent / "conexus" / "plans" / "builtin"


def _write_template(directory: Path, name: str, **overrides) -> Path:
    """Write a minimal valid template, applying *overrides* on top."""
    template: dict = {
        "name": name,
        "description": f"Seed reconcile fixture {name}.",
        "dimensions": {"verb": "research", "scope": "global", "strategy": name},
        "tags": "builtin-template,test-fixture",
        "plan_json": {"steps": [{"tool": "query", "args": {"question": "$intent"}}]},
    }
    template.update(overrides)
    path = directory / f"{name}.yml"
    path.write_text(yaml.safe_dump(template, sort_keys=False))
    return path


@pytest.fixture
def library():
    from nexus.db.t2.http_plan_library import HttpPlanLibrary

    return HttpPlanLibrary()


def _row_for(library, template_path: Path) -> dict:
    template = yaml.safe_load(template_path.read_text())
    canonical = canonical_dimensions_json(template["dimensions"])
    row = library.get_plan_by_dimensions(project="", dimensions=canonical)
    assert row is not None, f"no library row at {canonical}"
    return row


# ── reconcile leg ──────────────────────────────────────────────────────────


def test_insert_only_pass_leaves_drift_in_place(tmp_path, library):
    """The pre-fix behaviour, pinned as the thing reconcile exists to fix.

    Without ``reconcile=True`` an edited description is invisible: the
    deduper keys on canonical dimensions, matches, and skips.
    """
    path = _write_template(tmp_path, "drift-a")
    assert load_seed_directory(tmp_path, library=library).inserted == ["drift-a.yml"]

    _write_template(tmp_path, "drift-a", description="Rewritten description.")
    result = load_seed_directory(tmp_path, library=library)

    assert result.skipped_existing == ["drift-a.yml"]
    assert result.updated == []
    assert _row_for(library, path)["query"] == "Seed reconcile fixture drift-a."


def test_reconcile_rewrites_a_changed_description_without_duplicating(tmp_path, library):
    """The description is the upsert key, so this is the path that would
    collide with the dimensional unique index if the stale row were not
    dropped first."""
    path = _write_template(tmp_path, "drift-b")
    load_seed_directory(tmp_path, library=library)
    _write_template(tmp_path, "drift-b", description="Rewritten description.")

    result = load_seed_directory(tmp_path, library=library, reconcile=True)

    assert result.updated == ["drift-b.yml"]
    assert result.inserted == []
    assert _row_for(library, path)["query"] == "Rewritten description."


def test_reconcile_rewrites_plan_json_under_an_unchanged_description(tmp_path, library):
    """Same description => the upsert lands in place, no delete needed."""
    path = _write_template(tmp_path, "drift-c")
    load_seed_directory(tmp_path, library=library)
    new_steps = {"steps": [{"tool": "search", "args": {"query": "$intent"}}]}
    _write_template(tmp_path, "drift-c", plan_json=new_steps)

    result = load_seed_directory(tmp_path, library=library, reconcile=True)

    assert result.updated == ["drift-c.yml"]
    assert json.loads(_row_for(library, path)["plan_json"]) == new_steps


def test_reconcile_is_a_no_op_when_nothing_changed(tmp_path, library):
    """A reconcile run over a current library must perform zero writes —
    otherwise every run would reset every row's counters."""
    _write_template(tmp_path, "drift-d")
    load_seed_directory(tmp_path, library=library)

    result = load_seed_directory(tmp_path, library=library, reconcile=True)

    assert result.updated == []
    assert result.skipped_existing == ["drift-d.yml"]


def test_reconcile_inserts_a_template_the_library_has_never_seen(tmp_path, library):
    _write_template(tmp_path, "drift-e")
    result = load_seed_directory(tmp_path, library=library, reconcile=True)
    assert result.inserted == ["drift-e.yml"]
    assert result.updated == []


def test_reconcile_refuses_to_overwrite_a_grown_plan(tmp_path, library):
    """Grown plans are the only plans in this library that currently match
    anything. A reconcile that clobbered one would take the working path
    down with it, so a dimensional collision is reported, not resolved."""
    path = _write_template(tmp_path, "drift-f")
    template = yaml.safe_load(path.read_text())
    canonical = canonical_dimensions_json(template["dimensions"])
    desired = desired_row_for_template(template)
    library.save_plan(
        query="What did the user actually ask?",
        plan_json=desired.plan_json,
        tags="grown,ad-hoc",
        project="",
        name="grown-collision",
        verb="research",
        scope="global",
        dimensions=canonical,
    )

    result = load_seed_directory(tmp_path, library=library, reconcile=True)

    assert result.updated == []
    assert result.inserted == []
    assert [name for name, _ in result.protected] == ["drift-f.yml"]
    assert "grown" in result.protected[0][1]
    surviving = library.get_plan_by_dimensions(project="", dimensions=canonical)
    assert surviving["query"] == "What did the user actually ask?"


# ── doctor parity assert ───────────────────────────────────────────────────


def _live_rows_from_disk() -> list[dict]:
    """Build the library rows a correctly-seeded global tier would hold."""
    rows = []
    for path in sorted(_BUILTIN_DIR.glob("*.yml")):
        template = dict(yaml.safe_load(path.read_text()))
        dims = dict(template["dimensions"])
        dims["scope"] = "global"
        template["dimensions"] = dims
        desired = desired_row_for_template(template)
        rows.append({
            "id": len(rows) + 1,
            "project": "",
            "dimensions": canonical_dimensions_json(dims),
            "query": desired.query,
            "plan_json": desired.plan_json,
            "tags": desired.tags,
            "name": desired.name,
            "verb": desired.verb,
            "scope": desired.scope,
            "default_bindings": desired.default_bindings,
            "parent_dims": desired.parent_dims,
        })
    return rows


def test_parity_passes_against_a_correctly_seeded_library():
    from nexus.commands.doctor import _plan_library_parity

    report = _plan_library_parity(_live_rows_from_disk(), truncated=False)

    assert report.unavailable is None, report.unavailable
    assert report.missing == []
    assert report.drifted == []
    assert report.orphaned == []
    assert not report.failed


def test_parity_reds_on_a_template_missing_from_the_library():
    """Non-vacuity, direction 1 — the exact condition the count floor
    passed green on."""
    from nexus.commands.doctor import _plan_library_parity

    rows = _live_rows_from_disk()
    dropped = rows.pop()

    report = _plan_library_parity(rows, truncated=False)

    assert report.failed
    assert len(report.missing) == 1
    assert dropped["name"] in report.missing[0] or report.missing


def test_parity_reds_on_a_row_that_drifted_from_its_template():
    """Non-vacuity, direction 2 — a library row whose stored text no
    longer matches the file that shipped it."""
    from nexus.commands.doctor import _plan_library_parity

    rows = _live_rows_from_disk()
    rows[0]["query"] = "Text this template never carried."

    report = _plan_library_parity(rows, truncated=False)

    assert report.failed
    assert len(report.drifted) == 1


def test_parity_reports_an_orphan_without_failing():
    """A builtin row with no template on disk is left alone and named —
    disk is authoritative for the templates it ships, not for the table."""
    from nexus.commands.doctor import _plan_library_parity

    rows = _live_rows_from_disk()
    rows.append({
        "id": 999,
        "project": "",
        "dimensions": '{"scope":"global","strategy":"retired","verb":"research"}',
        "query": "A template that used to ship.",
        "plan_json": "{}",
        "tags": "builtin-template",
        "name": "retired-template",
    })

    report = _plan_library_parity(rows, truncated=False)

    assert not report.failed
    assert report.orphaned == ["retired-template"]


def test_parity_degrades_to_unavailable_when_the_listing_was_truncated():
    """A template absent from a capped page proves nothing. The check must
    not manufacture a red any more than it may manufacture a green."""
    from nexus.commands.doctor import _plan_library_parity

    rows = _live_rows_from_disk()[:3]

    report = _plan_library_parity(rows, truncated=True)

    assert report.unavailable is not None
    assert not report.failed
