# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ou4tb (a): catalog post-store hook failures must be LOUD and counted.

Rug audit 2026-07-15, items 2/3/10. Four catalog-registration hooks swallowed
their failures to ``_log.debug`` and nothing else, so a document could land in
T3 and never be registered in the catalog — no doc_id, no manifest, no links —
while the index run reported success. Only a rebuild recovers it, and nobody
was told to run one.

These pin the recording half of the fix: the log level, and the audit row
(``record_catalog_hook_failure`` -> engine ``hook_failures``). The doctor
RENDERING half (the local-SQLite ``_catalog_hook_failure_lines`` census) died
with the =sqlite opt-out (RDR-158 P3, nexus-7bomn); its service twin — the
engine has ``list_hook_failures`` — is a recorded GAP on nexus-i711w.1.
"""
from __future__ import annotations

import pytest


class TestTheFourSitesAreNoLongerSilent:
    """Grep-level pins: DEBUG here means a silent non-registration."""

    @pytest.mark.parametrize(
        ("module_path", "event"),
        [
            ("src/nexus/indexer.py", "catalog_hook_failed"),
            ("src/nexus/indexer.py", "catalog_link_generation_failed"),
            ("src/nexus/catalog/store_hook.py", "catalog_store_hook_failed"),
            ("src/nexus/pipeline_stages.py", "catalog_pdf_hook_failed"),
        ],
    )
    def test_site_logs_at_warning_and_records_an_audit_row(
        self, module_path: str, event: str
    ) -> None:
        from pathlib import Path

        src = Path(module_path).read_text()
        assert f'_log.debug("{event}"' not in src, (
            f"{event} is back at DEBUG — a silent non-registration"
        )
        assert f'_log.warning("{event}"' in src

        # The audit row is what doctor counts; a WARNING alone is still only
        # visible to whoever is reading logs at the time.
        idx = src.index(f'_log.warning("{event}"')
        following = src[idx: idx + 600]
        assert "record_catalog_hook_failure(" in following, (
            f"{event} logs loudly but records no audit row, so nx doctor "
            f"still cannot tell the user anything"
        )
