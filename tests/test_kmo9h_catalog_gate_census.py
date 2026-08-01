# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-kmo9h: completed census of the local ``Catalog.is_initialized``
gate class (parts 1+2 of the f1itv/e9ru2 sweep) + behavior pins.

Two halves:

1. A STATIC CENSUS TRIPWIRE: exact per-file occurrence counts of
   ``Catalog.is_initialized`` across ``src/nexus``. Every remaining
   occurrence is deliberate (factory internals, SQLite-opt-out-only
   guards, frozen-era tooling, dead code). A NEW occurrence anywhere
   fails this test with a pointer to the fix pattern — the class cannot
   silently regrow (the original e9ru2 census undercounted precisely
   because nothing mechanical held the line).

2. BEHAVIOR PINS for one representative of each fixed shape, fresh
   service-mode box + fake HTTP boundary (pattern:
   tests/test_e9ru2_catalog_gate_sweep.py): the store tombstone reap,
   the catalog writer helper, the document_aspects identity probe, the
   ``nx catalog setup`` divergence refusal, and the doctor-verb
   diagnostic honesty helper.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import nexus
import nexus.catalog.factory as factory

_SRC = Path(nexus.__file__).parent

#: Exact allowlist: file (relative to src/nexus) -> occurrence count of
#: ``Catalog.is_initialized`` (any receiver spelled ``*Catalog``). Every
#: entry is deliberate:
#: - catalog/factory.py: the SQLite-branch presence check INSIDE the
#:   factory — the single place presence semantics live (f1itv contract).
#:   2 -> 1 (nexus-i711w Stage 2 sub-stage C-store): make_catalog_admin held
#:   the second one and was deleted with the two deep-maintenance verbs it
#:   served (dedupe-owners, undelete). A SHRINK, caught by this census's own
#:   exact-equality assert rather than by the author — which is the argument
#:   for two-sided ledgers over one-sided counts (cf. nexus-th15h).
#: - catalog/catalog.py: the definition + intra-class uses.
#: - commands/catalog.py: SQLite-opt-out-only guards (service mode
#:   bypasses via storage_backend_for / refuses setup divergence).
#: - commands/catalog_cmds/doctor.py: REMOVED (was 4). The local-artifact
#:   doctor surface — synthesize-log, _check_bootstrap_status,
#:   _run_replay_equality, _run_t3_doc_id_coverage — held all four gates and
#:   was deleted in nexus-i711w Stage 2 sub-stage C-store. The verbs were
#:   local-only by design (the event log / JSONL / projection ARE local) and
#:   already refused in service mode; replay-equality has no service meaning
#:   at all. doctor itself survives with its six service-capable checks and
#:   now reaches the catalog only through the factory.
#: - db/collection_purge.py: unreachable in service mode (early return
#:   at the atomic engine cascade) — verified nexus-e9ru2.
#: - catalog/synthesizer.py: local-catalog bootstrap tooling (RDR-101);
#:   synthesize_t3_chunks additionally has zero callers.
#: - db/embed_migrate.py: Chroma-era tool operating coherently on frozen
#:   sources ("SERVICE path only (never embed_migrate)" — guided_upgrade);
#:   dies at RDR-155 P4b.
#: - indexer.py: the CORRECT service-aware form (catalog_service_mode
#:   boolean) — the pattern the sweep normalized everything else to.
#: EMPTIED by the nexus-i711w terminal deletion: every allowed site died with
#: the local catalog (factory SQLite leg, collection_audit else-leg, the
#: init/setup verbs, collection_purge local leg, synthesizer, embed_migrate's
#: handle, indexer's three gates). Kept as an empty two-sided tombstone pin:
#: the Catalog class no longer exists, so ANY new ``Catalog.is_initialized``
#: is a reintroduction and fails this census. Delete the file at Stage 4/5.
_ALLOWED: dict[str, int] = {}

#: Matches ``Catalog.is_initialized`` including aliased imports
#: (``_Catalog.is_initialized``); comment-only lines are skipped so prose
#: references to the pattern don't count as gates.
_PATTERN = re.compile(r"Catalog\.is_initialized")


