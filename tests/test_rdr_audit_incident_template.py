# SPDX-License-Identifier: AGPL-3.0-or-later
"""Structural test for the RDR-067 Phase 3 incident template.

``conexus/resources/rdr_process/INCIDENT-TEMPLATE.md`` carries the enum values
the canonical audit prompt (T2 ``nexus_rdr/067-canonical-prompt-v1``) filters
on; drift between the two is what this test catches. The template's prose
(section headings, filing guidance) is not pinned.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
TEMPLATE = REPO_ROOT / "conexus" / "resources" / "rdr_process" / "INCIDENT-TEMPLATE.md"

_ENUMS = {
    "drift_class": ("unwiring", "dim-mismatch", "deferred-integration", "other"),
    "caught_by": ("substantive-critic", "composition-probe", "dim-contracts", "user", "post-hoc"),
    "outcome": ("reopened", "partial", "shipped-silently"),
}


def test_incident_template_exists_with_its_enum_values() -> None:
    assert TEMPLATE.exists(), f"{TEMPLATE} does not exist"
    text = TEMPLATE.read_text()
    for field, values in _ENUMS.items():
        for value in values:
            assert value in text, f"{field} enum value `{value}` missing from the incident template"
