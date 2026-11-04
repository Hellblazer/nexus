# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-238 Phase 1 Step 1 (nexus-qo7ie.6): the AUDIT_TAGS table is checked and pinned.

``src/nexus/tables/audit-tags.toml`` is the one home of the tags that mark a T2 row as an
audit record. The engine refuses a ``replaces`` whose target carries one (the Java mirror,
``AuditTags``, is generated from this table) and ``nx memory curate --t2`` (Phase 3) reads the
same set. These tests hold the table to the checker, to the six tags RDR-238 names, and to the
normalisation rule the header states: split on ``,``, trim, exact case-sensitive whole-tag match.
"""
from __future__ import annotations

import re

import pytest

from nexus.tables import audit_tags
from nexus.tables.check import BLOCKING_CODES, check_table

#: Typed out here on purpose: the table must equal the RDR's list, not itself.
_RDR_238_SET = {"kind:gate", "gate", "critique", "research", "fix-check", "commit-review"}


def test_table_lints_clean_under_the_house_checker():
    findings = [f for f in check_table(audit_tags.load_table()) if f.code in BLOCKING_CODES]
    assert findings == []


def test_the_set_is_the_six_tags_rdr_238_names():
    assert audit_tags.AUDIT_TAGS == _RDR_238_SET
    assert isinstance(audit_tags.AUDIT_TAGS, frozenset)


def test_every_declared_tag_has_exactly_one_row():
    declared = audit_tags.load_table().dimensions["tag"].domain
    assert len(declared) == 6, "the sweep examined too little to mean anything"
    assert sorted(declared) == sorted(audit_tags.AUDIT_TAGS)


def test_the_header_states_the_normalisation_rule():
    text = audit_tags.header_text()
    for needle in ("split on ','", "trim", "case-sensitive", "whole tag"):
        assert needle in text, needle


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ()),
        (None, ()),
        ("a,b", ("a", "b")),
        (" a , b ,, c ", ("a", "b", "c")),
        ("kind:gate,bead:nexus-x", ("kind:gate", "bead:nexus-x")),
        (",", ()),
    ],
)
def test_split_tags_splits_on_comma_and_trims(raw, expected):
    assert audit_tags.split_tags(raw) == expected


@pytest.mark.parametrize("tag", sorted(_RDR_238_SET))
def test_each_audit_tag_matches_alone_and_among_others(tag):
    assert audit_tags.audit_tags_of(tag) == (tag,)
    assert audit_tags.audit_tags_of(f"bead:nexus-x, {tag} ,rdr:238") == (tag,)
    assert audit_tags.is_audit_tagged(f"{tag},x")


@pytest.mark.parametrize(
    "raw",
    [
        "",
        None,
        "bead:nexus-x,rdr:238",
        "Gate",  # case-sensitive: exact match only
        "KIND:GATE",
        "gates",  # whole tag, never a substring
        "research-notes",
        "kind:gate-extra",
        "pre-critique",
        "kind:",
        "kind:gate bead:x",  # a space is not a separator
    ],
)
def test_near_misses_are_not_audit_tagged(raw):
    assert audit_tags.audit_tags_of(raw) == ()
    assert not audit_tags.is_audit_tagged(raw)


def test_a_body_mentioning_a_tag_is_not_in_scope_of_the_tag_match():
    """The match reads the TAGS string only; content never reaches it."""
    assert not audit_tags.is_audit_tagged("notes")


# ── Title patterns (the K4 backstop, Sam chose option a) ────────────────────────

#: Typed out on purpose: the table must equal this list, not itself.
_RDR_238_PATTERNS = (
    "(^|[-_.: ])review([-_.: ]|$)",
    "(^|[-_.: ])code-review([-_.: ]|$)",
    "(^|[-_.: ])commit-review([-_.: ]|$)",
    "(^|[-_.: ])critique([-_.: ]|$)",
    "(^|[-_.: ])gate([-_.: ]|$)",
    "(^|[-_.: ])fix-check([-_.: ]|$)",
    "-research-[0-9]+([.: ]|$)",
    "(^|[-_.: ])confirm([-_.: ]|$)",
    "(^|[-_.: ])audit([-_.: ]|$)",
    "(^|[-_.: ])critic([-_.: ]|$)",
    "(^|[-_.: ])confirmation([-_.: ]|$)",
    "(^|[-_.: ])rereview([-_.: ]|$)",
    "(^|[-_.: ])codereview([-_.: ]|$)",
    "(^|[-_.: ])fixcheck([-_.: ]|$)",
)


def test_the_title_patterns_are_the_ones_the_decision_names():
    assert audit_tags.TITLE_PATTERNS == _RDR_238_PATTERNS
    assert len(set(audit_tags.TITLE_PATTERNS)) == len(audit_tags.TITLE_PATTERNS)


def test_no_pattern_puts_a_quantifier_after_a_group():
    """Java's regex engine recurses per character on (a|b)* and overflows the stack."""
    bad = re.compile(r"\)[*+{]")
    assert len(audit_tags.TITLE_PATTERNS) >= 7, "the sweep examined too little to mean anything"
    for pat in audit_tags.TITLE_PATTERNS:
        assert not bad.search(pat), pat


