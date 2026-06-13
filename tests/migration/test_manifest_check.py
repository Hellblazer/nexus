# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-159 P3.M.T (nexus-ue6g7.21) — the manifest-orphans validation leg.

RDR-159 RF-3 + §Approach P3. The manifest-orphan check reads the MIGRATED PG
catalog and is non-vacuous ONLY after T2 ``migrate all`` populated it. The
locked behaviours:

* **vacuous-skip is LOUD** — an empty migrated catalog (T2 absent) raises
  ``ValidationCheckVacuous`` instead of a false-clean zero (the original
  production vacuous-pass, RDR-159 gap #2);
* **non-vacuous after T2** — a populated catalog runs the real check;
* **backfill BEFORE orphans** — the P-1b sequencing is honored (pre-backfill
  NULL-collection rows read as false orphans);
* **orphans BLOCK** — a positive orphan count flows into the P3 gate as a block.

A fake catalog client records call order, so the contract is pinned without a
live service.
"""
from __future__ import annotations

import pytest

from nexus.migration.manifest_check import (
    _CATALOG_DOCS_RELATION,
    build_manifest_orphan_check,
)
from nexus.migration.validation import ValidationCheckVacuous, validate_migration


class _FakeCatalogClient:
    def __init__(self, *, doc_count: int, orphans_by_dim: dict[int, int]) -> None:
        self._doc_count = doc_count
        self._orphans_by_dim = orphans_by_dim
        self.calls: list[str] = []

    def relation_counts(self, relations: list[str]) -> dict[str, int]:
        self.calls.append(f"relation_counts:{relations}")
        if self._doc_count is None:
            return {}  # relation absent / unreachable
        return {_CATALOG_DOCS_RELATION: self._doc_count}

    def manifest_backfill(self) -> int:
        self.calls.append("manifest_backfill")
        return 0

    def manifest_orphans(self, dim: int, *, limit: int = 100) -> dict:
        self.calls.append(f"manifest_orphans:{dim}")
        return {"dim": dim, "count": self._orphans_by_dim.get(dim, 0), "orphans": []}


# --------------------------------------------------------------------------
# Vacuous-skip is LOUD when the migrated catalog is empty (T2 absent)
# --------------------------------------------------------------------------


def test_vacuous_when_catalog_empty_raises_loud() -> None:
    client = _FakeCatalogClient(doc_count=0, orphans_by_dim={})
    check = build_manifest_orphan_check(client, dims=(1024,))
    with pytest.raises(ValidationCheckVacuous) as exc:
        check()
    assert "T2 migrate-all has not populated" in str(exc.value)
    # It probed the catalog and STOPPED — never ran backfill/orphans on an
    # empty catalog (which would be the false-clean pass).
    assert client.calls == [f"relation_counts:['{_CATALOG_DOCS_RELATION}']"]


def test_vacuous_when_relation_absent_raises_loud() -> None:
    client = _FakeCatalogClient(doc_count=None, orphans_by_dim={})
    check = build_manifest_orphan_check(client, dims=(1024,))
    with pytest.raises(ValidationCheckVacuous):
        check()


# --------------------------------------------------------------------------
# Non-vacuous after T2: backfill BEFORE orphans, sums across dims
# --------------------------------------------------------------------------


def test_non_vacuous_runs_backfill_before_orphans() -> None:
    client = _FakeCatalogClient(doc_count=120, orphans_by_dim={})
    check = build_manifest_orphan_check(client, dims=(384, 1024))
    assert check() == 0
    # Order: probe → backfill → orphans (per dim). Backfill precedes orphans.
    assert client.calls == [
        f"relation_counts:['{_CATALOG_DOCS_RELATION}']",
        "manifest_backfill",
        "manifest_orphans:384",
        "manifest_orphans:1024",
    ]


def test_orphan_count_sums_across_dims() -> None:
    client = _FakeCatalogClient(doc_count=120, orphans_by_dim={384: 2, 1024: 3})
    check = build_manifest_orphan_check(client, dims=(384, 1024))
    assert check() == 5


# --------------------------------------------------------------------------
# Wired into the P3 gate: orphans BLOCK, vacuous BLOCKS loud
# --------------------------------------------------------------------------


def test_manifest_orphans_block_the_gate() -> None:
    client = _FakeCatalogClient(doc_count=120, orphans_by_dim={1024: 4})
    outcome = validate_migration(
        taxonomy_check=lambda: [],
        count_check=lambda: {"code__a__minilm-l6-v2-384__v1": (10, 10)},
        manifest_orphan_check=build_manifest_orphan_check(client, dims=(1024,)),
    )
    assert outcome.unlocked is False
    assert outcome.manifest_orphan_count == 4
    assert any("manifest" in r.lower() for r in outcome.blocking_reasons)


def test_vacuous_manifest_blocks_the_gate_loud() -> None:
    client = _FakeCatalogClient(doc_count=0, orphans_by_dim={})
    outcome = validate_migration(
        taxonomy_check=lambda: [],
        count_check=lambda: {"code__a__minilm-l6-v2-384__v1": (10, 10)},
        manifest_orphan_check=build_manifest_orphan_check(client, dims=(1024,)),
    )
    assert outcome.unlocked is False
    assert outcome.manifest_vacuous is True
    assert any(
        "vacuous" in r.lower() and "manifest" in r.lower()
        for r in outcome.blocking_reasons
    )
