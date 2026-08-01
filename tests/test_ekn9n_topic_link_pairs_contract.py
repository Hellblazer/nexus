# SPDX-License-Identifier: AGPL-3.0-or-later
"""``get_topic_link_pairs`` returns the SAME shape on both T2 twins (nexus-ekn9n).

The two twins disagreed: :class:`~nexus.db.t2.catalog_taxonomy.CatalogTaxonomy`
returned ``{(from, to): count}`` while
:class:`~nexus.db.t2.http_taxonomy_store.HttpTaxonomyStore` returned
``[(from, to, count)]``. Every declared consumer type is the mapping —
:func:`nexus.scoring.apply_topic_boost` annotates ``topic_links:
dict[tuple[int, int], int] | None``, and ``search_engine`` annotates the local
it feeds there identically — so the list shape reached
``apply_topic_boost``'s ``for (a, b) in links`` and raised ``ValueError: too
many values to unpack``. ``search_engine`` wraps that call in a best-effort
``except Exception`` that logs at DEBUG, so in service mode the WHOLE topic
boost (the same-topic half included, not just the linked-topic half) was
discarded with no user-visible signal.

Invisible to the parity suite because ``tests/db/t2_store_contract.py`` checks
parameter NAMES only, never return shapes; and invisible in the taxonomy
suites because two of them normalize the divergence with ``isinstance``
helpers instead of failing on it.

Both assertions below are substrate-neutral on purpose: they state the
contract, not one twin's implementation, so they keep their teeth after the
RDR-155 P4b flip deletes the SQLite twin.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from nexus.db.t2 import T2Database
from nexus.scoring import _TOPIC_LINKED_BOOST, apply_topic_boost
from nexus.types import SearchResult
from tests.conftest import next_import_seed_id

pytestmark = pytest.mark.integration


@pytest.fixture
def db(tmp_path: Path) -> T2Database:
    with T2Database(tmp_path / "t2.db") as database:
        yield database


def _seed_topic(db: T2Database, label: str, collection: str) -> int:
    """Insert one topic and return its id (substrate-neutral)."""
    return db.taxonomy.import_topic(
        src_id=next_import_seed_id(),
        label=label,
        parent_id=None,
        collection=collection,
        centroid_hash=None,
        doc_count=1,
        created_at="2026-07-28T00:00:00Z",
        review_status="pending",
        terms=None,
    )


def _seed_linked_pair(db: T2Database) -> tuple[int, int]:
    a = _seed_topic(db, "ekn9n-a", "code__ekn9n")
    b = _seed_topic(db, "ekn9n-b", "code__ekn9n")
    db.taxonomy.upsert_topic_links([
        {
            "from_topic_id": a,
            "to_topic_id": b,
            "link_count": 5,
            "link_types": ["cites"],
        },
    ])
    return a, b


def test_get_topic_link_pairs_is_a_pair_keyed_mapping(db: T2Database) -> None:
    """The read is subscriptable by ``(from, to)`` — the consumers' contract."""
    a, b = _seed_linked_pair(db)

    pairs = db.taxonomy.get_topic_link_pairs([a, b])

    assert isinstance(pairs, Mapping), (
        f"get_topic_link_pairs must return a {{(from, to): count}} mapping; "
        f"got {type(pairs).__name__}: {pairs!r}"
    )
    assert pairs[(a, b)] == 5


def test_topic_boost_survives_a_real_link_read(db: T2Database) -> None:
    """store -> apply_topic_boost, the seam ``search_engine`` actually runs.

    Non-vacuity: the two results are in DIFFERENT topics, so the same-topic
    boost cannot fire and the measured delta is the linked-topic boost alone.
    Feeding the pre-fix list shape here raises ``ValueError`` rather than
    under-boosting, so this fails loudly on a regression instead of silently
    asserting zero.
    """
    a, b = _seed_linked_pair(db)
    results = [
        SearchResult(id="doc-a", content="a", distance=0.5, collection="code__ekn9n"),
        SearchResult(id="doc-b", content="b", distance=0.5, collection="code__ekn9n"),
    ]

    links = db.taxonomy.get_topic_link_pairs([a, b])
    apply_topic_boost(results, {"doc-a": a, "doc-b": b}, topic_links=links)

    assert [r.distance for r in results] == pytest.approx(
        [0.5 - _TOPIC_LINKED_BOOST] * 2,
    ), "linked-topic boost did not reach the results"