def test_the_header_states_the_title_matching_rule():
    text = audit_tags.header_text()
    for needle in ("case-insensitively", "anywhere in the title", "'-', '_', '.', ':' or a space", "UNIX_LINES", "quantifier"):
        assert needle in text, needle


@pytest.mark.parametrize(
    ("title", "pattern"),
    [
        ("code-review-nexus-d9xt2", "(^|[-_.: ])review([-_.: ]|$)"),
        ("critique-wbfpw28-r3", "(^|[-_.: ])critique([-_.: ]|$)"),
        ("nexus-1si7z-critique-round2-2026-07-12", "(^|[-_.: ])critique([-_.: ]|$)"),
        ("nexus-5b9v0-collision-guard-review-round4", "(^|[-_.: ])review([-_.: ]|$)"),
        ("pr539-a2-failure-handling-code-review-round2-2026-10-12", "(^|[-_.: ])review([-_.: ]|$)"),
        ("test-suite-compression-P2-reduced-round2-critique", "(^|[-_.: ])critique([-_.: ]|$)"),
        ("210-gate-r1-answers-sam-2026-10-17", "(^|[-_.: ])gate([-_.: ]|$)"),
        ("215-fix-check-dd95743fa", "(^|[-_.: ])fix-check([-_.: ]|$)"),
        ("232-research-24", "-research-[0-9]+([.: ]|$)"),
        ("confirm-dsr6a-431c62543-2026-10-17", "(^|[-_.: ])confirm([-_.: ]|$)"),
        ("225-plan-audit-r1", "(^|[-_.: ])audit([-_.: ]|$)"),
        ("x_review", "(^|[-_.: ])review([-_.: ]|$)"),
        ("review", "(^|[-_.: ])review([-_.: ]|$)"),
        ("Critique-X", "(^|[-_.: ])critique([-_.: ]|$)"),  # case-insensitive
        ("CODE-REVIEW-1", "(^|[-_.: ])review([-_.: ]|$)"),
        # the first review's survey: a .md suffix, ':' and a space are boundaries too
        ("nexus-sdp0u-round1-critique.md", "(^|[-_.: ])critique([-_.: ]|$)"),
        ("nexus-9qq85-phase1-code-review.md", "(^|[-_.: ])review([-_.: ]|$)"),
        ("qc4p1-substantive-critique.md", "(^|[-_.: ])critique([-_.: ]|$)"),
        ("rdr-238-gate.md", "(^|[-_.: ])gate([-_.: ]|$)"),
        ("gate:x", "(^|[-_.: ])gate([-_.: ]|$)"),
        ("review of x", "(^|[-_.: ])review([-_.: ]|$)"),
        ("238-research-9.md", "-research-[0-9]+([.: ]|$)"),
        ("232-research-3: title-chain method", "-research-[0-9]+([.: ]|$)"),
        ("rdr232-critic-pass-2", "(^|[-_.: ])critic([-_.: ]|$)"),
        ("wwaqm-confirmation-72f62dab4-2026-09-14", "(^|[-_.: ])confirmation([-_.: ]|$)"),
        ("nexus-4lkmz-rereview-2026-08-07.md", "(^|[-_.: ])rereview([-_.: ]|$)"),
        ("x-codereview-1", "(^|[-_.: ])codereview([-_.: ]|$)"),
        ("y_fixcheck", "(^|[-_.: ])fixcheck([-_.: ]|$)"),
    ],
)
def test_real_audit_titles_match_a_pattern(title, pattern):
    assert audit_tags.title_pattern_of(title) == pattern
    assert audit_tags.is_audit_title(title)


