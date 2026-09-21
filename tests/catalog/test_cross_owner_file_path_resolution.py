# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-yzij1: several catalog documents per ``file_path``, against the REAL catalog.

One file catalogued under two owners is a NORMAL STEADY STATE. Nothing in the
schema forbids it — the only uniqueness is the PARTIAL index on
``(tenant_id, source_uri)`` — and it arises whenever two indexers reach the
same file by different routes. It was measured 19 times in a single run during
the 2026-09-21 MinerU remediation (nexus-z0lu4), where ``nx dt index`` split one
document's identity across two owners.

The reviewers who opened nexus-yzij1 found that EVERY test of this resolution
used a hand-built fake catalog, so a divergence between what the fakes model and
what the engine actually does would not be caught. This suite drives the real
engine substrate (``t2_service_env``) and pins the three-way contract the
resolvers now owe:

  * ``by_file_path(owner, path)`` is owner-scoped and CANNOT see the other
    owner's row. That is structural, not a bug — it is why a writer holding only
    a path is exposed.
  * ``find_all_by_file_path(path)`` returns EVERY match. The honest shape.
  * ``find_by_file_path(path)`` returns the first AND says out loud that it
    chose, naming the count, the chosen tumbler and the others.

The last one is the load-bearing assertion. Returning a first match was always
this method's contract; the cost was that choosing among several was
indistinguishable from there being only one. A test that merely asserts a
document comes back would pass against the silent version too, so it would pin
nothing about the fix.
"""
from __future__ import annotations

import os

import pytest
from structlog.testing import capture_logs

from nexus.catalog.http_catalog_client import HttpCatalogClient

# The shared path. Deliberately not a real file: resolution here is a catalog
# question, and nothing in this suite reads the filesystem.
_SHARED_PATH = "docs/shared/one-file-two-owners.md"


def _client() -> HttpCatalogClient:
    return HttpCatalogClient(
        base_url=os.environ["NX_SERVICE_URL"],
        _token=os.environ["NX_SERVICE_TOKEN"],
    )


@pytest.fixture
def two_owners_one_path(t2_service_env):
    """Register the same ``file_path`` under two owners; yield both tumblers.

    The two documents carry DIFFERENT ``source_uri`` values, because
    ``(tenant_id, source_uri)`` is the one thing the schema does enforce. That
    is the real shape of the duplication class: identity split across owners is
    reachable precisely because the path is the weaker key and the URI is the
    stronger one.
    """
    client = _client()
    owner_a = client.register_owner(
        "yzij1-owner-a", owner_type="curator", tumbler_prefix="71.1",
    )
    owner_b = client.register_owner(
        "yzij1-owner-b", owner_type="curator", tumbler_prefix="71.2",
    )
    tumbler_a = client.register(
        owner_a, "one-file-two-owners (A)",
        content_type="knowledge",
        file_path=_SHARED_PATH,
        source_uri=f"file:///yzij1/a/{_SHARED_PATH}",
    )
    tumbler_b = client.register(
        owner_b, "one-file-two-owners (B)",
        content_type="knowledge",
        file_path=_SHARED_PATH,
        source_uri=f"file:///yzij1/b/{_SHARED_PATH}",
    )
    assert str(tumbler_a) != str(tumbler_b), (
        "seed setup itself must mint two distinct documents — if the engine "
        "collapsed them, every assertion below is vacuous"
    )
    return client, str(tumbler_a), str(tumbler_b)


class TestTheEngineAllowsIt:
    def test_two_owners_may_share_one_file_path(self, two_owners_one_path) -> None:
        """The premise the whole bead rests on, asserted against the engine.

        If this ever fails, the duplication class is closed at the schema and
        the accommodation work below is dead weight — which is worth learning
        from a failing test rather than from a reviewer.
        """
        client, tumbler_a, tumbler_b = two_owners_one_path

        matches = client.find_all_by_file_path(_SHARED_PATH)

        assert {m.tumbler and str(m.tumbler) for m in matches} >= {tumbler_a, tumbler_b}


class TestOwnerScopedLookupIsBlind:
    def test_each_owner_sees_only_its_own_row(self, two_owners_one_path) -> None:
        """``by_file_path(owner, path)`` structurally cannot see the sibling.

        This is the exposure a path-only writer inherits: it asks its own
        owner, is told "no such document", and mints a second one.
        """
        client, tumbler_a, tumbler_b = two_owners_one_path

        from_a = client.by_file_path("71.1", _SHARED_PATH)
        from_b = client.by_file_path("71.2", _SHARED_PATH)

        assert from_a is not None and str(from_a.tumbler) == tumbler_a
        assert from_b is not None and str(from_b.tumbler) == tumbler_b
        assert str(from_a.tumbler) != str(from_b.tumbler)


class TestOwnerAgnosticResolutionSaysItChose:
    def test_find_all_returns_every_match(self, two_owners_one_path) -> None:
        client, tumbler_a, tumbler_b = two_owners_one_path

        matches = client.find_all_by_file_path(_SHARED_PATH)

        assert len(matches) >= 2
        assert {tumbler_a, tumbler_b} <= {str(m.tumbler) for m in matches}

    def test_find_by_file_path_warns_and_names_the_others(
        self, two_owners_one_path,
    ) -> None:
        """THE LOAD-BEARING ASSERTION.

        A test that only checked "a document comes back" would pass against the
        silent version this bead exists to replace. What must be true is that
        the resolver ANNOUNCES the choice: the count, the tumbler it took, and
        the ones it did not.
        """
        client, tumbler_a, tumbler_b = two_owners_one_path

        with capture_logs() as logs:
            chosen = client.find_by_file_path(_SHARED_PATH)

        assert chosen is not None
        assert str(chosen.tumbler) in {tumbler_a, tumbler_b}

        ambiguous = [e for e in logs if e.get("event") == "catalog_file_path_ambiguous"]
        assert len(ambiguous) == 1, (
            "resolving a path that names several documents must warn exactly "
            f"once; got {len(ambiguous)} such events out of {len(logs)} logged"
        )
        entry = ambiguous[0]
        assert entry["file_path"] == _SHARED_PATH
        assert entry["matches"] >= 2
        assert entry["chose"] == str(chosen.tumbler)
        # The point of the warning is the ROAD NOT TAKEN. Naming only the
        # winner would leave a reader exactly as blind as the silent version.
        not_chosen = ({tumbler_a, tumbler_b} - {str(chosen.tumbler)}).pop()
        assert not_chosen in entry["others"]
        assert str(chosen.tumbler) not in entry["others"]

    def test_an_unambiguous_path_does_not_warn(self, t2_service_env) -> None:
        """The other arm. Without it the warning could fire unconditionally and
        every assertion above would still pass.
        """
        client = _client()
        owner = client.register_owner(
            "yzij1-owner-solo", owner_type="curator", tumbler_prefix="71.3",
        )
        solo_path = "docs/shared/one-file-one-owner.md"
        client.register(
            owner, "one-file-one-owner",
            content_type="knowledge",
            file_path=solo_path,
            source_uri=f"file:///yzij1/solo/{solo_path}",
        )

        with capture_logs() as logs:
            chosen = client.find_by_file_path(solo_path)

        assert chosen is not None
        assert not [e for e in logs if e.get("event") == "catalog_file_path_ambiguous"], (
            "a path naming exactly one document must resolve silently"
        )
