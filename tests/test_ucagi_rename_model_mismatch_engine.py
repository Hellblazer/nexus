# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ucagi: the engine refuses a rename whose new name's model token
disagrees with the source row's embedding_model, end to end.

The wire pins live in tests/test_ucagi_rename_embedding_model_wire.py and the
engine's own cases in CatalogHandlerRenameTest (real PG); this one drives the
public data-plane function and the real client against the gate service, with
no CLI guard in front, which is the path the bead said was unguarded.
"""
from __future__ import annotations

import click
import pytest

from nexus.catalog.factory import make_catalog_reader
from nexus.collection_rename import rename_collection_data_plane
from nexus.db.http_vector_client import HttpVectorClient

pytestmark = pytest.mark.integration

_OLD = "docs__ucagi-old__bge-base-en-v15-768__v1"
_BAD = "docs__ucagi-bad__minilm-l6-v2-384__v1"
_GOOD = "docs__ucagi-good__bge-base-en-v15-768__v1"
_FORCED = "docs__ucagi-forced__minilm-l6-v2-384__v1"


def _names(reader) -> set[str]:
    return {c.get("name") for c in reader.list_collections()}


def test_the_public_data_plane_is_refused_by_the_engine_and_writes_nothing(
    t2_service_env, tmp_path,
):
    from nexus.doc_indexer import index_markdown

    client = HttpVectorClient(tenant=t2_service_env)
    md = tmp_path / "ucagi.md"
    md.write_text("# ucagi\n\n" + " ".join(f"ucagi sentence {j}." for j in range(60)))
    assert index_markdown(md, corpus="ucagi", t3=client, collection_name=_OLD)
    reader = make_catalog_reader()
    assert reader is not None
    row = next(c for c in reader.list_collections() if c.get("name") == _OLD)
    assert row.get("embedding_model") == "bge-base-en-v15-768", row

    # The model-mismatched name: refused by the engine, the message carries the
    # engine's text (both models), and nothing is written.
    with pytest.raises(click.ClickException) as ei:
        rename_collection_data_plane(_OLD, _BAD, t3_db=client, catalog=reader)
    msg = str(ei.value.message)
    assert "bge-base-en-v15-768" in msg and "minilm-l6-v2-384" in msg, msg
    names = _names(reader)
    assert _BAD not in names, "the refused name was registered"
    assert _OLD in names
    old_row = next(c for c in reader.list_collections() if c.get("name") == _OLD)
    assert not old_row.get("superseded_by"), "the source was retired by a refused rename"

    # The agreeing name renames.
    rename_collection_data_plane(_OLD, _GOOD, t3_db=client, catalog=reader)
    assert _GOOD in _names(reader)


def test_the_force_opt_out_withholds_the_token(t2_service_env, tmp_path):
    """nx collection rename --force-prefix-change keeps working: without the
    token the engine does what it did before, keeping the source row's model."""
    from nexus.doc_indexer import index_markdown

    client = HttpVectorClient(tenant=t2_service_env)
    src = "docs__ucagi-f-old__bge-base-en-v15-768__v1"
    md = tmp_path / "ucagi-f.md"
    md.write_text("# ucagi f\n\n" + " ".join(f"ucagi f sentence {j}." for j in range(60)))
    assert index_markdown(md, corpus="ucagi-f", t3=client, collection_name=src)
    reader = make_catalog_reader()
    assert reader is not None

    rename_collection_data_plane(
        src, _FORCED, t3_db=client, catalog=reader, check_model_token=False,
    )
    row = next(c for c in reader.list_collections() if c.get("name") == _FORCED)
    assert row.get("embedding_model") == "bge-base-en-v15-768", row
