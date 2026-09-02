# SPDX-License-Identifier: AGPL-3.0-or-later
"""TDD tests for the RDR-201 P1.8 closed-vocabulary audit (nexus-j9z30.8).

``nx rdr preamble rdr-audit`` must report any ``docs/rdr/*.md`` frontmatter
``status:`` value that falls outside the packaged lifecycle table's
``status`` domain as a FINDING (naming the file and the value), and must
SKIP files carrying ``kind: companion`` (they carry no lifecycle status at
all) -- counted separately, never reported as a finding. T2 project
``<repo>_rdr`` statuses are a SEPARATE, clearly labelled census line, never
merged into the file findings (the reconcile hook that would keep the two
surfaces in sync never runs in this repo -- nexus-e19sa -- so merging them
would report ~200 T2 records as perpetually stale).

Scan scope (RDR-201 P1.8 task corrections, T2
nexus/plan-rdr-201-closed-vocabularies.md [23998] residual 7 / [24001]):
``docs/rdr/*.md`` non-recursive, excluding ``AGENTS.md``/``README.md`` by
filename (case-insensitive). ``docs/rdr/post-mortem/`` is a separate
document set carrying its own status values and must never surface here --
proven by the non-recursive glob, not a special-cased exclusion.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

import nexus.commands.rdr as rdr_mod
from nexus.commands.rdr import (
    _rdr_audit_status_findings,
    _t2_rdr_status_census,
    rdr,
)


def _runner() -> CliRunner:
    return CliRunner()


def _write(rdr_dir: Path, filename: str, frontmatter: dict[str, str], body: str = "") -> Path:
    fm_lines = ["---"]
    for k, v in frontmatter.items():
        fm_lines.append(f"{k}: {v}")
    fm_lines.append("---")
    fm_lines.append("")
    if body:
        fm_lines.append(body)
    p = rdr_dir / filename
    p.write_text("\n".join(fm_lines) + "\n", encoding="utf-8")
    return p


_STATUS_DOMAIN = frozenset(
    {"draft", "accepted", "deferred", "closed", "superseded", "abandoned"}
)


# ---------------------------------------------------------------------------
# _rdr_audit_status_findings — pure scan logic
# ---------------------------------------------------------------------------


def test_out_of_vocabulary_status_reported_as_finding(tmp_path: Path):
    rdr_dir = tmp_path / "docs" / "rdr"
    rdr_dir.mkdir(parents=True)
    _write(rdr_dir, "rdr-200-phase1b-prereg.md", {"status": "frozen-before-arms"})

    findings, companions, scanned = _rdr_audit_status_findings(rdr_dir, _STATUS_DOMAIN)

    assert findings == [("rdr-200-phase1b-prereg.md", "frozen-before-arms")]
    assert companions == 0
    assert scanned == 1


def test_in_vocabulary_statuses_produce_no_findings(tmp_path: Path):
    rdr_dir = tmp_path / "docs" / "rdr"
    rdr_dir.mkdir(parents=True)
    for i, status in enumerate(sorted(_STATUS_DOMAIN)):
        _write(rdr_dir, f"rdr-{i}-x.md", {"status": status})

    findings, companions, scanned = _rdr_audit_status_findings(rdr_dir, _STATUS_DOMAIN)

    assert findings == []
    assert companions == 0
    assert scanned == len(_STATUS_DOMAIN)


def test_kind_companion_file_skipped_not_a_finding(tmp_path: Path):
    """A companion file carries no lifecycle status; even one whose leftover
    status: value would be out-of-vocabulary must be SKIPPED, not flagged."""
    rdr_dir = tmp_path / "docs" / "rdr"
    rdr_dir.mkdir(parents=True)
    _write(
        rdr_dir,
        "rdr-200-frozen-companion.md",
        {"kind": "companion", "status": "frozen-pending-question-set"},
    )
    # revised-after-implementation shape: kind: companion AND a real status
    _write(
        rdr_dir,
        "rdr-201-revised.md",
        {"kind": "companion", "status": "closed"},
    )

    findings, companions, scanned = _rdr_audit_status_findings(rdr_dir, _STATUS_DOMAIN)

    assert findings == []
    assert companions == 2
    assert scanned == 2


def test_companion_file_with_no_status_field_skipped(tmp_path: Path):
    rdr_dir = tmp_path / "docs" / "rdr"
    rdr_dir.mkdir(parents=True)
    _write(rdr_dir, "rdr-202-companion.md", {"kind": "companion", "title": '"x"'})

    findings, companions, scanned = _rdr_audit_status_findings(rdr_dir, _STATUS_DOMAIN)

    assert findings == []
    assert companions == 1
    assert scanned == 1


def test_agents_and_readme_excluded_by_name_case_insensitive(tmp_path: Path):
    rdr_dir = tmp_path / "docs" / "rdr"
    rdr_dir.mkdir(parents=True)
    _write(rdr_dir, "AGENTS.md", {"status": "bogus"})
    _write(rdr_dir, "README.md", {"status": "bogus"})
    _write(rdr_dir, "agents.md", {"status": "bogus"})

    findings, companions, scanned = _rdr_audit_status_findings(rdr_dir, _STATUS_DOMAIN)

    assert findings == []
    assert companions == 0
    assert scanned == 0


def test_post_mortem_subdirectory_not_scanned_non_recursive(tmp_path: Path):
    rdr_dir = tmp_path / "docs" / "rdr"
    postmortem = rdr_dir / "post-mortem"
    postmortem.mkdir(parents=True)
    _write(postmortem, "rdr-300-post.md", {"status": "bogus-postmortem-status"})
    _write(rdr_dir, "rdr-301-real.md", {"status": "draft"})

    findings, companions, scanned = _rdr_audit_status_findings(rdr_dir, _STATUS_DOMAIN)

    assert findings == []
    assert scanned == 1  # only rdr-301-real.md; post-mortem/ untouched


def test_file_with_no_frontmatter_is_neither_finding_nor_companion(tmp_path: Path):
    rdr_dir = tmp_path / "docs" / "rdr"
    rdr_dir.mkdir(parents=True)
    (rdr_dir / "status-census-2026-09-01.md").write_text(
        "# Census\n\nNo frontmatter here.\n", encoding="utf-8"
    )

    findings, companions, scanned = _rdr_audit_status_findings(rdr_dir, _STATUS_DOMAIN)

    assert findings == []
    assert companions == 0
    assert scanned == 1  # counted as scanned (it IS a non-excluded .md file)


def test_findings_sorted_by_filename(tmp_path: Path):
    rdr_dir = tmp_path / "docs" / "rdr"
    rdr_dir.mkdir(parents=True)
    _write(rdr_dir, "rdr-999-z.md", {"status": "bogus-z"})
    _write(rdr_dir, "rdr-001-a.md", {"status": "bogus-a"})

    findings, _companions, _scanned = _rdr_audit_status_findings(rdr_dir, _STATUS_DOMAIN)

    assert [f[0] for f in findings] == ["rdr-001-a.md", "rdr-999-z.md"]


def test_real_docs_rdr_tree_is_clean_and_the_scan_is_not_vacuous():
    """Non-vacuity anchor (nexus-moht0 doctrine): run the scan against THIS
    repo's real docs/rdr/ tree. After the RDR-201 Phase 1 sweep plus the
    Phase 1 gate's conversion of the two late RDR-200 sub-documents
    (rdr-200-phase1b-prereg.md, rdr-200-phase1b-gate-result.md) to
    ``kind: companion``, the tree carries zero out-of-vocabulary statuses;
    the scan is proved non-vacuous by the scanned and companion counts, and
    the fixture-tree tests above prove it reports a planted defect."""
    repo_root = Path(__file__).resolve().parent.parent
    rdr_dir = repo_root / "docs" / "rdr"
    if not rdr_dir.is_dir():  # pragma: no cover — defensive, not expected in this repo
        pytest.skip("docs/rdr/ not present in this checkout")

    findings, companions, scanned = _rdr_audit_status_findings(rdr_dir, _STATUS_DOMAIN)

    assert scanned > 200, "the sweep examined nothing (nexus-moht0 vacuous-gate doctrine)"
    assert companions >= 9, f"expected the nine known companion documents, saw {companions}"
    assert findings == [], findings


# ---------------------------------------------------------------------------
# _t2_rdr_status_census — injectable T2 facade
# ---------------------------------------------------------------------------


class _FakeT2CensusClient:
    """Test double matching the ``get_all(project=...) -> list[dict]``
    contract ``_preamble_get_rdrs_from_t2`` already reads through the same
    ``T2Database`` facade."""

    def __init__(
        self,
        entries: list[dict[str, Any]] | None = None,
        *,
        raise_on_get_all: Exception | None = None,
    ) -> None:
        self._entries = entries or []
        self._raise_on_get_all = raise_on_get_all

    def __enter__(self) -> "_FakeT2CensusClient":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def get_all(self, project: str | None = None) -> list[dict[str, Any]]:
        if self._raise_on_get_all is not None:
            raise self._raise_on_get_all
        return self._entries


def test_t2_census_counts_statuses_by_project(monkeypatch: pytest.MonkeyPatch):
    fake = _FakeT2CensusClient(
        entries=[
            {"title": "1", "content": "status: closed\n"},
            {"title": "2", "content": "status: closed\n"},
            {"title": "3", "content": "status: draft\n"},
            {"title": "not-numeric", "content": "status: draft\n"},  # excluded
        ]
    )
    monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)

    counts, ambiguous, error = _t2_rdr_status_census("nexus")

    assert error is None
    assert counts == Counter({"closed": 2, "draft": 1})
    assert ambiguous == []


def test_t2_census_accepts_rdr_prefixed_titles(monkeypatch: pytest.MonkeyPatch):
    """Bare digits AND ``RDR-NNN`` titles both count -- fix round (code
    review [24047] finding 2): the original ``^\\d+$``-only match silently
    dropped every ``RDR-NNN``-titled record (~42 on the live project)."""
    fake = _FakeT2CensusClient(
        entries=[
            {"title": "1", "content": "status: closed\n"},
            {"title": "RDR-2", "content": "status: draft\n"},
            {"title": "RDR-3", "content": "status: accepted\n"},
        ]
    )
    monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)

    counts, ambiguous, error = _t2_rdr_status_census("nexus")

    assert error is None
    assert counts == Counter({"closed": 1, "draft": 1, "accepted": 1})
    assert ambiguous == []


def test_t2_census_same_number_both_shapes_same_status_counts_once(
    monkeypatch: pytest.MonkeyPatch,
):
    fake = _FakeT2CensusClient(
        entries=[
            {"title": "42", "content": "status: closed\n"},
            {"title": "RDR-42", "content": "status: closed\n"},
        ]
    )
    monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)

    counts, ambiguous, error = _t2_rdr_status_census("nexus")

    assert error is None
    assert counts == Counter({"closed": 1})
    assert ambiguous == []


def test_t2_census_same_number_both_shapes_differing_status_is_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
):
    fake = _FakeT2CensusClient(
        entries=[
            {"title": "42", "content": "status: closed\n"},
            {"title": "RDR-42", "content": "status: draft\n"},
        ]
    )
    monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)

    counts, ambiguous, error = _t2_rdr_status_census("nexus")

    assert error is None
    # Neither shape's status is silently picked into the counts.
    assert counts == Counter()
    assert len(ambiguous) == 1
    assert "42" in ambiguous[0]
    assert "closed" in ambiguous[0]
    assert "draft" in ambiguous[0]


def test_t2_census_unreachable_reports_reason_never_raises(monkeypatch: pytest.MonkeyPatch):
    fake = _FakeT2CensusClient(raise_on_get_all=ConnectionError("boom"))
    monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)

    counts, ambiguous, error = _t2_rdr_status_census("nexus")

    assert counts == Counter()
    assert ambiguous == []
    assert error is not None
    assert "T2 unreachable" in error
    assert "boom" in error


# ---------------------------------------------------------------------------
# CLI-level: nx rdr preamble rdr-audit -- <target>
# ---------------------------------------------------------------------------


def test_preamble_rdr_audit_prints_finding_and_skips_companion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    roots = tmp_path / "roots"
    project_dir = roots / "demo-project"
    rdr_dir = project_dir / "docs" / "rdr"
    rdr_dir.mkdir(parents=True)
    _write(rdr_dir, "rdr-1-bad.md", {"status": "bogus-status"})
    _write(rdr_dir, "rdr-2-companion.md", {"kind": "companion", "status": "closed"})
    _write(rdr_dir, "rdr-3-ok.md", {"status": "draft"})

    monkeypatch.setenv("NEXUS_PROJECT_ROOTS", str(roots))
    monkeypatch.setattr(
        rdr_mod, "_t2_client_factory", lambda: _FakeT2CensusClient(entries=[])
    )

    result = _runner().invoke(rdr, ["preamble", "rdr-audit", "--", "demo-project"])

    assert result.exit_code == 0, result.output
    assert "rdr-1-bad.md" in result.output
    assert "bogus-status" in result.output
    assert "rdr-2-companion.md" not in result.output
    assert "rdr-3-ok.md" not in result.output
    assert "T2" in result.output and "demo-project_rdr" in result.output


def test_preamble_rdr_audit_t2_unreachable_reported_never_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    roots = tmp_path / "roots"
    project_dir = roots / "demo-project"
    rdr_dir = project_dir / "docs" / "rdr"
    rdr_dir.mkdir(parents=True)
    _write(rdr_dir, "rdr-1-ok.md", {"status": "draft"})

    monkeypatch.setenv("NEXUS_PROJECT_ROOTS", str(roots))
    monkeypatch.setattr(
        rdr_mod,
        "_t2_client_factory",
        lambda: _FakeT2CensusClient(raise_on_get_all=ConnectionError("down")),
    )

    result = _runner().invoke(rdr, ["preamble", "rdr-audit", "--", "demo-project"])

    assert result.exit_code == 0, result.output
    assert "T2 unreachable" in result.output


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
