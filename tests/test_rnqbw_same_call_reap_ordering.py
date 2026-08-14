# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-rnqbw (RDR-191 GATE-2, critic-filed P0): Python-level ordering
coverage for the SAME-CALL tombstone-then-delete pattern nexus-mmkqe's
anti-join fix reopened as a leak.

nexus-mmkqe made ``PgVectorRepository#delete``'s anti-join protect a
TOMBSTONED owner's manifest row too (RDR-191 definition-of-record class b
-- T2 ``nexus/rdr-191-dangling-definition-of-record`` [22364], amended
2026-08-12). That is correct for an INDEPENDENT deleter (GC, quarantine,
a different session), but it broke the SAME-CALL tombstone-then-delete
pattern used by THREE callers that all funnel through
``nexus.catalog.store_hook``'s two reap helpers:

* ``nx store delete --id``            -> ``commands/store.py::delete_cmd``
                                          -> ``reap_catalog_manifest_for_chashes``
* MCP ``store_delete``                -> ``mcp/core.py::store_delete``
                                          -> ``store_delete_catalog_cleanup``
* ``HttpVectorClient.expire()``       -> ``reap_catalog_manifest_for_chashes``
  (already covered by ``tests/test_o8dil5_expire_manifest_reap.py`` --
  not duplicated here.)

Pre-fix, tombstoning alone (via ``delete_document``, a soft tombstone
that deliberately leaves ``catalog_document_chunks`` in place) was
enough to unblock the same-call T3 delete, because the anti-join treated
a tombstoned owner as "not live". Post-mmkqe, tombstoning no longer
unblocks it -- the freshly-tombstoned manifest row is class-b-protected
too -- so each caller's OWN reap must explicitly RETRACT the manifest
row(s) for the chash being deleted (see
``nexus.catalog.store_hook._retract_manifest_rows_for_chash``), not rely
on the anti-join being tombstone-blind.

FAILURE MODES PRE-FIX, PER CALLER (why each needs its own test — a
single shared assertion would hide two of the three):
* CLI ``nx store delete --id``: raises ``click.ClickException`` ("existed
  ... moments ago but the delete did not remove it") -- LOUD, but for the
  wrong reason (a false anti-join-protected report on an ordinary
  single-owner delete).
* MCP ``store_delete``: reports success with a "WARNING: catalog row ...
  NOT removed" qualifier if the retraction step itself fails, or (pre any
  fix at all) simply leaves the chunk behind while claiming "Deleted: ...
  from ..." -- the SILENT failure mode, and the worst of the three (no
  loud signal at all): the no-silent-fallback directive is what this
  suite's MCP assertions are pinned against.
* ``HttpVectorClient.expire()``: reports success with a count that does
  not match reality -- covered by ``test_o8dil5_expire_manifest_reap.py``,
  cited here only for completeness.

