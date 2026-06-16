# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-154 P1.1 (bead nexus-tjptr): security_invoker standing-rule guard.

Structural guard mirroring ``TestRlsTableCompleteness`` (RDR-152 P5.4): grep
the Liquibase changelog XMLs at test time and assert the RDR-154 Decision 3
standing rule holds for every view over a tenant (RLS) schema:

  - Every ``CREATE [OR REPLACE] VIEW nexus.* / t1.*`` MUST be declared
    ``WITH (security_invoker = true)``. A default (``security_definer``) view
    over a FORCE-RLS table is a silent cross-tenant leak.
  - Materialized views are DEFERRED (RDR-154 P1). A matview cannot honor RLS,
    so the rule requires a ``tenant_id`` column + a ``security_invoker`` wrapper
    plain view. Until that machinery lands, no ``CREATE MATERIALIZED VIEW`` may
    exist over a tenant schema; adding one must extend this guard.

NON-VACUOUS: a negative self-test proves the matcher would catch a view that
omits the storage parameter.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_CHANGELOG_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "service" / "src" / "main" / "resources" / "db" / "changelog"
)

# CREATE [OR REPLACE] VIEW <schema>.<name> <storage-params...> AS
# Captures the text between the view name and the AS keyword (the storage
# parameter clause, if any). DOTALL so the clause may span lines.
_VIEW_RE = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+((?:nexus|t1)\.\w+)\b(.*?)\bAS\b",
    re.IGNORECASE | re.DOTALL,
)
_MATVIEW_RE = re.compile(
    r"CREATE\s+MATERIALIZED\s+VIEW\s+((?:nexus|t1)\.\w+)",
    re.IGNORECASE,
)
_INVOKER_RE = re.compile(r"security_invoker\s*=\s*true", re.IGNORECASE)


def _xml_texts() -> list[tuple[str, str]]:
    return [
        (p.name, p.read_text(encoding="utf-8"))
        for p in sorted(_CHANGELOG_DIR.glob("*.xml"))
    ]


def _plain_views() -> list[tuple[str, str, str]]:
    """Return (file, view_name, storage_clause) for every plain view found."""
    out: list[tuple[str, str, str]] = []
    for fname, text in _xml_texts():
        for m in _VIEW_RE.finditer(text):
            out.append((fname, m.group(1), m.group(2)))
    return out


class TestViewSecurityInvokerGuard:
    def test_changelog_dir_present(self):
        if not _CHANGELOG_DIR.exists():
            pytest.skip(f"changelog dir not found: {_CHANGELOG_DIR}")

    def test_every_tenant_view_is_security_invoker(self):
        """Every CREATE VIEW over nexus.* / t1.* carries security_invoker=true."""
        if not _CHANGELOG_DIR.exists():
            pytest.skip("changelog dir not found")

        views = _plain_views()
        # There is at least one such view today (catalog-005 collection_vector_stats);
        # if the matcher finds none, it has silently stopped working.
        assert views, (
            "No CREATE VIEW over nexus.*/t1.* found in the changelog XMLs — "
            "the matcher is broken (expected at least collection_vector_stats)."
        )

        offenders = [
            f"{fname}: {view}"
            for (fname, view, clause) in views
            if not _INVOKER_RE.search(clause)
        ]
        assert not offenders, (
            "RDR-154 Decision 3: every view over a tenant (RLS) table MUST be "
            "created WITH (security_invoker = true). Offending views:\n  "
            + "\n  ".join(sorted(offenders))
        )

    def test_no_undeferred_materialized_views(self):
        """Matviews are deferred (RDR-154 P1): none may exist over a tenant
        schema until the tenant_id + security_invoker-wrapper discipline lands."""
        if not _CHANGELOG_DIR.exists():
            pytest.skip("changelog dir not found")

        matviews = [
            f"{fname}: {m.group(1)}"
            for fname, text in _xml_texts()
            for m in _MATVIEW_RE.finditer(text)
        ]
        assert not matviews, (
            "RDR-154 P1 defers materialized views. A matview over a tenant "
            "schema requires a tenant_id column + a security_invoker wrapper "
            "plain view; extend this guard before introducing one. Found:\n  "
            + "\n  ".join(sorted(matviews))
        )

    def test_guard_is_non_vacuous(self):
        """The matcher flags a view that omits security_invoker."""
        bad = (
            "CREATE OR REPLACE VIEW nexus.leaky_view AS "
            "SELECT tenant_id FROM nexus.topics"
        )
        m = _VIEW_RE.search(bad)
        assert m is not None and m.group(1) == "nexus.leaky_view"
        assert _INVOKER_RE.search(m.group(2)) is None  # would be flagged

        good = (
            "CREATE OR REPLACE VIEW nexus.safe_view "
            "WITH (security_invoker = true) AS SELECT tenant_id FROM nexus.topics"
        )
        m2 = _VIEW_RE.search(good)
        assert m2 is not None and _INVOKER_RE.search(m2.group(2)) is not None
