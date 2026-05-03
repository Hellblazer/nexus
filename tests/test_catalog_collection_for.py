# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-103 Phase 2: Catalog.collection_for + owner_segment_for_tumbler.

Pinned decisions exercised here:

  #1  Migration uses indexer's CURRENT canonical model. The version-bump
      lookup ignores grandfathered legacy rows; only conformant rows
      contribute to MAX(model_version).
  #2  Model-name change is NOT a version bump. A new ``embedding_model``
      tuple lands in ``v1`` even when an older model already has rows at
      ``v3``.

Phase 2 also adds the compound index ``idx_collections_tuple`` so the
version-bump lookup is a single index seek; the test below pins the
schema.
"""
from __future__ import annotations

import pytest

from nexus.catalog.catalog import Catalog
from nexus.catalog.collection_name import (
    CollectionName,
    owner_segment_for_tumbler,
)
from nexus.catalog.tumbler import Tumbler


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture()
def catalog(tmp_path):
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    db_path = tmp_path / "catalog.sqlite"
    return Catalog(catalog_dir=catalog_dir, db_path=db_path)


# ── owner_segment_for_tumbler ────────────────────────────────────────────

def test_owner_segment_for_tumbler_string_basic() -> None:
    """``store.owner.document`` to ``store-owner``."""
    assert owner_segment_for_tumbler("1.7.42") == "1-7"


def test_owner_segment_for_tumbler_string_two_segment() -> None:
    """``store.owner`` (no document) still yields the owner segment."""
    assert owner_segment_for_tumbler("1.7") == "1-7"


def test_owner_segment_for_tumbler_string_four_segment() -> None:
    """Chunk-suffixed tumblers truncate to ``store-owner``."""
    assert owner_segment_for_tumbler("1.7.42.3") == "1-7"


def test_owner_segment_for_tumbler_accepts_tumbler_instance() -> None:
    t = Tumbler.parse("1.7.42")
    assert owner_segment_for_tumbler(t) == "1-7"


def test_owner_segment_for_tumbler_two_segment_tumbler() -> None:
    t = Tumbler.parse("1.7")
    assert owner_segment_for_tumbler(t) == "1-7"


def test_owner_segment_for_tumbler_empty_string_returns_empty() -> None:
    """Pre-promotion behaviour: malformed input returns empty rather than
    raising. Callers (e.g. the ``migrate`` verb) rely on the empty string
    to skip the row with a warning instead of aborting the loop.
    """
    assert owner_segment_for_tumbler("") == ""


def test_owner_segment_for_tumbler_single_segment_returns_empty() -> None:
    assert owner_segment_for_tumbler("1") == ""


# ── Catalog.collection_for: validation ──────────────────────────────────

def test_collection_for_rejects_unknown_content_type(catalog) -> None:
    with pytest.raises(ValueError, match="content_type"):
        catalog.collection_for(
            content_type="other",
            owner="1.1",
            embedding_model="voyage-code-3",
        )


def test_collection_for_rejects_non_canonical_embedding_model(catalog) -> None:
    """Pre-canonical-set models (``voyage-3``) and arbitrary strings are
    rejected at the public API. Pinned decision #1."""
    with pytest.raises(ValueError, match="embedding_model"):
        catalog.collection_for(
            content_type="code",
            owner="1.1",
            embedding_model="voyage-3",
        )


def test_collection_for_rejects_empty_owner(catalog) -> None:
    """An owner with no derivable segment cannot be turned into a
    collection name; the catalog raises rather than silently emitting
    ``code____voyage-code-3__v1``."""
    with pytest.raises(ValueError, match="owner"):
        catalog.collection_for(
            content_type="code",
            owner="",
            embedding_model="voyage-code-3",
        )


# ── Catalog.collection_for: new tuple ───────────────────────────────────

def test_collection_for_new_tuple_returns_v1(catalog) -> None:
    name = catalog.collection_for(
        content_type="code",
        owner="1.7",
        embedding_model="voyage-code-3",
    )
    assert isinstance(name, CollectionName)
    assert name.content_type == "code"
    assert name.owner_id == "1-7"
    assert name.embedding_model == "voyage-code-3"
    assert name.model_version == 1


def test_collection_for_new_tuple_with_tumbler_instance(catalog) -> None:
    t = Tumbler.parse("2.3.99")
    name = catalog.collection_for(
        content_type="docs",
        owner=t,
        embedding_model="voyage-context-3",
    )
    assert name.render() == "docs__2-3__voyage-context-3__v1"


# ── Catalog.collection_for: existing tuple is idempotent ─────────────────

def test_collection_for_existing_tuple_returns_same_version(catalog) -> None:
    """An already-registered conformant ``(c, o, m)`` returns its
    existing ``vN`` rather than minting a new one."""
    catalog.register_collection(
        "code__1-7__voyage-code-3__v3",
        content_type="code",
        owner_id="1-7",
        embedding_model="voyage-code-3",
        model_version="v3",
    )
    name = catalog.collection_for(
        content_type="code",
        owner="1.7",
        embedding_model="voyage-code-3",
    )
    assert name.model_version == 3


