# SPDX-License-Identifier: AGPL-3.0-or-later
"""GAP item 17 (nexus-i711w.1): service-mode orphan-audit degradation is
REPORTED, never silent.

MOVED out of tests/db/test_i711w_gap_xfails.py (stacked-review HIGH finding,
2026-07-29): the test is fully hermetic — the service-mode branch of
``_open_catalog_conn`` returns ``None`` before any I/O, and the other audit
sections are monkeypatched off — but that module's pytestmark gates every
test behind ``-m integration`` plus jar/PG prerequisites, so the pin NEVER
ran in the routine suite. Housed here as a plain unit test (no markers, no
fixture prereqs) it runs on every push, which is the point of a pin.

Items and beads: nexus-i711w.1 item 17 -> nexus-e9ru2 (CLOSED — the
explicit-degradation contract, ``AuditReport.orphans_checked`` + the rendered
skip line, landed via nexus-kmo9h). NOT an xfail; it must keep passing.
"""
from __future__ import annotations

import json

import pytest


class TestOrphanAuditServiceModeDegradation:
    """In service mode the orphan-chunks audit section degrades BY DESIGN
    (the local .catalog.db is a frozen migration source; the service-side
    orphan read lands with P5 catalog-collapse). The contract this pins is
    the standing no-silent-fallbacks directive: the degradation must be
    REPORTED to the operator, never rendered as "checked, clean".

    NOT an xfail: nexus-e9ru2 is CLOSED and the explicit-degradation
    contract (AuditReport.orphans_checked + the rendered skip line) already
    landed via nexus-kmo9h. This is the service-side pin so the i711w
    deletion of the local-catalog audit tests cannot unpin it. When the P5
    service-side orphan read lands, this test gets REPLACED by a positive
    orphan-computation test — until then it must keep passing.
    """

    # nexus-i711w.1 item 17 -> nexus-e9ru2
    def test_service_mode_orphan_degradation_is_reported_not_silent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexus import collection_audit as ca

        # Force the real service-mode branch in _open_catalog_conn
        # (collection_audit.py:376-382) without touching any live install
        # state: per-store env wins over the global backend default.
        monkeypatch.setenv("NX_STORAGE_BACKEND_CATALOG", "service")
        # Keep the audit hermetic: no live-T2 open, no T3/network coverage
        # probe (both sections have their own tests; this one is about the
        # orphan section's degradation reporting).
        monkeypatch.setattr(ca, "_open_t2", lambda: None)
        monkeypatch.setattr(ca, "compute_chash_coverage", lambda collection: None)

        report = ca.run_collection_audit("code__i711w-audit__voyage-code-3__v1")

        # Degraded (no rows) — but EXPLICITLY degraded, not silently clean.
        assert report.orphans == []
        assert report.orphans_checked is False

        human = ca.format_audit_human(report)
        # nexus-kmo9h: never render "couldn't check" as "checked, clean".
        assert "no local catalog to audit" in human
        orphan_section = human[
            human.index("=== orphan chunks") : human.index(
                "=== top-10 cross-collection hubs"
            )
        ]
        assert "skipped" in orphan_section
        assert "(none)" not in orphan_section

        payload = json.loads(ca.format_audit_json(report))
        assert payload["orphans_checked"] is False