def test_is_initialized_census_is_closed():
    found: dict[str, int] = {}
    for py in sorted(_SRC.rglob("*.py")):
        rel = str(py.relative_to(_SRC))
        n = sum(
            len(_PATTERN.findall(line))
            for line in py.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")
        )
        if n:
            found[rel] = n
    assert found == _ALLOWED, (
        "Catalog.is_initialized census drifted.\n"
        f"  found:   {found}\n"
        f"  allowed: {_ALLOWED}\n"
        "A NEW local presence gate short-circuits service mode (the Java "
        "service owns the catalog; fresh boxes have no local state). Use "
        "the factory delegation instead: reader = make_catalog_reader(); "
        "if reader is None: <sqlite-opt-out skip>. See nexus-f1itv/e9ru2/"
        "kmo9h and tests/test_e9ru2_catalog_gate_sweep.py. If the new site "
        "is genuinely local-artifact tooling, add it here WITH a rationale "
        "comment."
    )


#: Sibling tripwire for the SECOND shape of the class: raw direct
#: ``Catalog(<path>, ... ".catalog.db")`` constructions that bypass the
#: factory entirely — the pre-fix ``document_aspects._resolve_doc_id``
#: shape, which read the FROZEN migration-source catalog on migrated
#: service-mode boxes with no gate at all. Allowed sites:
#: (daemon/t2_daemon.py held one — the T2 daemon WAS the local single-writer
#: CatalogWriter proxied to in SQLite mode. Removed with the daemon in
#: nexus-i711w Stage 2 sub-stage B. DOWNWARD-only edit.)
#: - db/collection_purge.py: local-mode fan-out branch only (service mode
#:   returns earlier at the atomic engine cascade — verified nexus-e9ru2).
#: - db/embed_migrate.py: Chroma-era tool on frozen sources; dies at P4b.
#: - catalog/synthesizer.py: local-catalog bootstrap tooling (RDR-101).
#: EMPTIED by the nexus-i711w terminal deletion (see _ALLOWED above).
_RAW_ALLOWED: dict[str, int] = {}

_RAW_PATTERN = re.compile(r"Catalog\(\s*\w+,\s*\w+\s*/\s*\"\.catalog\.db\"")


def test_raw_catalog_construction_census_is_closed():
    found: dict[str, int] = {}
    for py in sorted(_SRC.rglob("*.py")):
        rel = str(py.relative_to(_SRC))
        if rel.startswith("catalog/catalog") or rel == "catalog/factory.py":
            continue  # the class's own definition + the factory that wraps it
        n = sum(
            len(_RAW_PATTERN.findall(line))
            for line in py.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")
        )
        if n:
            found[rel] = n
    assert found == _RAW_ALLOWED, (
        "Raw Catalog(<path>, .catalog.db) construction census drifted.\n"
        f"  found:   {found}\n"
        f"  allowed: {_RAW_ALLOWED}\n"
        "Direct local-catalog opens bypass the factory's service-mode "
        "routing and read the FROZEN migration-source catalog on migrated "
        "boxes (the pre-fix document_aspects shape, nexus-kmo9h class B). "
        "Route through make_catalog_reader()/make_catalog_writer() instead, "
        "or add the site here WITH a rationale comment."
    )


# ── behavior pins (fresh service-mode box, fake HTTP boundary) ──────────────


class _Entry:
    def __init__(self, tumbler: str, *, content_type: str = "knowledge",
                 file_path: str = "") -> None:
        self.tumbler = tumbler
        self.content_type = content_type
        self.file_path = file_path


class _FakeHttpCatalogClient:
    instances: list["_FakeHttpCatalogClient"] = []

    def __init__(self, *_a, **_kw) -> None:
        self.deleted: list[str] = []
        self.by_doc_id_map: dict[str, _Entry] = dict(_FakeHttpCatalogClient._seed)
        _FakeHttpCatalogClient.instances.append(self)

    _seed: dict[str, _Entry] = {}

    def by_doc_id(self, doc_id):
        return self.by_doc_id_map.get(doc_id)

    def docs_for_chashes(self, chashes):
        # nexus-5axey: the reap's chash-appropriate lookup. Keyed the same
        # as by_doc_id_map here since this fake seeds one entry per chash.
        return {
            chash: [self.by_doc_id_map[chash].tumbler]
            for chash in chashes
            if chash in self.by_doc_id_map
        }

    def resolve(self, tumbler):
        for entry in self.by_doc_id_map.values():
            if entry.tumbler == tumbler:
                return entry
        return None

    def lookup_doc_id_by_collection_and_path(self, collection, source_path):
        return f"resolved:{collection}:{source_path}"

    def delete_document(self, tumbler):
        self.deleted.append(str(tumbler))
        return True

    def close(self):
        pass


