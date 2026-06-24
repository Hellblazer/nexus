# SPDX-License-Identifier: AGPL-3.0-or-later
"""TDD tests for ``nx rdr set-status`` — the code-enforced frontmatter flip.

Root-cause fix for the RDR accept/close *ledger-drift* class (RDR-165 / RDR-166,
2026-06-24): the accept step wrote T2 ``status: accepted`` but the RDR *file*
frontmatter was never flipped from ``draft`` because the flip was a soft,
agent-driven skill instruction that silently got skipped. ``rdr-close`` then
BLOCKED on the stale file status and required manual reconciliation.

This command makes the flip a single, deterministic, tested filesystem action
(no T2 dependency) that the accept/close skills call instead of editing the
frontmatter by hand. It rewrites the RDR file ``status:`` line (plus the
matching ``accepted_date`` / ``closed_date`` key) and the README index-row
status cell.
"""
from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from nexus.commands.rdr import rdr


def _runner() -> CliRunner:
    return CliRunner()


def _rdr_dir(tmp_path: Path) -> Path:
    d = tmp_path / "docs" / "rdr"
    d.mkdir(parents=True, exist_ok=True)
    return d


_RDR_BODY = """## Problem Statement

Some prose.

## Decision

A decision.
"""


def _write_rdr(rdr_dir: Path, num: int, status: str, extra_fm: str = "") -> Path:
    fm = (
        "---\n"
        f'title: "RDR-{num:03d} Example Title"\n'
        f"id: RDR-{num:03d}\n"
        "type: Architecture\n"
        f"status: {status}\n"
        "priority: high\n"
        "created: 2026-06-22\n"
        f"{extra_fm}"
        "---\n\n"
    )
    p = rdr_dir / f"rdr-{num:03d}-example-title.md"
    p.write_text(fm + _RDR_BODY, encoding="utf-8")
    return p


def _write_readme(rdr_dir: Path, num: int, status_cell: str) -> Path:
    readme = rdr_dir / "README.md"
    readme.write_text(
        "# RDR Index\n\n"
        "| RDR | Title | Type | Status | Date |\n"
        "|-----|-------|------|--------|------|\n"
        f"| [RDR-{num:03d}](rdr-{num:03d}-example-title.md) | RDR-{num:03d} Example "
        f"Title | Architecture | {status_cell} | 2026-06-22 |\n",
        encoding="utf-8",
    )
    return readme


def _invoke(rdr_dir: Path, *args: str):
    return _runner().invoke(
        rdr, ["set-status", *args, "--root", str(rdr_dir.parent.parent)]
    )


def test_draft_to_accepted_flips_file_and_adds_accepted_date(tmp_path):
    rdr_dir = _rdr_dir(tmp_path)
    f = _write_rdr(rdr_dir, 200, "draft")
    _write_readme(rdr_dir, 200, "Draft")

    res = _invoke(rdr_dir, "200", "accepted", "--date", "2026-06-24")
    assert res.exit_code == 0, res.output

    text = f.read_text()
    assert "status: accepted" in text
    assert "status: draft" not in text
    assert "accepted_date: 2026-06-24" in text
    # body preserved
    assert "## Problem Statement" in text
    assert "## Decision" in text


def test_accepted_to_closed_flips_file_and_adds_closed_date(tmp_path):
    rdr_dir = _rdr_dir(tmp_path)
    f = _write_rdr(rdr_dir, 201, "accepted", extra_fm="accepted_date: 2026-06-22\n")
    _write_readme(rdr_dir, 201, "Accepted")

    res = _invoke(rdr_dir, "201", "closed", "--date", "2026-06-24")
    assert res.exit_code == 0, res.output

    text = f.read_text()
    assert "status: closed" in text
    assert "closed_date: 2026-06-24" in text
    # accepted_date preserved, not duplicated
    assert text.count("accepted_date:") == 1
    assert "accepted_date: 2026-06-22" in text


def test_readme_status_cell_updated(tmp_path):
    rdr_dir = _rdr_dir(tmp_path)
    _write_rdr(rdr_dir, 202, "draft")
    readme = _write_readme(rdr_dir, 202, "Draft")

    res = _invoke(rdr_dir, "202", "closed", "--date", "2026-06-24")
    assert res.exit_code == 0, res.output

    row = [ln for ln in readme.read_text().splitlines() if "RDR-202" in ln][0]
    assert "| Closed |" in row
    assert "Draft" not in row


def test_unknown_id_errors_nonzero(tmp_path):
    rdr_dir = _rdr_dir(tmp_path)
    _write_rdr(rdr_dir, 203, "draft")
    res = _invoke(rdr_dir, "999", "closed", "--date", "2026-06-24")
    assert res.exit_code != 0


def test_unknown_status_errors_and_does_not_write(tmp_path):
    """A typo'd status must be rejected, not silently written to the file."""
    rdr_dir = _rdr_dir(tmp_path)
    f = _write_rdr(rdr_dir, 206, "draft")
    before = f.read_text()
    res = _invoke(rdr_dir, "206", "clsoed", "--date", "2026-06-24")
    assert res.exit_code != 0
    assert "unknown status" in res.output.lower()
    assert f.read_text() == before  # untouched


def test_idempotent_status_does_not_overwrite_existing_date(tmp_path):
    """Re-setting the current status must preserve the original date value."""
    rdr_dir = _rdr_dir(tmp_path)
    f = _write_rdr(rdr_dir, 207, "accepted", extra_fm="accepted_date: 2026-06-22\n")
    _write_readme(rdr_dir, 207, "Accepted")

    res = _invoke(rdr_dir, "207", "accepted", "--date", "2026-06-24")
    assert res.exit_code == 0, res.output
    text = f.read_text()
    assert "accepted_date: 2026-06-22" in text
    assert "2026-06-24" not in text


def test_status_key_not_duplicated_when_already_target(tmp_path):
    """Setting the status that's already present must not duplicate the key."""
    rdr_dir = _rdr_dir(tmp_path)
    f = _write_rdr(rdr_dir, 204, "accepted", extra_fm="accepted_date: 2026-06-22\n")
    _write_readme(rdr_dir, 204, "Accepted")

    res = _invoke(rdr_dir, "204", "accepted", "--date", "2026-06-24")
    assert res.exit_code == 0, res.output

    text = f.read_text()
    assert text.count("status:") == 1
    # idempotent: accepted_date not overwritten / duplicated
    assert text.count("accepted_date:") == 1


def test_body_with_horizontal_rule_is_preserved(tmp_path):
    """A '---' inside the body must not be mistaken for the frontmatter fence."""
    rdr_dir = _rdr_dir(tmp_path)
    f = rdr_dir / "rdr-205-example-title.md"
    f.write_text(
        "---\n"
        'title: "RDR-205 Example Title"\n'
        "id: RDR-205\n"
        "type: Architecture\n"
        "status: draft\n"
        "priority: high\n"
        "created: 2026-06-22\n"
        "---\n\n"
        "## Section A\n\nText.\n\n---\n\n## Section B\n\nMore text.\n",
        encoding="utf-8",
    )
    _write_readme(rdr_dir, 205, "Draft")

    res = _invoke(rdr_dir, "205", "accepted", "--date", "2026-06-24")
    assert res.exit_code == 0, res.output

    text = f.read_text()
    assert "status: accepted" in text
    assert "## Section A" in text
    assert "## Section B" in text
    # the body horizontal rule survives
    assert "\n---\n\n## Section B" in text
