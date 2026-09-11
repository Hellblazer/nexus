# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-m20mf P3: T2Database-level shared-client injection count harness.

Design record: T2 nexus/design-nexus-m20mf-single-t2-transport (T2 [24553]).
Bead: nexus-m20mf (option A -- the general fix for the callers option B's
``t2_index_write`` singleton does not reach: ``taxonomy_cmd``'s ~20
per-command ``_T2Database(...)`` sites and the indexer's per-file facades).

Mirrors the counting technique of ``tests/test_nx_answer_t2_fanout_budget.py``
(wrap ``httpx.Client.__init__`` to COUNT constructions without faking
behavior -- every instance is still real) but at the ``T2Database`` facade
level rather than nx_answer's call sites, and against a plain construction
rather than a live nx_answer call.

Both ``taxonomy_cmd._T2Database(default_db_path())`` (``src/nexus/commands/
taxonomy_cmd.py:35``) and ``index.py``'s ``T2Database(default_db_path())``
(``src/nexus/commands/index.py:1636``/``1929``) construct a ``T2Database``
with NO OTHER ARGUMENTS -- exactly the shape this file exercises. This file
does not modify either production call site (that is a further wiring
step, not this phase's deliverable); it proves the MECHANISM those sites
would use produces the claimed before/after counts on the identical
constructor shape they call today.

Runs against the self-provisioned engine TEST substrate (the suite's
autouse ``_pin_t2_substrate`` fixture) -- never production, never a mocked
transport, since real ``httpx.Client``/pool construction is exactly what
this file counts.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest


def _instrument_httpx_clients(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Wrap ``httpx.Client.__init__`` to COUNT constructions without
    faking behavior -- every instance is still real. Returns the running
    tally list (append-only; read ``len(tally)``)."""
    tally: list[int] = []
    orig_init = httpx.Client.__init__

    def _counting_init(self: httpx.Client, *args: Any, **kwargs: Any) -> None:
        tally.append(1)
        orig_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", _counting_init)
    return tally


class TestT2DatabaseSharedClientFanout:
    def test_default_construction_builds_nine_httpx_clients(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BEFORE: the shape every existing ``T2Database(path)`` call site
        uses today (``client=`` omitted) still builds one ``httpx.Client``
        per domain store -- 8, per the facade's own docstring inventory
        (memory, plans, taxonomy, telemetry, chash_index, document_aspects,
        aspect_queue, document_highlights). This is the regression pin: a
        change that collapses these without an explicit ``client=`` opt-in
        would be a silent behavior change for every caller that passes
        nothing, which the design record's "nothing changes for callers
        that pass nothing" contract forbids."""
        from nexus.db.t2 import T2Database

        tally = _instrument_httpx_clients(monkeypatch)

        db = T2Database(tmp_path / "t2.db")
        try:
            assert len(tally) == 9, (
                f"expected T2Database(path) with no client= to construct "
                f"exactly 9 httpx.Client()s (one per domain store); got "
                f"{len(tally)}"
            )
        finally:
            db.close()

    def test_injected_shared_client_builds_zero_additional_httpx_clients(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AFTER: a caller building ONE shared client via
        ``build_shared_t2_client()`` and passing it as ``client=`` gets
        all 8 domain stores reusing that single pool -- 8 -> 0 additional
        constructions for the facade call itself (1 total across the
        whole scenario, for the shared client's own construction, counted
        separately below so the two numbers are not conflated)."""
        from nexus.db.t2 import T2Database
        from nexus.db.t2._refreshable_client import build_shared_t2_client

        shared = build_shared_t2_client()
        tally = _instrument_httpx_clients(monkeypatch)

        db = T2Database(tmp_path / "t2.db", client=shared)
        try:
            assert len(tally) == 0, (
                f"expected T2Database(path, client=shared) to construct "
                f"ZERO additional httpx.Client()s (all 8 stores reuse the "
                f"injected client); got {len(tally)} additional "
                f"constructions -- some store is still building its own"
            )
            assert db.memory._client is shared
            assert db.plans._client is shared
            assert db.taxonomy._client is shared
            assert db.telemetry._client is shared
            assert db.chash_index._client is shared
            assert db.document_aspects._client is shared
            assert db.aspect_queue._client is shared
            assert db.document_highlights._client is shared
        finally:
            db.close()
            # The facade's close() must NOT have closed the injected
            # client -- ownership stays with whoever built it (this test).
            assert not shared.is_closed, (
                "T2Database.close() must be a no-op on an injected shared "
                "client -- ownership belongs to the caller that built it"
            )
            shared.close()

    def test_shared_client_survives_a_real_round_trip(self, tmp_path: Path) -> None:
        """Structural sharing is not enough on its own -- prove the shared
        pool is actually FUNCTIONAL end to end against the real engine
        substrate: a real authenticated read through a store that got the
        injected client, the same call shape ``index.py``'s own
        ``_collections_without_topics`` probe uses
        (``db.taxonomy.get_topics_for_collection``)."""
        from nexus.db.t2 import T2Database
        from nexus.db.t2._refreshable_client import build_shared_t2_client

        shared = build_shared_t2_client()
        db = T2Database(tmp_path / "t2.db", client=shared)
        try:
            # Any collection name is fine -- an unknown collection returns
            # an empty list rather than erroring; the property under test
            # is that the round trip COMPLETES (auth headers, tenant
            # header, and the shared pool all worked), not its content.
            topics = db.taxonomy.get_topics_for_collection(
                "nexus-m20mf-p3-fanout-harness-nonexistent-collection"
            )
            assert topics == []
        finally:
            db.close()
            assert not shared.is_closed
            shared.close()