@pytest.fixture()
def service_mode_fresh_box(tmp_path, monkeypatch):
    monkeypatch.setenv("NX_STORAGE_BACKEND_CATALOG", "service")
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(tmp_path / "no-catalog-here"))
    _FakeHttpCatalogClient.instances = []
    _FakeHttpCatalogClient._seed = {}
    monkeypatch.setattr(
        "nexus.catalog.http_catalog_client.HttpCatalogClient",
        _FakeHttpCatalogClient,
    )
    factory.reset_shared_service_catalog_client_for_tests()
    yield tmp_path / "no-catalog-here"
    factory.reset_shared_service_catalog_client_for_tests()


def test_store_reap_runs_on_fresh_service_box(service_mode_fresh_box):
    """Pre-fix: the post-delete catalog tombstone reap silently returned
    on any box without a local catalog dir — orphan catalog rows survived
    every 'nx store delete' on fresh service-mode installs."""
    from nexus.commands.store import _reap_catalog_for_doc_ids

    _FakeHttpCatalogClient._seed = {"d" * 64: _Entry("1.1.9")}
    _reap_catalog_for_doc_ids(["d" * 64])
    (client,) = _FakeHttpCatalogClient.instances
    assert client.deleted == ["1.1.9"], (
        "reap must route through the service catalog on a fresh box"
    )


def test_catalog_writer_helper_no_false_setup_error(service_mode_fresh_box):
    """Pre-fix: _get_catalog_writer raised ClickException 'run nx catalog
    setup' on a healthy fresh service-mode box (active false diagnostic)."""
    from nexus.commands.catalog import _get_catalog_writer

    writer = _get_catalog_writer()
    try:
        assert writer is not None
    finally:
        writer.close()


# test_document_aspects_identity_probe_routes_service DELETED (nexus-i711w
# Stage 2 sub-stage A3): its subject was ``document_aspects._resolve_doc_id``
# — the SQLite DocumentAspects.upsert's catalog identity probe, whose kmo9h
# fix routed it through the factory instead of the frozen local catalog. The
# store (and the probe) died with the SQLite substrate; the Http store sends
# ``record.doc_id`` verbatim and the surviving client-side resolution lives
# in aspect_worker (``lookup_doc_id_by_collection_and_path`` via the
# factory-routed reader, tests/test_aspect_worker.py). The class-B census
# above (`test_raw_catalog_construction_census_is_closed`) still tripwires
# the raw-local-Catalog shape this test existed to prevent.


def test_catalog_setup_refuses_divergence_in_service_mode(
    service_mode_fresh_box,
):
    """'nx catalog setup' in service mode must NOT create a local catalog
    (divergent substrate) — it errors with the honest explanation."""
    from click.testing import CliRunner

    from nexus.commands.catalog import catalog as catalog_group

    result = CliRunner().invoke(catalog_group, ["setup"])
    assert result.exit_code != 0
    assert "service owns the catalog" in result.output
    assert not service_mode_fresh_box.exists(), (
        "setup must not have created a local catalog dir in service mode"
    )


def test_audit_render_distinguishes_skipped_from_clean():
    """nexus-kmo9h (critic item 2): 'couldn't check' must never render as
    'checked, clean' — the orphans leg is skipped in service mode (frozen
    local catalog) and that must be operator-visible."""
    from nexus.collection_audit import AuditReport, DistanceHistogram, format_audit_human

    def _report(checked: bool) -> str:
        return format_audit_human(
            AuditReport(
                collection="docs__x__bge-base-en-v15-768__v1",
                distance_histogram=DistanceHistogram(
                    buckets=[0] * 10, source="empty", sample_size=0
                ),
                cross_projections=[],
                orphans=[],
                hub_assignments=[],
                orphans_checked=checked,
            )
        )

    skipped = _report(False)
    assert "skipped — no local catalog to audit" in skipped
    assert "(none)" not in skipped.split("orphan chunks")[1].split("===")[0]

    clean = _report(True)
    assert "(none)" in clean
    assert "skipped" not in clean.split("orphan chunks")[1].split("===")[0]
