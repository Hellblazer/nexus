# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-089 Phase 1.2: ``aspect_extractor`` synchronous extractor.

Contract tests for the subprocess-based aspect extractor:

- Collection-prefix routing (knowledge__* only in Phase 1).
- Content-sourcing fallback: content="" reads source_path itself.
- subprocess invocation shape (``claude -p PROMPT --json``).
- JSON response parsed into ``AspectRecord``.
- Retry semantics: TimeoutExpired / transient stderr / JSON parse
  failure → retry (capped at 3 attempts, exponential backoff).
- Non-retriable: schema validation failure / hard subprocess error
  → return null-fields record without retrying.
- Final-failure null-fields fallback (row exists, fields are null,
  failure visible via logs).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from tests.conftest import make_vector_test_client


# ── Helpers ──────────────────────────────────────────────────────────────────


def _ok_stdout(
    *,
    problem_formulation: str = "Sharded WAL bottleneck.",
    proposed_method: str = "Hybrid Paxos with batched leader appends.",
    experimental_datasets: list[str] | None = None,
    experimental_baselines: list[str] | None = None,
    experimental_results: str = "30% throughput improvement on YCSB-A.",
    extras: dict | None = None,
    confidence: float = 0.9,
    fence: bool = False,
) -> str:
    """Build a Claude CLI ``--output-format json`` wrapper containing
    the model's response. The model response is the JSON object the
    extractor wants; the outer wrapper is the session metadata
    Claude Code emits.
    """
    inner = {
        "problem_formulation": problem_formulation,
        "proposed_method": proposed_method,
        "experimental_datasets": experimental_datasets or ["TPC-C", "YCSB"],
        "experimental_baselines": experimental_baselines or ["raft", "paxos"],
        "experimental_results": experimental_results,
        "extras": extras or {"venue": "VLDB", "ablations_present": True},
        "confidence": confidence,
    }
    inner_text = json.dumps(inner)
    if fence:
        inner_text = f"```json\n{inner_text}\n```"
    wrapper = {
        "result": inner_text,
        "session_id": "test-session",
        "usage": {"input_tokens": 100, "output_tokens": 50},
    }
    return json.dumps(wrapper)


def _wrap_inner(inner_payload_json: str, *, fence: bool = False) -> str:
    """Wrap a raw inner-JSON string into the outer ``--output-format json``
    envelope shape so tests can inject pathological inner payloads.
    """
    inner_text = inner_payload_json
    if fence:
        inner_text = f"```json\n{inner_text}\n```"
    return json.dumps({
        "result": inner_text,
        "session_id": "test-session",
        "usage": {},
    })


def _make_completed(stdout: str, stderr: str = "", returncode: int = 0):
    completed = MagicMock(spec=subprocess.CompletedProcess)
    completed.stdout = stdout
    completed.stderr = stderr
    completed.returncode = returncode
    return completed