def test_collection_for_takes_max_existing_version(catalog) -> None:
    """When multiple versions exist for the same ``(c, o, m)``, the
    helper returns the highest one. Lexical comparison would put ``v10``
    before ``v9``; this test pins the integer ordering."""
    for v in (1, 2, 9, 10):
        catalog.register_collection(
            f"docs__1-1__voyage-context-3__v{v}",
            content_type="docs",
            owner_id="1-1",
            embedding_model="voyage-context-3",
            model_version=f"v{v}",
        )
    name = catalog.collection_for(
        content_type="docs",
        owner="1.1",
        embedding_model="voyage-context-3",
    )
    assert name.model_version == 10


# ── Catalog.collection_for: bump=True ───────────────────────────────────

def test_collection_for_bump_increments_version(catalog) -> None:
    catalog.register_collection(
        "code__1-7__voyage-code-3__v3",
        content_type="code",
        owner_id="1-7",
        embedding_model="voyage-code-3",
        model_version="v3",
    )
    name = catalog.collection_for(
        content_type="code",
        owner="1.7",
        embedding_model="voyage-code-3",
        bump=True,
    )
    assert name.model_version == 4


def test_collection_for_bump_on_new_tuple_returns_v1(catalog) -> None:
    """``bump=True`` on a tuple that has never existed still returns
    ``v1``; bump only takes effect when a prior version exists."""
    name = catalog.collection_for(
        content_type="code",
        owner="1.7",
        embedding_model="voyage-code-3",
        bump=True,
    )
    assert name.model_version == 1


# ── Pinned decision #2: model change is not a version bump ──────────────

def test_collection_for_different_model_lands_in_v1(catalog) -> None:
    """Pinned decision #2: a new ``embedding_model`` produces a new
    tuple ``(c, o, m_new)`` and naturally lands in ``v1`` even when the
    old model has rows at ``v3``. Operator runs ``nx catalog
    supersede-collection`` to retire the old tuple."""
    catalog.register_collection(
        "code__1-7__voyage-code-3__v3",
        content_type="code",
        owner_id="1-7",
        embedding_model="voyage-code-3",
        model_version="v3",
    )
    name = catalog.collection_for(
        content_type="code",
        owner="1.7",
        embedding_model="voyage-context-3",
    )
    assert name.embedding_model == "voyage-context-3"
    assert name.model_version == 1


# ── Legacy rows must not contribute to MAX(model_version) ────────────────

def test_collection_for_ignores_grandfathered_rows(catalog) -> None:
    """A legacy 2-segment name registered as grandfathered must NOT
    poison the version-bump lookup for the conformant tuple.

    Pinned decision #1 in operational form: the migration that comes
    later (Phase 4) builds the conformant name from the indexer's
    current canonical model. If the legacy row's empty-string
    ``model_version`` were CAST to 0 and treated as vN, ``bump=True``
    would skip ``v1`` and emit ``v2`` for a fresh tuple.
    """
    catalog.register_collection("docs__nexus-571b8edd")  # legacy 2-segment
    name = catalog.collection_for(
        content_type="docs",
        owner="1.1",
        embedding_model="voyage-context-3",
    )
    assert name.model_version == 1


def test_collection_for_conformant_row_with_empty_model_version(catalog) -> None:
    """Defensive: a conformant name registered without the
    ``model_version`` kwarg has ``legacy_grandfathered = 0`` and an
    empty ``model_version`` text. ``CAST(SUBSTR("", 2) AS INTEGER)``
    yields 0; the new-tuple branch returns ``v1`` regardless. When a
    legitimate ``v1`` row coexists, ``MAX`` correctly returns 1 and
    ``bump=True`` returns ``v2``.
    """
    catalog.register_collection(
        "code__1-7__voyage-code-3__v1",  # conformant, model_version omitted
        content_type="code",
        owner_id="1-7",
        embedding_model="voyage-code-3",
    )
    catalog.register_collection(
        "code__1-7__voyage-code-3__v1",  # idempotent re-register WITH version
        content_type="code",
        owner_id="1-7",
        embedding_model="voyage-code-3",
        model_version="v1",
    )
    name = catalog.collection_for(
        content_type="code",
        owner="1.7",
        embedding_model="voyage-code-3",
        bump=True,
    )
    assert name.model_version == 2


# ── Schema: compound index for version-bump lookup ───────────────────────

def test_collections_compound_index_exists(catalog) -> None:
    """RDR-103 Phase 2 enrichment GAP 3: the version-bump access pattern
    is a triple-keyed lookup on ``(content_type, owner_id, embedding_model)``.
    The compound index ``idx_collections_tuple`` must be present."""
    rows = catalog._db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='index' AND name='idx_collections_tuple'"
    ).fetchall()
    assert rows, "idx_collections_tuple compound index missing"