@pytest.mark.parametrize("pattern", _RDR_238_PATTERNS)
def test_each_pattern_matches_some_title_alone(pattern):
    samples = {
        "(^|[-_.: ])review([-_.: ]|$)": "review-x",
        "(^|[-_.: ])code-review([-_.: ]|$)": "code-review",
        "(^|[-_.: ])commit-review([-_.: ]|$)": "commit-review-abc",
        "(^|[-_.: ])critique([-_.: ]|$)": "a-critique",
        "(^|[-_.: ])gate([-_.: ]|$)": "238-gate-latest",
        "(^|[-_.: ])fix-check([-_.: ]|$)": "x_fix-check_y",
        "-research-[0-9]+([.: ]|$)": "238-research-9",
        "(^|[-_.: ])critic([-_.: ]|$)": "rdr232-critic-pass-2",
        "(^|[-_.: ])confirmation([-_.: ]|$)": "wwaqm-confirmation-72f62dab4-2026-09-14",
        "(^|[-_.: ])rereview([-_.: ]|$)": "nexus-4lkmz-rereview-2026-08-07.md",
        "(^|[-_.: ])codereview([-_.: ]|$)": "x-codereview-1",
        "(^|[-_.: ])fixcheck([-_.: ]|$)": "y_fixcheck",
        "(^|[-_.: ])confirm([-_.: ]|$)": "confirm-dsr6a-431c62543",
        "(^|[-_.: ])audit([-_.: ]|$)": "225-plan-audit-r1",
    }
    assert re.search(pattern, samples[pattern], re.I | re.A)


@pytest.mark.parametrize(
    "title",
    [
        "reviewer-notes",  # "reviewer" is not the token "review"
        "research-notes",
        "preview",
        "previewer-plan",
        "gates-notes",
        "gated-release",
        "critiques-index",
        "238-research-notes",  # -research- must be followed by digits to the end
        "238-research-9-notes",
        "continuation-state",
        "rdr-238-a2-read-sample",
        "project_art_is_the_founding_use_case",
        "chas-test-host-howto",
        "confirmations-log",
        "auditor-notes",
        "reviewer-notes.md",
        "238-research-9x",
        "238-research-v9",
        "",
        None,
    ],
)
def test_near_misses_and_ordinary_notes_are_replaceable(title):
    assert audit_tags.title_pattern_of(title) is None
    assert not audit_tags.is_audit_title(title)


@pytest.mark.parametrize(
    ("title", "audit"),
    [
        ("x-gate ", True),  # a space is a boundary
        ("x-gate\n", True),  # '$' also matches before a final \n, in Python and (UNIX_LINES) in Java
        ("232-research-24\n", True),
        ("x-gate\r", False),  # \r, NEL and the Unicode separators are not boundaries in either
        ("x-gate\u0085", False),
        ("x-gate\u2028", False),
        ("232-research-24\r", False),
    ],
)
def test_dollar_and_space_boundaries_match_the_java_side(title, audit):
    """Mirrors AuditTagsTest.dollarMatchesOnlyBeforeAFinalNewline_asInPython."""
    assert audit_tags.is_audit_title(title) is audit
