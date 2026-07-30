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

  PINNED (``local_catalog`` + ``local_repo_with_owner``): every test that
  EXECUTES decision-tree case 1 (the rename). The executed leg is the
  SQLite-era client-side fan-out (T2 cascade -> ``t3_db.rename_collection`` ->
  catalog cascade) driven with an injected local Catalog + in-memory T3; in
  service mode ``collection_rename.py`` branches to a single transactional
  ``client.rename_collection_cascade(old, new)`` on the engine and never calls
  ``t3_db.rename_collection``, so the injected pairing is meaningless there —
  and the unit suite's engine substrate cannot SEED pgvector chunks (no
  embedding model provisioned; the same wall recorded on
  tests/db/test_i711w_gap_xfails.py's item-13 verb test), so the case-1 trigger
  (probe finds legacy chunks PRESENT in the engine's T3) is unreachable. These
  retire with the local catalog fan-out, in the same commit as the src.

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
from unittest.mock import patch

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
def local_catalog(tmp_path: Path, local_catalog_backend):
    """A real LOCAL SQLite ``Catalog`` — DIE-set tests only.

    ``local_catalog_backend`` is requested so the pin is explicit: it keeps
    ``storage_backend_for("catalog")`` on SQLITE, which is what routes
    ``rename_collection_data_plane`` down the client-side fan-out these
    tests observe. Retirement note: this fixture and its tests go with the
    local catalog itself (nexus-i711w), in the same commit as the src.
    """
    from nexus.catalog.catalog import Catalog  # noqa: PLC0415 — dying import stays body-local so the file still collects post-deletion

    cat_dir = tmp_path / "catalog"
    cat = Catalog.init(cat_dir)
    return cat


@pytest.fixture()
def local_repo_with_owner(local_catalog, tmp_path: Path, monkeypatch) -> Path:
    """A repo registered in the LOCAL catalog with a known repo_hash that
    ``_migrate_legacy_collections`` can resolve. DIE-set companion of
    ``service_repo_with_owner``."""
    repo = tmp_path / "myproject"
    repo.mkdir()
    local_catalog.register_owner(
        name="myproject",
        owner_type="repo",
        repo_hash="cafef00d",
        repo_root=str(repo),
    )
    monkeypatch.setattr(
        "nexus.repo_identity._repo_identity",
        lambda r: ("myproject", "cafef00d"),
    )
    return repo


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


# ── Greenfield-with-legacy: rename happens once, message emitted once ──────
#
# PINNED (DIE set) — every test in this section EXECUTES decision-tree
# case 1, whose executed leg is the local client-side fan-out (T2 cascade ->
# ``t3_db.rename_collection`` -> catalog cascade). See the module docstring's
# substrate note: the service arm folds all of that into one engine call the
# injected local T3 never sees, and the engine's own vector store cannot be
# seeded from the unit suite, so these retire with the local catalog
# (nexus-i711w) — with the case-1 decision-tree contract recorded as a
# GAP-CANDIDATE for an engine-backed successor test.


def test_migration_greenfield_legacy_renamed_to_conformant(
    local_repo_with_owner: Path, local_catalog, t3, registry: RepoRegistry,
) -> None:
    """Repo has legacy ``code__myproject-cafef00d`` from a pre-RDR-103
    index. Migration renames to conformant
    ``code__1-1__voyage-code-3__v1`` and emits one upgrade message.
    """
    from nexus.indexer import _migrate_legacy_collections

    legacy = _collection_name(local_repo_with_owner)
    _seed_collection_with_chunk(t3, legacy)
    registry.add(local_repo_with_owner)  # legacy shape, no catalog
    messages: list[str] = []

    result = _migrate_legacy_collections(
        local_repo_with_owner,
        cat=local_catalog, t3_db=t3, registry=registry,
        on_message=messages.append,
    )

    # Conformant name returned.
    assert is_conformant_collection_name(result["code"])
    assert result["code"] == "code__1-1__voyage-code-3__v1"
    # Exactly one upgrade message emitted (for code).
    code_msgs = [m for m in messages if "code" in m]
    assert len(code_msgs) == 1
    assert "Upgraded" in code_msgs[0] and legacy in code_msgs[0]


def test_migration_renames_t3_collection(
    local_repo_with_owner: Path, local_catalog, t3, registry: RepoRegistry,
) -> None:
    """Post-migration, T3 has the conformant collection but NOT the
    legacy. Native modify(name=) preserved the data."""
    from nexus.indexer import _migrate_legacy_collections

    legacy = _collection_name(local_repo_with_owner)
    _seed_collection_with_chunk(t3, legacy)
    registry.add(local_repo_with_owner)

    _migrate_legacy_collections(
        local_repo_with_owner, cat=local_catalog, t3_db=t3, registry=registry,
    )

    conformant = "code__1-1__voyage-code-3__v1"
    assert t3.collection_exists(conformant)
    assert not t3.collection_exists(legacy)


def test_migration_updates_registry_to_conformant(
    local_repo_with_owner: Path, local_catalog, t3, registry: RepoRegistry,
) -> None:
    """Registry's ``code_collection`` field is rewritten so subsequent
    indexer runs see the conformant name directly."""
    from nexus.indexer import _migrate_legacy_collections

    _seed_collection_with_chunk(t3, _collection_name(local_repo_with_owner))
    registry.add(local_repo_with_owner)

    _migrate_legacy_collections(
        local_repo_with_owner, cat=local_catalog, t3_db=t3, registry=registry,
    )

    info = registry.get(local_repo_with_owner)
    assert info is not None
    assert info["code_collection"] == "code__1-1__voyage-code-3__v1"


# ── Idempotency: re-index after migration ──────────────────────────────────


def test_migration_idempotent_no_message_second_run(
    local_repo_with_owner: Path, local_catalog, t3, registry: RepoRegistry,
) -> None:
    """Second invocation against the same repo emits no upgrade
    message (legacy already absent in T3).

    PINNED (DIE set): the FIRST run executes the case-1 rename via the
    local fan-out. The second-run steady-state contract on its own is
    ported below (``test_migration_returns_conformant_when_steady_state``
    reaches the same case-2 branch without the rename prologue).
    """
    from nexus.indexer import _migrate_legacy_collections

    _seed_collection_with_chunk(t3, _collection_name(local_repo_with_owner))
    registry.add(local_repo_with_owner)

    # First run does the rename.
    _migrate_legacy_collections(
        local_repo_with_owner, cat=local_catalog, t3_db=t3, registry=registry,
    )

    # Second run: legacy absent in T3, conformant present, no message.
    messages: list[str] = []
    _migrate_legacy_collections(
        local_repo_with_owner, cat=local_catalog, t3_db=t3, registry=registry,
        on_message=messages.append,
    )
    assert messages == []


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


# ── Atomic rename (not per-document retarget) ──────────────────────────────


def test_migration_uses_atomic_rename_not_per_doc_retarget(
    local_repo_with_owner: Path, local_catalog, t3, registry: RepoRegistry,
) -> None:
    """The migration must use the data-plane atomic rename
    (``rename_collection_data_plane``), NOT a per-document
    ``update_documents_collection_batch`` loop. The atomic path is
    O(1) on the T3 side via native ``modify(name=)``; the per-doc
    retarget would be O(n) and would re-embed if it touched chunks.

    Test surface: spy on both possible code paths and assert the
    atomic one fires.

    PINNED (DIE set): the spy target ``t3.rename_collection`` IS the
    local fan-out observable — the service branch never calls it by
    design. The service-arm form of this same atomicity contract is
    tests/test_collection_rename_service_mode.py::
    test_service_mode_uses_single_endpoint_and_maps_counts (one cascade
    call, ``t3.rename_collection.assert_not_called()``).
    """
    from nexus.indexer import _migrate_legacy_collections

    _seed_collection_with_chunk(t3, _collection_name(local_repo_with_owner))
    registry.add(local_repo_with_owner)

    rename_calls = []
    original = t3.rename_collection

    def spy(old, new):
        rename_calls.append((old, new))
        return original(old, new)

    with patch.object(t3, "rename_collection", side_effect=spy):
        _migrate_legacy_collections(
            local_repo_with_owner, cat=local_catalog, t3_db=t3, registry=registry,
        )

    # Exactly one atomic rename invocation for the code collection.
    assert len(rename_calls) == 1
    assert rename_calls[0][0] == _collection_name(local_repo_with_owner)
    assert rename_calls[0][1] == "code__1-1__voyage-code-3__v1"


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


# ── Partial migration failure: data-plane succeeds, projection fails ────────


def test_migration_returns_conformant_when_register_fails_after_rename(
    local_repo_with_owner: Path, local_catalog, t3, registry: RepoRegistry,
) -> None:
    """If ``rename_collection_data_plane`` succeeds (data is now at
    conformant in T3) but the subsequent ``register_collection``
    raises, the migration MUST return the conformant name so the
    caller writes fresh chunks to the same collection that holds the
    migrated data. Returning the legacy name here would create a
    fresh empty legacy collection alongside the conformant one and
    split the data.
    """
    from nexus.indexer import _migrate_legacy_collections

    legacy = _collection_name(local_repo_with_owner)
    _seed_collection_with_chunk(t3, legacy)
    registry.add(local_repo_with_owner)

    with patch.object(
        local_catalog, "register_collection",
        side_effect=RuntimeError("simulated projection failure"),
    ):
        result = _migrate_legacy_collections(
            local_repo_with_owner, cat=local_catalog, t3_db=t3, registry=registry,
        )

    # Conformant name returned: data lives there now.
    assert result["code"] == "code__1-1__voyage-code-3__v1"
    # T3 reflects the rename even though register_collection raised.
    assert t3.collection_exists("code__1-1__voyage-code-3__v1")
    assert not t3.collection_exists(legacy)


# ── Multiple content types in one pass ─────────────────────────────────────


def test_migration_handles_code_and_docs_independently(
    local_repo_with_owner: Path, local_catalog, t3, registry: RepoRegistry,
) -> None:
    """The decision tree applies per content_type. A repo with a
    legacy code collection AND a steady-state conformant docs
    collection migrates only the code one and emits one message."""
    from nexus.indexer import _migrate_legacy_collections

    legacy_code = _collection_name(local_repo_with_owner)
    conformant_docs = "docs__1-1__voyage-context-3__v1"
    _seed_collection_with_chunk(t3, legacy_code)
    _make_collection(t3, conformant_docs)
    registry.add(local_repo_with_owner)
    # Make registry's docs_collection point to conformant manually.
    registry.update(local_repo_with_owner, docs_collection=conformant_docs)

    messages: list[str] = []
    result = _migrate_legacy_collections(
        local_repo_with_owner, cat=local_catalog, t3_db=t3, registry=registry,
        on_message=messages.append,
    )

    assert result["code"] == "code__1-1__voyage-code-3__v1"
    assert result["docs"] == conformant_docs
    upgrade_msgs = [m for m in messages if "Upgraded" in m]
    assert len(upgrade_msgs) == 1
    assert "code" in upgrade_msgs[0]
