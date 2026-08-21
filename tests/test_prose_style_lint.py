# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ptwm2: prose style gate over the project's written surfaces.

Two tolerance classes (rescoped after the fable design review, T2
ptwm2-design-critique-fable-2026-08-21):

- **Zero**: the RDR templates, ``docs/writing-style.md``, and the
  ``[Unreleased]`` CHANGELOG section. Surfaces authored now, under the
  spec.
- **Ratchet**: docs/ and blog/ trees, README, and ACTIVE RDRs (draft /
  accepted / deferred), against ``docs/.prose-baseline.json``. A file may
  never exceed its recorded count; a count that drops must be written
  back (stale-high fails); new files start at 0. Active RDRs sit here,
  not at zero: mechanically rewriting the decision record was reviewed
  and reverted (T2 ptwm2-rdr-sweep-editorial-fable-2026-08-21), and a
  draft RDR must not red CI mid-draft. New RDR prose is enforced at gate
  time by ``nx rdr preamble rdr-gate`` (override: ``--skip-prose``).

Closed and superseded RDRs and shipped CHANGELOG sections are exempt:
rewriting them costs git-blame for no reader benefit (nexus-ibdl).
Regenerate the baseline with ``uv run python -m tests.test_prose_style_lint``.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from nexus.prose_lint import (
    check_against_baseline,
    lint_file,
    lint_text,
    load_baseline,
)

pytestmark = pytest.mark.lint

REPO = Path(__file__).resolve().parents[1]
BASELINE = REPO / "docs" / ".prose-baseline.json"
ACTIVE_STATUSES = {"draft", "accepted", "deferred"}
_STATUS = re.compile(r"^status:\s*([A-Za-z-]+)", re.MULTILINE)


def _rel(p: Path) -> str:
    return p.relative_to(REPO).as_posix()


def _rdr_status(p: Path) -> str | None:
    head = p.read_text(encoding="utf-8")[:2000]
    m = _STATUS.search(head)
    return m.group(1).lower() if m else None


def active_rdrs() -> list[Path]:
    return sorted(
        p for p in (REPO / "docs" / "rdr").glob("rdr-*.md")
        if _rdr_status(p) in ACTIVE_STATUSES
    )


def rdr_templates() -> list[Path]:
    return sorted((REPO / "conexus" / "resources" / "rdr").rglob("*.md"))


def ratchet_files() -> list[Path]:
    """docs/ tree (minus docs/rdr, handled by status, and the spec itself),
    docs/rdr's own AGENTS.md and README.md, ACTIVE RDRs, blog/ (minus
    ``.pulled.md`` mirrors of published posts), and README.md."""
    files = [
        p for p in (REPO / "docs").rglob("*.md")
        if "rdr" not in p.relative_to(REPO / "docs").parts and p.name != "writing-style.md"
    ]
    files += [REPO / "docs" / "rdr" / "AGENTS.md", REPO / "docs" / "rdr" / "README.md"]
    files += active_rdrs()
    files += [p for p in (REPO / "blog").rglob("*.md") if ".pulled." not in p.name]
    files.append(REPO / "README.md")
    return sorted(p for p in files if p.is_file())


def changelog_unreleased() -> str:
    text = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    m = re.search(r"^## \[Unreleased\].*?(?=^## \[)", text, re.MULTILINE | re.DOTALL)
    assert m, "CHANGELOG.md has no [Unreleased] section"
    return m.group(0)


# --- non-vacuity ---------------------------------------------------------


def test_gate_scans_a_real_population():
    n_active, n_tpl, n_ratchet = len(active_rdrs()), len(rdr_templates()), len(ratchet_files())
    assert n_active >= 5, f"only {n_active} active RDRs found; status regex drifted?"
    assert n_tpl >= 2, f"only {n_tpl} RDR templates found"
    assert n_ratchet >= 60, f"only {n_ratchet} ratchet files found"
    ratchet_rel = {_rel(p) for p in ratchet_files()}
    assert all(_rel(p) in ratchet_rel for p in active_rdrs()), (
        "active RDRs must ride the ratchet"
    )


# --- zero tolerance ------------------------------------------------------


@pytest.mark.parametrize("path", rdr_templates(), ids=lambda p: _rel(p))
def test_rdr_template_is_clean(path: Path):
    findings = lint_file(path)
    assert not findings, "\n".join(f.render(_rel(path)) for f in findings[:20])


def test_writing_style_spec_is_clean():
    path = REPO / "docs" / "writing-style.md"
    findings = lint_file(path)
    assert not findings, "\n".join(f.render(_rel(path)) for f in findings)


def test_changelog_unreleased_section_is_clean():
    findings = lint_text(changelog_unreleased())
    assert not findings, "\n".join(f.render("CHANGELOG.md[Unreleased]") for f in findings)


def test_zero_tolerance_surfaces_have_no_baseline_entry():
    base = load_baseline(BASELINE)
    zero = {_rel(p) for p in rdr_templates()}
    zero.add("docs/writing-style.md")
    leaked = sorted(zero & set(base))
    assert not leaked, f"baseline must not carry zero-tolerance files: {leaked}"


# --- ratchet -------------------------------------------------------------


def test_ratchet_surfaces_within_baseline():
    counts = {_rel(p): len(lint_file(p)) for p in ratchet_files()}
    problems = check_against_baseline(counts, load_baseline(BASELINE))
    assert not problems, (
        "\n".join(problems)
        + "\n\nregenerate: uv run python -m tests.test_prose_style_lint"
    )


if __name__ == "__main__":  # regenerate the ratchet baseline from this file's own file set
    from nexus.prose_lint import write_baseline

    import sys

    write_baseline(BASELINE, {_rel(p): len(lint_file(p)) for p in ratchet_files()})
    sys.stdout.write(f"wrote {BASELINE}\n")
