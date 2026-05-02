# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the RDR-101 Phase 2 PR δ ``--t3-doc-id-coverage`` doctor flag.

Coverage:
- Verb fails with usage error when no flag is passed.
- ``--t3-doc-id-coverage`` PASSes when every chunk carries the right doc_id.
- FAILs when chunks lack doc_id metadata.
- FAILs when chunks carry the wrong doc_id.
- FAILs when the event log claims chunks T3 doesn't have.
- Orphan chunks (synthesized_orphan=True) without doc_id do not fail.
- ``--json`` payload contains per-collection counts.
- Combined with ``--replay-equality``: both checks run, JSON has both keys.
"""

from __future__ import annotations

import json
from pathlib import Path

import chromadb
import pytest
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
from click.testing import CliRunner

from nexus.catalog import events as ev
from nexus.catalog.catalog import Catalog
from nexus.catalog.event_log import EventLog
from nexus.commands.catalog import doctor_cmd


@pytest.fixture()
def isolated_nexus(tmp_path: Path) -> Path:
    return tmp_path / "test-catalog"


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def chroma_client():
    client = chromadb.EphemeralClient()
    for col in list(client.list_collections()):
        try:
            client.delete_collection(col.name)
        except Exception:
            pass
    return client


def _seed(client, name: str, chunks: list[dict]) -> None:
    col = client.get_or_create_collection(
        name=name, embedding_function=DefaultEmbeddingFunction(),
    )
    col.add(
        ids=[c["id"] for c in chunks],
        documents=[c["content"] for c in chunks],
        metadatas=[c["metadata"] for c in chunks],
    )


def _seed_log(catalog_dir: Path, events: list[ev.Event]) -> None:
    catalog_dir.mkdir(parents=True, exist_ok=True)
    Catalog.init(catalog_dir)
    log = EventLog(catalog_dir)
    log.append_many(events)


def _chunk(
    chunk_id: str, doc_id: str, coll_id: str,
    *, orphan: bool = False,
) -> ev.Event:
    return ev.Event(
        type=ev.TYPE_CHUNK_INDEXED, v=0,
        payload=ev.ChunkIndexedPayload(
            chunk_id=chunk_id, chash="h", doc_id=doc_id,
            coll_id=coll_id, position=0,
            synthesized_orphan=orphan,
        ),
        ts="2026-04-30T00:00:00Z",
    )


# ── Usage ────────────────────────────────────────────────────────────────


class TestUsage:
    def test_no_flag_is_usage_error(self, isolated_nexus, runner):
        result = runner.invoke(doctor_cmd, [])
        assert result.exit_code != 0
        assert "Pass a check flag" in (result.output + (result.stderr or ""))


# ── Pass paths ───────────────────────────────────────────────────────────


class TestCoveragePasses:
    def test_full_coverage_passes(
        self, isolated_nexus, runner, chroma_client,
        monkeypatch: pytest.MonkeyPatch,
    ):
        events = [_chunk("ch1", "uuid7-A", "code__test")]
        _seed_log(isolated_nexus, events)
        _seed(chroma_client, "code__test", [
            {
                "id": "ch1", "content": "x",
                "metadata": {"doc_id": "uuid7-A"},
            },
        ])

        class _FakeT3:
            _client = chroma_client

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())
        result = runner.invoke(
            doctor_cmd, ["--t3-doc-id-coverage", "--json"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)["t3_doc_id_coverage"]
        assert payload["pass"] is True
        assert payload["tables"]["code__test"]["coverage"] == 1.0

    def test_orphan_without_doc_id_does_not_fail(
        self, isolated_nexus, runner, chroma_client,
        monkeypatch: pytest.MonkeyPatch,
    ):
        events = [
            _chunk("orphan", "", "code__test", orphan=True),
            _chunk("ch1", "uuid7-A", "code__test"),
        ]
        _seed_log(isolated_nexus, events)
        _seed(chroma_client, "code__test", [
            {"id": "orphan", "content": "x", "metadata": {"_": "_"}},
            {"id": "ch1", "content": "y", "metadata": {"doc_id": "uuid7-A"}},
        ])

        class _FakeT3:
            _client = chroma_client

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())
        result = runner.invoke(
            doctor_cmd, ["--t3-doc-id-coverage", "--json"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)["t3_doc_id_coverage"]
        assert payload["pass"] is True
        coll = payload["tables"]["code__test"]
        assert coll["expected_orphans"] == 1
        assert coll["with_doc_id"] == 1
        assert coll["total_chunks"] == 2


# ── Fail paths ───────────────────────────────────────────────────────────


class TestCoverageFails:
    def test_missing_doc_id_fails(
        self, isolated_nexus, runner, chroma_client,
        monkeypatch: pytest.MonkeyPatch,
    ):
        events = [_chunk("ch1", "uuid7-A", "code__test")]
        _seed_log(isolated_nexus, events)
        _seed(chroma_client, "code__test", [
            {"id": "ch1", "content": "x", "metadata": {"_": "_"}},  # no doc_id
        ])

        class _FakeT3:
            _client = chroma_client

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())
        result = runner.invoke(
            doctor_cmd, ["--t3-doc-id-coverage", "--json"],
        )
        assert result.exit_code == 1
        payload = json.loads(result.output)["t3_doc_id_coverage"]
        assert payload["pass"] is False
        coll = payload["tables"]["code__test"]
        assert "ch1" in coll["missing_doc_id_sample"]

    def test_mismatched_doc_id_fails(
        self, isolated_nexus, runner, chroma_client,
        monkeypatch: pytest.MonkeyPatch,
    ):
        events = [_chunk("ch1", "uuid7-A", "code__test")]
        _seed_log(isolated_nexus, events)
        _seed(chroma_client, "code__test", [
            {
                "id": "ch1", "content": "x",
                "metadata": {"doc_id": "uuid7-WRONG"},
            },
        ])

        class _FakeT3:
            _client = chroma_client

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())
        result = runner.invoke(
            doctor_cmd, ["--t3-doc-id-coverage", "--json"],
        )
        assert result.exit_code == 1
        payload = json.loads(result.output)["t3_doc_id_coverage"]
        coll = payload["tables"]["code__test"]
        assert coll["mismatched_doc_id_count"] == 1
        m = coll["mismatched_doc_id_sample"][0]
        assert m["actual"] == "uuid7-WRONG"
        assert m["expected"] == "uuid7-A"

    def test_chunk_in_log_but_not_in_t3_fails_under_strict(
        self, isolated_nexus, runner, chroma_client,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Pre-hardening, ``not_in_t3`` was a hard fail by default.
        Post-hardening, the default treats it as a warning so legitimate
        T3 deletions don't permanently red the doctor; ``--strict-not-
        in-t3`` opts back into the strict 'event log = authoritative'
        contract."""
        events = [_chunk("missing-from-t3", "uuid7-A", "code__test")]
        _seed_log(isolated_nexus, events)
        _seed(chroma_client, "code__test", [
            # Some other chunk; not the one the log references.
            {
                "id": "different-chunk", "content": "x",
                "metadata": {"doc_id": "uuid7-A"},
            },
        ])

        class _FakeT3:
            _client = chroma_client

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())
        result = runner.invoke(
            doctor_cmd,
            ["--t3-doc-id-coverage", "--strict-not-in-t3", "--json"],
        )
        assert result.exit_code == 1
        payload = json.loads(result.output)["t3_doc_id_coverage"]
        coll = payload["tables"]["code__test"]
        assert coll["not_in_t3_count"] == 1
        assert "missing-from-t3" in coll["not_in_t3_sample"]