Real engine substrate (``t2_service_env``) is required for the same
reason ``test_o8dil5_expire_manifest_reap.py`` requires it: the bug lives
at the ``PgVectorRepository`` anti-join boundary in the real Postgres
service. Any mocked/in-memory T3 client (``tests/test_c53hy_delete_reap_
scoping.py``'s ``T3Database`` over ``InMemoryVectorClient``, for example)
is structurally blind to it -- exactly the gap that let the mmkqe
regression through the Java-only test suite unseen.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

pytestmark = [pytest.mark.integration]

_COLLECTION = "knowledge__rnqbw-reap-ordering__bge-base-en-v15-768__v1"


def _seed_owned_note(client, content: str, title: str) -> str:
    """Seed a real store_put-shaped single-owner note: catalog row +
    manifest row + T3 chunk, all real, no TTL involved -- nexus-rnqbw is
    about the ORDINARY same-call delete path, not TTL expiry (that is
    nexus-o8dil.5's own scope). Returns the chash.
    """
    from nexus.catalog.store_hook import (
        catalog_store_hook_tracked,
        single_chunk_manifest_metadata,
        store_put_manifest_direct,
    )

    chash, manifest_metadatas = single_chunk_manifest_metadata(content)
    tumbler, created = catalog_store_hook_tracked(
        title=title, doc_id=chash, collection_name=_COLLECTION,
    )
    assert created is True
    store_put_manifest_direct(tumbler, manifest_metadatas, collection=_COLLECTION)
    client.upsert_chunks_with_embeddings(
        _COLLECTION,
        ids=[chash],
        documents=[content],
        embeddings=[],
        metadatas=[{"title": title, "chunk_text_hash": chash, "doc_id": tumbler}],
    )
    return chash


def _chunk_present(client, chash: str) -> bool:
    from nexus.errors import CollectionNotFoundError

    try:
        result = client.get_collection(_COLLECTION).get(ids=[chash], include=[])
    except CollectionNotFoundError:
        return False
    return chash in (result.get("ids") or [])


def test_cli_store_delete_reclaims_single_owner_chunk_same_call(t2_service_env):
    """``nx store delete --id`` must actually remove the chunk in the SAME
    call, exit 0. A non-zero exit here is the false anti-join-protected
    ``ClickException`` failure mode nexus-rnqbw flags for this caller."""
    import nexus.db.http_vector_client as hvc
    from click.testing import CliRunner

    from nexus.cli import main

    tenant = t2_service_env
    client = hvc.HttpVectorClient(tenant=tenant)
    content = "rnqbw cli same-call reap fixture, single owner, no sharing"
    chash = _seed_owned_note(client, content, title="rnqbw-cli-note")

    assert _chunk_present(client, chash), "control: chunk must exist before delete"

    with patch("nexus.commands.store._t3", return_value=client):
        result = CliRunner().invoke(
            main, ["store", "delete", "--collection", _COLLECTION, "--id", chash],
        )

    assert result.exit_code == 0, (
        "nx store delete --id must succeed on a genuine single-owner chunk. "
        "A non-zero exit here means the anti-join silently refused the "
        "delete because the note's own just-tombstoned manifest row was "
        f"never explicitly retracted first. Output: {result.output!r}"
    )
    assert not _chunk_present(client, chash), (
        "the chunk must actually be gone from T3 after nx store delete --id"
    )


def test_mcp_store_delete_reclaims_single_owner_chunk_same_call(t2_service_env):
    """MCP ``store_delete`` must actually remove the chunk in the SAME
    call and report a clean ``Deleted:`` -- not the silent-failure shape
    (a success message while the chunk survives) or the WARNING-qualified
    partial-cleanup shape."""
    import nexus.db.http_vector_client as hvc
    from nexus.mcp.core import store_delete

    tenant = t2_service_env
    client = hvc.HttpVectorClient(tenant=tenant)
    content = "rnqbw mcp same-call reap fixture, single owner, no sharing"
    chash = _seed_owned_note(client, content, title="rnqbw-mcp-note")

    assert _chunk_present(client, chash), "control: chunk must exist before delete"

    with patch("nexus.mcp.core._get_t3", return_value=client):
        result = store_delete(chash, collection=_COLLECTION)

    assert not _chunk_present(client, chash), (
        "the chunk must actually be gone from T3 after MCP store_delete -- "
        "surviving here while the tool call itself did not raise is exactly "
        "the SILENT failure mode nexus-rnqbw's critique flags as the worst "
        f"of the three callers (no-silent-fallback directive). result={result!r}"
    )
    assert result.startswith("Deleted:"), (
        "MCP store_delete must report a clean deletion for a genuine "
        "single-owner chunk, not a WARNING-qualified 'NOT removed' (which "
        f"would mean the manifest retraction itself failed). Got: {result!r}"
    )
    assert "NOT removed" not in result, (
        f"a 'WARNING ... NOT removed' qualifier means catalog cleanup "
        f"(and therefore the manifest retraction) failed. Got: {result!r}"
    )


def test_mcp_store_delete_still_protects_a_chunk_genuinely_shared_with_a_live_document(
    t2_service_env,
):
    """Non-vacuity control: the retraction fix must not degrade into
    blanket permission -- a chash genuinely shared with another LIVE
    document's manifest (RDR-108 collapse-by-design) must still survive,
    and the caller must be told the truth (not-found / not fully
    removed), not a false "Deleted" for content that is still referenced
    elsewhere."""
    import nexus.db.http_vector_client as hvc
    from nexus.mcp.core import store_delete
    from tests._catalog_fixture_ops import ActiveCatalog

    tenant = t2_service_env
    cat = ActiveCatalog()
    client = hvc.HttpVectorClient(tenant=tenant)
    content = "rnqbw mcp shared-content fixture: one deletable note, one permanent twin"
    chash = _seed_owned_note(client, content, title="rnqbw-mcp-twin-note")

    # A second, permanent document manifesting the SAME chash (mirrors
    # test_o8dil5_expire_manifest_reap.py's identical shared-content
    # control fixture).
    owner = cat.register_owner("rnqbw-mcp-shared", "curator")
    permanent_tumbler = cat.register(
        owner, "rnqbw-permanent-twin", content_type="knowledge",
        physical_collection=_COLLECTION, meta={"doc_id": chash},
    )
    cat.append_manifest_chunks(
        str(permanent_tumbler), [{"chash": chash, "position": 0}], collection=_COLLECTION,
    )
    cat.resync_chunk_count_cache(str(permanent_tumbler))

    assert _chunk_present(client, chash), "control: shared chunk must exist before delete"

    with patch("nexus.mcp.core._get_t3", return_value=client):
        result = store_delete(chash, collection=_COLLECTION)

    assert _chunk_present(client, chash), (
        "the shared chunk must survive: the permanent twin's manifest row "
        f"still references it. result={result!r}"
    )
    assert not result.startswith("Deleted:"), (
        "the tool must not falsely claim a clean 'Deleted:' for content a "
        f"live document still references. Got: {result!r}"
    )
    assert "anti-join-protected" in result, (
        "the tool must say WHY the delete did not happen (anti-join, "
        f"another live reference) rather than a bare/ambiguous refusal. "
        f"Got: {result!r}"
    )
