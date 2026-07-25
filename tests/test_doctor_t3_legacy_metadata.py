# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-1714: ``nx doctor --check-t3-legacy-metadata``.

RDR-108 Phase 3 retired ``doc_id`` and ``source_path`` from T3 chunk
metadata; the catalog ``document_chunks`` manifest is now authoritative.
Several deliberate *tolerance* branches still read those fields for
pre-Phase-3 chunks (``src/nexus/mcp/core.py``, ``indexer_utils.py``,
``search_engine.py``). Without a check, operators cannot tell whether the
legacy corpus is fully pruned, so the branches — and the split-identity
exposure they carry — stay forever.

This check surveys local-mode (Chroma) T3 collections and reports which
still carry chunks with ``doc_id`` / ``source_path`` metadata. It is a
local-Chroma concern by design: the RDR-155 pgvector service path stores
chunks under a different schema and does not expose arbitrary metadata
``where`` filters, so the check reports *not applicable* in service mode
rather than producing a misleading result.
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from nexus.cli import main
from tests.conftest import make_vector_test_client


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


class _FakeCol:
    """Chroma-collection stub modelling real ``get(limit, offset, metadatas)``.

    The probe pages metadatas and inspects keys Python-side (Chroma's ``$ne``
    matches missing keys, so a ``where`` filter cannot isolate legacy chunks).
    This stub returns one metadata dict per chunk: clean chunks carry only
    ``chunk_text_hash``; legacy chunks additionally carry the legacy field — so
    a clean collection must NOT be flagged.
    """

    def __init__(self, total: int, legacy_fields: set[str]) -> None:
        self._metas = []
        for i in range(total):
            m = {"chunk_text_hash": f"h{i}"}
            if i == 0:  # only the first chunk carries the legacy field(s)
                for f in legacy_fields:
                    m[f] = "legacy-value"
            self._metas.append(m)

    def get(self, where=None, limit=10, offset=0, include=None):  # noqa: ANN001
        window = self._metas[offset:offset + limit]
        return {"ids": [str(i) for i in range(len(window))], "metadatas": window}


class _FakeT3:
    def __init__(self, cols: dict[str, tuple[int, set[str]]]) -> None:
        self._cols = cols

    def list_collections(self):
        return [{"name": n, "count": c} for n, (c, _) in self._cols.items()]

    def get_collection(self, name: str):
        total, legacy = self._cols[name]
        return _FakeCol(total, legacy)


def _patch(monkeypatch, *, local: bool, t3: _FakeT3 | None) -> None:
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: local)
    if t3 is not None:
        monkeypatch.setattr("nexus.db.make_t3", lambda: t3)


def test_service_backed_handle_reports_not_applicable(runner, monkeypatch):
    """Successor to test_service_mode_reports_not_applicable (nexus-cv6jp).

    The old test asserted make_t3() must NOT be called, pinning the mechanism
    (an env check short-circuiting first) rather than the contract (the survey
    does not run against a backend whose schema it cannot read). That mechanism
    was the bug: is_local_mode() means "local install", which since RDR-155
    ALSO uses the pgvector service — so on a migrated local box the guard did
    not fire and the survey ran with the wrong frame.

    Gating on the handle necessarily requires obtaining the handle. That is
    free: constructing HttpVectorClient performs no I/O (connection details are
    resolved per request), so calling make_t3() costs nothing and is now the
    correct thing to do.
    """
    from nexus.db.http_vector_client import HttpVectorClient

    made = {"count": 0}

    def _service_handle():
        made["count"] += 1
        return HttpVectorClient.__new__(HttpVectorClient)  # no I/O

    monkeypatch.setattr("nexus.db.make_t3", _service_handle)
    # is_local_mode deliberately TRUE — the migrated-local-box shape that the
    # old gate got wrong. The verdict must come from the handle regardless.
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)

    res = runner.invoke(main, ["doctor", "--check-t3-legacy-metadata"])

    assert res.exit_code == 0, res.output
    assert "not applicable" in res.output.lower()
    assert made["count"] == 1, "the handle IS obtained now — that is the gate"


