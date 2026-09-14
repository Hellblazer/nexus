# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ume6q: the doctor "eight supplementary checks" list in
docs/cli-reference.md must name exactly the checks
``doctor.py``'s ``_SUPPLEMENTARY_CHECK_NAMES`` actually runs, in the same
order -- not a hand-typed copy that can drift the moment a check is
added, removed, or reordered (nexus/04wmv-critic-pass-2026-09-13, Q1:
"zero tests reference ``_SUPPLEMENTARY_CHECK_NAMES`` at all").

Also cross-checks the companion "flags a default run still does NOT
cover" list against ``_OPT_IN_ONLY_CHECKS`` -- the same doc paragraph
names both sets and they are two views of one partition (the promoted
subset vs. everything still opt-in-only), so a drift in either is the
identical bug class.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from nexus.commands.doctor import _OPT_IN_ONLY_CHECKS, _SUPPLEMENTARY_CHECK_NAMES

pytestmark = pytest.mark.lint

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_DOC = _REPO_ROOT / "docs" / "cli-reference.md"

_BACKTICK_TOKEN = re.compile(r"`([^`]+)`")


def _extract_backtick_tokens(text: str, start_marker: str, end_marker: str) -> tuple[str, ...]:
    start = text.index(start_marker) + len(start_marker)
    end = text.index(end_marker, start)
    return tuple(_BACKTICK_TOKEN.findall(text[start:end]))


def test_supplementary_check_names_doc_matches_the_constant():
    """Non-vacuity floor first: the constant itself must be non-empty."""
    assert len(_SUPPLEMENTARY_CHECK_NAMES) > 0

    text = _DOC.read_text()
    doc_names = _extract_backtick_tokens(
        text, "diagnostics inline: ", "(the last has no"
    )
    assert doc_names == _SUPPLEMENTARY_CHECK_NAMES, (
        f"docs/cli-reference.md's supplementary-checks sentence names "
        f"{doc_names} but doctor.py's _SUPPLEMENTARY_CHECK_NAMES is "
        f"{_SUPPLEMENTARY_CHECK_NAMES} -- doc has drifted from the actual "
        f"promoted-check list (or its order)"
    )


def test_opt_in_only_check_flags_doc_matches_the_constant():
    assert len(_OPT_IN_ONLY_CHECKS) > 0

    text = _DOC.read_text()
    doc_flags = _extract_backtick_tokens(
        text, "still does NOT cover\n(", ") so the"
    )
    assert doc_flags == _OPT_IN_ONLY_CHECKS, (
        f"docs/cli-reference.md's 'still does NOT cover' list is "
        f"{doc_flags} but doctor.py's _OPT_IN_ONLY_CHECKS is "
        f"{_OPT_IN_ONLY_CHECKS} -- doc has drifted from the actual "
        f"opt-in-only flag list (or its order)"
    )