#: Paper-shaped input content so per-document shape routing (nexus-kmbys)
#: classifies it as a scholarly paper (Abstract + References headings +
#: "we propose" + citation marker = 4 signals) and keeps the scholarly
#: extractor. Tests that exercise the scholarly path feed this so they are
#: not silently re-routed to general-prose-v1.
_PAPER_SHAPED_CONTENT = (
    "Abstract\n\n"
    "We propose a sharded consensus protocol. References\n"
    "[1] Foo et al. (2020).\n"
)


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Make exponential backoff instant in tests."""
    import nexus.aspect_extractor as mod
    monkeypatch.setattr(mod, "_sleep_with_jitter", lambda attempt: None)


# ── Collection-prefix routing ────────────────────────────────────────────────


class TestCollectionRouting:
    """Phase 1 ships ONE extractor config: knowledge__* only.
    Collections without a registered config return ``None``.
    """

    def test_knowledge_collection_routes_to_scholarly_paper_v1(self) -> None:
        from nexus.aspect_extractor import select_config

        config = select_config("knowledge__delos")
        assert config is not None
        assert config.extractor_name == "scholarly-paper-v1"

    def test_unrelated_collection_returns_none(self) -> None:
        from nexus.aspect_extractor import select_config

        # code__* targets source-code chunks, not prose claims —
        # aspect extraction does not apply, so no config is registered.
        # taxonomy__* holds embedding centroids with no source docs.
        assert select_config("code__nexus") is None
        assert select_config("taxonomy__centroids") is None

    def test_docs_prefix_returns_none_revert_of_377(self) -> None:
        """nexus-z70w: revert of #377. docs__<repo> collections are
        populated by ``nx index repo``, which sweeps ANY prose file:
        markdown documentation, dictionary text (.dict), JSONL test
        fixtures, dot graphs. None of these are paper-shaped, but
        scholarly-paper-v1 happily hallucinates 5-field aspect rows
        (problem_formulation / proposed_method / datasets / baselines
        / results) on whatever it sees.

        Live evidence (ART, 2026-04-30): pre-revert, 286 of 287
        aspect rows extracted on docs__ART content where the input
        was markdown documentation, .dict phonetic dictionaries, and
        .jsonl test fixtures. All technically 'populated' but the
        extracted fields were uniformly hallucinated against
        non-paper input.

        Post-revert: docs__* matches no config, so
        ``nx enrich aspects docs__<X>`` short-circuits at the
        select_config gate (same shape as code__<X> today). Papers
        in a repo go through ``nx index pdf`` into
        knowledge__<repo>-papers, which routes correctly.
        """
        from nexus.aspect_extractor import select_config

        assert select_config("docs__handbook") is None
        assert select_config("docs__ART-8c2e74c0") is None

    def test_extract_aspects_returns_none_for_unsupported_collection(
        self, tmp_path: Path,
    ) -> None:
        """No subprocess invocation for unsupported collections —
        the function short-circuits at config selection. Asserts via
        a subprocess mock that would have raised if called."""
        from nexus.aspect_extractor import extract_aspects

        with patch("nexus.aspect_extractor._run_claude_isolated", side_effect=AssertionError("must not be called")):
            result = extract_aspects(
                content="content",
                source_path="/p1.pdf",
                collection="code__nexus",
            )
        assert result is None


# ── Successful extraction (knowledge__*) ─────────────────────────────────────


class TestSuccessfulExtraction:
    def test_subprocess_invocation_shape(self, monkeypatch) -> None:
        """``extract_aspects`` routes through the isolated runner with the prompt
        (carrying the paper content) and the single-paper 180s budget. The
        claude-argv + stdin + start_new_session invariants are pinned at the
        runner level in tests/test_aspect_extractor_subprocess_isolation.py
        (RDR-173 RF-8 moved the subprocess call into _run_claude_isolated)."""
        from nexus.aspect_extractor import extract_aspects

        captured: list[dict] = []

        def fake_run(prompt, timeout, **kwargs):
            captured.append({"prompt": prompt, "timeout": timeout})
            return _make_completed(_ok_stdout())

        monkeypatch.setattr("nexus.aspect_extractor._run_claude_isolated", fake_run)
        extract_aspects(
            content="some paper text",
            source_path="/p1.pdf",
            collection="knowledge__delos",
        )

        assert len(captured) == 1
        assert "some paper text" in captured[0]["prompt"]   # content flows via the prompt
        assert captured[0]["timeout"] == 180

    def test_argv_size_is_bounded_regardless_of_content_length(
        self, monkeypatch,
    ) -> None:
        """Long documents never touch argv — the content rides the prompt (which
        the isolated runner feeds via stdin), so a 14-page paper cannot trip
        macOS ARG_MAX (errno 7)."""
        from nexus.aspect_extractor import extract_aspects

        captured: list[str] = []

        def fake_run(prompt, timeout, **kwargs):
            captured.append(prompt)
            return _make_completed(_ok_stdout())

        monkeypatch.setattr("nexus.aspect_extractor._run_claude_isolated", fake_run)
        big_content = "X" * 200_000  # 200 KB — would have tripped E2BIG on argv
        extract_aspects(
            content=big_content,
            source_path="/big.pdf",
            collection="knowledge__delos",
        )
        # The big content is carried by the prompt (→ stdin in the runner),
        # never as an argv token.
        assert len(captured[0]) >= 200_000

    def test_strips_markdown_code_fence_around_json(self, monkeypatch) -> None:
        """The Claude CLI sometimes wraps JSON in a ```json ... ```
        fence even when the prompt asks for raw JSON. The extractor
        strips the fence before parsing.
        """
        from nexus.aspect_extractor import extract_aspects

        monkeypatch.setattr(
            "nexus.aspect_extractor._run_claude_isolated",
            lambda *a, **kw: _make_completed(_ok_stdout(fence=True)),
        )
        record = extract_aspects(
            content="x",
            source_path="/p1.pdf",
            collection="knowledge__delos",
        )
        assert record is not None
        assert record.problem_formulation == "Sharded WAL bottleneck."

    def test_outer_wrapper_missing_result_key_is_hard_failure(
        self, monkeypatch,
    ) -> None:
        """If Claude returns a wrapper without a ``result`` field,
        the extractor raises hard failure (no retry — the CLI
        contract is wrong, retry won't fix it).
        """
        from nexus.aspect_extractor import extract_aspects

        calls: list[int] = []

        def fake_run(*a, **kw):
            calls.append(1)
            return _make_completed(json.dumps({"session_id": "x"}))

        monkeypatch.setattr("nexus.aspect_extractor._run_claude_isolated", fake_run)
        record = extract_aspects(
            content="x", source_path="/p1.pdf", collection="knowledge__delos",
        )
        assert len(calls) == 1  # no retry on hard failure
        assert record is not None
        assert record.problem_formulation is None  # null-fields fallback

    def test_json_response_parsed_into_aspect_record(self, monkeypatch) -> None:
        from nexus.aspect_extractor import extract_aspects

        monkeypatch.setattr(
            "nexus.aspect_extractor._run_claude_isolated", lambda *a, **kw: _make_completed(_ok_stdout()),
        )
        record = extract_aspects(
            content=_PAPER_SHAPED_CONTENT,
            source_path="/p1.pdf",
            collection="knowledge__delos",
        )

        assert record is not None
        assert record.collection == "knowledge__delos"
        assert record.source_path == "/p1.pdf"
        assert record.problem_formulation == "Sharded WAL bottleneck."
        assert record.proposed_method.startswith("Hybrid Paxos")
        assert record.experimental_datasets == ["TPC-C", "YCSB"]
        assert record.experimental_baselines == ["raft", "paxos"]
        assert record.experimental_results.startswith("30% throughput")
        assert record.extras == {"venue": "VLDB", "ablations_present": True}
        assert record.confidence == 0.9
        assert record.extractor_name == "scholarly-paper-v1"
        assert record.model_version  # populated from config — non-empty
        assert record.extracted_at  # ISO-8601 timestamp from now()


# ── Content-sourcing contract (audit F4) ─────────────────────────────────────


class TestContentSourcing:
    @pytest.fixture(autouse=True)
    def _stub_get_t3(self, monkeypatch):
        """RDR-120 P6 (nexus-qg86h): aspect_extractor calls
        ``get_t3()`` before dispatching to ``read_source``. After
        direct mode was decommissioned, ``get_t3`` routes through
        the T3 daemon — which doesn't exist in unit tests, so the
        exception path runs and the test sees
        ``ExtractFail(reason='infra_unavailable')`` instead of
        whatever ``read_source`` was monkeypatched to return.
        Stub ``get_t3`` with a MagicMock so the test's
        ``read_source`` monkeypatch is what actually drives the
        result.
        """
        monkeypatch.setattr(
            "nexus.aspect_extractor.get_t3", lambda: MagicMock(),
        )

    def test_passes_through_content_when_populated(self, monkeypatch) -> None:
        from nexus.aspect_extractor import extract_aspects

        def fake_run(args, **kwargs):
            assert "INLINE_CONTENT_HERE" in args
            return _make_completed(_ok_stdout())

        monkeypatch.setattr("nexus.aspect_extractor._run_claude_isolated", fake_run)
        extract_aspects(
            content="INLINE_CONTENT_HERE",
            source_path="/path/never/read.pdf",
            collection="knowledge__delos",
        )

    def test_returns_extract_fail_on_chroma_miss(
        self, monkeypatch,
    ) -> None:
        """RDR-096: ``content=""`` plus a chroma miss returns
        ``ExtractFail`` (no row written). The disk-read fallback that
        the prior contract had is gone — read failures are surfaced
        as a typed sentinel the upsert-guard skips on. Replaces the
        prior ``test_reads_source_path_when_content_empty_and_t3_miss``
        which exercised the deprecated disk fallback.
        """
        from nexus.aspect_extractor import ExtractFail, extract_aspects
        from nexus.aspect_readers import ReadFail

        # Stub read_source to return ReadFail directly.
        monkeypatch.setattr(
            "nexus.aspect_extractor.read_source",
            lambda uri, t3=None, **_kw: ReadFail(
                reason="empty",
                detail=f"no chunks for the test fixture {uri!r}",
            ),
        )

        with patch("nexus.aspect_extractor._run_claude_isolated", side_effect=AssertionError(
            "subprocess must NOT be called when read_source fails",
        )):
            result = extract_aspects(
                content="",
                source_path="ghost-source",
                collection="knowledge__delos",
            )

        assert isinstance(result, ExtractFail)
        assert result.reason == "empty"
        assert "ghost-source" in result.uri or result.uri.endswith("/ghost-source")
        assert result.uri.startswith("chroma://knowledge__delos/")

    def test_sources_via_read_source_when_content_empty_and_chroma_hit(
        self, monkeypatch,
    ) -> None:
        """When ``content=""`` and ``read_source`` returns ``ReadOk``,
        the extractor uses the reassembled text from chroma. Replaces
        the prior ``test_sources_from_t3_when_content_empty_and_t3_hit``
        which monkeypatched the deprecated ``_source_content_from_t3``
        helper directly.
        """
        from nexus.aspect_extractor import extract_aspects
        from nexus.aspect_readers import ReadOk

        captured_uris: list[str] = []

        def fake_read(uri, t3=None, **_kw):
            captured_uris.append(uri)
            return ReadOk(
                text="T3_REASSEMBLED_CONTENT_FROM_CHUNKS",
                metadata={"scheme": "chroma", "chunk_count": 1},
            )

        monkeypatch.setattr("nexus.aspect_extractor.read_source", fake_read)

        def fake_run(args, **kwargs):
            assert "T3_REASSEMBLED_CONTENT_FROM_CHUNKS" in args
            return _make_completed(_ok_stdout())

        monkeypatch.setattr("nexus.aspect_extractor._run_claude_isolated", fake_run)
        record = extract_aspects(
            content="",
            source_path="/missing/on/disk.pdf",  # would fail if disk was tried
            collection="knowledge__delos",
        )
        assert record is not None
        assert hasattr(record, "problem_formulation")
        assert record.problem_formulation == "Sharded WAL bottleneck."
        # URI was constructed from (collection, source_path).
        assert captured_uris == ["chroma://knowledge__delos//missing/on/disk.pdf"]

    def test_strips_embedded_null_bytes_from_content(self, monkeypatch) -> None:
        """Some PDF extractors emit \\x00 bytes. Strip them before
        prompting (no semantic content) and before subprocess hand-off
        (POSIX argv would reject them outright; stdin is more forgiving
        but the strip stays as defense in depth)."""
        from nexus.aspect_extractor import extract_aspects

        captured: list[str] = []

        def fake_run(args, **kwargs):
            captured.append(args)
            return _make_completed(_ok_stdout())

        monkeypatch.setattr("nexus.aspect_extractor._run_claude_isolated", fake_run)
        extract_aspects(
            content="paper text with\x00null\x00bytes embedded",
            source_path="/p1.pdf",
            collection="knowledge__delos",
        )
        assert captured, "subprocess must have been invoked"
        assert "\x00" not in captured[0]
        assert "paper text withnullbytes embedded" in captured[0]

    def test_unreadable_source_returns_extract_fail(
        self, monkeypatch,
    ) -> None:
        """RDR-096: ``content=""`` plus a read failure returns
        ``ExtractFail`` instead of a null-field record. No row is
        written; the upsert-guard in P1.3 skips on the typed sentinel.
        Replaces the prior null-fields-record contract.
        """
        from nexus.aspect_extractor import ExtractFail, extract_aspects
        from nexus.aspect_readers import ReadFail

        monkeypatch.setattr(
            "nexus.aspect_extractor.read_source",
            lambda uri, t3=None, **_kw: ReadFail(
                reason="unreachable",
                detail="FileNotFoundError: missing.pdf",
            ),
        )
        with patch("nexus.aspect_extractor._run_claude_isolated", side_effect=AssertionError(
            "subprocess must NOT be called when read_source fails",
        )):
            result = extract_aspects(
                content="",
                source_path="missing.pdf",
                collection="knowledge__delos",
            )

        assert isinstance(result, ExtractFail)
        assert result.reason == "unreachable"
        assert "FileNotFoundError" in result.detail


class TestReadIndexedText:
    """nexus-vwns1: read_indexed_text returns the SAME reassembled T3
    chunk text the extractor consumes, so --validate-sample verifies
    against the extracted prose rather than raw (PDF-binary) file bytes."""

    @pytest.fixture(autouse=True)
    def _stub_get_t3(self, monkeypatch):
        monkeypatch.setattr(
            "nexus.aspect_extractor.get_t3", lambda: MagicMock(),
        )

    def test_returns_reassembled_chunk_text_on_read_ok(self, monkeypatch) -> None:
        from nexus.aspect_extractor import read_indexed_text
        from nexus.aspect_readers import ReadOk

        captured: list[str] = []

        def fake_read(uri, t3=None, **_kw):
            captured.append(uri)
            return ReadOk(text="OCR PROSE\x00 from chunks", metadata={})

        monkeypatch.setattr("nexus.aspect_extractor.read_source", fake_read)
        text = read_indexed_text(
            collection="knowledge__delos",
            source_path="/scanned/paper.pdf",
        )
        # Null bytes stripped; same identity-URI shape the extractor builds.
        assert text == "OCR PROSE from chunks"
        assert captured == ["chroma://knowledge__delos//scanned/paper.pdf"]

    def test_lookup_path_overrides_source_path_in_uri(self, monkeypatch) -> None:
        from nexus.aspect_extractor import read_indexed_text
        from nexus.aspect_readers import ReadOk

        captured: list[str] = []

        def fake_read(uri, t3=None, **_kw):
            captured.append(uri)
            return ReadOk(text="x", metadata={})

        monkeypatch.setattr("nexus.aspect_extractor.read_source", fake_read)
        read_indexed_text(
            collection="knowledge__delos",
            source_path="relative/p.pdf",
            lookup_path="/abs/p.pdf",
        )
        assert captured == ["chroma://knowledge__delos//abs/p.pdf"]

    def test_returns_none_on_read_fail(self, monkeypatch) -> None:
        from nexus.aspect_extractor import read_indexed_text
        from nexus.aspect_readers import ReadFail

        monkeypatch.setattr(
            "nexus.aspect_extractor.read_source",
            lambda uri, t3=None, **_kw: ReadFail(reason="empty", detail="no chunks"),
        )
        assert read_indexed_text(
            collection="knowledge__delos", source_path="ghost",
        ) is None

    def test_returns_none_when_t3_unavailable(self, monkeypatch) -> None:
        from nexus.aspect_extractor import read_indexed_text

        def _boom():
            raise RuntimeError("no daemon")

        monkeypatch.setattr("nexus.aspect_extractor.get_t3", _boom)
        assert read_indexed_text(
            collection="knowledge__delos", source_path="x",
        ) is None


# ── URI dispatch + chroma integration (RDR-096 P1.2) ────────────────────────


class TestUriDispatch:
    """Integration tests exercising the new ``read_source`` call path
    inside ``extract_aspects`` against a real ``chromadb.EphemeralClient``.
    Verifies the URI is constructed from ``(collection, source_path)``,
    chunk reassembly works end-to-end, and read failures surface as
    ``ExtractFail`` (no row written).
    """

    @pytest.fixture(autouse=True)
    def _stub_get_t3(self, monkeypatch):
        """RDR-120 P6 (nexus-qg86h): see TestContentSourcing for the
        rationale. ``extract_aspects`` calls ``get_t3()`` before
        dispatching to ``read_source``; tests that monkeypatch only
        ``read_source`` would otherwise short-circuit on the daemon-
        unreachable path. Tests that need a specific ``get_t3``
        behaviour override this fixture by monkeypatching the same
        symbol later in the test body.
        """
        monkeypatch.setattr(
            "nexus.aspect_extractor.get_t3", lambda: MagicMock(),
        )

    def test_succeeds_via_chroma_for_knowledge_collection(
        self, monkeypatch,
    ) -> None:
        """End-to-end: knowledge__ shape with ``title``-keyed chunks
        + EphemeralClient + a fake subprocess. The chroma reader
        reassembles, the extractor parses, and an ``AspectRecord``
        emerges with non-null fields.
        """

        from nexus.aspect_extractor import AspectRecord, extract_aspects

        client = make_vector_test_client()
        try:
            client.delete_collection("knowledge__uri_dispatch")
        except Exception:
            pass
        coll = client.get_or_create_collection("knowledge__uri_dispatch")
        title = "decision-bfdb-update-capture-rdr005"
        coll.add(
            ids=[f"{title}::0"],
            documents=["Knowledge note about update capture."],
            metadatas=[{"title": title, "chunk_index": 0}],
        )
        monkeypatch.setattr("nexus.aspect_extractor.get_t3", lambda: client)

        def fake_run(args, **kwargs):
            assert "Knowledge note about update capture." in args
            return _make_completed(_ok_stdout())

        monkeypatch.setattr("nexus.aspect_extractor._run_claude_isolated", fake_run)

        result = extract_aspects(
            content="",
            source_path=title,
            collection="knowledge__uri_dispatch",
        )
        assert isinstance(result, AspectRecord)
        assert result.problem_formulation == "Sharded WAL bottleneck."
        assert result.collection == "knowledge__uri_dispatch"
        assert result.source_path == title

    def test_returns_extract_fail_on_planted_ghost(self, monkeypatch) -> None:
        """Catalog-stale source_path: collection exists, but no chunks
        match the queried identity. Returns ``ExtractFail(reason='empty')``;
        no row written; subprocess never invoked.
        """

        from nexus.aspect_extractor import ExtractFail, extract_aspects

        client = make_vector_test_client()
        try:
            client.delete_collection("knowledge__ghost")
        except Exception:
            pass
        coll = client.get_or_create_collection("knowledge__ghost")
        # Plant unrelated chunks so the collection exists but the
        # ghost source_path matches none of them.
        coll.add(
            ids=["unrelated::0"],
            documents=["irrelevant"],
            metadatas=[{"title": "unrelated", "chunk_index": 0}],
        )
        monkeypatch.setattr("nexus.aspect_extractor.get_t3", lambda: client)

        with patch("nexus.aspect_extractor._run_claude_isolated", side_effect=AssertionError(
            "subprocess must NOT be called for a planted ghost",
        )):
            result = extract_aspects(
                content="",
                source_path="ghost-source-not-in-collection",
                collection="knowledge__ghost",
            )

        assert isinstance(result, ExtractFail)
        assert result.reason == "empty"
        assert result.uri == "chroma://knowledge__ghost/ghost-source-not-in-collection"

    def test_percent_encodes_source_path_with_anchor_or_query(
        self, monkeypatch,
    ) -> None:
        """``source_path`` may contain ``#`` (markdown anchors) or
        ``?`` (query params). Without percent-encoding,
        ``urlparse`` would split them off as URI fragment / query and
        corrupt the path component the chroma reader matches against.
        """
        from nexus.aspect_extractor import extract_aspects
        from nexus.aspect_readers import ReadOk

        captured: list[str] = []

        def fake_read(uri, t3=None, **_kw):
            captured.append(uri)
            return ReadOk(text="x", metadata={})

        monkeypatch.setattr("nexus.aspect_extractor.read_source", fake_read)
        monkeypatch.setattr("nexus.aspect_extractor._run_claude_isolated", lambda *a, **kw: _make_completed(_ok_stdout()))

        extract_aspects(
            content="",
            source_path="docs/notes#section-2?v=1",
            collection="rdr__corpus",
        )
        assert len(captured) == 1
        # Anchor + query characters are percent-encoded; ``/`` is preserved.
        assert "%23" in captured[0]
        assert "%3F" in captured[0]
        assert captured[0].startswith("chroma://rdr__corpus/docs/notes")

    def test_returns_extract_fail_on_infra_unavailable(
        self, monkeypatch,
    ) -> None:
        """``get_t3()`` raising surfaces as
        ``ExtractFail(reason='infra_unavailable')`` — distinct from
        ``unreachable`` so operators distinguish "no client" from
        "bad URI".
        """
        from nexus.aspect_extractor import ExtractFail, extract_aspects

        def boom():
            raise RuntimeError("chroma cloud unreachable")

        monkeypatch.setattr("nexus.aspect_extractor.get_t3", boom)

        result = extract_aspects(
            content="",
            source_path="anything",
            collection="knowledge__test",
        )
        assert isinstance(result, ExtractFail)
        assert result.reason == "infra_unavailable"
        assert "chroma cloud unreachable" in result.detail

    def test_constructs_chroma_uri_from_collection_and_source_path(
        self, monkeypatch,
    ) -> None:
        """Verifies the URI shape passed to ``read_source``: the
        scheme is always ``chroma://`` for empty-content calls in
        Phase 1, and ``<collection>/<source_path>`` is the path.
        """
        from nexus.aspect_extractor import extract_aspects
        from nexus.aspect_readers import ReadOk

        captured: list[str] = []

        def fake_read(uri, t3=None, **_kw):
            captured.append(uri)
            return ReadOk(text="content", metadata={"scheme": "chroma"})

        monkeypatch.setattr("nexus.aspect_extractor.read_source", fake_read)
        monkeypatch.setattr("nexus.aspect_extractor._run_claude_isolated", lambda *a, **kw: _make_completed(_ok_stdout()))

        extract_aspects(
            content="",
            source_path="docs/rdr/rdr-090.md",
            collection="rdr__corpus",
        )
        assert captured == ["chroma://rdr__corpus/docs/rdr/rdr-090.md"]


# ── Retry behavior (audit F8) ────────────────────────────────────────────────


class TestRetry:
    def test_retries_on_timeout_expired(self, monkeypatch) -> None:
        from nexus.aspect_extractor import extract_aspects

        calls: list[int] = []

        def fake_run(args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise subprocess.TimeoutExpired(cmd=args, timeout=180)
            return _make_completed(_ok_stdout())

        monkeypatch.setattr("nexus.aspect_extractor._run_claude_isolated", fake_run)
        record = extract_aspects(
            content="x", source_path="/p1.pdf", collection="knowledge__delos",
        )
        assert len(calls) == 2  # one retry, then success
        assert record is not None
        assert record.problem_formulation == "Sharded WAL bottleneck."

    def test_retries_on_transient_stderr(self, monkeypatch) -> None:
        from nexus.aspect_extractor import extract_aspects

        outputs = [
            _make_completed("", stderr="Error: rate limit exceeded", returncode=1),
            _make_completed("", stderr="overloaded_error", returncode=1),
            _make_completed(_ok_stdout()),
        ]
        idx = [0]

        def fake_run(*a, **kw):
            o = outputs[idx[0]]
            idx[0] += 1
            return o

        monkeypatch.setattr("nexus.aspect_extractor._run_claude_isolated", fake_run)
        record = extract_aspects(
            content="x", source_path="/p1.pdf", collection="knowledge__delos",
        )
        assert idx[0] == 3  # all three slots used
        assert record is not None

    def test_retries_on_json_parse_failure(self, monkeypatch) -> None:
        from nexus.aspect_extractor import extract_aspects

        outputs = [
            _make_completed("not valid json {{{"),
            _make_completed(_ok_stdout()),
        ]
        idx = [0]

        def fake_run(*a, **kw):
            o = outputs[idx[0]]
            idx[0] += 1
            return o

        monkeypatch.setattr("nexus.aspect_extractor._run_claude_isolated", fake_run)
        record = extract_aspects(
            content="x", source_path="/p1.pdf", collection="knowledge__delos",
        )
        assert idx[0] == 2
        assert record is not None

    def test_does_not_retry_on_schema_validation_failure(self, monkeypatch) -> None:
        """Schema-shaped JSON missing required fields returns null-fields
        record WITHOUT retrying — retrying produces the same shape."""
        from nexus.aspect_extractor import extract_aspects

        # Valid inner JSON but missing required fields (e.g. proposed_method).
        # Wrap it in the outer ``--output-format json`` envelope.
        bad_payload = _wrap_inner(json.dumps({"problem_formulation": "x"}))
        calls: list[int] = []

        def fake_run(*a, **kw):
            calls.append(1)
            return _make_completed(bad_payload)

        monkeypatch.setattr("nexus.aspect_extractor._run_claude_isolated", fake_run)
        record = extract_aspects(
            content=_PAPER_SHAPED_CONTENT, source_path="/p1.pdf",
            collection="knowledge__delos",
        )

        assert len(calls) == 1  # exactly one — no retry
        assert record is not None
        # Null-fields record (preserves source_path + extractor metadata)
        assert record.problem_formulation is None
        assert record.proposed_method is None
        assert record.confidence is None
        assert record.extractor_name == "scholarly-paper-v1"

    def test_does_not_retry_on_hard_subprocess_error(self, monkeypatch) -> None:
        """A non-zero exit with non-transient stderr (e.g. authentication
        error) is NOT retried — retrying does not change the underlying
        problem.
        """
        from nexus.aspect_extractor import extract_aspects

        calls: list[int] = []

        def fake_run(*a, **kw):
            calls.append(1)
            return _make_completed(
                "", stderr="Error: invalid API key", returncode=2,
            )

        monkeypatch.setattr("nexus.aspect_extractor._run_claude_isolated", fake_run)
        record = extract_aspects(
            content="x", source_path="/p1.pdf", collection="knowledge__delos",
        )
        assert len(calls) == 1
        assert record is not None
        assert record.problem_formulation is None

    def test_caps_retries_at_three_attempts(self, monkeypatch) -> None:
        """Continuous transient failures yield 3 attempts then null-fields
        fallback — does not loop forever.
        """
        from nexus.aspect_extractor import extract_aspects

        calls: list[int] = []

        def fake_run(*a, **kw):
            calls.append(1)
            raise subprocess.TimeoutExpired(cmd=a[0], timeout=180)

        monkeypatch.setattr("nexus.aspect_extractor._run_claude_isolated", fake_run)
        record = extract_aspects(
            content="x", source_path="/p1.pdf", collection="knowledge__delos",
        )
        assert len(calls) == 3
        assert record is not None
        assert record.problem_formulation is None

    def test_oserror_from_subprocess_yields_null_fields_no_retry(
        self, monkeypatch,
    ) -> None:
        """Round-3 review Critical #1: ``OSError`` (e.g. ``E2BIG`` for
        argv > ARG_MAX) leaked out of the retry loop pre-fix, propagated
        out of ``extract_aspects``, and got swallowed only by the
        worker's broad except — silently marking the row failed without
        the documented null-fields fallback. Post-fix the OSError is
        classified as a hard failure and yields a null-fields record on
        the first attempt with no retries.
        """
        from nexus.aspect_extractor import extract_aspects

        calls: list[int] = []

        def fake_run(*a, **kw):
            calls.append(1)
            raise OSError(7, "Argument list too long")

        monkeypatch.setattr("nexus.aspect_extractor._run_claude_isolated", fake_run)
        record = extract_aspects(
            content="x", source_path="/p1.pdf",
            collection="knowledge__delos",
        )
        # Hard failure → no retries, single call, null-fields record.
        assert len(calls) == 1
        assert record is not None
        assert record.problem_formulation is None


# ── Sync-only contract ───────────────────────────────────────────────────────


class TestSyncContract:
    """The extractor MUST be synchronous (RDR-089 load-bearing). Pin
    via ``inspect`` — the public API has no async signature.
    """

    def test_extract_aspects_is_sync(self) -> None:
        import inspect
        from nexus.aspect_extractor import extract_aspects

        assert not inspect.iscoroutinefunction(extract_aspects)
        assert not inspect.isasyncgenfunction(extract_aspects)


# ── Phase D: extract_aspects_batch ──────────────────────────────────────────


class TestBatchExtraction:
    """RDR-089 Phase D: one Claude call extracts N papers."""

    def test_batch_invokes_subprocess_once_for_n_papers(
        self, monkeypatch,
    ) -> None:
        """A batch of 3 papers triggers exactly one subprocess.run."""
        from nexus.aspect_extractor import extract_aspects_batch

        captured: list[dict] = []

        def fake_run(args, **kwargs):
            captured.append({"args": args, "kwargs": kwargs})
            inner = json.dumps({
                "papers": [
                    {
                        "source_path": "/p1.pdf",
                        "problem_formulation": "P1",
                        "proposed_method": "M1",
                        "experimental_datasets": ["d1"],
                        "experimental_baselines": ["b1"],
                        "experimental_results": "R1",
                        "extras": {},
                        "confidence": 0.9,
                    },
                    {
                        "source_path": "/p2.pdf",
                        "problem_formulation": "P2",
                        "proposed_method": "M2",
                        "experimental_datasets": [],
                        "experimental_baselines": [],
                        "experimental_results": "R2",
                        "extras": {},
                        "confidence": 0.8,
                    },
                    {
                        "source_path": "/p3.pdf",
                        "problem_formulation": "P3",
                        "proposed_method": "M3",
                        "experimental_datasets": ["d3"],
                        "experimental_baselines": ["b3"],
                        "experimental_results": "R3",
                        "extras": {"venue": "V"},
                        "confidence": 0.95,
                    },
                ],
            })
            return _make_completed(_wrap_inner(inner))

        monkeypatch.setattr("nexus.aspect_extractor._run_claude_isolated", fake_run)
        records = extract_aspects_batch([
            ("knowledge__delos", "/p1.pdf", "content 1"),
            ("knowledge__delos", "/p2.pdf", "content 2"),
            ("knowledge__delos", "/p3.pdf", "content 3"),
        ])

        assert len(captured) == 1, "batch must use a single subprocess call"
        assert len(records) == 3
        assert records[0].source_path == "/p1.pdf"
        assert records[0].problem_formulation == "P1"
        assert records[1].source_path == "/p2.pdf"
        assert records[1].problem_formulation == "P2"
        assert records[2].source_path == "/p3.pdf"
        assert records[2].problem_formulation == "P3"

    def test_batch_demuxes_by_source_path_not_position(
        self, monkeypatch,
    ) -> None:
        """If the model returns papers in a different order than the
        input, the demux still aligns by source_path."""
        from nexus.aspect_extractor import extract_aspects_batch

        def fake_run(args, **kwargs):
            inner = json.dumps({
                "papers": [
                    {
                        "source_path": "/p2.pdf",  # reversed order
                        "problem_formulation": "P2",
                        "proposed_method": "M2",
                        "experimental_datasets": [],
                        "experimental_baselines": [],
                        "experimental_results": "R2",
                        "extras": {},
                        "confidence": 0.8,
                    },
                    {
                        "source_path": "/p1.pdf",
                        "problem_formulation": "P1",
                        "proposed_method": "M1",
                        "experimental_datasets": ["d1"],
                        "experimental_baselines": ["b1"],
                        "experimental_results": "R1",
                        "extras": {},
                        "confidence": 0.9,
                    },
                ],
            })
            return _make_completed(_wrap_inner(inner))

        monkeypatch.setattr("nexus.aspect_extractor._run_claude_isolated", fake_run)
        records = extract_aspects_batch([
            ("knowledge__delos", "/p1.pdf", "content 1"),
            ("knowledge__delos", "/p2.pdf", "content 2"),
        ])

        # Output order matches input order despite reversed response.
        assert records[0].source_path == "/p1.pdf"
        assert records[0].problem_formulation == "P1"
        assert records[1].source_path == "/p2.pdf"
        assert records[1].problem_formulation == "P2"

    def test_batch_missing_entry_yields_null_fields(
        self, monkeypatch,
    ) -> None:
        """If the batch response omits one paper, that paper gets
        null-fields in its slot; the others extract normally."""
        from nexus.aspect_extractor import extract_aspects_batch

        def fake_run(args, **kwargs):
            inner = json.dumps({
                "papers": [
                    {
                        "source_path": "/p1.pdf",
                        "problem_formulation": "P1",
                        "proposed_method": "M1",
                        "experimental_datasets": [],
                        "experimental_baselines": [],
                        "experimental_results": "R1",
                        "extras": {},
                        "confidence": 0.9,
                    },
                    # /p2.pdf omitted
                ],
            })
            return _make_completed(_wrap_inner(inner))

        monkeypatch.setattr("nexus.aspect_extractor._run_claude_isolated", fake_run)
        records = extract_aspects_batch([
            ("knowledge__delos", "/p1.pdf", "content 1"),
            ("knowledge__delos", "/p2.pdf", "content 2"),
        ])

        assert records[0].problem_formulation == "P1"
        # /p2.pdf got null-fields fallback
        assert records[1].source_path == "/p2.pdf"
        assert records[1].problem_formulation is None

    def test_batch_per_entry_schema_validation_failure_isolated(
        self, monkeypatch,
    ) -> None:
        """A malformed entry (missing required field) yields null-fields
        for that paper without affecting the others."""
        from nexus.aspect_extractor import extract_aspects_batch

        def fake_run(args, **kwargs):
            inner = json.dumps({
                "papers": [
                    {
                        "source_path": "/p1.pdf",
                        "problem_formulation": "P1",
                        # missing proposed_method (required)
                        "experimental_datasets": [],
                        "experimental_baselines": [],
                        "experimental_results": "R1",
                        "extras": {},
                        "confidence": 0.9,
                    },
                    {
                        "source_path": "/p2.pdf",
                        "problem_formulation": "P2",
                        "proposed_method": "M2",
                        "experimental_datasets": [],
                        "experimental_baselines": [],
                        "experimental_results": "R2",
                        "extras": {},
                        "confidence": 0.8,
                    },
                ],
            })
            return _make_completed(_wrap_inner(inner))

        monkeypatch.setattr("nexus.aspect_extractor._run_claude_isolated", fake_run)
        records = extract_aspects_batch([
            ("knowledge__delos", "/p1.pdf", "content 1"),
            ("knowledge__delos", "/p2.pdf", "content 2"),
        ])

        # /p1.pdf failed schema validation → null-fields
        assert records[0].source_path == "/p1.pdf"
        assert records[0].problem_formulation is None
        # /p2.pdf is fine
        assert records[1].problem_formulation == "P2"

    def test_batch_empty_content_uri_read_fail_yields_extract_fail(
        self, monkeypatch,
    ) -> None:
        """nexus-8g79.34 (RDR-096 P5.1): when a batch input has
        ``content=""`` and the URI read fails, the slot returns
        ``ExtractFail`` (not ``_empty_record``). Mirrors the single-doc
        path's contract so the worker can ``mark_done`` and skip
        retry on unreadable sources.
        """
        from nexus.aspect_extractor import (
            ExtractFail,
            extract_aspects_batch,
        )
        from nexus.aspect_readers import ReadFail

        # Mock read_source to return ReadFail for the empty-content row.
        # First row has content (subprocess path); second is empty
        # (URI path → ReadFail → ExtractFail).
        def fake_read(uri, **_kw):
            return ReadFail(reason="unreachable", detail=f"mocked: {uri}")

        # Mock get_t3 so the t3_handle init succeeds.
        class _FakeT3:
            def get_collection(self, _n):
                raise AssertionError("read_source is mocked; chroma never called")

        monkeypatch.setattr("nexus.aspect_extractor.read_source", fake_read)
        monkeypatch.setattr(
            "nexus.aspect_extractor.get_t3", lambda: _FakeT3(),
        )
        # subprocess.run must not fire — the empty-content row should
        # short-circuit to ExtractFail before the subprocess prompt
        # is built. (The first row would normally trigger subprocess,
        # but we exercise the read-fail path on the second row only;
        # use a single-input batch.)
        monkeypatch.setattr(
            "nexus.aspect_extractor.subprocess.run",
            lambda *a, **kw: (_ for _ in ()).throw(
                AssertionError("subprocess must not run when content is empty + read fails"),
            ),
        )

        records = extract_aspects_batch([
            ("knowledge__delos", "/missing.pdf", "", "1.99.1"),
        ])
        assert len(records) == 1
        assert isinstance(records[0], ExtractFail)
        assert records[0].reason == "unreachable"
        assert "/missing.pdf" in records[0].uri

    def test_batch_empty_input_returns_empty(self, monkeypatch) -> None:
        from nexus.aspect_extractor import extract_aspects_batch

        with patch("nexus.aspect_extractor._run_claude_isolated", side_effect=AssertionError(
            "must not call subprocess on empty input",
        )):
            records = extract_aspects_batch([])
        assert records == []

    def test_batch_unsupported_collection_yields_none(
        self, monkeypatch,
    ) -> None:
        from nexus.aspect_extractor import extract_aspects_batch

        # Unsupported collection only → no subprocess call.
        # code__* is unsupported by design (aspect extraction targets
        # prose claims, not source-code chunks).
        with patch("nexus.aspect_extractor._run_claude_isolated", side_effect=AssertionError(
            "must not call subprocess for unsupported-only batch",
        )):
            records = extract_aspects_batch([
                ("code__nexus", "/p1.py", "content"),
            ])
        assert records == [None]

    def test_batch_mixed_supported_and_unsupported(
        self, monkeypatch,
    ) -> None:
        """Mixed: supported collection rows extract together; the
        unsupported row gets None in its slot without disrupting
        the others."""
        from nexus.aspect_extractor import extract_aspects_batch

        def fake_run(args, **kwargs):
            inner = json.dumps({
                "papers": [
                    {
                        "source_path": "/p1.pdf",
                        "problem_formulation": "P1",
                        "proposed_method": "M1",
                        "experimental_datasets": [],
                        "experimental_baselines": [],
                        "experimental_results": "R1",
                        "extras": {},
                        "confidence": 0.9,
                    },
                ],
            })
            return _make_completed(_wrap_inner(inner))

        monkeypatch.setattr("nexus.aspect_extractor._run_claude_isolated", fake_run)
        records = extract_aspects_batch([
            ("code__nexus", "/skip.py", "content"),  # unsupported
            ("knowledge__delos", "/p1.pdf", "content 1"),
        ])

        assert records[0] is None  # code__nexus → unsupported
        assert records[1].source_path == "/p1.pdf"
        assert records[1].problem_formulation == "P1"

    def test_batch_strips_null_bytes_from_content(
        self, monkeypatch,
    ) -> None:
        """The null-byte defense from single-paper extends to batch."""
        from nexus.aspect_extractor import extract_aspects_batch

        captured_prompt: list[str] = []

        def fake_run(args, **kwargs):
            captured_prompt.append(args)
            inner = json.dumps({
                "papers": [
                    {
                        "source_path": "/p1.pdf",
                        "problem_formulation": "P1",
                        "proposed_method": "M1",
                        "experimental_datasets": [],
                        "experimental_baselines": [],
                        "experimental_results": "R1",
                        "extras": {},
                        "confidence": 0.9,
                    },
                ],
            })
            return _make_completed(_wrap_inner(inner))

        monkeypatch.setattr("nexus.aspect_extractor._run_claude_isolated", fake_run)
        extract_aspects_batch([
            ("knowledge__delos", "/p1.pdf", "content with\x00null bytes"),
        ])
        assert "\x00" not in captured_prompt[0]

    def test_batch_partitions_mixed_paper_and_prose(self, monkeypatch) -> None:
        """nexus-kmbys: a knowledge__ batch containing both a paper and a
        prose doc partitions into TWO subprocess calls — scholarly-paper-v1
        on the paper, general-prose-v1 on the prose — preserving input order
        and stamping the correct extractor_name per row."""
        from nexus.aspect_extractor import extract_aspects_batch

        headers_seen: list[str] = []

        def fake_run(args, **kwargs):
            prompt = args
            is_prose = "NOT scholarly papers" in prompt
            headers_seen.append("prose" if is_prose else "paper")
            # Echo back whichever source_paths the prompt carried.
            papers = []
            for sp in ("/paper.pdf", "/note.md"):
                if f"source_path: {sp}" in prompt:
                    papers.append({
                        "source_path": sp,
                        "problem_formulation": f"PF {sp}",
                        "proposed_method": f"PM {sp}",
                        "experimental_datasets": [],
                        "experimental_baselines": [],
                        "experimental_results": "R",
                        "extras": {},
                        "confidence": 0.9,
                    })
            # Prose header instructs a "documents" array; scholarly "papers".
            key = "documents" if is_prose else "papers"
            return _make_completed(_wrap_inner(json.dumps({key: papers})))

        monkeypatch.setattr("nexus.aspect_extractor._run_claude_isolated", fake_run)
        records = extract_aspects_batch([
            ("knowledge__delos", "/paper.pdf", _PAPER_SHAPED_CONTENT),
            ("knowledge__delos", "/note.md", "A short design note about caching. No paper structure here."),
        ])

        # Two partitions -> two subprocess calls (one per shape).
        assert sorted(headers_seen) == ["paper", "prose"]
        assert len(records) == 2
        # Order preserved by original input index.
        assert records[0].source_path == "/paper.pdf"
        assert records[0].extractor_name == "scholarly-paper-v1"
        assert records[1].source_path == "/note.md"
        assert records[1].extractor_name == "general-prose-v1"

    def test_batch_homogeneous_prose_is_single_call(self, monkeypatch) -> None:
        """nexus-kmbys: an all-prose batch still runs exactly one call."""
        from nexus.aspect_extractor import extract_aspects_batch

        calls: list[int] = []

        def fake_run(args, **kwargs):
            calls.append(1)
            prompt = args
            papers = [
                {
                    "source_path": sp,
                    "problem_formulation": "PF",
                    "proposed_method": "PM",
                    "experimental_datasets": [],
                    "experimental_baselines": [],
                    "experimental_results": "",
                    "extras": {},
                    "confidence": 0.7,
                }
                for sp in ("/a.md", "/b.md")
                if f"source_path: {sp}" in prompt
            ]
            return _make_completed(_wrap_inner(json.dumps({"papers": papers})))

        monkeypatch.setattr("nexus.aspect_extractor._run_claude_isolated", fake_run)
        records = extract_aspects_batch([
            ("knowledge__delos", "/a.md", "Just a note about onboarding."),
            ("knowledge__delos", "/b.md", "Another general essay on workflow."),
        ])
        assert len(calls) == 1
        assert all(r.extractor_name == "general-prose-v1" for r in records)

    def test_batch_rdr_uses_parser_fn_not_subprocess(self, monkeypatch) -> None:
        """nexus-kmbys review (Critical): a deterministic parser_fn config
        (rdr-frontmatter-v1) in the batch path must run the parser inline,
        NEVER the LLM subprocess with a scholarly-prompt fallback."""
        from nexus.aspect_extractor import extract_aspects_batch

        def boom(*a, **kw):
            raise AssertionError("subprocess must not run for a parser_fn config")

        monkeypatch.setattr("nexus.aspect_extractor._run_claude_isolated", boom)
        rdr_content = (
            "---\n"
            "id: RDR-999\nstatus: accepted\ntype: decision\n"
            "---\n\n"
            "## Problem Statement\n\nThe problem.\n\n"
            "## Proposed Solution\n\nThe solution.\n"
        )
        records = extract_aspects_batch([
            ("rdr__nexus", "docs/rdr/rdr-999.md", rdr_content),
        ])
        assert len(records) == 1
        assert records[0].extractor_name == "rdr-frontmatter-v1"
        assert records[0].problem_formulation  # parser populated it


class TestDocumentShapeClassifier:
    """nexus-kmbys: deterministic paper-vs-prose routing for knowledge__."""

    _PAPER = (
        "# Title\n\nAbstract\n\nWe propose a new index. In this paper we show "
        "gains.\n\n## References\n\n[1] Foo et al. (2020). Bar.\n"
    )
    _PROSE = (
        "# Design Note: Caching\n\nThis note sketches how we might cache plan "
        "matches. It is a working document, not a published result.\n"
    )

    def test_paper_shaped_classifies_as_paper(self):
        from nexus.aspect_extractor import _classify_document_shape
        assert _classify_document_shape(self._PAPER) == "paper"

    def test_prose_classifies_as_prose(self):
        from nexus.aspect_extractor import _classify_document_shape
        assert _classify_document_shape(self._PROSE) == "prose"

    def test_single_incidental_signal_stays_prose(self):
        """One citation marker alone (below the 2-signal threshold) must NOT
        promote a design note to the paper extractor."""
        from nexus.aspect_extractor import _classify_document_shape
        text = "A note that happens to reference [1] once, nothing else paper-like."
        assert _classify_document_shape(text) == "prose"

    def test_empty_content_is_prose(self):
        from nexus.aspect_extractor import _classify_document_shape
        assert _classify_document_shape("") == "prose"

    def test_technical_note_with_list_and_year_stays_prose(self):
        """Review finding: a design note using numbered list markers, a
        parenthetical year, and generic first-person ('our approach') must
        NOT be promoted to the paper extractor — those are not paper signals
        (false-positive prose->paper is the hallucination being fixed)."""
        from nexus.aspect_extractor import _classify_document_shape
        text = (
            "# Caching design\n\n"
            "Our approach ranks options:\n"
            "[1] LRU\n[2] LFU (2024 revision)\n\n"
            "We picked LRU for simplicity.\n"
        )
        assert _classify_document_shape(text) == "prose"

    def test_rdr172_fullstack_workload_doc_a_classifies_as_paper(self):
        """RDR-172 P2.2 (nexus-jr84c): pin that the --fullstack harness's
        paper-shaped workload doc (a) actually routes to scholarly-paper-v1.

        The harness asserts ``document_aspects > 0`` after storing this doc;
        the assertion is only the intended *paper-shaped* non-vacuous signal
        if this string still classifies as a paper. The classifier threshold
        is 2 and this string scores exactly 2 ("we propose"/"in this paper" +
        "et al."), so a tightening of _PAPER_SHAPE_SIGNALS/_THRESHOLD would
        silently downgrade the workload to prose — this test fails loudly
        instead. KEEP IN SYNC with rehearse_fullstack.sh doc (a)."""
        from nexus.aspect_extractor import (
            _SCHOLARLY_PAPER_CONFIG,
            _classify_document_shape,
            _resolve_config_for_document,
            select_config,
        )

        doc_a = (
            "We propose a widget-assembly index. In this paper we present a "
            "method for mechanical-part retrieval, evaluated against the prior "
            "approach of Gear et al. (2021). fsmark12345 widget paper fragment."
        )
        assert _classify_document_shape(doc_a) == "paper"
        routed = _resolve_config_for_document(
            "knowledge__knowledge", doc_a, select_config("knowledge__knowledge"),
        )
        assert routed is _SCHOLARLY_PAPER_CONFIG

    def test_resolver_only_substitutes_for_scholarly(self):
        """rdr-frontmatter (and any non-scholarly base) is never re-routed."""
        from nexus.aspect_extractor import (
            _GENERAL_PROSE_CONFIG,
            _SCHOLARLY_PAPER_CONFIG,
            _resolve_config_for_document,
            _REGISTRY,
        )
        rdr_cfg = _REGISTRY["rdr__"]
        # rdr base stays rdr even for prose-shaped content.
        assert _resolve_config_for_document("rdr__x", "prose blob", rdr_cfg) is rdr_cfg
        # scholarly base -> prose content routes to general-prose.
        out = _resolve_config_for_document("knowledge__x", self._PROSE, _SCHOLARLY_PAPER_CONFIG)
        assert out is _GENERAL_PROSE_CONFIG
        # scholarly base -> paper content stays scholarly.
        out2 = _resolve_config_for_document("knowledge__x", self._PAPER, _SCHOLARLY_PAPER_CONFIG)
        assert out2 is _SCHOLARLY_PAPER_CONFIG

    def test_single_doc_prose_routes_and_stamps_general_prose(self, monkeypatch):
        """End-to-end single-doc: prose content in knowledge__ produces a row
        stamped general-prose-v1 (nexus-kmbys)."""
        from nexus.aspect_extractor import extract_aspects

        def fake_run(args, **kwargs):
            prompt = args
            assert "NOT a" in prompt and "scholarly paper" in prompt, (
                "prose doc must use the general-prose prompt"
            )
            return _make_completed(_wrap_inner(json.dumps({
                "problem_formulation": "What caching strategy to use",
                "proposed_method": "Cache plan matches by intent hash",
                "experimental_datasets": [],
                "experimental_baselines": [],
                "experimental_results": "",
                "extras": {},
                "confidence": 0.7,
            })))

        monkeypatch.setattr("nexus.aspect_extractor._run_claude_isolated", fake_run)
        rec = extract_aspects(
            content=self._PROSE, source_path="/note.md",
            collection="knowledge__delos",
        )
        assert rec is not None
        assert rec.extractor_name == "general-prose-v1"
        assert rec.experimental_datasets == []
        assert rec.experimental_baselines == []


class TestGap3KnowledgeNoteRouting:
    """RDR-145 Phase 1 (P1.1, nexus-3g0l4): regression test pinning the
    shipped nexus-kmbys shape-aware routing for the ``knowledge__knowledge``
    collection — the exact locus of the aspect-orphan bug (nexus-pfzgb).

    MCP-stored notes (``store_put`` with no explicit collection) land in
    ``knowledge__knowledge``. Before nexus-kmbys these prose notes routed to
    ``scholarly-paper-v1``, whose prompt invites the model to invent
    datasets/baselines/venue — the fabrication this fix removed. These tests
    are the machine-checkable form of "non-fabricated": a representative note
    must route to ``general-prose-v1`` (NOT ``scholarly-paper-v1``) and the
    resulting aspect must carry empty experimental fields.

    Note: ``general-prose-v1`` enforces non-fabrication via *routing* (its
    prose prompt declares datasets/baselines ALWAYS ``[]``), not by stripping
    values in ``_build_record`` — so the discriminating, non-vacuous assertion
    is the routing decision plus the prose prompt being the one invoked.
    """

    #: A representative MCP-stored knowledge note: prose, no paper structure.
    _NOTE = (
        "# Session note: plan-match thresholds\n\n"
        "We settled on a 0.40 confidence floor for the plan-match gate. "
        "This is a working note captured during a session, not a published "
        "result. It records a decision, nothing more.\n"
    )

    def test_knowledge_note_routes_to_general_prose(self):
        """``knowledge__knowledge`` prose note resolves to general-prose-v1,
        never scholarly-paper-v1 (the fabricating extractor)."""
        from nexus.aspect_extractor import (
            _GENERAL_PROSE_CONFIG,
            _SCHOLARLY_PAPER_CONFIG,
            _resolve_config_for_document,
            select_config,
        )

        base = select_config("knowledge__knowledge")
        assert base is _SCHOLARLY_PAPER_CONFIG  # prefix selection unchanged
        chosen = _resolve_config_for_document(
            "knowledge__knowledge", self._NOTE, base,
        )
        assert chosen is _GENERAL_PROSE_CONFIG
        assert chosen is not _SCHOLARLY_PAPER_CONFIG

    def test_general_prose_config_does_not_require_experimental_fields(self):
        """Schema contract: general-prose-v1 requires ONLY the two prose
        fields, so a prose note never spuriously null-fields for missing
        datasets/baselines/results."""
        from nexus.aspect_extractor import _GENERAL_PROSE_CONFIG

        assert _GENERAL_PROSE_CONFIG.required_fields == (
            "problem_formulation",
            "proposed_method",
        )
        assert "experimental_datasets" not in _GENERAL_PROSE_CONFIG.required_fields
        assert "experimental_baselines" not in _GENERAL_PROSE_CONFIG.required_fields
        assert "experimental_results" not in _GENERAL_PROSE_CONFIG.required_fields

    def test_knowledge_note_aspect_has_empty_experimental_fields(self, monkeypatch):
        """End-to-end: a ``knowledge__knowledge`` note runs through the
        general-prose prompt and the row carries empty experimental fields.

        Non-vacuity: the fixture ASSERTS the prose prompt was invoked (the
        prompt that instructs datasets/baselines ``[]``) before returning the
        prose-contract payload — so the empty fields are tied to the routing
        under test, not merely echoed."""
        from nexus.aspect_extractor import extract_aspects

        def fake_run(prompt, *a, **kwargs):
            assert "NOT a" in prompt and "scholarly paper" in prompt, (
                "knowledge__knowledge prose note must use the general-prose prompt"
            )
            return _make_completed(_wrap_inner(json.dumps({
                "problem_formulation": "What confidence floor for plan-match",
                "proposed_method": "Use a 0.40 floor",
                "experimental_datasets": [],
                "experimental_baselines": [],
                "experimental_results": "",
                "extras": {},
                "confidence": 0.6,
            })))

        monkeypatch.setattr("nexus.aspect_extractor._run_claude_isolated", fake_run)
        rec = extract_aspects(
            content=self._NOTE, source_path="my-note",
            collection="knowledge__knowledge",
        )
        assert rec is not None
        assert rec.extractor_name == "general-prose-v1"
        assert rec.experimental_datasets == []
        assert rec.experimental_baselines == []
        assert rec.experimental_results in ("", None)