def test_local_mode_true_does_not_force_the_survey(runner, monkeypatch):
    """The exact regression: a migrated LOCAL install must not be surveyed.

    is_local_mode() is True here, which is what the old gate keyed on. If the
    verdict ever goes back to install mode, this fails.
    """
    from nexus.db.http_vector_client import HttpVectorClient

    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
    monkeypatch.setattr(
        "nexus.db.make_t3", lambda: HttpVectorClient.__new__(HttpVectorClient)
    )
    res = runner.invoke(main, ["doctor", "--check-t3-legacy-metadata"])
    assert res.exit_code == 0, res.output
    assert "not applicable" in res.output.lower()
    assert "legacy doc_id" not in res.output or "not applicable" in res.output.lower()


def test_no_collections(runner, monkeypatch):
    _patch(monkeypatch, local=True, t3=_FakeT3({}))
    res = runner.invoke(main, ["doctor", "--check-t3-legacy-metadata"])
    assert res.exit_code == 0, res.output
    assert "no collections" in res.output.lower()


def test_all_clean_collections(runner, monkeypatch):
    _patch(monkeypatch, local=True, t3=_FakeT3({
        "code__a": (10, set()),
        "docs__b": (5, set()),
    }))
    res = runner.invoke(main, ["doctor", "--check-t3-legacy-metadata"])
    assert res.exit_code == 0, res.output
    assert "clean" in res.output.lower()
    assert "code__a" in res.output
    assert "docs__b" in res.output


def test_mixed_legacy_doc_id_warns_exit_zero_by_default(runner, monkeypatch):
    _patch(monkeypatch, local=True, t3=_FakeT3({
        "code__a": (10, {"doc_id"}),
        "docs__b": (5, set()),
    }))
    res = runner.invoke(main, ["doctor", "--check-t3-legacy-metadata"])
    assert res.exit_code == 0, res.output  # default warn
    out = res.output.lower()
    assert "legacy" in out
    assert "code__a" in res.output


def test_legacy_source_path_detected(runner, monkeypatch):
    _patch(monkeypatch, local=True, t3=_FakeT3({
        "docs__b": (5, {"source_path"}),
    }))
    res = runner.invoke(main, ["doctor", "--check-t3-legacy-metadata"])
    assert res.exit_code == 0, res.output
    assert "source_path" in res.output


def test_strict_exits_nonzero_when_legacy_present(runner, monkeypatch):
    _patch(monkeypatch, local=True, t3=_FakeT3({
        "code__a": (10, {"doc_id", "source_path"}),
    }))
    res = runner.invoke(
        main, ["doctor", "--check-t3-legacy-metadata", "--strict-legacy-metadata"]
    )
    assert res.exit_code != 0, res.output
    assert "legacy" in res.output.lower()


def test_strict_clean_exits_zero(runner, monkeypatch):
    _patch(monkeypatch, local=True, t3=_FakeT3({"code__a": (10, set())}))
    res = runner.invoke(
        main, ["doctor", "--check-t3-legacy-metadata", "--strict-legacy-metadata"]
    )
    assert res.exit_code == 0, res.output


# ── Real-Chroma semantics (pins the `$ne` trap the fake cannot prove) ─────────


def test_probe_against_real_chroma_semantics():
    """Phase-3-clean chunks (no doc_id key) must NOT be flagged; legacy ones must.

    Guards the exact defect a `where={field: {'$ne': ''}}` probe hits: Chroma
    returns key-absent documents from a `$ne` query, so such a probe reports
    every clean collection as legacy. This exercises the real ChromaDB engine.
    """

    from nexus.commands.doctor import _legacy_fields_present  # noqa: PLC0415

    client = make_vector_test_client()

    clean = client.create_collection("clean")
    clean.add(
        ids=["c0", "c1"],
        embeddings=[[0.1, 0.2], [0.3, 0.4]],
        metadatas=[{"chunk_text_hash": "h0"}, {"chunk_text_hash": "h1"}],
    )
    assert _legacy_fields_present(clean, ("doc_id", "source_path")) == set()

    legacy = client.create_collection("legacy")
    legacy.add(
        ids=["l0", "l1"],
        embeddings=[[0.1, 0.2], [0.3, 0.4]],
        metadatas=[
            {"chunk_text_hash": "h0", "doc_id": "1.2.3"},
            {"chunk_text_hash": "h1", "source_path": "/x/a.py"},
        ],
    )
    assert _legacy_fields_present(legacy, ("doc_id", "source_path")) == {
        "doc_id",
        "source_path",
    }
