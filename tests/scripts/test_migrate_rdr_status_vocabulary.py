# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``scripts/migrate_rdr_status_vocabulary.py`` — RDR-201 P1.7.

Two legs. LEG 1 (files): rewrites docs/rdr/*.md frontmatter to the six-value
closed vocabulary (draft/accepted/deferred/closed/superseded/abandoned),
retiring `scrapped` -> `abandoned` and demoting the RDR-200 companion
sub-documents (companion-note/frozen/frozen-pending-question-set/complete)
to `kind: companion` with no lifecycle status; `revised-after-implementation`
becomes `status: closed` + `kind: companion`. The README index is rewritten
in the SAME run via a shape-aware cell parser that only touches the status
WORD inside a decorated cell, preserving dates/parens/successor RDR ids and
the cell's original case. LEG 2 (T2) mirrors the same mapping against T2
project ``nexus_rdr`` through an injectable client, dry-run by default.

Built over a tmp_path fixture tree carrying one file per each of the twelve
on-disk statuses (T2 [24001]/[23999]), decorated README rows exercising the
real shapes, an ``rdr137``-style misnamed companion deliverable, and a
``post-mortem/`` subdirectory that must never be touched by the (non-
recursive) sweep.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "migrate_rdr_status_vocabulary.py"


@pytest.fixture
def mod():
    spec = importlib.util.spec_from_file_location("migrate_rdr_status_vocabulary", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # dataclasses with `from __future__ import annotations` resolve string
    # annotations via sys.modules[cls.__module__] -- must be registered
    # before exec_module for the module's own dataclasses to build.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fm(status_line: str, *, title: str = "RDR-NNN Title", extra: str = "") -> str:
    return (
        "---\n"
        f'title: "{title}"\n'
        "created: 2026-05-01\n"
        f"{status_line}\n"
        f"{extra}"
        "---\n\n"
        "# Body\n\nSome content.\n"
    )


README_HEADER = (
    "# RDR Index\n\n"
    "## All RDRs\n\n"
    "| ID | Title | Type | Status | Created |\n"
    "| -- | ----- | ---- | ------ | ------- |\n"
)


@pytest.fixture
def rdr_tree(tmp_path: Path) -> Path:
    """One file per each of the twelve on-disk statuses (T2 [24001]), plus
    decoration/exclusion fixtures. Returns the ``docs/rdr``-equivalent dir."""
    rdr_dir = tmp_path / "docs" / "rdr"
    rdr_dir.mkdir(parents=True)

    # -- AGENTS.md / README.md themselves must never be treated as RDRs ----
    (rdr_dir / "AGENTS.md").write_text("# agents\n", encoding="utf-8")

    # -- the six canonical lifecycle statuses (identity mapping) -----------
    (rdr_dir / "rdr-001-draft-one.md").write_text(_fm("status: draft"), encoding="utf-8")
    (rdr_dir / "rdr-002-accepted-one.md").write_text(_fm("status: accepted"), encoding="utf-8")
    (rdr_dir / "rdr-003-deferred-one.md").write_text(_fm("status: deferred"), encoding="utf-8")
    (rdr_dir / "rdr-004-closed-one.md").write_text(_fm("status: closed"), encoding="utf-8")
    (rdr_dir / "rdr-005-superseded-one.md").write_text(_fm("status: superseded"), encoding="utf-8")
    (rdr_dir / "rdr-006-abandoned-one.md").write_text(_fm("status: abandoned"), encoding="utf-8")

    # -- the retired value ---------------------------------------------------
    (rdr_dir / "rdr-007-scrapped-one.md").write_text(_fm("status: scrapped"), encoding="utf-8")

    # -- companion-outcome statuses (status removed, kind: companion added) --
    (rdr_dir / "rdr-008-companion-note-one.md").write_text(_fm("status: companion-note"), encoding="utf-8")
    (rdr_dir / "rdr-009-frozen-one.md").write_text(_fm("status: frozen"), encoding="utf-8")
    (rdr_dir / "rdr-010-frozen-pending-one.md").write_text(
        _fm("status: frozen-pending-question-set"), encoding="utf-8"
    )
    (rdr_dir / "rdr-011-complete-one.md").write_text(_fm("status: complete"), encoding="utf-8")

    # -- revised-after-implementation: status KEPT (closed) + kind: companion
    # Misnamed like the real rdr137 file -- filename has no hyphen after "rdr".
    (rdr_dir / "rdr012-revised-deliverable.md").write_text(
        _fm("status: revised-after-implementation"), encoding="utf-8"
    )

    # -- an unmapped 13th-family status (mirrors the real "frozen-before-arms"
    # drift discovered on disk after the plan's census was taken): must be
    # reported, never guessed into a bucket, and the file left untouched.
    (rdr_dir / "rdr-013-unmapped-one.md").write_text(_fm("status: frozen-before-arms"), encoding="utf-8")

    # -- post-mortem/ subdir: must be excluded by the non-recursive sweep ----
    pm_dir = rdr_dir / "post-mortem"
    pm_dir.mkdir()
    (pm_dir / "001-postmortem.md").write_text(_fm("status: scrapped"), encoding="utf-8")

    # -- README index: bare + every real decorated shape ---------------------
    readme = (
        README_HEADER
        + "| [RDR-001](rdr-001-draft-one.md) | Draft One | Feature | Draft | 2026-05-01 |\n"
        + "| [RDR-004](rdr-004-closed-one.md) | Closed One | Feature | Closed (implemented) | 2026-05-01 |\n"
        + "| [RDR-005](rdr-005-superseded-one.md) | Superseded One | Feature | Superseded by RDR-108 | 2026-05-01 |\n"
        + "| [RDR-007](rdr-007-scrapped-one.md) | Retired One | Feature | **Scrapped 2026-05-19** | 2026-05-01 |\n"
        + "| [RDR-014](rdr-014-missing.md) | Ghost Row | Feature | Closed | 2026-05-01 |\n"
    )
    (rdr_dir / "README.md").write_text(readme, encoding="utf-8")

    return rdr_dir


# ---------------------------------------------------------------------------
# Leg 1 — frontmatter transforms
# ---------------------------------------------------------------------------


def test_dry_run_writes_nothing(mod, rdr_tree: Path) -> None:
    before = (rdr_tree / "rdr-007-scrapped-one.md").read_text(encoding="utf-8")
    before_readme = (rdr_tree / "README.md").read_text(encoding="utf-8")

    file_results = mod.compute_file_results(rdr_tree, apply=False)

    assert (rdr_tree / "rdr-007-scrapped-one.md").read_text(encoding="utf-8") == before
    assert (rdr_tree / "README.md").read_text(encoding="utf-8") == before_readme
    # the computation still reports what WOULD change
    scrapped = next(r for r in file_results if r.path.name == "rdr-007-scrapped-one.md")
    assert scrapped.old_status == "scrapped"
    assert scrapped.outcome.new_status == "abandoned"
    assert scrapped.changed is True


def test_canonical_statuses_pass_through_unchanged(mod, rdr_tree: Path) -> None:
    file_results = mod.compute_file_results(rdr_tree, apply=True)
    for name, status in [
        ("rdr-001-draft-one.md", "draft"),
        ("rdr-002-accepted-one.md", "accepted"),
        ("rdr-003-deferred-one.md", "deferred"),
        ("rdr-004-closed-one.md", "closed"),
        ("rdr-005-superseded-one.md", "superseded"),
        ("rdr-006-abandoned-one.md", "abandoned"),
    ]:
        r = next(x for x in file_results if x.path.name == name)
        assert r.outcome.new_status == status
        assert r.outcome.kind is None
        assert r.changed is False
        text = (rdr_tree / name).read_text(encoding="utf-8")
        assert f"status: {status}" in text
        assert "kind:" not in text


def test_scrapped_retires_to_abandoned_on_disk(mod, rdr_tree: Path) -> None:
    mod.compute_file_results(rdr_tree, apply=True)
    text = (rdr_tree / "rdr-007-scrapped-one.md").read_text(encoding="utf-8")
    assert "status: abandoned" in text
    assert "scrapped" not in text


@pytest.mark.parametrize(
    "filename",
    [
        "rdr-008-companion-note-one.md",
        "rdr-009-frozen-one.md",
        "rdr-010-frozen-pending-one.md",
        "rdr-011-complete-one.md",
    ],
)
def test_companion_statuses_lose_status_gain_kind(mod, rdr_tree: Path, filename: str) -> None:
    mod.compute_file_results(rdr_tree, apply=True)
    text = (rdr_tree / filename).read_text(encoding="utf-8")
    assert "kind: companion" in text
    assert "status:" not in text


def test_revised_after_implementation_keeps_closed_and_gains_kind(mod, rdr_tree: Path) -> None:
    mod.compute_file_results(rdr_tree, apply=True)
    text = (rdr_tree / "rdr012-revised-deliverable.md").read_text(encoding="utf-8")
    assert "status: closed" in text
    assert "kind: companion" in text


def test_unmapped_status_is_reported_and_left_untouched(mod, rdr_tree: Path) -> None:
    before = (rdr_tree / "rdr-013-unmapped-one.md").read_text(encoding="utf-8")
    file_results = mod.compute_file_results(rdr_tree, apply=True)
    after = (rdr_tree / "rdr-013-unmapped-one.md").read_text(encoding="utf-8")
    assert after == before

    r = next(x for x in file_results if x.path.name == "rdr-013-unmapped-one.md")
    assert r.old_status == "frozen-before-arms"
    assert r.outcome is None


def test_post_mortem_subdir_is_never_touched(mod, rdr_tree: Path) -> None:
    pm_file = rdr_tree / "post-mortem" / "001-postmortem.md"
    before = pm_file.read_text(encoding="utf-8")
    file_results = mod.compute_file_results(rdr_tree, apply=True)
    assert pm_file.read_text(encoding="utf-8") == before
    assert all("post-mortem" not in str(r.path) for r in file_results)


def test_agents_and_readme_excluded_from_file_sweep(mod, rdr_tree: Path) -> None:
    file_results = mod.compute_file_results(rdr_tree, apply=False)
    names = {r.path.name for r in file_results}
    assert "AGENTS.md" not in names
    assert "README.md" not in names


# ---------------------------------------------------------------------------
# Leg 1 — README decorated-row rewriter
# ---------------------------------------------------------------------------


def test_readme_decorated_rows_rewritten_preserving_decoration_and_case(mod, rdr_tree: Path) -> None:
    mod.main(["--rdr-dir", str(rdr_tree), "--leg", "files", "--apply"])
    readme = (rdr_tree / "README.md").read_text(encoding="utf-8")

    # scrapped -> abandoned, bold + date decoration preserved verbatim
    assert "**Abandoned 2026-05-19**" in readme
    assert "Scrapped" not in readme

    # unchanged decorated cells stay byte-identical (value didn't change)
    assert "Closed (implemented)" in readme
    assert "Superseded by RDR-108" in readme  # successor id preserved

    # bare row untouched
    assert "| Draft | 2026-05-01 |" in readme


def test_readme_row_rewrite_is_pure_and_shape_aware(mod) -> None:
    line = "| [RDR-007](rdr-007-scrapped-one.md) | Scrapped One | Feature | **Scrapped 2026-05-19** (superseded by RDR-120) | 2026-05-01 |"
    new_line, result = mod.rewrite_readme_row(line)
    assert "**Abandoned 2026-05-19** (superseded by RDR-120)" in new_line
    assert result.status_word == "scrapped"
    assert result.mapped_word == "abandoned"
    # only the status cell changed
    assert new_line.split("|")[1:4] == line.split("|")[1:4]


# ---------------------------------------------------------------------------
# Census
# ---------------------------------------------------------------------------


def test_census_reports_no_readme_row_files_and_nonzero_residual(mod, rdr_tree: Path) -> None:
    file_results = mod.compute_file_results(rdr_tree, apply=False)
    readme_text = (rdr_tree / "README.md").read_text(encoding="utf-8")
    _, readme_results = mod.rewrite_readme_text(readme_text)
    before = mod.build_census(file_results, readme_results, after=False)

    # 13 statused files in the fixture; only 4 have README rows (001, 004, 005, 007)
    statused = [r for r in file_results if r.old_status is not None]
    assert len(statused) == 13
    assert set(before.no_readme_row) == {
        "rdr-002-accepted-one.md",
        "rdr-003-deferred-one.md",
        "rdr-006-abandoned-one.md",
        "rdr-008-companion-note-one.md",
        "rdr-009-frozen-one.md",
        "rdr-010-frozen-pending-one.md",
        "rdr-011-complete-one.md",
        "rdr012-revised-deliverable.md",
        "rdr-013-unmapped-one.md",
    }
    # residual must be reported, not assumed zero: README references a file
    # (rdr-014-missing.md) that doesn't exist on disk at all.
    assert before.readme_row_count == 5


def test_census_md_is_written(mod, rdr_tree: Path, tmp_path: Path) -> None:
    out = tmp_path / "status-census.md"
    rc = mod.main(["--rdr-dir", str(rdr_tree), "--leg", "files", "--census-out", str(out)])
    assert rc == 0
    text = out.read_text(encoding="utf-8")
    assert "## Before" in text
    assert "## After" in text
    assert "frozen-before-arms" in text  # unmapped status surfaced, not swallowed


def test_main_files_leg_dry_run_writes_nothing_by_default(mod, rdr_tree: Path, tmp_path: Path) -> None:
    before = (rdr_tree / "rdr-007-scrapped-one.md").read_text(encoding="utf-8")
    out = tmp_path / "status-census.md"
    mod.main(["--rdr-dir", str(rdr_tree), "--leg", "files", "--census-out", str(out)])
    assert (rdr_tree / "rdr-007-scrapped-one.md").read_text(encoding="utf-8") == before


def test_main_files_leg_apply_writes(mod, rdr_tree: Path, tmp_path: Path) -> None:
    out = tmp_path / "status-census.md"
    mod.main(["--rdr-dir", str(rdr_tree), "--leg", "files", "--apply", "--census-out", str(out)])
    text = (rdr_tree / "rdr-007-scrapped-one.md").read_text(encoding="utf-8")
    assert "status: abandoned" in text


# ---------------------------------------------------------------------------
# Leg 2 — T2 (fake client; NEVER touches a real store)
# ---------------------------------------------------------------------------


class FakeT2Client:
    """Captures the FULL put() call (project/title/content/tags/ttl) so
    tests can assert ttl fidelity, not just content."""

    def __init__(self, records: dict[str, dict[str, Any]]) -> None:
        self._records = records
        self.put_calls: list[dict[str, Any]] = []

    def get_all(self, project: str) -> list[dict[str, Any]]:
        return [dict(v, title=k) for k, v in self._records.items()]

    def put(self, project: str, title: str, content: str, tags: str = "", ttl: int | None = None) -> int:
        self.put_calls.append(
            {"project": project, "title": title, "content": content, "tags": tags, "ttl": ttl}
        )
        self._records[title]["content"] = content
        return 1


def test_t2_leg_dry_run_prints_diff_and_writes_nothing(mod, rdr_tree: Path) -> None:
    file_results = mod.compute_file_results(rdr_tree, apply=False)
    client = FakeT2Client({
        "007": {"content": "status: scrapped\n", "tags": "rdr", "ttl": None},
    })
    report = mod.run_t2_leg(client, "nexus_rdr", file_results, apply=False)
    assert len(report.diffs) == 1
    assert report.diffs[0].old_status == "scrapped"
    assert report.diffs[0].new_status == "abandoned"
    assert report.diffs[0].reason == "migrate"
    assert report.drift == []
    assert client.put_calls == []  # dry-run never writes


def test_t2_leg_apply_writes_mapped_status(mod, rdr_tree: Path) -> None:
    file_results = mod.compute_file_results(rdr_tree, apply=False)
    client = FakeT2Client({
        "007": {"content": "status: scrapped\n", "tags": "rdr", "ttl": None},
    })
    mod.run_t2_leg(client, "nexus_rdr", file_results, apply=True)
    assert len(client.put_calls) == 1
    call = client.put_calls[0]
    assert call["title"] == "007"
    assert "status: abandoned" in call["content"]


def test_set_status_in_content_inserts_when_no_existing_status_line(mod) -> None:
    """``set_status_in_content`` must not silently no-op when there is no
    ``status:`` line to replace -- a caller relying on its return value
    (e.g. a future direct migrate path) must see the status actually land."""
    result = mod.set_status_in_content("title: x\nno status line here\n", "draft")
    assert mod.extract_status_from_content(result) == "draft"


def test_t2_leg_no_status_line_is_drift_not_migrate(mod, rdr_tree: Path) -> None:
    """T2 [24031] CRITICAL fix: a T2 record with NO status line at all must
    propose nothing under the vocabulary mapping — it's a drift/backfill
    case (rdr_hook / nexus-e19sa scope), reported separately, never applied."""
    file_results = mod.compute_file_results(rdr_tree, apply=False)
    client = FakeT2Client({
        "001": {"content": "title: x\nno status line here\n", "tags": "rdr", "ttl": None},
    })
    report = mod.run_t2_leg(client, "nexus_rdr", file_results, apply=True)
    assert report.diffs == []
    assert len(report.drift) == 1
    assert report.drift[0].old_status is None
    assert report.drift[0].new_status == "draft"
    assert report.drift[0].reason == "drift-report-only"
    assert client.put_calls == []  # drift is never applied


def test_t2_leg_already_canonical_status_is_drift_not_migrate(mod, rdr_tree: Path) -> None:
    """T2 [24031] CRITICAL fix, RDR-090-shaped case: T2's own status is
    ALREADY in the six-value domain (here 'accepted') but disagrees with the
    file's target ('draft' for file 001) -- this is drift reconciliation,
    not a vocabulary migration, and must never be proposed for --apply."""
    file_results = mod.compute_file_results(rdr_tree, apply=False)
    client = FakeT2Client({
        "001": {"content": "status: accepted\n", "tags": "rdr", "ttl": None},
    })
    report = mod.run_t2_leg(client, "nexus_rdr", file_results, apply=True)
    assert report.diffs == []
    assert len(report.drift) == 1
    assert report.drift[0].old_status == "accepted"
    assert report.drift[0].new_status == "draft"
    assert report.drift[0].reason == "drift-report-only"
    assert client.put_calls == []


def test_t2_leg_canonical_status_agreeing_with_file_proposes_nothing(mod, rdr_tree: Path) -> None:
    file_results = mod.compute_file_results(rdr_tree, apply=False)
    client = FakeT2Client({
        "001": {"content": "status: draft\n", "tags": "rdr", "ttl": None},
    })
    report = mod.run_t2_leg(client, "nexus_rdr", file_results, apply=True)
    assert report.diffs == []
    assert report.drift == []
    assert client.put_calls == []


def test_t2_leg_clears_status_for_companion_outcome_records(mod, rdr_tree: Path) -> None:
    file_results = mod.compute_file_results(rdr_tree, apply=False)
    client = FakeT2Client({
        "009": {"content": "title: x\nstatus: frozen\n", "tags": "rdr", "ttl": None},
    })
    report = mod.run_t2_leg(client, "nexus_rdr", file_results, apply=True)
    assert report.diffs[0].old_status == "frozen"
    assert report.diffs[0].new_status is None
    assert report.diffs[0].reason == "clear-companion"
    call = client.put_calls[0]
    assert "status:" not in call["content"]


def test_t2_leg_reports_disk_only_and_t2_only(mod, rdr_tree: Path) -> None:
    file_results = mod.compute_file_results(rdr_tree, apply=False)
    # "001" has a disk file (draft) but no T2 record; "999" has a T2 record
    # with no matching disk file.
    client = FakeT2Client({
        "999": {"content": "status: draft\n", "tags": "rdr", "ttl": None},
    })
    report = mod.run_t2_leg(client, "nexus_rdr", file_results, apply=False)
    assert "001" in report.disk_only
    assert "999" in report.t2_only
    assert client.put_calls == []


def test_t2_leg_reports_ambiguous_number_collisions(mod, rdr_tree: Path) -> None:
    # Two disk files share number "012": the fixture's rdr012 companion
    # deliverable, plus a synthetic second file with the same digits.
    extra = rdr_tree / "rdr-012-second-file.md"
    extra.write_text(_fm("status: draft"), encoding="utf-8")
    file_results = mod.compute_file_results(rdr_tree, apply=False)

    client = FakeT2Client({"012": {"content": "status: closed\n", "tags": "rdr", "ttl": None}})
    report = mod.run_t2_leg(client, "nexus_rdr", file_results, apply=True)
    assert "012" in report.ambiguous
    assert client.put_calls == []  # ambiguous numbers are never guessed


# ---------------------------------------------------------------------------
# Leg 2 — ttl fidelity (code review [24032] finding (b))
# ---------------------------------------------------------------------------


def test_t2_leg_apply_preserves_permanent_ttl(mod, rdr_tree: Path) -> None:
    file_results = mod.compute_file_results(rdr_tree, apply=False)
    client = FakeT2Client({
        "007": {"content": "status: scrapped\n", "tags": "rdr", "ttl": None},
    })
    mod.run_t2_leg(client, "nexus_rdr", file_results, apply=True)
    assert client.put_calls[0]["ttl"] is None  # permanent stays permanent, not reset to a number


def test_t2_leg_apply_preserves_numeric_ttl(mod, rdr_tree: Path) -> None:
    file_results = mod.compute_file_results(rdr_tree, apply=False)
    client = FakeT2Client({
        "007": {"content": "status: scrapped\n", "tags": "rdr", "ttl": 90},
    })
    mod.run_t2_leg(client, "nexus_rdr", file_results, apply=True)
    assert client.put_calls[0]["ttl"] == 90  # the record's own policy, not silently permanented


def test_t2_leg_apply_skips_write_when_ttl_field_absent(mod, rdr_tree: Path) -> None:
    """A record with no `ttl` key at all is indistinguishable, via plain
    dict.get(), between "explicitly permanent" and "field just missing" --
    refuse to guess rather than silently mutate the expiry policy either way."""
    file_results = mod.compute_file_results(rdr_tree, apply=False)
    client = FakeT2Client({
        "007": {"content": "status: scrapped\n", "tags": "rdr"},  # no "ttl" key
    })
    report = mod.run_t2_leg(client, "nexus_rdr", file_results, apply=True)
    assert len(report.diffs) == 1  # still proposed...
    assert client.put_calls == []  # ...but never written
    assert report.ttl_unknown == ["007"]


# ---------------------------------------------------------------------------
# Leg 1 — pre-existing non-companion `kind:` line preserved
# (substantive-critic [24031] SIGNIFICANT finding)
# ---------------------------------------------------------------------------


def test_pre_existing_non_companion_kind_line_is_preserved(mod, tmp_path: Path) -> None:
    rdr_dir = tmp_path / "docs" / "rdr"
    rdr_dir.mkdir(parents=True)
    f = rdr_dir / "rdr-099-native-kind.md"
    f.write_text(
        "---\n"
        'title: "RDR-099"\n'
        "kind: rdr-native\n"
        "status: draft\n"
        "---\n\n# Body\n",
        encoding="utf-8",
    )
    file_results = mod.compute_file_results(rdr_dir, apply=True)
    text = f.read_text(encoding="utf-8")
    assert "kind: rdr-native" in text
    assert "status: draft" in text
    # values survive; the rewriter may still normalise line ORDER (kind:
    # always lands at the status line's position) even when neither value
    # itself changed -- that's a cosmetic reflow, not data loss.
    r = next(x for x in file_results if x.path.name == "rdr-099-native-kind.md")
    assert r.old_status == "draft"
    assert r.outcome.new_status == "draft"


def test_pre_existing_kind_line_overwritten_when_outcome_is_companion(mod, tmp_path: Path) -> None:
    rdr_dir = tmp_path / "docs" / "rdr"
    rdr_dir.mkdir(parents=True)
    f = rdr_dir / "rdr-098-native-kind-companion.md"
    f.write_text(
        "---\n"
        'title: "RDR-098"\n'
        "kind: rdr-native\n"
        "status: frozen\n"
        "---\n\n# Body\n",
        encoding="utf-8",
    )
    mod.compute_file_results(rdr_dir, apply=True)
    text = f.read_text(encoding="utf-8")
    assert "kind: companion" in text
    assert "kind: rdr-native" not in text
    assert "status:" not in text


def test_pre_existing_kind_line_after_status_line_is_preserved(mod, tmp_path: Path) -> None:
    """kind: appearing AFTER status: in the source file must survive too --
    the pre-scan must not depend on ordering."""
    rdr_dir = tmp_path / "docs" / "rdr"
    rdr_dir.mkdir(parents=True)
    f = rdr_dir / "rdr-097-kind-after-status.md"
    f.write_text(
        "---\ntitle: \"RDR-097\"\nstatus: draft\nkind: rdr-native\n---\n\n# Body\n",
        encoding="utf-8",
    )
    mod.compute_file_results(rdr_dir, apply=True)
    text = f.read_text(encoding="utf-8")
    assert "kind: rdr-native" in text
    assert "status: draft" in text


# ---------------------------------------------------------------------------
# README — loosened row-prefix regex (code review [24032] finding (a))
# ---------------------------------------------------------------------------


def test_readme_row_with_labeled_rdr_id_is_matched(mod) -> None:
    """`[RDR-079 P5](rdr-079-calibration.md)` -- a labeled sub-RDR link, the
    real shape found in docs/rdr/README.md -- must not be mis-reported as
    having no README row: the exact-`[RDR-NNN]`-only prefix regex missed it
    entirely."""
    line = "| [RDR-079 P5](rdr-079-calibration.md) | Calibration | Feature | Closed | 2026-04-15 |"
    new_line, result = mod.rewrite_readme_row(line)
    assert result is not None
    assert result.filename == "rdr-079-calibration.md"
    assert result.rdr_id == "079"
    assert new_line == line  # closed -> closed is a no-op rewrite


def test_census_no_readme_row_excludes_labeled_link(mod, tmp_path: Path) -> None:
    rdr_dir = tmp_path / "docs" / "rdr"
    rdr_dir.mkdir(parents=True)
    (rdr_dir / "rdr-079-calibration.md").write_text(_fm("status: closed"), encoding="utf-8")
    (rdr_dir / "README.md").write_text(
        README_HEADER
        + "| [RDR-079 P5](rdr-079-calibration.md) | Calibration | Feature | Closed | 2026-04-15 |\n",
        encoding="utf-8",
    )
    file_results = mod.compute_file_results(rdr_dir, apply=False)
    readme_text = (rdr_dir / "README.md").read_text(encoding="utf-8")
    _, readme_results = mod.rewrite_readme_text(readme_text)
    census = mod.build_census(file_results, readme_results, after=False)
    assert "rdr-079-calibration.md" not in census.no_readme_row
