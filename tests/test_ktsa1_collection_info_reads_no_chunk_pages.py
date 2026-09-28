# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ktsa1 and nexus-sis0m F8: ``nx collection info`` answers from the
catalog, not by paging every chunk.

Shakeout 7.64.1 Surface E F2 (T2 nexus/shakeout-7.64.1-cli-2026-09-28): info
on a 48,553-chunk collection took 5m05s, paging all chunk metadata 300 at a
time to compute MAX(indexed_at). The same command printed the local
embedder's name as the model of every collection on a local install (F8).
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from click.testing import CliRunner

import nexus.db.http_vector_client as hvc
from nexus.cli import main
from nexus.db.http_vector_client import HttpVectorClient

pytestmark = pytest.mark.integration

_COLLECTION = "docs__ktsa1-info__bge-base-en-v15-768__v1"


def test_info_reports_catalog_facts_without_reading_chunk_pages(t2_service_env, tmp_path):
    from nexus.catalog.factory import make_catalog_reader
    from nexus.doc_indexer import index_markdown

    client = HttpVectorClient(tenant=t2_service_env)
    for i in range(3):
        md = tmp_path / f"ktsa1-{i}.md"
        md.write_text(f"# ktsa1 {i}\n\n" + " ".join(f"ktsa1 doc {i} sentence {j}." for j in range(80)))
        assert index_markdown(md, corpus="ktsa1", t3=client, collection_name=_COLLECTION)

    reader = make_catalog_reader()
    assert reader is not None
    newest = max(e.indexed_at for e in reader.list_by_collection(_COLLECTION))
    assert newest

    real_post = hvc._post
    vector_gets: list[dict] = []

    def _counting_post(path, payload=None, **kwargs):
        if path == "/v1/vectors/get":
            vector_gets.append(payload or {})
        return real_post(path, payload, **kwargs)

    with patch("nexus.commands.collection._t3", return_value=client), \
            patch.object(hvc, "_post", _counting_post):
        result = CliRunner().invoke(main, ["collection", "info", _COLLECTION])

    assert result.exit_code == 0, result.output
    assert vector_gets == [], f"info paged chunk metadata {len(vector_gets)} time(s)"
    assert f"Indexed:     {newest}" in result.output, result.output
    assert "Index model: bge-base-en-v15-768" in result.output, result.output
    assert "Query model: bge-base-en-v15-768" in result.output, result.output
