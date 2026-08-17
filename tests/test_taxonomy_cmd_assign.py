# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx taxonomy assign`` DOC_ID CLI-boundary validation (RDR-194 D1, nexus-tk070.p3a).

topic_assignments.doc_id is a chunk chash end to end (RDR-180 Item6/Item6a),
not a free-form identifier. ``assign_cmd`` validates DOC_ID is 64 lowercase
hex characters before ever touching the store, so a malformed argument gets
a named CLI error instead of an opaque Postgres decode failure once D1's
bytea conversion lands (P3c).
"""
from __future__ import annotations

import json as _json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

import nexus.mcp_infra as _mi
from nexus.commands.taxonomy_cmd import taxonomy
from nexus.db.t2 import T2Database

from tests.conftest import next_import_seed_id


def _seed_chunk(tenant: str, collection: str, chash_hex: str, *, dim: int = 384) -> None:
    """RDR-194 P3d (nexus-tk070.p3d): seed a real nexus.chunks row so a
    topic_assignments insert for (tenant, collection, chash) satisfies
    the new topic_assignments_chunk_fk composite FK. Mirrors
    tests/test_taxonomy.py's ``_seed_chunks_for_tenant`` (this module has
    no import path to it, so a lean single-row copy lives here instead).
    """
    from tests._engine_substrate import ensure_engine  # noqa: PLC0415 — laziness contract, see module docstring

    state = ensure_engine()
    embed_col = {384: "embedding_384", 768: "embedding_768", 1024: "embedding_1024"}[dim]
    vec = "[" + ",".join(["0"] * dim) + "]"
    sql = (
        f"INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('{tenant}', '{collection}') "
        "ON CONFLICT DO NOTHING; "
        f"INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, {embed_col}) "
        f"VALUES ('{tenant}', '{collection}', decode('{chash_hex}', 'hex'), 'seed', '{vec}'::vector) "
        "ON CONFLICT DO NOTHING;"
    )
    psql = Path(state["pg_bin"]) / "psql"
    proc = subprocess.run(
        [
            str(psql), "-h", "127.0.0.1", "-p", str(state["pg_port"]),
            "-U", state["pg_user"], "-d", state["pg_dbname"],
            "-v", "ON_ERROR_STOP=1", "-c", sql,
        ],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"_seed_chunk failed: {proc.stdout}\n{proc.stderr}"


@pytest.fixture(autouse=True)
def _engine_substrate(t2_service_env):
    """RDR-155 P4b P0a' representative batch: real engine-backed T2 (per-test minted tenant)."""


def _seed_topic(db_path: Path, label: str, *, collection: str = "proj") -> int:
    with T2Database(db_path) as db:
        return db.taxonomy.import_topic(
            src_id=next_import_seed_id(),
            label=label,
            parent_id=None,
            collection=collection,
            centroid_hash=None,
            doc_count=0,
            created_at="2026-01-01T00:00:00Z",
            review_status="pending",
            terms=_json.dumps(["term-a"]),
        )


def _t2_router(db_path: Path):
    def _router(fn):
        with T2Database(db_path) as db:
            return fn(db)
    return _router


VALID_CHASH = "a" * 64


def test_assign_rejects_non_hex_doc_id_by_name(tmp_path: Path) -> None:
    """A free-form (non-hex) DOC_ID must fail loud, naming the offending value."""
    db_path = tmp_path / "memory.db"
    _seed_topic(db_path, "assign-reject-topic")

    runner = CliRunner()
    with patch("nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path):
        result = runner.invoke(
            taxonomy, ["assign", "some-memory-note-title", "assign-reject-topic"]
        )

    assert result.exit_code != 0
    assert "some-memory-note-title" in str(result.output)


@pytest.mark.parametrize(
    "bad_doc_id",
    [
        "a" * 63,       # one short of 64
        "a" * 65,       # one over 64
        "A" * 64,       # uppercase hex is not the conformant lowercase shape
        "g" * 64,       # non-hex character
        "",             # empty
    ],
)
def test_assign_rejects_malformed_hex_shapes(tmp_path: Path, bad_doc_id: str) -> None:
    db_path = tmp_path / "memory.db"
    _seed_topic(db_path, "assign-reject-shape-topic")

    runner = CliRunner()
    with patch("nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path):
        result = runner.invoke(
            taxonomy, ["assign", bad_doc_id, "assign-reject-shape-topic"]
        )

    assert result.exit_code != 0, f"{bad_doc_id!r} must be rejected at the CLI boundary"


def test_assign_accepts_conformant_64hex_doc_id(tmp_path: Path, t2_service_env: str) -> None:
    """A conformant 64-hex DOC_ID passes CLI validation and reaches the store.

    Falsification companion to the reject tests above: proves the new
    validation does not also block the value it exists to admit.
    """
    db_path = tmp_path / "memory.db"
    topic_id = _seed_topic(db_path, "assign-accept-topic")
    # RDR-194 P3d: topic_assignments_chunk_fk requires a matching
    # nexus.chunks row for (tenant, "proj", VALID_CHASH) before assignOne's
    # INSERT below.
    _seed_chunk(t2_service_env, "proj", VALID_CHASH)

    runner = CliRunner()
    with (
        patch("nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path),
        patch.object(_mi, "t2_index_write", _t2_router(db_path)),
    ):
        result = runner.invoke(
            taxonomy, ["assign", VALID_CHASH, "assign-accept-topic", "--collection", "proj"]
        )

    assert result.exit_code == 0, result.output
    with T2Database(db_path) as db:
        assignments = db.taxonomy.get_assignments_for_docs([VALID_CHASH])
        details = db.taxonomy.get_assignment_details([VALID_CHASH])
    assert assignments.get(VALID_CHASH) == topic_id, (
        "the conformant doc_id must reach the store once validation passes"
    )
    # Reviewer Minor-3 (RDR-194 P3b/nexus-11pe7 stacked review, T2 nexus/
    # rdr194-p3b-substantive-critique-2026-08-16 [22755]): topic_id alone
    # does not prove source_collection was populated -- assign_cmd resolves
    # it from the RESOLVED topic's own `collection` field (get_topic_by_id),
    # not from the --collection lookup-scope flag; assert the WRITTEN row
    # actually carries it, at the CLI level, not merely that the topic_id
    # link exists.
    assert len(details) == 1, details
    assert details[0]["source_collection"] == "proj", (
        "source_collection must be populated from the resolved topic's own "
        f"collection ('proj'), not left NULL: got {details[0]!r}"
    )
