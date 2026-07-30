# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-103 Phase 4: legacy-to-conformant collection migration on first index.

The indexer detects pre-RDR-103 legacy collection names in T3 and
renames them in place to the conformant
``<content_type>__<owner_id>__<embedding_model>__v1`` shape. The
migration:

  - Runs once per (repo, content_type) pair. Idempotent: re-runs
    skip the rename and emit no message.
  - Uses ``rename_collection_data_plane`` (T3 native modify + T2
    cascade + catalog re-point + collections projection update +
    CollectionSuperseded event), NOT a per-document update loop.
  - Skips the rename when both legacy and conformant exist (partial
    state from a prior interrupted run); the indexer proceeds against
    the conformant collection and the legacy collection is left for
    operator cleanup.
  - Updates the registry so subsequent runs see conformant names
    directly without invoking the migration path.

Tests pin the decision tree per the bead's scope (`nexus-yqnr.6`).

CATALOG SUBSTRATE (nexus-i711w Stage 2 C-store). ``_migrate_legacy_collections``
is LIVE in service mode — first index after a guided upgrade probes T3 for
legacy shapes and, when found, renames via the ONE-call engine cascade — so the
verb contract ports. The split here is per decision-tree case:

  PORT (``active_catalog`` + ``service_repo_with_owner``): the NO-RENAME
  cases — steady state (2), both-exist skip (3), greenfield (4), catalog-None
  and owner-unregistered no-ops. These exercise ``owner_for_repo`` +
  ``collection_for`` on whichever catalog is live (the engine in this suite),
  with the injected ``t3`` remaining the canonical vector-test substitute the
  ``make_t3(_client=...)`` seam documents.

  RETIRED (nexus-i711w terminal deletion — was the PINNED
  ``local_catalog`` + ``local_repo_with_owner`` cohort, 7 tests): every
  test that EXECUTED decision-tree case 1 (the rename). The executed leg is the
  SQLite-era client-side fan-out (T2 cascade -> ``t3_db.rename_collection`` ->
  catalog cascade) driven with an injected local Catalog + in-memory T3; in
  service mode ``collection_rename.py`` branches to a single transactional
  ``client.rename_collection_cascade(old, new)`` on the engine and never calls
  ``t3_db.rename_collection``, so the injected pairing is meaningless there —
  and the unit suite's engine substrate cannot SEED pgvector chunks (no
  embedding model provisioned; the same wall recorded on
  tests/db/test_i711w_gap_xfails.py's item-13 verb test), so the case-1 trigger
  (probe finds legacy chunks PRESENT in the engine's T3) is unreachable. They
  retired with the local catalog fan-out, in the same commit as the src.

  GAP-CANDIDATE (recorded, not silently dropped): once the local fan-out is
  deleted, decision-tree case 1 — rename fires, one Upgraded message, registry
  rewrite, conformant-name return on post-rename projection failure — has NO
  test executing it against a real engine. The service-arm coverage that
  exists is the client ROUTING contract (tests/test_collection_rename_
  service_mode.py, MagicMock cascade) and the engine's rename txn itself; the
  decision tree above them is unpinned. Needs a new engine-backed test that
  seeds chunk rows via SQL (bypassing server-side embed), per rule-5 of the
  substrate-port discipline.
"""
from __future__ import annotations
from nexus.db.minilm_direct import MiniLMDirectEmbeddingFunction

from pathlib import Path

import pytest

from nexus.corpus import is_conformant_collection_name
from nexus.db.t3 import T3Database
from nexus.indexer import _legacy_collection_name
from nexus.registry import RepoRegistry
from tests._catalog_fixture_ops import ActiveCatalog
from tests.conftest import make_vector_test_client

# RDR-109 Phase 2: this file asserts cloud-mode canonical behavior
# (voyage-* embedder names, canonical-set defaults). The cloud_mode
# fixture sets credentials and forces ``is_local_mode()`` to False so
# the assertions hold regardless of the host environment.
#
# nexus-i711w C-store: the module-wide ``local_catalog_backend`` pin moved
# into the ``local_catalog`` fixture so only the DIE-set tests carry it —
# a single module ``pytestmark`` assignment REPLACES rather than appends
# (the nexus-aqbrk lesson), so cloud_mode stays the one module mark.
pytestmark = pytest.mark.usefixtures("cloud_mode")


def _collection_name(repo):
    return _legacy_collection_name(repo, "code")


@pytest.fixture()
def t3():
    """T3 backed by the canonical vector-test client with a local
    embedding function (no Voyage API key required).

    This is NOT dying machinery: ``make_t3(_client=...)`` documents the
    injected-client T3Database facade as the canonical test substitute
    (RDR-155 P4b P2). The fixture clears every collection on entry to
    keep tests isolated (the client shares process-level state across
    instances). Mirrors ``test_t3_strict_collection_naming.py``.
    """
    client = make_vector_test_client()
    ef = MiniLMDirectEmbeddingFunction()
    db = T3Database(_client=client, _ef_override=ef)
    for raw in list(db._client.list_collections()):
        name = raw if isinstance(raw, str) else getattr(raw, "name", str(raw))
        try:
            db._client.delete_collection(name)
        except Exception:
            pass
    return db


@pytest.fixture()
def active_catalog() -> ActiveCatalog:
    """Seed and read through whichever catalog is live (nexus-i711w Stage 2).

    The migration helper only calls reads (``owner_for_repo``,
    ``collection_for``) on it outside decision-tree case 1, and
    ``ActiveCatalog`` proxies both to the live reader.
    """
    return ActiveCatalog()


@pytest.fixture()
def service_repo_with_owner(active_catalog, tmp_path: Path, monkeypatch) -> Path:
    """A repo whose owner is registered in the LIVE catalog under a pinned
    tumbler prefix, so the conformant-name assertions stay literal.

    ``tumbler_prefix="1.1"`` uses the ETL/migration path of
    ``register_owner`` (accepted by the service client; each test runs in
    a freshly minted tenant so the prefix is always free). The local
    ``Catalog.register_owner`` has no such kwarg — this fixture is for the
    PORTED tests, which run against the live (engine) catalog.
    """
    repo = tmp_path / "myproject"
    repo.mkdir()
    active_catalog.register_owner(
        name="myproject",
        owner_type="repo",
        repo_hash="cafef00d",
        repo_root=str(repo),
        tumbler_prefix="1.1",  # PORT-VERIFY: deterministic owner segment "1-1"
    )
    monkeypatch.setattr(
        "nexus.repo_identity._repo_identity",
        lambda r: ("myproject", "cafef00d"),
    )
    return repo


@pytest.fixture()
def registry(tmp_path: Path) -> RepoRegistry:
    return RepoRegistry(tmp_path / "repos.json")


def _make_collection(t3: T3Database, name: str) -> None:
    """Create an empty collection in T3 so the migration sees it.

    Uses ``strict=False`` so test fixtures may seed pre-RDR-103 legacy
    2-segment names (the very thing the migration helper exists to
    rename); production callers go through the strict default.
    """
    t3.get_or_create_collection(name, strict=False)


def _seed_collection_with_chunk(t3: T3Database, name: str) -> None:
    """Create a collection and seed one document so the migration's
    rename has data to move (smoke-tests that data survives the
    rename). Pre-creates with ``strict=False`` so legacy fixture names
    bypass the strict-naming guard before ``t3.put`` lands the chunk.
    """
    t3.get_or_create_collection(name, strict=False)
    t3.put(collection=name, content="seed body", title="seed", tags="seed")


# nexus-i711w terminal deletion: the case-1 (rename executes) DIE cohort —
# test_migration_greenfield_legacy_renamed_to_conformant,
# test_migration_renames_t3_collection,
# test_migration_updates_registry_to_conformant,
# test_migration_idempotent_no_message_second_run,
# test_migration_uses_atomic_rename_not_per_doc_retarget,
# test_migration_returns_conformant_when_register_fails_after_rename,
# test_migration_handles_code_and_docs_independently — retired WITH the
# local catalog fan-out. Case-1 decision-tree coverage against a real
# engine is the GAP-CANDIDATE recorded in the module docstring.


# ── Idempotency: re-index after migration ──────────────────────────────────


def test_migration_returns_conformant_when_steady_state(
    service_repo_with_owner: Path, active_catalog, t3, registry: RepoRegistry,
) -> None:
    """Steady state: only the conformant collection exists. The
    helper still returns the conformant name for the caller to use.

    PORTED (nexus-i711w C-store): resolves the owner + conformant name
    through the LIVE catalog; no rename executes on this branch.
    """
    from nexus.indexer import _migrate_legacy_collections

    conformant = "docs__1-1__voyage-context-3__v1"
    _make_collection(t3, conformant)
    # Registry is empty / has conformant docs already.
    registry.add(service_repo_with_owner, cat=active_catalog)

    result = _migrate_legacy_collections(
        service_repo_with_owner, cat=active_catalog, t3_db=t3, registry=registry,
    )
    assert result["docs"] == conformant


# ── Partial state both-exist: skip rename ──────────────────────────────────


def test_migration_skips_when_both_collections_exist(
    service_repo_with_owner: Path, active_catalog, t3, registry: RepoRegistry,
) -> None:
    """If a prior interrupted run left both legacy and conformant in
    T3, the migration must NOT attempt the rename (would fail because
    target already exists). The helper returns the conformant name and
    the legacy collection is left untouched.

    PORTED (nexus-i711w C-store): case 3 never calls the data plane, so
    the whole decision runs against the live catalog + injected T3.
    """
    from nexus.indexer import _migrate_legacy_collections

    legacy = _collection_name(service_repo_with_owner)
    conformant = "code__1-1__voyage-code-3__v1"
    _seed_collection_with_chunk(t3, legacy)
    _make_collection(t3, conformant)
    registry.add(service_repo_with_owner)

    messages: list[str] = []
    result = _migrate_legacy_collections(
        service_repo_with_owner, cat=active_catalog, t3_db=t3, registry=registry,
        on_message=messages.append,
    )

    # Conformant returned (indexer proceeds against it).
    assert result["code"] == conformant
    # Legacy is untouched (operator cleanup later).
    assert t3.collection_exists(legacy)
    assert t3.collection_exists(conformant)
    # No "Upgraded" message — only an advisory about the partial state.
    assert not any("Upgraded" in m for m in messages)


# ── Catalog absent / owner missing ─────────────────────────────────────────


def test_migration_no_op_when_catalog_uninitialized(
    tmp_path: Path, t3, registry: RepoRegistry, monkeypatch,
) -> None:
    """Catalog is None: migration is a no-op. Returns an empty map so
    the caller falls back to its own resolution (registry or legacy
    helper). The legacy collection in T3 is untouched.

    PORTED (nexus-i711w C-store): substrate-neutral — ``cat=None`` short-
    circuits before any catalog access.
    """
    from nexus.indexer import _migrate_legacy_collections

    repo = tmp_path / "uncataloged"
    repo.mkdir()
    monkeypatch.setattr(
        "nexus.repo_identity._repo_identity",
        lambda r: ("uncataloged", "abcdef12"),
    )
    legacy = _collection_name(repo)
    _seed_collection_with_chunk(t3, legacy)

    result = _migrate_legacy_collections(
        repo, cat=None, t3_db=t3, registry=registry,
    )

    # Empty map: caller's existing fallback handles name resolution.
    assert result == {}
    # Legacy collection untouched.
    assert t3.collection_exists(legacy)


def test_migration_no_op_when_owner_unregistered(
    active_catalog, tmp_path: Path, t3, registry: RepoRegistry, monkeypatch,
) -> None:
    """Catalog initialized but no owner row for this repo: migration
    is a no-op. Returns an empty map so the caller's existing fallback
    handles this run. The _catalog_hook registers the owner later;
    subsequent runs will migrate.

    PORTED (nexus-i711w C-store): the live catalog (a fresh per-test
    tenant on the engine arm) genuinely has no owner for this hash, so
    ``owner_for_repo`` returns None through the real read path.
    """
    from nexus.indexer import _migrate_legacy_collections

    repo = tmp_path / "unregistered"
    repo.mkdir()
    monkeypatch.setattr(
        "nexus.repo_identity._repo_identity",
        lambda r: ("unregistered", "fade1234"),
    )
    legacy = _collection_name(repo)
    _seed_collection_with_chunk(t3, legacy)

    result = _migrate_legacy_collections(
        repo, cat=active_catalog, t3_db=t3, registry=registry,
    )

    assert result == {}
    assert t3.collection_exists(legacy)


# ── Greenfield (no T3 collections yet) ─────────────────────────────────────


def test_migration_returns_conformant_rdr_on_greenfield(
    service_repo_with_owner: Path, active_catalog, t3, registry: RepoRegistry,
) -> None:
    """Greenfield repo: catalog has owner, registry is empty, T3 has
    no collections yet. The migration must return the conformant rdr
    name so the indexer creates the conformant collection on first
    write rather than re-creating a legacy one. This pins the case-4
    branch (neither legacy nor conformant exists).

    PORTED (nexus-i711w C-store): ``collection_for`` resolves v1 for a
    never-registered tuple through the live catalog (the service arm's
    /collections/for_tuple 404 -> v1 contract, nexus-njrcn.2).
    """
    from nexus.indexer import _migrate_legacy_collections

    # No collections in T3, no registry entry beyond the empty repo.
    registry.add(service_repo_with_owner)

    result = _migrate_legacy_collections(
        service_repo_with_owner, cat=active_catalog, t3_db=t3, registry=registry,
    )
    assert result["rdr"] == "rdr__1-1__voyage-context-3__v1"
    assert result["code"] == "code__1-1__voyage-code-3__v1"
    assert result["docs"] == "docs__1-1__voyage-context-3__v1"
