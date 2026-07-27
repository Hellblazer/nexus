# SPDX-License-Identifier: AGPL-3.0-or-later
"""A ``<rollback>`` with a dollar-quoted body must carry splitStatements="false".

WHY THIS EXISTS. ``staging-4-svc-grants``'s rollback was raw text — no ``<sql>``
child, so it could not carry the attribute — and Liquibase split it on the ``;``
inside its ``DECLARE`` section. PostgreSQL received the fragment::

    DO $$
    DECLARE
        rel RECORD

    ERROR: Unterminated dollar quote started at position 3. Expected terminating $$

That took out the ENTIRE rollback chain six positions before it reached the
v0.1.57 FTS changesets. Found 2026-07-27 while checking whether 0.1.56 was still
a rollback target — not by any test, because nothing executes rollback.

THE ASYMMETRY THAT HID IT. The FORWARD ``<sql>`` in that same changeset always
had ``splitStatements="false"``. Only the rollback lacked it. So the defect was
invisible on every deploy and fatal on the one path an incident response uses.
That is the general shape: forward SQL is exercised constantly, rollback SQL is
exercised never.

WHY THE RULE IS SHAPE-BASED RATHER THAN "DOES IT ACTUALLY BREAK". When this lint
was written, six OTHER raw-text rollbacks contained ``$$`` bodies
(catalog-008-1..3, catalog-012-1..3) and all six happened to SURVIVE the split —
their ``LANGUAGE sql`` function bodies contain no internal ``;``. They were one
added semicolon away from silently breaking, in a path nobody runs until an
incident. Requiring the attribute makes the survival deliberate instead of
incidental. All six were wrapped in the same change that added this lint.

WHAT THIS LINT DOES NOT COVER — read before trusting it. It checks SHAPE, not
behaviour: a rollback can carry the attribute, parse fine, and still restore the
wrong thing. Only executing a rollback proves that. See
``ChangelogRollbackRoundTripTest`` (service/src/test) for the update -> rollback
-> update round trip that catches the semantic class; this lint is the cheap
fast guard that runs on every PR without a container.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from tests.test_changelog_rls_lint import CHANGELOG_DIR

_NS = "{http://www.liquibase.org/xml/ns/dbchangelog}"


def _rollback_bodies() -> list[tuple[str, str, str, list[tuple[str, str | None]]]]:
    """(file, changeset id, RAW text under <rollback>, [(child text, splitStatements)]).

    Reported PER ELEMENT, not as one concatenated body (nexus-a0m60).
    ``splitStatements`` is an attribute of each ``<sql>`` change, so the
    splitting decision is per element — and aggregating hid a fault in BOTH
    directions:

    * false POSITIVE — a rollback that puts its ``$$`` block in
      ``<sql splitStatements="false">`` and its plain statements in a sibling
      ``<sql splitStatements="true">`` is correct, but looked like a violation
      because one sibling attr was not ``"false"``. rdr180-11's guarded rollback
      is exactly that shape.
    * false NEGATIVE, the one that mattered — raw ``$$`` text sitting DIRECTLY
      under ``<rollback>`` alongside any compliant ``<sql>`` child passed,
      because the child's attr satisfied the all() check while the raw text
      (which cannot carry the attribute at all) was never examined on its own.
      That is the staging-4 shape, surviving in a mixed block.
    """
    out = []
    for path in sorted(CHANGELOG_DIR.glob("*.xml")):
        # NO `except ParseError: continue` here, deliberately. Skipping an
        # unparseable file is the silent-skip class this whole lint exists to
        # prevent: a malformed changelog would sail through green while being
        # entirely unexamined. If a changelog does not parse, that IS the
        # finding — surface it rather than stepping over it. (Caught during
        # falsification of this very lint, when a botched mutation nearly
        # produced exactly that hole.)
        tree = ET.parse(path)
        for cs in tree.getroot().iter(f"{_NS}changeSet"):
            rb = cs.find(f"{_NS}rollback")
            if rb is None:
                continue
            out.append((
                path.name,
                cs.get("id", ""),
                rb.text or "",
                [(e.text or "", e.get("splitStatements")) for e in rb.findall(f"{_NS}sql")],
            ))
    return out


def test_dollar_quoted_rollbacks_disable_statement_splitting() -> None:
    """Every ``$$`` in a rollback must sit inside ``<sql splitStatements="false">``.

    Evaluated per element: raw text under ``<rollback>`` can never carry the
    attribute, and each ``<sql>`` child stands or falls on its own attr.

    Falsify by unwrapping any currently-compliant block back to raw text — it
    reappears here immediately.
    """
    violations = []
    for fname, cs_id, raw, children in _rollback_bodies():
        if "$$" in raw:
            violations.append(
                f"  {fname}::{cs_id} — raw-text <rollback> with a $$ body; it "
                f"CANNOT carry splitStatements and will be split on ';'"
            )
        for text, attr in children:
            if "$$" in text and attr != "false":
                violations.append(
                    f"  {fname}::{cs_id} — <sql splitStatements={attr!r}> holds a "
                    f"$$ body; it needs splitStatements=\"false\""
                )

    assert not violations, (
        "Rollback bodies containing $$ must disable statement splitting.\n"
        + "\n".join(violations) + "\n\n"
        "Liquibase splits on ';' by default, including the ';' inside a DO/"
        "function body, so PostgreSQL receives a truncated fragment and fails "
        "with \"Unterminated dollar quote\". This took out the whole rollback "
        "chain once already (staging-4-svc-grants, 2026-07-27) and it is "
        "invisible until someone actually needs a rollback.\n"
        "Wrap the body:  <rollback><sql splitStatements=\"false\" "
        "stripComments=\"false\">...</sql></rollback>\n"
        "Editing a <rollback> is checksum-SAFE — Liquibase excludes it from the "
        "changeset md5sum (verified: staging-4 kept 9:84da1012... across the "
        "fix), so this needs no validCheckSum even on already-executed "
        "changesets."
    )


def _violations_for(xml: str) -> list[str]:
    """Run the rule's exact logic over one synthetic <rollback>."""
    rb = ET.fromstring(xml.replace("<rollback>", f"<rollback xmlns='{_NS[1:-1]}'>"))
    raw = rb.text or ""
    children = [(e.text or "", e.get("splitStatements")) for e in rb.findall(f"{_NS}sql")]
    found = []
    if "$$" in raw:
        found.append("raw")
    found += [f"child:{attr!r}" for text, attr in children if "$$" in text and attr != "false"]
    return found