# ── Combined check ───────────────────────────────────────────────────────


class TestCombined:
    def test_both_flags_run_both_checks(
        self, isolated_nexus, runner, chroma_client,
        monkeypatch: pytest.MonkeyPatch,
    ):
        # Build a real catalog so --replay-equality can run.
        Catalog.init(isolated_nexus)
        cat = Catalog(isolated_nexus, isolated_nexus / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="abab")
        cat.register(owner, "doc.md", content_type="prose", file_path="doc.md")
        cat._db.close()

        # Add a ChunkIndexed event so coverage check has something to check.
        log = EventLog(isolated_nexus)
        log.append_many([_chunk("ch1", "uuid7-A", "code__test")])
        _seed(chroma_client, "code__test", [
            {
                "id": "ch1", "content": "x",
                "metadata": {"doc_id": "uuid7-A"},
            },
        ])

        class _FakeT3:
            _client = chroma_client

        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())
        result = runner.invoke(
            doctor_cmd,
            ["--replay-equality", "--t3-doc-id-coverage", "--json"],
        )
        # Replay equality may or may not pass depending on whether the
        # synthesized log matches the live SQLite — what we care about
        # here is that both checks ran and got a payload.
        payload = json.loads(result.output)
        assert "replay_equality" in payload
        assert "t3_doc_id_coverage" in payload
        assert payload["t3_doc_id_coverage"]["pass"] is True


