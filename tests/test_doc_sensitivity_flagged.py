# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-243 Phase 1 fix round 1 (nexus-rfvyy.6.19): ``doc_sensitivity.flagged`` and a merged duplicate.

The engine's batch route (``resolve_many``) returns the LIVE row of a merged duplicate (an alias), with the
alias's own flag, never the canonical's. The earlier code only read an alias through ``by_doc_id`` for a
tumbler the batch did not return at all, which the batch always does, so that branch never ran and a gate
asked about the alias said "not sensitive" for a document whose canonical is flagged. Against the real
engine and catalog.
"""
from __future__ import annotations

import os

import pytest

from nexus.catalog.http_catalog_client import HttpCatalogClient, require_catalog_sensitivity
from nexus.doc_sensitivity import flagged, is_sensitive

pytestmark = pytest.mark.usefixtures("t2_service_env")


@pytest.fixture(autouse=True)
def _no_document_is_sensitive() -> None:
    """Overrides tests/conftest.py's fixture of this name: the subject here is the real catalog read."""


@pytest.fixture
def cat():
    c = HttpCatalogClient(base_url=os.environ["NX_SERVICE_URL"], _token=os.environ["NX_SERVICE_TOKEN"])
    require_catalog_sensitivity(c)
    try:
        yield c
    finally:
        c.close()


def _canonical_and_duplicate(cat: HttpCatalogClient) -> tuple[str, str]:
    owner = cat.register_owner("rfvyy-flagged-owner", owner_type="curator", tumbler_prefix="69.1")
    canonical = str(cat.register(owner, "canonical", content_type="knowledge", source_uri="file:///rfvyy69/c.md"))
    dup = str(cat.register(owner, "dup", content_type="knowledge", source_uri="file:///rfvyy69/d.md"))
    cat.merge_documents(dup, canonical)  # dup becomes an alias of canonical
    return canonical, dup


def test_a_merged_duplicate_of_a_flagged_canonical_is_flagged(cat: HttpCatalogClient) -> None:
    canonical, dup = _canonical_and_duplicate(cat)
    # Control: the batch route really hands back the alias row with its OWN (unset) flag, so only the
    # alias-following read can answer True below.
    row = cat.resolve_many([dup])[dup]
    assert row.alias_of and row.sensitive is False, "the premise: the batch route does not follow the alias"
    assert flagged([dup], cat=cat) == set()

    assert cat.set_sensitivity([canonical], True, reason="rfvyy69")["updated"] == [1]

    assert cat.resolve_many([dup])[dup].sensitive is False, "the alias row itself stays unflagged"
    assert flagged([dup], cat=cat) == {dup}
    assert is_sensitive(dup, cat=cat) is True
    assert flagged([canonical, dup], cat=cat) == {canonical, dup}


def test_a_plain_document_beside_a_flagged_alias_is_not_flagged(cat: HttpCatalogClient) -> None:
    canonical, dup = _canonical_and_duplicate(cat)
    owner = cat.register_owner("rfvyy-flagged-owner", owner_type="curator", tumbler_prefix="69.1")
    other = str(cat.register(owner, "other", content_type="knowledge", source_uri="file:///rfvyy69/o.md"))
    cat.set_sensitivity([canonical], True, reason="rfvyy69")
    assert flagged([dup, other], cat=cat) == {dup}