def test_collections_compound_index_columns(catalog) -> None:
    """The compound index covers exactly the three lookup columns in
    the right order."""
    rows = catalog._db.execute(
        "PRAGMA index_info(idx_collections_tuple)"
    ).fetchall()
    cols = [row[2] for row in rows]
    assert cols == ["content_type", "owner_id", "embedding_model"]


# ── canonical_embedding_model ────────────────────────────────────────────

def test_canonical_embedding_model_code() -> None:
    from nexus.corpus import canonical_embedding_model
    assert canonical_embedding_model("code") == "voyage-code-3"


def test_canonical_embedding_model_docs() -> None:
    from nexus.corpus import canonical_embedding_model
    assert canonical_embedding_model("docs") == "voyage-context-3"


def test_canonical_embedding_model_rdr() -> None:
    from nexus.corpus import canonical_embedding_model
    assert canonical_embedding_model("rdr") == "voyage-context-3"


def test_canonical_embedding_model_knowledge() -> None:
    from nexus.corpus import canonical_embedding_model
    assert canonical_embedding_model("knowledge") == "voyage-context-3"


def test_canonical_embedding_model_unknown_raises() -> None:
    from nexus.corpus import canonical_embedding_model
    with pytest.raises(ValueError, match="content_type"):
        canonical_embedding_model("other")


# ── Catalog.collection_for_repo ────────────────────────────────────────

def test_collection_for_repo_uses_registered_owner(catalog, tmp_path) -> None:
    """Given a registered repo owner, ``collection_for_repo`` returns
    the conformant ``CollectionName`` with the canonical model."""
    repo_root = tmp_path / "myrepo"
    repo_root.mkdir()
    # Stable identity: register an owner under a fixed repo_hash, then
    # patch _repo_identity to return that hash for the repo path.
    catalog.register_owner(
        name="myrepo",
        owner_type="repo",
        repo_hash="deadbeef",
        repo_root=str(repo_root),
    )
    import nexus.registry as reg_mod
    original = reg_mod._repo_identity
    reg_mod._repo_identity = lambda r: ("myrepo", "deadbeef")
    try:
        name = catalog.collection_for_repo(repo_root, "code")
    finally:
        reg_mod._repo_identity = original
    assert name.content_type == "code"
    assert name.embedding_model == "voyage-code-3"
    assert name.model_version == 1
    assert name.owner_id  # whatever the owner segment is, it must not be empty


def test_collection_for_repo_docs_uses_voyage_context_3(catalog, tmp_path) -> None:
    repo_root = tmp_path / "docs-repo"
    repo_root.mkdir()
    catalog.register_owner(
        name="docs-repo",
        owner_type="repo",
        repo_hash="cafe1234",
        repo_root=str(repo_root),
    )
    import nexus.registry as reg_mod
    original = reg_mod._repo_identity
    reg_mod._repo_identity = lambda r: ("docs-repo", "cafe1234")
    try:
        name = catalog.collection_for_repo(repo_root, "docs")
    finally:
        reg_mod._repo_identity = original
    assert name.embedding_model == "voyage-context-3"


def test_collection_for_repo_unregistered_owner_raises(catalog, tmp_path) -> None:
    """Without a prior owner registration, the helper raises rather
    than silently emitting a malformed name. The indexer's
    ``_catalog_hook`` flow registers owners up front; callers that
    bypass that flow get a loud failure instead of bad data.
    """
    repo_root = tmp_path / "unregistered"
    repo_root.mkdir()
    import nexus.registry as reg_mod
    original = reg_mod._repo_identity
    reg_mod._repo_identity = lambda r: ("unregistered", "unknown1")
    try:
        with pytest.raises(LookupError, match="owner"):
            catalog.collection_for_repo(repo_root, "code")
    finally:
        reg_mod._repo_identity = original


def test_collection_for_repo_bump_propagates(catalog, tmp_path) -> None:
    """``bump=True`` flag is forwarded to ``collection_for``."""
    repo_root = tmp_path / "bumprepo"
    repo_root.mkdir()
    owner = catalog.register_owner(
        name="bumprepo",
        owner_type="repo",
        repo_hash="b00b1e55",
        repo_root=str(repo_root),
    )
    catalog.register_collection(
        f"code__{owner.store}-{owner.owner}__voyage-code-3__v1",
        content_type="code",
        owner_id=f"{owner.store}-{owner.owner}",
        embedding_model="voyage-code-3",
        model_version="v1",
    )
    import nexus.registry as reg_mod
    original = reg_mod._repo_identity
    reg_mod._repo_identity = lambda r: ("bumprepo", "b00b1e55")
    try:
        name = catalog.collection_for_repo(repo_root, "code", bump=True)
    finally:
        reg_mod._repo_identity = original
    assert name.model_version == 2
