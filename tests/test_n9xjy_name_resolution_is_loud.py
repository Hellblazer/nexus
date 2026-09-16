# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-n9xjy: a name-resolution failure is loud, never a synthesized rename.

The client half of the owner-1.1 stranding. The engine's ``collectionForTuple``
resolved a contested tuple to its ``quarantine-`` sibling (nexus-bc7ps); the
client's version oracle parsed that name with the real ``CollectionName.parse``
and raised ``ValueError`` (``quarantine-code`` is not a content type); and both
resolver wrappers absorbed that ``ValueError`` in a bare ``except Exception`` at
DEBUG and returned a path-derived synthesized name. The indexer then repointed
every document of the repo to it: ``code__1-1`` -> ``code__nexus-571b8edd``,
41,032 rows orphaned inside 81 minutes on 2026-09-08, nothing above DEBUG
logged.

Both wrappers now let anything that is not ``LookupError`` propagate. The fake
catalog below stands in for the transport only: the ``ValueError`` it raises
comes from the real parser on the real name the engine returned.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from nexus.catalog.collection_name import CollectionName
from nexus.indexer import _repo_collection_or_legacy
from nexus.registry import RepoRegistry
from nexus.repo_identity import _resolve_repo_collection

# The exact name the deployed engine returned for the contested tuple
# (nexus-bc7ps, verified against engine-service-v0.1.121 and live catalog data).
QUARANTINE_SIBLING = "quarantine-code__1-1__voyage-code-3__v1"
SYNTH_OWNER = "nexus-571b8edd"


class _ResolverReturnsQuarantineSibling:
    """A catalog whose version oracle got the quarantine sibling back.

    Mirrors the failing frame of ``HttpCatalogClient.collection_for``: the
    resolver's returned name goes through the real ``CollectionName.parse``,
    which is where the ``ValueError`` originates. Nothing here fabricates the
    exception.
    """

    def collection_for_repo(self, repo: Path, content_type: str, *, bump: bool = False):
        return CollectionName.parse(QUARANTINE_SIBLING)


class _OwnerUnregistered:
    def collection_for_repo(self, repo: Path, content_type: str, *, bump: bool = False):
        raise LookupError("no owner registered for repo_hash")


class _CatalogUnreachable:
    def collection_for_repo(self, repo: Path, content_type: str, *, bump: bool = False):
        raise ConnectionError("simulated: catalog service unreachable")


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    r = tmp_path / "nexus"
    r.mkdir()
    # The synthesized owner segment the error path produced in production.
    monkeypatch.setattr("nexus.repo_identity._repo_identity", lambda _r: ("nexus", "571b8edd"))
    return r


def test_the_oracle_parse_is_the_loud_frame() -> None:
    """The ValueError originates in the real parser, not in a fake."""
    with pytest.raises(ValueError, match="quarantine-code"):
        CollectionName.parse(QUARANTINE_SIBLING)


# ── indexer._repo_collection_or_legacy ─────────────────────────────────────


def test_indexer_resolver_propagates_the_value_error(repo: Path) -> None:

    with patch(
        "nexus.catalog.factory.make_catalog_reader",
        return_value=_ResolverReturnsQuarantineSibling(),
    ):
        with pytest.raises(ValueError, match="quarantine-code"):
            _repo_collection_or_legacy(repo, "code")


def test_indexer_resolver_never_returns_the_synth_name_for_a_naming_failure(repo: Path) -> None:
    """The exact production outcome, asserted negatively: no synthesized name."""

    with patch(
        "nexus.catalog.factory.make_catalog_reader",
        return_value=_ResolverReturnsQuarantineSibling(),
    ):
        try:
            name = _repo_collection_or_legacy(repo, "code")
        except ValueError:
            return
        pytest.fail(f"naming failure was absorbed and synthesized as {name!r}")


def test_indexer_resolver_still_synthesizes_for_an_unregistered_owner(repo: Path) -> None:
    """LookupError is the ONE legitimate fall-through: the ad-hoc workflow."""

    with patch("nexus.catalog.factory.make_catalog_reader", return_value=_OwnerUnregistered()):
        name = _repo_collection_or_legacy(repo, "code")
    assert name.split("__")[1] == SYNTH_OWNER


def test_indexer_resolver_propagates_an_unreachable_catalog(repo: Path) -> None:
    """An unreachable catalog is not an unregistered owner; it must not synthesize."""

    with patch("nexus.catalog.factory.make_catalog_reader", return_value=_CatalogUnreachable()):
        with pytest.raises(ConnectionError):
            _repo_collection_or_legacy(repo, "code")


# ── repo_identity._resolve_repo_collection ─────────────────────────────────


def test_registry_resolver_propagates_the_value_error(repo: Path) -> None:

    with pytest.raises(ValueError, match="quarantine-code"):
        _resolve_repo_collection(repo, "code", cat=_ResolverReturnsQuarantineSibling())


def test_registry_resolver_never_returns_the_synth_name_for_a_naming_failure(repo: Path) -> None:

    try:
        name = _resolve_repo_collection(repo, "code", cat=_ResolverReturnsQuarantineSibling())
    except ValueError:
        return
    pytest.fail(f"naming failure was absorbed and synthesized as {name!r}")


def test_registry_resolver_still_synthesizes_for_an_unregistered_owner(repo: Path) -> None:

    name = _resolve_repo_collection(repo, "code", cat=_OwnerUnregistered())
    assert name.split("__")[1] == SYNTH_OWNER


def test_registry_resolver_propagates_an_unreachable_catalog(repo: Path) -> None:

    with pytest.raises(ConnectionError):
        _resolve_repo_collection(repo, "code", cat=_CatalogUnreachable())


def test_registry_add_with_catalog_does_not_persist_a_synth_name_on_naming_failure(
    repo: Path, tmp_path: Path,
) -> None:
    """``RepoRegistry.add(repo, cat=...)`` is the registry-side writer of the
    two collection names; on a naming failure it must raise and write nothing."""
    reg = RepoRegistry(tmp_path / "repos.json")
    with pytest.raises(ValueError, match="quarantine-code"):
        reg.add(repo, cat=_ResolverReturnsQuarantineSibling())
    assert reg.get(repo) is None
