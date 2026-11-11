# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-f3yqd: the engine's chash width constraints are the client's poison tables.

``GET /v1/admin/chash-conformance`` reports the width CHECKs of the tables in ``ChashConformanceConstraints.TARGETS``.
``nx doctor`` skips its chash scans on the strength of that answer, so the engine list must be the client's list of tables
that carry such a CHECK (``POISON_CHASH_TABLES``): a table the client counts that the route does not report would read as
"validated" by omission, and a table the route reports that the client does not know would gate on a constraint the
client never reasoned about. The Java is read as text; its constraint names are held to the changelogs that add them.
"""
from __future__ import annotations

import re
from pathlib import Path

from nexus.db.chash_tables import POISON_CHASH_TABLES

_ROOT = Path(__file__).resolve().parents[1]
_JAVA = _ROOT / "service/src/main/java/dev/nexus/service/db/ChashConformanceConstraints.java"
_CHANGELOG = _ROOT / "service/src/main/resources/db/changelog"


def _targets() -> list[tuple[str, str]]:
    """(``nexus.<table>``, constraint name) of each ``new Target(TABLE_CONSTANT, "name")`` in the Java class."""
    found = re.findall(r'new Target\(([A-Z_]+), "([a-z_0-9]+)"\)', _JAVA.read_text(encoding="utf-8"))
    return [(f"nexus.{constant.lower()}", name) for constant, name in found]


def test_the_engine_targets_are_exactly_the_clients_poison_tables() -> None:
    targets = _targets()
    assert len(targets) >= 2, targets
    assert {table for table, _ in targets} == {t.table for t in POISON_CHASH_TABLES}


def test_each_target_names_a_constraint_a_changelog_adds_on_that_table() -> None:
    changelogs = {p.name: p.read_text(encoding="utf-8") for p in _CHANGELOG.glob("*.xml")}
    for table, name in _targets():
        bare = table.removeprefix("nexus.")
        adding = [
            fname for fname, text in changelogs.items()
            if re.search(rf"ALTER TABLE {re.escape(table)}\s+ADD CONSTRAINT {re.escape(name)} CHECK \(octet_length\(chash\) = 32\)", text)
        ]
        assert adding, f"no changelog adds {name} on {table} ({bare})"
