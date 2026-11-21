# SPDX-License-Identifier: AGPL-3.0-or-later
"""Seed state an install holds from BEFORE RDR-229 P3.1 and the engine no longer lets a writer create.

P3.1 (nexus-p2m1x.17) made the engine refuse a document under a collection with no registry row, and added the
``collection_id`` columns beside the name columns under ``NOT VALID`` CHECKs (changeset ``vectors-068``). Rows written
before the cut are not rewritten at boot: an upgraded install still holds a document that names a collection and has
a NULL id, and that names a collection no registry row backs. No route writes that state any more, so a test of how
the code behaves on an upgraded install builds it here, the way the Java side does with ``P31CheckReplay``.

The CHECK is lifted for the seed and put back ``NOT VALID`` afterwards, from the changeset's own text (its
``<rollback>`` SQL, then its forward SQL), so a test carries no DDL string and cannot drift from the shipped
constraint. The seed itself is a superuser ``INSERT`` (RLS does not apply), the substrate's own trust-auth psql.

Use :func:`seed_pre_p31_document` for a document row, :func:`p31_check_lifted` to seed another table's row, and
:func:`assert_pre_p31_document` for the non-vacuity check a seeded row needs.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from tests._chunk_seed import _lit, _psql_superuser, _pg_state, ambient_tenant

_NS = "{http://www.liquibase.org/xml/ns/dbchangelog}"
CHANGELOG_DIR = Path(__file__).resolve().parents[1] / "service/src/main/resources/db/changelog"

#: vectors-068 changeset ids (``vectors-068-collection-id-expand.xml``) whose CHECK a seed lifts.
CATALOG_DOCUMENTS = "vectors-068-13"  # catalog_documents.physical_collection_id
MANIFEST = "vectors-068-5"  # catalog_document_chunks.collection_id
DOCUMENT_ASPECTS = "vectors-068-8"  # document_aspects.collection_id
DOCUMENT_HIGHLIGHTS = "vectors-068-11"  # document_highlights.collection_id

_CHANGELOG_FILE = "vectors-068-collection-id-expand.xml"


def changeset_sql(changeset_id: str, *, rollback: bool, changelog: str = _CHANGELOG_FILE) -> str:
    """The SQL of one changeset in a product changelog: its direct ``<sql>`` children, or its ``<rollback>``'s.

    Mirrors ``PgContainerHelper.replayChangeset`` / ``replayChangesetRollback``: nested descendants are not
    followed (a forward body never picks up the rollback's SQL), XML entities are decoded, and a changeset that is
    absent or whose selected part is empty raises, so a replay of nothing cannot pass vacuously.
    """
    root = ET.parse(CHANGELOG_DIR / changelog).getroot()
    matches = [cs for cs in root.iter(f"{_NS}changeSet") if cs.get("id") == changeset_id]
    if len(matches) != 1:
        raise LookupError(f"changeset {changeset_id!r} found {len(matches)} times in {changelog}")
    cs = matches[0]
    if rollback:
        parts: list[str] = []
        for rb in cs.findall(f"{_NS}rollback"):
            sqls = rb.findall(f"{_NS}sql")
            parts.append("\n".join("".join(s.itertext()) for s in sqls) if sqls else "".join(rb.itertext()))
        text = "\n".join(parts)
    else:
        text = "\n".join("".join(s.itertext()) for s in cs.findall(f"{_NS}sql"))
    if not text.strip():
        raise LookupError(f"changeset {changeset_id!r} in {changelog} has no {'rollback' if rollback else 'forward'} SQL")
    return text


@contextmanager
def p31_check_lifted(changeset_id: str) -> Iterator[None]:
    """Lift the changeset's CHECK constraints for the block, then add them again ``NOT VALID`` (even on error).

    ``NOT VALID`` is what an upgraded install holds: rows already written are not checked, every new or updated
    row after the block is. Safe only because a test's substrate is its worker's own (the same reasoning as
    ``fk_dropped_for_dangling_seed``): no other test observes the window.
    """
    state = _pg_state()
    _psql_superuser(state, changeset_sql(changeset_id, rollback=True))
    try:
        yield
    finally:
        _psql_superuser(state, changeset_sql(changeset_id, rollback=False))


def seed_pre_p31_document(
    owner: object, *, title: str, physical_collection: str, content_type: str = "text", file_path: str = "",
) -> str:
    """Insert a ``catalog_documents`` row as it was before P3.1 and return its tumbler.

    The row names *physical_collection* with ``physical_collection_id`` NULL, and no registry row backs the name
    (use a name nothing registered: a quarantine sibling is the case an upgraded install can hold). *owner* is a
    registered owner's tumbler; the document takes its next free ordinal. Asserts, before returning, that the
    seeded row really is that state (:func:`assert_pre_p31_document`).
    """
    state = _pg_state()
    tenant = ambient_tenant()
    prefix = f"{owner}."
    taken = _psql_superuser(
        state,
        f"SELECT tumbler FROM nexus.catalog_documents WHERE tenant_id = {_lit(tenant)} "
        f"AND tumbler LIKE {_lit(prefix + '%')}",
    ).split()
    ordinals = [int(t[len(prefix):]) for t in taken if t[len(prefix):].isdigit()]
    tumbler = f"{prefix}{max(ordinals, default=0) + 1}"
    with p31_check_lifted(CATALOG_DOCUMENTS):
        _psql_superuser(
            state,
            "INSERT INTO nexus.catalog_documents "
            "(tenant_id, tumbler, title, content_type, file_path, physical_collection, source_uri) "
            f"VALUES ({_lit(tenant)}, {_lit(tumbler)}, {_lit(title)}, {_lit(content_type)}, {_lit(file_path)}, "
            f"{_lit(physical_collection)}, {_lit(file_path and 'file://' + file_path)});",
        )
    assert_pre_p31_document(tumbler, physical_collection)
    return tumbler


def assert_pre_p31_document(tumbler: str, physical_collection: str) -> None:
    """Non-vacuity: the document names *physical_collection*, its id is NULL, and no registry row backs the name."""
    state = _pg_state()
    tenant = ambient_tenant()
    row = _psql_superuser(
        state,
        "SELECT physical_collection, physical_collection_id IS NULL FROM nexus.catalog_documents "
        f"WHERE tenant_id = {_lit(tenant)} AND tumbler = {_lit(tumbler)}",
    )
    assert row == f"{physical_collection}|t", f"seeded document {tumbler} is not the pre-P3.1 state: {row!r}"
    registered = _psql_superuser(
        state,
        "SELECT count(*) FROM nexus.catalog_collections "
        f"WHERE tenant_id = {_lit(tenant)} AND name = {_lit(physical_collection)}",
    )
    assert registered == "0", f"{physical_collection} has a registry row ({registered}); the state is not pre-P3.1"
