# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Non-vacuity and falsification controls for ``tests/_lint_line_anchor.
py``'s ``resolve_anchor`` -- the shared content-addressed anchor
primitive nexus-vkpr3 introduced so a lint exemption survives an
insertion above the site it names. See that module's docstring for the
defect class and design.
"""
from __future__ import annotations

import pathlib

import pytest

from tests._lint_line_anchor import resolve_anchor, resolve_ledger

pytestmark = pytest.mark.lint


def test_resolves_a_unique_single_line_anchor(tmp_path: pathlib.Path) -> None:
    (tmp_path / "f.py").write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
    lineno, err = resolve_anchor(tmp_path, "f.py", ("b = 2",))
    assert (lineno, err) == (2, "")


def test_stale_when_content_is_absent(tmp_path: pathlib.Path) -> None:
    """The line the anchor names was fixed/deleted/rewritten -- this must
    fail loud, never silently resolve to nothing or to a guessed line."""
    (tmp_path / "f.py").write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
    lineno, err = resolve_anchor(tmp_path, "f.py", ("b = 999",))
    assert lineno is None
    assert "STALE" in err
    assert "b = 999" in err


def test_missing_file_fails_loud(tmp_path: pathlib.Path) -> None:
    lineno, err = resolve_anchor(tmp_path, "does_not_exist.py", ("x = 1",))
    assert lineno is None
    assert "no such file" in err


def test_ambiguous_when_content_occurs_more_than_once(tmp_path: pathlib.Path) -> None:
    """A single-line anchor that recurs verbatim elsewhere in the file
    must fail loud as AMBIGUOUS -- resolving to "the first match" would
    be exactly the silent-mistarget hazard this primitive exists to
    close: a caller could not tell whether it landed on the reviewed
    site or a coincidentally identical one."""
    (tmp_path / "f.py").write_text(
        "def one():\n    x = head(1)\n\ndef two():\n    x = head(1)\n",
        encoding="utf-8",
    )
    lineno, err = resolve_anchor(tmp_path, "f.py", ("x = head(1)",))
    assert lineno is None
    assert "AMBIGUOUS" in err
    assert "2 times" in err


def test_multiline_anchor_disambiguates_a_duplicate_line(tmp_path: pathlib.Path) -> None:
    """Widening the anchor with one line of leading context resolves the
    exact same ambiguity the single-line case above hits -- this is the
    documented remedy, not a hypothetical."""
    (tmp_path / "f.py").write_text(
        "def one():\n    x = head(1)\n\ndef two():\n    x = head(1)\n",
        encoding="utf-8",
    )
    lineno, err = resolve_anchor(tmp_path, "f.py", ("def two():", "x = head(1)"))
    assert (lineno, err) == (5, "")


def test_empty_anchor_is_rejected(tmp_path: pathlib.Path) -> None:
    (tmp_path / "f.py").write_text("a = 1\n", encoding="utf-8")
    lineno, err = resolve_anchor(tmp_path, "f.py", ())
    assert lineno is None
    assert "empty" in err


# ── the regression this whole primitive exists to prevent ─────────────


def test_anchor_survives_insertion_above_the_site(tmp_path: pathlib.Path) -> None:
    """THE CORE CLAIM, modeled directly on the nexus-vkpr3 incident report:
    ``_PIPEFAIL_EARLY_EXIT_EXEMPT``'s stale entry ":186" was written for
    a version-probe line; an 11-line insertion above BOTH that line and
    an unrelated, unreviewed WHEEL-extraction line (originally at 175)
    shifted the WHEEL line onto position 186 -- the exemption's stored
    number -- while the version-probe line it was actually written for
    moved on to 197. A naive line-number key silently re-exempted the
    WHEEL line under the version-probe's rationale; nothing failed.

    Reproduced on a synthetic fixture with the same shape: DECOY (an
    unrelated, never-reviewed violation) sits ABOVE VICTIM (the
    exempted, reviewed site) before the edit. An insertion at the top of
    the file, sized so DECOY's shifted line number exactly equals
    VICTIM's OLD stored line number, is the precise condition under
    which a line-number key silently swaps in DECOY. The content anchor
    is immune: it keeps resolving to VICTIM's own text wherever it
    lands, and never produces DECOY's line number.
    """
    before = (
        "#!/usr/bin/env bash\n"
        'echo "$Y" | grep -q other\n'        # DECOY, line 2 -- never exempted
        "set -o pipefail\n"
        "noop\n"
        'echo "$X" | grep -q pattern\n'      # VICTIM, line 5 -- the exempted site
    )
    victim_content = ('echo "$X" | grep -q pattern',)
    decoy_content = 'echo "$Y" | grep -q other'
    victim_lineno_before = 5
    decoy_lineno_before = 2

    f = tmp_path / "before.sh"
    f.write_text(before, encoding="utf-8")

    # Sanity: the anchor resolves to VICTIM's real line before any edit.
    lineno, err = resolve_anchor(tmp_path, "before.sh", victim_content)
    assert (lineno, err) == (victim_lineno_before, "")

    # Insert exactly enough lines at the top that DECOY's line shifts
    # onto VICTIM's OLD stored line number -- the precise shape of the
    # nexus-vkpr3 incident (an unrelated site slides into a stale
    # exemption's stored slot).
    shift = victim_lineno_before - decoy_lineno_before
    assert shift > 0
    after = "\n".join(f"# inserted {i}" for i in range(shift)) + "\n" + before
    g = tmp_path / "after.sh"
    g.write_text(after, encoding="utf-8")

    # RED CASE (what a line-number key would do): reusing
    # victim_lineno_before against the shifted file now names DECOY's
    # line, not VICTIM's -- a naive line-keyed lookup "resolves" to the
    # wrong site with no error of any kind.
    after_lines = after.splitlines()
    naive_line_keyed_result = after_lines[victim_lineno_before - 1].strip()
    assert naive_line_keyed_result == decoy_content, (
        "fixture stopped demonstrating the hazard -- the old stored line "
        "number must now land on DECOY's line, not VICTIM's"
    )

    # GREEN CASE: the content anchor resolves to VICTIM's new location,
    # never to DECOY's line, regardless of what shifted onto its old spot.
    lineno, err = resolve_anchor(tmp_path, "after.sh", victim_content)
    assert err == ""
    assert lineno == victim_lineno_before + shift
    assert after_lines[lineno - 1].strip() == victim_content[0]
    assert after_lines[lineno - 1].strip() != decoy_content


def test_anchor_survives_a_larger_multi_point_insertion(tmp_path: pathlib.Path) -> None:
    """The same claim under the shape of the real nexus-vkpr3 incident:
    multiple non-uniform insertions above a block of several exempted
    lines (the bead's own report: three separate edits shifted twelve
    entries by +11, then +12, then +13, non-uniformly). A single content
    anchor per site is immune to the shift regardless of its size or
    where it lands relative to the other sites."""
    lines = [f"noop_{i}()" for i in range(20)]
    lines[5] = 'echo "$A" | head -1'
    lines[12] = 'echo "$B" | head -1'  # a duplicate SINGLE-line snippet
    before = "\n".join(lines) + "\n"
    (tmp_path / "before.sh").write_text(before, encoding="utf-8")

    site_a = ('echo "$A" | head -1',)
    lineno, err = resolve_anchor(tmp_path, "before.sh", site_a)
    assert (lineno, err) == (6, "")  # 1-based

    # Insert 17 lines above everything (mirrors the bead's second
    # same-day incident: a 17-line guard block).
    after = "\n".join(["# inserted"] * 17 + lines) + "\n"
    (tmp_path / "after.sh").write_text(after, encoding="utf-8")

    lineno, err = resolve_anchor(tmp_path, "after.sh", site_a)
    assert (lineno, err) == (6 + 17, "")


# ── mandatory two-line minimum (fix-round finding) ─────────────────────


def test_blank_line_between_context_and_target_is_transparently_skipped(
    tmp_path: pathlib.Path,
) -> None:
    """A caller's (nearest-preceding-non-blank-line, target-line) anchor
    must resolve even when a blank line physically separates the two in
    the file -- the mandatory two-line convention pairs the target with
    its nearest NON-BLANK predecessor, not its literally adjacent one."""
    (tmp_path / "f.sh").write_text(
        "context line\n"
        "\n"
        "\n"
        'echo "$X" | head -1\n',
        encoding="utf-8",
    )
    lineno, err = resolve_anchor(
        tmp_path, "f.sh", ("context line", 'echo "$X" | head -1'),
    )
    assert (lineno, err) == (4, "")


def test_two_line_anchor_closes_the_coincidental_duplicate_mistarget(
    tmp_path: pathlib.Path,
) -> None:
    """THE CRITICAL fix-round finding, reproduced directly: a one-line
    anchor (the convention 1a147680f used whenever a line was NOT a
    same-file duplicate at authoring time) is silently wrong in a
    direction the first version of this module never tested for. If the
    originally-exempted line is later fixed/removed, and an UNRELATED,
    never-reviewed violation elsewhere in the file happens to have
    byte-identical stripped text, a one-line anchor finds exactly that
    one match and "resolves" to it -- there is nothing STALE about a
    single match, so the exemption silently reattaches to the wrong
    code. Widening the anchor to the target line PLUS its nearest
    preceding non-blank line (the new mandatory default -- see the
    module docstring's MANDATORY TWO-LINE MINIMUM section) closes this:
    the new violation's own preceding line differs, so the two-line
    block does not match anywhere, and resolution correctly reports
    STALE instead of a silent, wrong resolution.
    """
    # The exempted line ("x = head(1)") originally lived here, preceded
    # by "setup_a()".
    original = (
        "def one():\n"
        "    setup_a()\n"
        "    x = head(1)\n"      # the ORIGINAL exempted site, line 3
        "\n"
        "def two():\n"
        "    setup_b()\n"
        "    y = 1\n"
    )
    (tmp_path / "f.py").write_text(original, encoding="utf-8")

    one_line_anchor = ("x = head(1)",)
    two_line_anchor = ("setup_a()", "x = head(1)")

    # Sanity: both anchor shapes resolve to the real, original site.
    assert resolve_anchor(tmp_path, "f.py", one_line_anchor) == (3, "")
    assert resolve_anchor(tmp_path, "f.py", two_line_anchor) == (3, "")

    # The original site gets fixed (the early-exit consumer is
    # eliminated) -- AND, independently, elsewhere in the same file, an
    # unrelated new violation with byte-IDENTICAL stripped text appears,
    # preceded by a DIFFERENT line ("setup_b()", not "setup_a()"). This
    # is a realistic coincidence, not a contrived one: the exact same
    # snippet ("head -1", "x = head(1)"-shaped text) recurs verbatim
    # several times across real files in this repo's own ledgers.
    edited = (
        "def one():\n"
        "    setup_a()\n"
        "    x = fixed_no_pipe()\n"   # original site, now fixed
        "\n"
        "def two():\n"
        "    setup_b()\n"
        "    x = head(1)\n"           # NEW, unrelated, never-reviewed site
    )
    (tmp_path / "f.py").write_text(edited, encoding="utf-8")

    # RED (1a147680f's convention: a one-line anchor for a
    # not-a-duplicate-at-authoring-time entry): the anchor silently
    # "resolves" to the NEW, unreviewed site -- no STALE, no AMBIGUOUS,
    # just a wrong answer presented as a clean one.
    lineno, err = resolve_anchor(tmp_path, "f.py", one_line_anchor)
    assert (lineno, err) == (7, ""), (
        "fixture stopped demonstrating the hazard -- a one-line anchor "
        "must silently resolve to the new coincidental site here"
    )

    # GREEN (this fix round's mandatory two-line convention): the SAME
    # coincidence does not fool the two-line anchor, because the new
    # site's own preceding line ("setup_b()") does not match the
    # original site's ("setup_a()") -- the two-line block occurs nowhere
    # in the edited file, so resolution reports STALE instead of a
    # silent, wrong resolution.
    lineno, err = resolve_anchor(tmp_path, "f.py", two_line_anchor)
    assert lineno is None
    assert "STALE" in err


# ── resolve_ledger ───────────────────────────────────────────────────


def test_resolve_ledger_resolves_every_item_and_preserves_payload(
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / "f.py").write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
    items = [
        ("f.py", ("a = 1",), "payload-a"),
        ("f.py", ("b = 2", ), "payload-b"),
    ]
    resolved, problems = resolve_ledger(tmp_path, items)
    assert problems == []
    assert sorted(resolved) == [
        ("f.py", 1, "payload-a"),
        ("f.py", 2, "payload-b"),
    ]


def test_resolve_ledger_collects_problems_without_raising(
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / "f.py").write_text("a = 1\n", encoding="utf-8")
    items = [
        ("f.py", ("a = 1",), "ok"),
        ("f.py", ("z = 999",), "stale"),
        ("missing.py", ("whatever",), "missing"),
    ]
    resolved, problems = resolve_ledger(tmp_path, items)
    assert resolved == [("f.py", 1, "ok")]
    assert len(problems) == 2
    assert any("STALE" in p for p in problems)
    assert any("no such file" in p for p in problems)