# ── RDR-102 Phase C / D3: orphan-ratio surfacing ─────────────────────────


class TestOrphanRatioSurface:
    """RDR-102 D3: doctor must surface the per-collection orphan ratio
    so operators see when 84% of T3 lives outside the projection (the
    audit baseline). Pre-RDR-102 the doctor's PASS gate counted only
    non-orphan chunks and the orphan slice was invisible. Phase C adds:

      - report["t3_doc_id_coverage"]["orphan_ratio"] (top-level / global)
      - report["t3_doc_id_coverage"]["tables"][coll_name]["orphan_ratio"]
        (per-collection)
      - text output section "=== Orphan ratio ===" with WARN lines for
        any collection > 50% orphan
      - clarified "Collections in log" header that distinguishes
        "with non-orphan ChunkIndexed events" from
        "total in events.jsonl"

    The PASS gate stays as-is per A4 rejection (tightening would
    invalidate the host catalog's current PASS, which is operationally
    awkward and a Phase 5 concern).
    """

    def test_orphan_ratio_section_warn_above_threshold(
        self, isolated_nexus, runner, chroma_client,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """An 80%-orphan collection MUST surface a WARN line in the text
        output AND an orphan_ratio field in the JSON payload (both
        global and per-collection). The PASS gate stays — orphan ratio
        is a soft signal, not a hard fail.
        """
        # 8 orphan + 2 non-orphan ChunkIndexed events for one collection
        # → 80% orphan ratio. Single non-orphan chunk in T3 to satisfy
        # the existing PASS gate.
        events = [
            _chunk("nor1", "uuid7-N1", "code__warn"),
            _chunk("nor2", "uuid7-N2", "code__warn"),
        ]
        for i in range(8):
            events.append(_chunk(f"orp{i}", "", "code__warn", orphan=True))
        _seed_log(isolated_nexus, events)
        _seed(chroma_client, "code__warn", [
            {"id": "nor1", "content": "x",
             "metadata": {"doc_id": "uuid7-N1"}},
            {"id": "nor2", "content": "y",
             "metadata": {"doc_id": "uuid7-N2"}},
        ])

        class _FakeT3:
            _client = chroma_client
        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())

        # Text output assertions
        text_result = runner.invoke(doctor_cmd, ["--t3-doc-id-coverage"])
        assert text_result.exit_code == 0, text_result.output
        out = text_result.output
        assert "Orphan ratio" in out, (
            f"expected '=== Orphan ratio ===' section; got:\n{out}"
        )
        assert "WARN" in out, (
            f"80%-orphan collection must surface a WARN line; got:\n{out}"
        )
        # The clarified header carries both counts.
        assert "non-orphan" in out and "total in events.jsonl" in out, (
            f"'Collections in log' header must distinguish non-orphan "
            f"events count from total-in-log count; got:\n{out}"
        )

        # JSON payload assertions
        json_result = runner.invoke(
            doctor_cmd, ["--t3-doc-id-coverage", "--json"],
        )
        assert json_result.exit_code == 0, json_result.output
        coverage = json.loads(json_result.output)["t3_doc_id_coverage"]
        assert "orphan_ratio" in coverage, (
            f"top-level orphan_ratio field missing; got keys: "
            f"{sorted(coverage.keys())}"
        )
        assert 0.79 <= coverage["orphan_ratio"] <= 0.81, (
            f"global orphan_ratio should be ~0.80 (8/10); got "
            f"{coverage['orphan_ratio']!r}"
        )
        per_coll = coverage.get("tables", {}).get("code__warn", {})
        assert "orphan_ratio" in per_coll, (
            f"per-collection orphan_ratio missing in tables['code__warn']; "
            f"got keys: {sorted(per_coll.keys())}"
        )
        assert 0.79 <= per_coll["orphan_ratio"] <= 0.81

    @pytest.mark.parametrize("orphan_count,non_orphan_count,should_warn", [
        (5, 5, False),  # 50% exactly — RDR D3 says "> 0.50", so no WARN
        (51, 49, True),  # 51% — WARN
        (49, 51, False),  # 49% — no WARN
        (0, 10, False),  # 0% — no WARN
    ])
    def test_orphan_ratio_threshold_edges(
        self, isolated_nexus, runner, chroma_client,
        monkeypatch: pytest.MonkeyPatch,
        orphan_count, non_orphan_count, should_warn,
    ):
        """RDR D3 threshold is strict-greater-than 0.50. Verify the
        edge cases: exactly 50% does NOT WARN, 51% does, 49% does not."""
        coll = f"code__edge_{orphan_count}_{non_orphan_count}"
        events: list = []
        chunks_in_t3: list = []
        for i in range(non_orphan_count):
            events.append(_chunk(f"nor{i}", f"uuid7-N{i}", coll))
            chunks_in_t3.append({
                "id": f"nor{i}", "content": f"x{i}",
                "metadata": {"doc_id": f"uuid7-N{i}"},
            })
        for i in range(orphan_count):
            events.append(_chunk(f"orp{i}", "", coll, orphan=True))
        _seed_log(isolated_nexus, events)
        if chunks_in_t3:
            _seed(chroma_client, coll, chunks_in_t3)

        class _FakeT3:
            _client = chroma_client
        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())

        text_result = runner.invoke(doctor_cmd, ["--t3-doc-id-coverage"])
        assert text_result.exit_code == 0, text_result.output
        warn_present = "WARN" in text_result.output
        assert warn_present == should_warn, (
            f"orphans={orphan_count} non_orphans={non_orphan_count} "
            f"(ratio={orphan_count / (orphan_count + non_orphan_count):.2f}) "
            f"expected WARN={should_warn} got WARN={warn_present}; "
            f"output:\n{text_result.output}"
        )

    def test_orphan_ratio_zero_when_no_orphans(
        self, isolated_nexus, runner, chroma_client,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """A 100% non-orphan collection has orphan_ratio == 0.0 and no
        WARN line. Confirms the new field defaults sensibly when the
        collection is fully covered."""
        events = [_chunk("nor1", "uuid7-A", "code__clean")]
        _seed_log(isolated_nexus, events)
        _seed(chroma_client, "code__clean", [
            {"id": "nor1", "content": "x",
             "metadata": {"doc_id": "uuid7-A"}},
        ])

        class _FakeT3:
            _client = chroma_client
        monkeypatch.setattr("nexus.db.make_t3", lambda: _FakeT3())

        json_result = runner.invoke(
            doctor_cmd, ["--t3-doc-id-coverage", "--json"],
        )
        coverage = json.loads(json_result.output)["t3_doc_id_coverage"]
        assert coverage.get("orphan_ratio") == 0.0
        per_coll = coverage["tables"]["code__clean"]
        assert per_coll.get("orphan_ratio") == 0.0
        assert "WARN" not in runner.invoke(
            doctor_cmd, ["--t3-doc-id-coverage"],
        ).output
