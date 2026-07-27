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


def _rollback_bodies() -> list[tuple[str, str, str, list[str | None]]]:
    """(file, changeset id, body text, splitStatements attrs of <sql> children)."""
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
            children = rb.findall(f"{_NS}sql")
            body = (rb.text or "") + "".join((e.text or "") for e in children)
            out.append((path.name, cs.get("id", ""), body,
                        [e.get("splitStatements") for e in children]))
    return out


def test_dollar_quoted_rollbacks_disable_statement_splitting() -> None:
    """Any rollback body containing ``$$`` must be inside
    ``<sql splitStatements="false">``.

    Falsify by unwrapping any of the seven currently-compliant blocks back to
    raw text — it reappears here immediately.
    """
    violations = []
    for fname, cs_id, body, attrs in _rollback_bodies():
        if "$$" not in body:
            continue
        if not attrs:
            violations.append(
                f"  {fname}::{cs_id} — raw-text <rollback> with a $$ body; it "
                f"CANNOT carry splitStatements and will be split on ';'"
            )
        elif not all(a == "false" for a in attrs):
            violations.append(
                f"  {fname}::{cs_id} — <sql splitStatements={attrs}>; a $$ body "
                f"needs splitStatements=\"false\""
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


def test_lint_detects_a_synthetic_violation() -> None:
    """NON-VACUITY — a matcher that found nothing would pass the test above."""
    raw_bad = "<rollback>DO $$ DECLARE x int; BEGIN END $$;</rollback>"
    wrapped_ok = ('<rollback><sql splitStatements="false">DO $$ DECLARE x int; '
                  "BEGIN END $$;</sql></rollback>")

    def parse(xml: str):
        rb = ET.fromstring(xml.replace("<rollback>", f"<rollback xmlns='{_NS[1:-1]}'>"))
        children = rb.findall(f"{_NS}sql")
        body = (rb.text or "") + "".join((e.text or "") for e in children)
        return body, [e.get("splitStatements") for e in children]

    body, attrs = parse(raw_bad)
    assert "$$" in body and not attrs, "raw-text $$ rollback must look violating"

    body, attrs = parse(wrapped_ok)
    assert "$$" in body and attrs == ["false"], "wrapped $$ rollback must look compliant"


def test_the_rule_is_load_bearing_not_vacuous() -> None:
    """There ARE dollar-quoted rollbacks in the corpus.

    If every ``$$`` rollback disappeared, the rule above would pass trivially
    forever and quietly stop protecting anything. Assert the population is real
    so the lint cannot rot into a no-op unnoticed.
    """
    dollar_rollbacks = [
        f"{f}::{c}" for f, c, body, _ in _rollback_bodies() if "$$" in body
    ]
    assert dollar_rollbacks, (
        "no <rollback> contains $$ any more — this lint now protects nothing "
        "and should be deleted rather than left as reassuring dead code"
    )