def test_lint_detects_a_synthetic_violation() -> None:
    """NON-VACUITY — a matcher that found nothing would pass the test above.

    The mixed cases are the ones the earlier concatenated-body form got wrong in
    both directions (see :func:`_rollback_bodies`), so they are pinned here.
    """
    raw_bad = "<rollback>DO $$ DECLARE x int; BEGIN END $$;</rollback>"
    wrapped_ok = ('<rollback><sql splitStatements="false">DO $$ DECLARE x int; '
                  "BEGIN END $$;</sql></rollback>")
    # Correct, and previously a FALSE POSITIVE: the $$ is confined to the
    # non-splitting element; the sibling holds only plain statements.
    mixed_ok = ('<rollback>'
                '<sql splitStatements="false">DO $$ BEGIN END $$;</sql>'
                '<sql splitStatements="true">ALTER TABLE t DROP CONSTRAINT IF EXISTS c;</sql>'
                '</rollback>')
    # Broken, and previously a FALSE NEGATIVE: raw $$ text rode along beside a
    # compliant child, and the old all()-over-children check never looked at it.
    mixed_bad = ('<rollback>DO $$ BEGIN END $$;'
                 '<sql splitStatements="false">SELECT 1;</sql>'
                 '</rollback>')

    assert _violations_for(raw_bad) == ["raw"], "raw-text $$ rollback must violate"
    assert _violations_for(wrapped_ok) == [], "wrapped $$ rollback must be compliant"
    assert _violations_for(mixed_ok) == [], (
        "a $$ body confined to <sql splitStatements=\"false\"> is compliant even when a "
        "sibling <sql> splits — splitStatements is per element"
    )
    assert _violations_for(mixed_bad) == ["raw"], (
        "raw $$ text directly under <rollback> must violate even when a compliant "
        "<sql> child is present — this is the staging-4 shape hiding in a mixed block"
    )


def test_the_rule_is_load_bearing_not_vacuous() -> None:
    """There ARE dollar-quoted rollbacks in the corpus.

    If every ``$$`` rollback disappeared, the rule above would pass trivially
    forever and quietly stop protecting anything. Assert the population is real
    so the lint cannot rot into a no-op unnoticed.
    """
    dollar_rollbacks = [
        f"{f}::{c}"
        for f, c, raw, children in _rollback_bodies()
        if "$$" in raw or any("$$" in text for text, _ in children)
    ]
    assert dollar_rollbacks, (
        "no <rollback> contains $$ any more — this lint now protects nothing "
        "and should be deleted rather than left as reassuring dead code"
    )
