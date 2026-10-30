# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6pfgb.16 against the real engine: the extraction-provenance readers judge each document by its own
catalog ``meta`` when a degraded PDF A and a clean PDF B share a chunk row that B wrote last.

The stub-reader twin of the rules is ``tests/test_6pfgb16_extraction_readers.py``.
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from nexus import health
from nexus.cli import main
from nexus.db import make_t3
from nexus.mcp.core import store_get
from tests._catalog_fixture_ops import ActiveCatalog
from tests._chunk_seed import seed_chunks_direct

pytestmark = pytest.mark.integration

_COLLECTION = "knowledge__6pfgb16-extract__bge-base-en-v15-768__v1"
_SHARED = "6" * 64


def _register(cat: ActiveCatalog, owner, title: str, meta: dict) -> str:
    tumbler = str(cat.register(
        owner, title, content_type="paper", file_path=f"/tmp/6pfgb16/{title}.pdf",
        physical_collection=_COLLECTION, chunk_count=1, meta=meta,
    ))
    cat.write_manifest(tumbler, [{"chash": _SHARED, "position": 0}], collection=_COLLECTION)
    return tumbler


def test_degraded_a_and_clean_b_sharing_a_chunk_are_each_judged_by_their_own_meta(t2_service_env):
    # B indexed last: the shared row is B's (a title, the clean method, no override marker).
    seed_chunks_direct(
        _COLLECTION, ids=[_SHARED], documents=["6pfgb16 shared page text"], embed=True,
        metadatas=[{"content_type": "pdf", "title": "", "extraction_method": "mineru"}],
    )
    cat = ActiveCatalog()
    owner = cat.register_owner("6pfgb16-owner", "curator")
    a = _register(cat, owner, "Paper A", {
        "extraction_method": "mineru+degraded", "quality_gate_overridden": True,
        "extraction_source": "file", "extraction_pages": {"mineru-no-formula": [2]},
    })
    b = _register(cat, owner, "Paper B", {
        "extraction_method": "mineru", "quality_gate_overridden": False, "extraction_source": "file",
        "extraction_pages": {},
    })

    # C shares the chunk too but its post-pass never landed: no extraction meta at all
    c = _register(cat, owner, "Paper C", {})

    from unittest.mock import patch

    with patch("nexus.mcp.core._get_t3", return_value=make_t3()):
        out = store_get(doc_id=_SHARED, collection=_COLLECTION)

    lines = out.splitlines()
    assert "Extractor:  Paper A: mineru+degraded" in lines and "Extractor:  Paper B: mineru" in lines, out
    assert "Gate:       Paper A: overridden" in lines, out
    assert not any(ln.startswith("Extractor:  Paper C") for ln in lines), "C states no method"
    assert "Degraded:   Paper A: mineru-no-formula 2" in lines, out

    # the documented audit, catalog side: A is listed, B and C are not
    listed = CliRunner().invoke(main, ["catalog", "list", "--meta", "quality_gate_overridden=true"], catch_exceptions=False).output
    assert a in listed and b not in listed and c not in listed, listed

    # the doctor's stub scan: the shared row is a placeholder (empty title); A and B record a method, C does not
    stubs, _cut = health._pdf_stubs_in_collection(make_t3(), _COLLECTION, 300)
    assert len(stubs) == 1 and next(iter(stubs)).endswith("Paper C.pdf"), stubs
