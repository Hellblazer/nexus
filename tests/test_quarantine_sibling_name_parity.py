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
agreement: it reads the origin from each chunk's own ``origin_collection`` tag. Neither does the restore
verb (nexus-wbfpw.55): the engine finds the sibling, and the last tests of this module hand the verb catalog
rows that DISAGREE with their names (the catalog-044 shape), which the agreement tests cannot do because they
derive each row from the name.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from nexus.catalog import chunk_quarantine as cq

_REPO = Path(__file__).resolve().parent.parent
_REAPER = _REPO / "service" / "src" / "main" / "java" / "dev" / "nexus" / "service" / "ChunkReaper.java"

CONFORMANT_ORIGINS = [
    "knowledge__distributed-systems__bge-base-en-v15-768__v1",
    "docs__nexus-1-1__bge-base-en-v15-768__v1",
    "code__nexus-1-1__minilm-l6-v2-384__v1",
    "rdr__nexus-1-1__minilm-l6-v2-384__v2",
    "knowledge__rp2__minilm-l6-v2-384__v1",
    "code__nexus-1-1__bge-base-en-v15-768__v1",
    "code__nexus-1-1__bge-base-en-v15-768__v2",
    "docs__nexus-1-1__bge-base-en-v15-768__v3",
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

    origin = "knowledge__distributed-systems__bge-base-en-v15-768__v1"
    row = _row_for(origin) | {"embedding_model": "minilm-l6-v2-384"}   # the catalog row wins in the client
    monkeypatch.setattr(mcp_infra, "get_collection_row", lambda name, **kw: row)

    assert cq.quarantine_collection_name(origin) != _java_prefix() + origin


# ── the restore verb and the catalog-044 shape (nexus-wbfpw.55, RDR-192 Phase 3 gate I-1) ───────────────────────
#
# The tests above derive each row FROM the name, so they cannot fail for a row that disagrees with its name, which is
# exactly what catalog-044-3 (7.67.0) produced for repo collections. The tests below hand the verb rows that DISAGREE
# with the names, and check that nothing the verb sends depends on the row.

#: (origin name, the row the catalog holds for it after the rewrite). Each row differs from the name in the field
#: the row-derived sibling is built from: owner (catalog-044's own change), embedding model, content type.
DISAGREEING_ROWS = [
    ("docs__default__voyage-context-3__v1",
     {"content_type": "docs", "owner_id": "curator-9", "embedding_model": "voyage-context-3"}),
    ("knowledge__distributed-systems__bge-base-en-v15-768__v1",
     {"content_type": "knowledge", "owner_id": "distributed-systems", "embedding_model": "minilm-l6-v2-384"}),
    ("code__nexus-1-1__voyage-code-3__v1",
     {"content_type": "docs", "owner_id": "nexus-1-1", "embedding_model": "voyage-code-3"}),
]


class _RecordingClient:
    """Stands in for the T3 client: records every call and answers every chash restored."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    def gc_quarantine_restore(self, *args, **kwargs) -> dict:
        self.calls.append((args, kwargs))
        chashes = kwargs.get("chashes") or []
        rows = [{"chash": h, "outcome": "restored", "no_manifest": False, "reapable_after": None,
                 "reattach": "owned", "attached": False, "owner": None, "owner_title": None, "position": None,
                 "chunk_title": None, "reason": None, "owner_rows": None, "owner_chunks": None} for h in chashes]
        return {"origin_collection": args[0], "quarantine_collection": "quarantine-" + args[0],
                "quarantine_collections": ["quarantine-" + args[0]], "dry_run": False, "audit_id": None,
                "audit_ids": [], "restored": len(rows), "would_restore": 0, "present": 0, "dim_conflict": 0,
                "missing": 0, "rows": rows, "source": None, "next_after": None}


@pytest.mark.parametrize(("origin", "row"), DISAGREEING_ROWS)
def test_the_restore_verb_sends_no_sibling_so_a_row_that_disagrees_with_its_name_cannot_misdirect_it(
    origin: str, row: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import patch

    from click.testing import CliRunner

    import nexus.mcp_infra as mcp_infra
    from nexus.commands.t3 import t3
    from nexus.commands.t3_cmds import quarantine as verb

    monkeypatch.setattr(mcp_infra, "get_collection_row", lambda name, **kw: row)
    # Non-vacuity: for this row the client's own move rule names a sibling the reaper never uses. If it did not,
    # this test could not tell a verb that derives from the row from one that does not.
    assert cq.quarantine_collection_name(origin) != _java_prefix() + origin

    client = _RecordingClient()
    chash = "ab" * 32
    with patch.object(verb, "_make_t3", return_value=client):
        result = CliRunner().invoke(t3, ["quarantine", "restore", "--collection", origin, "--chash", chash])

    assert result.exit_code == 0, result.output
    ((args, kwargs),) = client.calls
    assert args == (origin,), "the origin is the only collection the verb names"
    assert not any("quarantine" in key or "sibling" in key for key in kwargs), kwargs
    assert not [v for v in list(args) + list(kwargs.values()) if isinstance(v, str) and v.startswith("quarantine-")]


def test_the_restore_verb_has_no_row_derived_sibling_helper_to_regress_to() -> None:
    """A source pin for the same property: the verb module neither defines nor imports the row-derived name."""
    source = (_REPO / "src" / "nexus" / "commands" / "t3_cmds" / "quarantine.py").read_text(encoding="utf-8")
    assert "quarantine_collection_name" not in source
    assert "_quarantine_name" not in source
