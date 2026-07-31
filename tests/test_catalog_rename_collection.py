# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-101 Phase 6: ``nx catalog rename-collection`` verb.

Combined verb that does both the data-plane rename (T3 native modify
+ T2 cascade + catalog docs re-point) and the Phase 6 control-plane
work (conformance check on new name, collections projection update,
CollectionSuperseded event emission).

Validation gates fire BEFORE any side effect:
  - new name must be conformant (or --allow-legacy)
  - old name must be in the collections projection
  - old name must not already be superseded
  - new name must not already exist in T3

CATALOG SUBSTRATE (nexus-i711w Stage 2 C-store). The verb is LIVE in service
mode, so its contracts PORT — but NOT by keeping the old
``patch("nexus.commands.catalog._get_catalog", return_value=<local Catalog>)``
seam. That patch made the verb's reads hit the injected local Catalog while
``rename_collection_data_plane``'s service branch (``storage_backend_for
("catalog") == SERVICE``, collection_rename.py:124) asked the REAL service to
rename a collection only the local fixture ever knew about — HTTP 404, or a
RecursionError when the injected object was itself factory-backed. The port
drops the catalog patches entirely: ``_get_catalog`` / ``_get_catalog_writer``
resolve through the env-routed factories against whichever catalog is live
(the engine's per-test tenant in this suite), and the tests SEED through the
same factories via :class:`tests._catalog_fixture_ops.ActiveCatalog`.

``patch("nexus.db.make_t3", ...)`` STAYS: it is the documented make_t3 test
seam (the engine substrate provisions no embedding model, so chunks cannot be
seeded into its vector store — the wall recorded on
tests/db/test_i711w_gap_xfails.py's item-13 verb test). The verb's T3
existence/collision gates read this injected handle; the service data plane
re-homes chunks engine-side where this suite has none to observe.

What the ported tests therefore do NOT assert, and where those contracts live:

  - OLD projection row surviving as a superseded tombstone
    (``superseded_by`` + ``superseded_at``): the engine rename DELETES the
    old ``catalog_collections`` row today, so the follow-up supersede no-ops.
    Carried as strict xfails by tests/db/test_i711w_gap_xfails.py::
    TestSupersedeCollectionSemantics (item 14 -> nexus-cecqy, nexus-g8z8n).
  - ``CollectionSuperseded`` in the local EVENT LOG: local-only machinery;
    the service-arm "supersession recorded" contract is the same item-14
    xfail family.
  - The local client-side fan-out (T2 cascade -> ``t3_db.rename_collection``
    -> catalog cascade + event emission): RETIRED with the local catalog
    (nexus-i711w terminal deletion — see the
    ``test_rename_conformant_new_succeeds_local_fanout`` tombstone below).
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from nexus.db.minilm_direct import MiniLMDirectEmbeddingFunction as DefaultEmbeddingFunction
from click.testing import CliRunner

from nexus.cli import main
from nexus.db.t3 import T3Database
from tests._catalog_fixture_ops import ActiveCatalog
from tests.conftest import make_vector_test_client


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def t3_db():
    """Fresh ephemeral T3 with all pre-existing collections deleted.

    The canonical injected vector-test substitute (the documented
    ``make_t3`` seam); process-singleton-ish, so other tests'
    collections persist across fixture rebuilds — explicitly clearing
    keeps assertions about collection_exists deterministic.
    """
    db = T3Database(
        _client=make_vector_test_client(),
        _ef_override=DefaultEmbeddingFunction(),
    )
    for raw in list(db._client.list_collections()):
        name = raw if isinstance(raw, str) else getattr(raw, "name", str(raw))
        try:
            db._client.delete_collection(name)
        except Exception:
            pass
    return db


@pytest.fixture()
def active_catalog() -> ActiveCatalog:
    """Seed and read through whichever catalog is live (nexus-i711w Stage 2)."""
    return ActiveCatalog()


# local_catalog fixture + _seed_local_catalog_doc helper RETIRED (nexus-i711w
# terminal deletion), together with their two DIE-set tests (tombstones at
# their original positions below). The local SQLite Catalog they constructed
# is deleted; the surviving contracts live in the ported tests here plus the
# tests/db/test_i711w_gap_xfails.py item-14/item-16 families.


def _seed_t3_collection(t3_db: T3Database, name: str) -> None:
    """Create a minimal collection in T3."""
    col = t3_db._client.get_or_create_collection(name)
    col.add(ids=["c1"], documents=["seeded chunk"], metadatas=[{"doc_id": "1.1.1"}])


# ── Validation gates fire before side effects ────────────────────────────


def test_rename_rejects_non_conformant_new(t3_db, active_catalog, runner):
    """The new name must match is_conformant_collection_name unless --allow-legacy."""
    active_catalog.register_collection("knowledge__delos")
    _seed_t3_collection(t3_db, "knowledge__delos")

    with patch("nexus.db.make_t3", return_value=t3_db):
        result = runner.invoke(
            main,
            ["catalog", "rename-collection",
             "knowledge__delos", "knowledge__papers"],
        )
    assert result.exit_code != 0
    assert "not conformant" in result.output.lower()
    # T3 untouched (old still exists, new wasn't created)
    assert t3_db.collection_exists("knowledge__delos")
    assert not t3_db.collection_exists("knowledge__papers")
    # Projection untouched: the gate fired before any write.
    assert active_catalog.get_collection("knowledge__papers") is None


def test_rename_rejects_old_not_in_projection(t3_db, active_catalog, runner):
    _seed_t3_collection(t3_db, "knowledge__delos")
    # Note: old name not registered in the collections projection —
    # a fresh per-test tenant genuinely has no row for it.
    assert active_catalog.get_collection("knowledge__delos") is None

    with patch("nexus.db.make_t3", return_value=t3_db):
        result = runner.invoke(
            main,
            ["catalog", "rename-collection",
             "knowledge__delos",
             "knowledge__1-1__voyage-context-3__v1"],
        )
    assert result.exit_code != 0
    assert "not registered" in result.output.lower()


def test_rename_rejects_already_superseded(t3_db, active_catalog, runner):
    """The verb refuses to rename a projection row that is already
    superseded.

    Retirement coordination (nexus-i711w C-store): this used to be the last
    LOCAL pin of the supersede-state machinery it seeds through. The
    supersede SEMANTICS themselves (guards, timestamp, tombstone survival)
    are now carried by tests/db/test_i711w_gap_xfails.py item-14
    (nexus-cecqy, nexus-g8z8n); what THIS test pins is the rename verb's
    client-side refusal gate, which reads ``get_collection(old).
    superseded_by`` off the live catalog. The non-vacuity guard below
    asserts the seeded state actually landed (on the service arm today the
    supersede does set ``superseded_by`` on a surviving row; only its
    validation/timestamping is the xfail'd gap).
    """
    active_catalog.register_collection("knowledge__delos")
    active_catalog.register_collection(
        "knowledge__1-1__voyage-context-3__v1",
        content_type="knowledge", owner_id="1-1",
        embedding_model="voyage-context-3", model_version="v1",
    )
    active_catalog.supersede_collection(
        "knowledge__delos", "knowledge__1-1__voyage-context-3__v1",
    )
    # NON-VACUITY guard: the already-superseded premise is real.
    seeded = active_catalog.get_collection("knowledge__delos")
    assert seeded is not None
    assert seeded.get("superseded_by") == "knowledge__1-1__voyage-context-3__v1", (
        "guard: the seed supersede must have landed before probing the gate"
    )
    _seed_t3_collection(t3_db, "knowledge__delos")

    with patch("nexus.db.make_t3", return_value=t3_db):
        result = runner.invoke(
            main,
            ["catalog", "rename-collection",
             "knowledge__delos",
             "knowledge__1-1__voyage-context-3__v2"],
        )
    assert result.exit_code != 0
    assert "superseded" in result.output.lower()


def test_rename_rejects_new_already_exists_in_t3(t3_db, active_catalog, runner):
    active_catalog.register_collection("knowledge__delos")
    _seed_t3_collection(t3_db, "knowledge__delos")
    _seed_t3_collection(t3_db, "knowledge__1-1__voyage-context-3__v1")

    with patch("nexus.db.make_t3", return_value=t3_db):
        result = runner.invoke(
            main,
            ["catalog", "rename-collection",
             "knowledge__delos",
             "knowledge__1-1__voyage-context-3__v1"],
        )
    assert result.exit_code != 0
    assert "already exists" in result.output.lower()


# ── Happy path ────────────────────────────────────────────────────────────


def test_rename_conformant_new_succeeds(t3_db, active_catalog, runner):
    """End-to-end rename against the LIVE catalog: projection row for the
    new name registered with parsed segments, catalog docs re-pointed by
    the cascade, verb exits 0.

    Converted by meaning (nexus-i711w C-store): the T3-movement and
    old-row-tombstone / event-log halves of the original local test are
    NOT asserted here — see the module docstring for where each contract
    now lives (make_t3 seam; item-14 xfails; the pinned
    ``..._local_fanout`` twin below).
    """
    active_catalog.register_collection("knowledge__delos")
    _seed_t3_collection(t3_db, "knowledge__delos")
    # Seed one catalog document in the old collection through the live
    # write path (replaces the raw ``_db.execute`` INSERT, which has no
    # service-mode equivalent).
    active_catalog.register_owner(
        name="rename-verb-owner",
        owner_type="curator",
        tumbler_prefix="1.1",  # PORT-VERIFY: fresh per-test tenant, prefix free
    )
    doc_tumbler = active_catalog.register(
        "1.1",
        "rename-verb doc",
        content_type="text",
        physical_collection="knowledge__delos",
        source_uri="file:///rename-verb/doc.md",
    )

    with patch("nexus.db.make_t3", return_value=t3_db):
        result = runner.invoke(
            main,
            ["catalog", "rename-collection",
             "knowledge__delos",
             "knowledge__1-1__voyage-context-3__v1",
             "--yes"],
        )
    assert result.exit_code == 0, result.output

    # Collections projection: new is registered with parsed segments and
    # not legacy (explicit-segment register_collection path — correct on
    # both arms; the BARE path's derivation gap is nexus-cecqy item 16).
    new_row = active_catalog.get_collection("knowledge__1-1__voyage-context-3__v1")
    assert new_row is not None
    assert new_row["legacy_grandfathered"] is False
    assert new_row["content_type"] == "knowledge"
    assert new_row["embedding_model"] == "voyage-context-3"

    # Catalog documents re-pointed by the cascade (service arm: inside the
    # engine's one-transaction rename; read back through the live reader).
    entry = active_catalog.by_doc_id(str(doc_tumbler))
    assert entry is not None
    assert entry.physical_collection == "knowledge__1-1__voyage-context-3__v1"


# test_rename_conformant_new_succeeds_local_fanout RETIRED (nexus-i711w
# terminal deletion): the PINNED DIE-set twin of the ported happy path above.
# Every observable was SQLite-era machinery (local client-side fan-out, local
# projection rows, CollectionSuperseded in the local event log, raw ``_db``
# re-point reads) — all deleted with the local catalog, exactly as its
# docstring scheduled. Surviving contracts: the ported
# test_rename_conformant_new_succeeds above + the item-14 strict xfails
# (tests/db/test_i711w_gap_xfails.py, nexus-cecqy / nexus-g8z8n).


def test_rename_dry_run_no_writes(t3_db, active_catalog, runner):
    """``--dry-run`` reports the plan and writes nothing."""
    active_catalog.register_collection("knowledge__delos")
    _seed_t3_collection(t3_db, "knowledge__delos")

    with patch("nexus.db.make_t3", return_value=t3_db):
        result = runner.invoke(
            main,
            ["catalog", "rename-collection",
             "knowledge__delos",
             "knowledge__1-1__voyage-context-3__v1",
             "--dry-run"],
        )
    assert result.exit_code == 0, result.output
    assert "would rename" in result.output.lower()
    assert t3_db.collection_exists("knowledge__delos")
    assert not t3_db.collection_exists("knowledge__1-1__voyage-context-3__v1")
    # Projection untouched: nothing registered for the new name, old row
    # not superseded. (``not ...get("superseded_by")`` rather than == "":
    # by meaning — the service arm may render the empty pointer as None.)
    assert active_catalog.get_collection("knowledge__1-1__voyage-context-3__v1") is None
    old_row = active_catalog.get_collection("knowledge__delos")
    assert old_row is not None
    assert not old_row.get("superseded_by")


def test_rename_no_yes_falls_back_to_report_only(t3_db, active_catalog, runner):
    """Neither ``--yes`` nor ``--dry-run`` falls back to report-only."""
    active_catalog.register_collection("knowledge__delos")
    _seed_t3_collection(t3_db, "knowledge__delos")

    with patch("nexus.db.make_t3", return_value=t3_db):
        result = runner.invoke(
            main,
            ["catalog", "rename-collection",
             "knowledge__delos",
             "knowledge__1-1__voyage-context-3__v1"],
        )
    assert result.exit_code == 0
    assert "add --yes" in result.output.lower()
    assert t3_db.collection_exists("knowledge__delos")
    # Report-only: nothing registered for the new name.
    assert active_catalog.get_collection("knowledge__1-1__voyage-context-3__v1") is None


def test_rename_to_self_rejected(t3_db, active_catalog, runner):
    """Renaming OLD to itself is a no-op; reject with a clear message
    rather than running the full data plane that would then trip the
    ``new already exists`` gate (misleading).
    """
    active_catalog.register_collection(
        "knowledge__1-1__voyage-context-3__v1",
        content_type="knowledge", owner_id="1-1",
        embedding_model="voyage-context-3", model_version="v1",
    )
    _seed_t3_collection(t3_db, "knowledge__1-1__voyage-context-3__v1")

    with patch("nexus.db.make_t3", return_value=t3_db):
        result = runner.invoke(
            main,
            ["catalog", "rename-collection",
             "knowledge__1-1__voyage-context-3__v1",
             "knowledge__1-1__voyage-context-3__v1",
             "--yes"],
        )
    assert result.exit_code != 0
    assert "identical" in result.output.lower()


# test_rename_allow_legacy_lets_non_conformant_through RETIRED (nexus-i711w
# terminal deletion): it observed the LOCAL projector's
# ``legacy_grandfathered`` derivation, which was deleted with the local
# catalog — the "stays pinned, passing" disposition it recorded could only
# hold while the substrate existed. The service-side derivation gap it
# documented (bare register_collection never lands legacy_grandfathered=True
# on the live catalog) remains tracked as nexus-cecqy / i711w.1 item 16, with
# the landed strict-xfail successor in tests/db/test_i711w_gap_xfails.py
# (item 16: legacy_grandfathered derivation) — that xfail flips to the live
# regression test when HttpCatalogClient.register_collection derives it.


# ── nexus-cecqy: the rename must not claim a supersede that did not happen ────


def test_rename_does_not_claim_supersede_when_no_row_was_marked(monkeypatch, tmp_path):
    """Service-mode rename DELETEs the old registry row (renameCollectionTxn —
    it must DELETE, because the fk-002/fk-003 collection FKs are ON UPDATE NO
    ACTION), so the follow-up supersede UPDATE matches ZERO rows.

    The CLI announced "Emitted CollectionSuperseded(...)" regardless. That was a
    claim about something that did not happen: the old name ends up ABSENT
    rather than marked-superseded, the rename's history is unrecorded in
    superseded_by/superseded_at, and the operator is told the opposite.
    """
    from unittest.mock import MagicMock

    from click.testing import CliRunner

    from nexus.commands import catalog as _cat_cmd
    from nexus.cli import main

    from nexus.db.collection_state import CollectionState

    writer = MagicMock()
    writer.supersede_collection.return_value = 0   # the row was already deleted
    writer.rename_collection_cascade.return_value = {"catalog_documents": 3}
    writer.rename_collection.return_value = 3

    reader = MagicMock()
    reader.get_collection.return_value = {"name": "code__old__stub-code-1024__v1"}

    monkeypatch.setattr(_cat_cmd, "_get_catalog_writer", lambda *a, **k: writer, raising=False)
    monkeypatch.setattr(_cat_cmd, "_get_catalog", lambda *a, **k: reader, raising=False)
    monkeypatch.setattr(
        "nexus.commands.catalog_cmds.collections.make_t3",
        lambda *a, **k: MagicMock(), raising=False,
    )
    # old exists in T3, new does not — the only state the rename proceeds from.
    def _state(_t3, name):
        return (CollectionState.ABSENT
                if name.endswith("new__stub-code-1024__v1")
                else CollectionState.PRESENT)

    # Patch every module that bound its OWN reference at import time — a
    # module-level `from ... import probe_collection_state` does not see a
    # patch applied to the source module.
    for target in (
        "nexus.db.collection_state.probe_collection_state",
        "nexus.commands.catalog_cmds.collections.probe_collection_state",
        "nexus.collection_rename.probe_collection_state",
    ):
        monkeypatch.setattr(target, _state, raising=False)
    monkeypatch.setattr(
        "nexus.collection_rename.rename_collection_everywhere",
        lambda *a, **k: {"t3": 3}, raising=False,
    )

    result = CliRunner().invoke(
        main,
        ["catalog", "rename-collection",
         "code__old__stub-code-1024__v1", "code__new__stub-code-1024__v1", "--yes"],
    )

    # NON-VACUITY FIRST: the command must actually have REACHED the announce
    # site. Asserting only the absence of a string passes trivially when the
    # CLI bailed earlier — which is exactly what a first cut of this test did.
    assert result.exit_code == 0, f"rename did not run: {result.output}"
    assert "Renamed:" in result.output, (
        f"the rename never reached its summary, so the assertion below would "
        f"pass vacuously. Output: {result.output}"
    )

    assert "Emitted CollectionSuperseded" not in result.output, (
        "the CLI announced a supersede that affected zero rows — the exact "
        "false claim nexus-cecqy is about"
    )
