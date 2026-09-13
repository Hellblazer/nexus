# SPDX-License-Identifier: AGPL-3.0-or-later
"""Parity between the packaged generic beads PRIME.md template and this
repo's own committed ``.beads/PRIME.md`` (nexus-cnzei.8, from the
nexus-cnzei.2 critique recorded on the bead's notes, T2
nexus/cnzei2-critic-pass-2026-09-13).

The packaged template (``nexus.beads_prime.load_template()``) is the
SOURCE OF TRUTH. This repo's tracked ``.beads/PRIME.md`` (landing with
nexus-cnzei.2) must equal that packaged text, optionally followed by AT
MOST one named repo-specific section -- a single markdown heading and its
body, appended verbatim after the packaged text with nothing else. This
pins BOTH drift directions: the packaged template changing without the
repo file following it, and the repo file diverging from (or adding more
than one section past) the packaged text.

nexus-cnzei.2 (worktree agent-a3c8be47a809217b4 as of 2026-09-13) has not
landed ``.beads/PRIME.md`` on this branch yet -- its current draft predates
this parity contract and does not embed the packaged text verbatim. The
real-file assertion below SKIPS (never passes vacuously) until that file
exists in this checkout; :class:`TestParityCheckerLogic` exercises the
checking logic itself against synthetic fixtures, so the mechanism has
real, always-running coverage in the meantime.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from nexus.beads_prime import load_template

_HEADING_RE = re.compile(r"^(#{1,6})\s+\S", re.MULTILINE)


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def parity_violation(repo_text: str, packaged_text: str) -> str | None:
    """``None`` when *repo_text* is *packaged_text* plus at most one named
    repo-specific section; otherwise a human-readable reason the pair
    violates the contract.

    "At most one named section" means: after stripping the packaged
    prefix, the remainder is either empty, or opens with a markdown
    heading line and never repeats a heading at that same (or a
    shallower) level again -- a nested subheading INSIDE that one section
    is fine; a second sibling section is not.
    """
    if not repo_text.startswith(packaged_text):
        return "repo .beads/PRIME.md does not start with the packaged template verbatim"
    remainder = repo_text[len(packaged_text):]
    if not remainder.strip():
        return None
    stripped = remainder.lstrip("\n")
    m = _HEADING_RE.match(stripped)
    if not m:
        return (
            "text after the packaged template must open with a markdown "
            "heading (a named repo-specific section) or be empty"
        )
    level = len(m.group(1))
    rest = stripped[m.end():]
    for other in _HEADING_RE.finditer(rest):
        if len(other.group(1)) <= level:
            return (
                "more than one repo-specific section found after the "
                "packaged template -- at most one is allowed"
            )
    return None


class TestParityCheckerLogic:
    """Always-running coverage of :func:`parity_violation`, against
    synthetic fixtures -- independent of whether the real repo file has
    landed yet.
    """

    def test_exact_match_is_no_violation(self) -> None:
        assert parity_violation("PACKAGED", "PACKAGED") is None

    def test_packaged_plus_one_named_section_is_no_violation(self) -> None:
        text = "PACKAGED\n\n## Repo-specific (nexus)\n\nSome repo-only text.\n"
        assert parity_violation(text, "PACKAGED\n\n") is None

    def test_missing_packaged_prefix_is_a_violation(self) -> None:
        assert parity_violation("something else entirely", "PACKAGED") is not None

    def test_drifted_packaged_portion_is_a_violation(self) -> None:
        # The packaged text itself changed (a word edited) but the repo
        # file was never updated to match -- this must be caught.
        repo_text = "PACKAGED (old wording)\n\n## Repo-specific\n\nX\n"
        assert parity_violation(repo_text, "PACKAGED (new wording)") is not None

    def test_extra_prose_with_no_heading_is_a_violation(self) -> None:
        repo_text = "PACKAGED\n\nsome unlabelled extra prose, not a section\n"
        assert parity_violation(repo_text, "PACKAGED") is not None

    def test_two_sections_at_the_same_level_is_a_violation(self) -> None:
        repo_text = "PACKAGED\n\n## First section\n\nX\n\n## Second section\n\nY\n"
        assert parity_violation(repo_text, "PACKAGED") is not None

    def test_nested_subheading_within_the_one_section_is_allowed(self) -> None:
        repo_text = "PACKAGED\n\n## Repo-specific\n\n### A sub-point\n\nX\n"
        assert parity_violation(repo_text, "PACKAGED") is None

    def test_empty_remainder_is_no_violation(self) -> None:
        assert parity_violation("PACKAGED\n", "PACKAGED\n") is None

    def test_packaged_template_itself_is_a_valid_prefix_of_itself(self) -> None:
        # Sanity: the real packaged text, matched against itself, is a
        # trivially valid (empty-remainder) case -- catches a regex/anchor
        # mistake that only a multi-line real document would expose.
        real = load_template()
        assert parity_violation(real, real) is None


def test_repo_beads_prime_matches_packaged_template_plus_at_most_one_section() -> None:
    repo_prime = _repo_root() / ".beads" / "PRIME.md"
    if not repo_prime.exists():
        pytest.skip(
            "repo .beads/PRIME.md not present on this branch yet -- this "
            "parity check activates once nexus-cnzei.2 lands it (bead "
            "nexus-cnzei.8)"
        )
    repo_text = repo_prime.read_text(encoding="utf-8")
    packaged_text = load_template()
    violation = parity_violation(repo_text, packaged_text)
    assert violation is None, (
        f"{violation}. The nexus repo's .beads/PRIME.md and the packaged "
        f"generic template (src/nexus/beads_prime_template.md) must not "
        f"drift independently -- see nexus-cnzei.8's bead notes."
    )
