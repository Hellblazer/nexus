# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-enej7: storing identical content under a second title registers a
second document; it never rewrites the first one's identity.

Shakeout 7.64.1 Surface F F1 (T2 nexus/shakeout-7.64.1-local-driver-2026-09-28):
``nx store put f -t shared-A`` then ``nx store put f -t shared-B`` left one
catalog document titled shared-A whose source_uri named shared-B. shared-B
was never registered, and a lookup of shared-A by title found nothing. The
chash dedup in ``catalog_store_hook_tracked`` reconciled onto any document
in the collection holding the chash, whatever its title.

A note's catalog identity is (collection, title). Identical content under two
titles is two documents whose manifests share one chunk (RDR-108). These
tests drive the real CLI and MCP writers against the real engine and read
the result back the way an operator would.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.aspect_readers import uri_for
from nexus.catalog.factory import make_catalog_reader
from nexus.cli import main
from nexus.corpus import t3_collection_name
from nexus.db.http_vector_client import HttpVectorClient

pytestmark = pytest.mark.integration


def _put(client, tmp_path, subject: str, title: str, body: str):
    f = tmp_path / f"{title}.md"
    f.write_text(body)
    with patch("nexus.commands.store._t3", return_value=client):
        return CliRunner().invoke(main, ["store", "put", str(f), "-c", subject, "-t", title])


def _assert_two_documents(client, collection: str, titles: tuple[str, str]) -> None:
    reader = make_catalog_reader()
    assert reader is not None
    docs = [reader.by_source_uri(uri_for(collection, t)) for t in titles]
    for title, doc in zip(titles, docs):
        assert doc is not None, f"{title!r} was never registered"
        assert doc.title == title, (doc.title, doc.source_uri)
        assert doc.source_uri == uri_for(collection, title)
    assert docs[0].tumbler != docs[1].tumbler
    manifests = [{r.chash for r in reader.get_manifest(str(d.tumbler))} for d in docs]
    assert manifests[0] and manifests[0] == manifests[1], "both documents own the one shared chunk"
    chash = next(iter(manifests[0]))
    assert chash in client.get_collection(collection).get(ids=[chash], include=[])["ids"]


def test_cli_second_title_registers_a_second_document(t2_service_env, tmp_path):
    client = HttpVectorClient(tenant=t2_service_env)
    subject = "enej7-cli"
    body = "enej7 identical body stored under two titles"

    first = _put(client, tmp_path, subject, "shared-A", body)
    second = _put(client, tmp_path, subject, "shared-B", body)
    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output

    collection = t3_collection_name(subject, t3=client)
    _assert_two_documents(client, collection, ("shared-A", "shared-B"))

    # Both titles resolve. The shared chunk row carries only the last
    # writer's title, so shared-A resolves through the catalog.
    from nexus.mcp.core import store_get

    for title in ("shared-A", "shared-B"):
        with patch("nexus.mcp.core._get_t3", return_value=client):
            got = store_get(title, collection=subject)
        assert body in got, got


def test_mcp_second_title_registers_a_second_document_and_both_resolve(t2_service_env):
    from nexus.mcp.core import store_get, store_put

    client = HttpVectorClient(tenant=t2_service_env)
    subject = "enej7-mcp"
    body = "enej7 identical MCP body stored under two titles"
    with patch("nexus.mcp.core._get_t3", return_value=client):
        store_put(content=body, collection=subject, title="mcp-A")
        store_put(content=body, collection=subject, title="mcp-B")
        got_a = store_get("mcp-A", collection=subject)
        got_b = store_get("mcp-B", collection=subject)

    collection = t3_collection_name(subject, t3=client)
    _assert_two_documents(client, collection, ("mcp-A", "mcp-B"))
    assert body in got_a, got_a
    assert body in got_b, got_b


def test_reput_under_the_same_title_still_reconciles(t2_service_env, tmp_path):
    """Control: the same content under the same title stays ONE document."""
    client = HttpVectorClient(tenant=t2_service_env)
    subject = "enej7-same"
    body = "enej7 same title twice"
    assert _put(client, tmp_path, subject, "same", body).exit_code == 0
    assert _put(client, tmp_path, subject, "same", body).exit_code == 0

    collection = t3_collection_name(subject, t3=client)
    reader = make_catalog_reader()
    assert reader is not None
    matches = [
        d for d in reader.all_documents()
        if d.physical_collection == collection and d.title == "same"
    ]
    assert len(matches) == 1, matches
