# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The quarantine sibling's name, as the engine reaper mints it and as the client builds it.

RDR-192 Step 9 (nexus-2x9xa, critique S6). The Java reaper moves a chunk into
``quarantine-<origin name>`` (``ChunkReaper.QUARANTINE_PREFIX + name``); the client's
``quarantine_collection_name`` builds ``quarantine-<content_type>__<owner>__<model>__<version>``
from the origin's catalog row. The two agree only for a conformant name whose row agrees with
it, and the runbook's "the same quarantine- collection" and "index repo moves it back" rest on
that. These tests pin the agreement for the conformant shapes and pin the Java constant to the
client's, so one cannot change without the other. The engine's expiry no longer depends on the
agreement: it reads the origin from each chunk's own ``origin_collection`` tag.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from nexus.catalog import chunk_quarantine as cq

_REPO = Path(__file__).resolve().parent.parent
_REAPER = _REPO / "service" / "src" / "main" / "java" / "dev" / "nexus" / "service" / "ChunkReaper.java"

CONFORMANT_ORIGINS = [
    "knowledge__distributed-systems__voyage-context-3__v1",
    "docs__nexus-1-1__voyage-context-3__v1",
    "code__nexus-1-1__voyage-code-3__v1",
    "rdr__nexus-1-1__voyage-context-3__v2",
    "knowledge__rp2__minilm-l6-v2-384__v1",
    "code__nexus-1-1__bge-base-en-v1.5__v1",
]


def _java_prefix() -> str:
    text = _REAPER.read_text(encoding="utf-8")
    match = re.search(r'QUARANTINE_PREFIX\s*=\s*"([^"]+)"', text)
    assert match, "ChunkReaper.QUARANTINE_PREFIX was not found: the constant moved or was renamed"
    return match.group(1)


def _row_for(origin: str) -> dict:
    content_type, owner, model, _version = origin.split("__")
    return {"content_type": content_type, "owner_id": owner, "embedding_model": model}


def test_the_javas_prefix_is_the_clients_prefix_plus_its_separator() -> None:
    assert _java_prefix() == f"{cq.QUARANTINE_PREFIX}-"


@pytest.mark.parametrize("origin", CONFORMANT_ORIGINS)
def test_a_conformant_origin_gets_the_same_sibling_from_both_sides(
    origin: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nexus.mcp_infra as mcp_infra

    monkeypatch.setattr(mcp_infra, "get_collection_row", lambda name, **kw: _row_for(name))

    assert cq.quarantine_collection_name(origin) == _java_prefix() + origin


def test_a_row_that_disagrees_with_its_name_gets_a_different_sibling_so_the_engine_must_not_parse_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The divergence the engine's expiry is built not to depend on (it reads the tag)."""
    import nexus.mcp_infra as mcp_infra

    origin = "knowledge__distributed-systems__voyage-context-3__v1"
    row = _row_for(origin) | {"embedding_model": "voyage-3"}   # the catalog row wins in the client
    monkeypatch.setattr(mcp_infra, "get_collection_row", lambda name, **kw: row)

    assert cq.quarantine_collection_name(origin) != _java_prefix() + origin
