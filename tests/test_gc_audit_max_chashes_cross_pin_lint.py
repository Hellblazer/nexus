# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cross-pin: ``GC_AUDIT_MAX_CHASHES`` must agree across its 3 live copies.

Substantive-critic finding (crit-fix pile review, 2026-08-19, nexus-sybbh):
``CatalogRepository.GC_AUDIT_MAX_CHASHES`` (Java, 5000) is duplicated as a
bare ``5000`` literal in ``catalog-033-gc-audit-producers.xml`` across THREE
independent SQL functions (``nexus.purge_trash``, ``nexus.gc_quarantine_orphans``,
``nexus.gc_expire_quarantine``) — none of which cross the JVM boundary to
reuse the Java constant, per that changelog's own header comment. No shared
source of truth existed, and no test caught a value bumped on one side
without the other three.

Cheapest honest form (per the remediation instruction): grep the SQL-side
literals and compare each against the Java constant, rather than building
real cross-language constant sharing for a single forensic-cap number. This
is a lint (repo-structure scan, not application behavior) — see
``tests/AGENTS.md`` § lint bucket.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

_REPO_ROOT = Path(__file__).parent.parent
_JAVA_FILE = _REPO_ROOT / "service/src/main/java/dev/nexus/service/db/CatalogRepository.java"
_XML_FILE = _REPO_ROOT / "service/src/main/resources/db/changelog/catalog-033-gc-audit-producers.xml"

_JAVA_CONST_RE = re.compile(r"GC_AUDIT_MAX_CHASHES\s*=\s*(\d+)\s*;")

#: A line containing this substring carries an UNRELATED ``5000`` — the two
#: ``set_config('statement_timeout', '5000', true)`` calls (milliseconds,
#: the RDR-191 sweep-gate timeout, catalog-028-1 heritage), not the chash
#: cap. Excluded by substring match, not line number, so a reflow of the
#: file doesn't silently blind this lint.
_UNRELATED_SUBSTRING = "statement_timeout"

#: Standalone "5000" — not part of a longer digit run (e.g. never matches
#: inside "50000" or "15000").
_STANDALONE_5000_RE = re.compile(r"(?<!\d)5000(?!\d)")


def _java_constant_value() -> int:
    text = _JAVA_FILE.read_text()
    m = _JAVA_CONST_RE.search(text)
    assert m, (
        f"GC_AUDIT_MAX_CHASHES declaration not found in {_JAVA_FILE} -- "
        "either the constant was renamed/removed (update this lint too) "
        "or the source tree moved."
    )
    return int(m.group(1))


def _xml_cap_literals() -> list[tuple[int, int]]:
    """Return ``(line_number, value)`` for every standalone ``5000`` literal
    in the changelog that is NOT the unrelated ``statement_timeout`` value."""
    out: list[tuple[int, int]] = []
    for i, line in enumerate(_XML_FILE.read_text().splitlines(), start=1):
        if _UNRELATED_SUBSTRING in line:
            continue
        for match in _STANDALONE_5000_RE.findall(line):
            out.append((i, int(match)))
    return out


def test_java_file_exists() -> None:
    assert _JAVA_FILE.is_file(), f"{_JAVA_FILE} is missing"


def test_xml_file_exists() -> None:
    assert _XML_FILE.is_file(), f"{_XML_FILE} is missing"


def test_java_constant_is_declared_and_parseable() -> None:
    """Non-vacuity for the Java side: the regex must actually find the
    declaration, not silently fall through to an assertion error that
    could be mistaken for "constant renamed" rather than "regex broke."""
    assert _java_constant_value() > 0


def test_xml_literals_found_nonvacuously() -> None:
    """Non-vacuity for the SQL side: this lint must actually find the
    literals it exists to cross-check. catalog-033's three functions
    (purge_trash, gc_quarantine_orphans, gc_expire_quarantine) each carry
    multiple mirroring '5000' mentions (a ceiling comparison, the
    truncation-detail echo, plus explanatory comments) -- an empty or
    near-empty result means either the changelog was rewritten (update this
    lint) or the scan regressed to catching nothing (RDR-129 house style:
    a lint that finds zero of what it exists to check is a failure, not a
    pass -- the nexus-moht0 vacuous-gate doctrine)."""
    literals = _xml_cap_literals()
    assert len(literals) >= 6, (
        "expected at least 6 GC_AUDIT_MAX_CHASHES-mirroring '5000' "
        f"literals across catalog-033's three SQL functions; found only "
        f"{len(literals)}: {literals}. If this legitimately shrank, "
        "tighten the assertion deliberately; if it silently regressed "
        "toward zero, this lint has gone blind."
    )


def test_xml_literals_match_the_java_constant() -> None:
    """The actual cross-pin: every SQL-side cap literal in catalog-033 must
    equal CatalogRepository.GC_AUDIT_MAX_CHASHES. A future bump to one
    side without the other three is exactly the drift this lint exists to
    catch -- and it is real: nothing else in the build enforces it, since
    plpgsql function bodies are opaque strings to every Java/Python
    compiler and linter that could otherwise cross-reference them."""
    java_value = _java_constant_value()
    literals = _xml_cap_literals()
    mismatched = [(line_no, value) for line_no, value in literals if value != java_value]
    assert not mismatched, (
        f"catalog-033-gc-audit-producers.xml line(s) {mismatched} carry a "
        f"cap literal that does not match CatalogRepository."
        f"GC_AUDIT_MAX_CHASHES = {java_value} -- purge_trash, "
        "gc_quarantine_orphans, and gc_expire_quarantine each hardcode "
        "this cap independently (they never cross the JVM boundary to "
        "reuse the Java constant, per that changelog's own header "
        "comment); update ALL FOUR sites in lockstep."
    )
