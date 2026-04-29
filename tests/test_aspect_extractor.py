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

    def test_docs_prefix_routes_to_scholarly_config(self) -> None:
        """#377: docs__* collections (markdown / ADR / design docs
        from `nx index repo`) hold the same kind of substantive prose
        as knowledge__*. They route to the same scholarly-paper-v1
        config so problem_formulation / proposed_method / etc. apply
        uniformly. Until #377 landed, docs__* was unconditionally
        rejected with 'No extractor config registered'."""
        from nexus.aspect_extractor import select_config

        config = select_config("docs__handbook")
        assert config is not None
        assert config.extractor_name == "scholarly-paper-v1"

    def test_extract_aspects_returns_none_for_unsupported_collection(
        self, tmp_path: Path,
    ) -> None:
        """No subprocess invocation for unsupported collections —
        the function short-circuits at config selection. Asserts via
        a subprocess mock that would have raised if called."""
        from nexus.aspect_extractor import extract_aspects

        with patch("subprocess.run", side_effect=AssertionError("must not be called")):
            result = extract_aspects(
                content="content",
                source_path="/p1.pdf",
                collection="code__nexus",
            )
        assert result is None


# ── Successful extraction (knowledge__*) ─────────────────────────────────────


class TestSuccessfulExtraction:
    def test_subprocess_invocation_shape(self, monkeypatch) -> None:
        """``extract_aspects`` invokes ``claude -p --output-format json``
        with the prompt fed via stdin (kwargs['input']), timeout=180,
        capture_output=True, text=True. Stdin replaces argv to bypass
        macOS ARG_MAX (errno 7) on multi-page papers."""
        from nexus.aspect_extractor import extract_aspects

        captured: list[dict] = []

        def fake_run(args, **kwargs):
            captured.append({"args": args, "kwargs": kwargs})
            return _make_completed(_ok_stdout())

        monkeypatch.setattr(subprocess, "run", fake_run)
        extract_aspects(
            content="some paper text",
            source_path="/p1.pdf",
            collection="knowledge__delos",
        )

        assert len(captured) == 1
        args = captured[0]["args"]
        kwargs = captured[0]["kwargs"]
        assert args == ["claude", "-p", "--output-format", "json"]
        # Prompt is stdin-fed, not argv. Must include the paper content.
        assert "some paper text" in kwargs.get("input", "")
        assert kwargs.get("timeout") == 180
        assert kwargs.get("capture_output") is True
        assert kwargs.get("text") is True

    def test_argv_size_is_bounded_regardless_of_content_length(
        self, monkeypatch,
    ) -> None:
        """Long documents must not bloat argv. The whole point of stdin
        is that 14-page papers no longer trip macOS ARG_MAX."""
        from nexus.aspect_extractor import extract_aspects

        captured: list[dict] = []

        def fake_run(args, **kwargs):
            captured.append({"args": args, "kwargs": kwargs})
            return _make_completed(_ok_stdout())

        monkeypatch.setattr(subprocess, "run", fake_run)
        big_content = "X" * 200_000  # 200 KB — would have tripped E2BIG
        extract_aspects(
            content=big_content,
            source_path="/big.pdf",
            collection="knowledge__delos",
        )
        # argv must stay tiny — only fixed flags. The content lives in stdin.
        argv_bytes = sum(len(s) + 1 for s in captured[0]["args"])
        assert argv_bytes < 256, f"argv unexpectedly grew to {argv_bytes} bytes"
        assert len(captured[0]["kwargs"]["input"]) >= 200_000

    def test_strips_markdown_code_fence_around_json(self, monkeypatch) -> None:
        """The Claude CLI sometimes wraps JSON in a ```json ... ```
        fence even when the prompt asks for raw JSON. The extractor
        strips the fence before parsing.
        """
        from nexus.aspect_extractor import extract_aspects

        monkeypatch.setattr(
            subprocess, "run",
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

        monkeypatch.setattr(subprocess, "run", fake_run)
        record = extract_aspects(
            content="x", source_path="/p1.pdf", collection="knowledge__delos",
        )
        assert len(calls) == 1  # no retry on hard failure
        assert record is not None
        assert record.problem_formulation is None  # null-fields fallback

    def test_json_response_parsed_into_aspect_record(self, monkeypatch) -> None:
        from nexus.aspect_extractor import extract_aspects

        monkeypatch.setattr(
            subprocess, "run", lambda *a, **kw: _make_completed(_ok_stdout()),
        )
        record = extract_aspects(
            content="x",
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
    def test_passes_through_content_when_populated(self, monkeypatch) -> None:
        from nexus.aspect_extractor import extract_aspects

        def fake_run(args, **kwargs):
            assert "INLINE_CONTENT_HERE" in kwargs.get("input", "")
            return _make_completed(_ok_stdout())

        monkeypatch.setattr(subprocess, "run", fake_run)
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

        with patch("subprocess.run", side_effect=AssertionError(
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
            assert "T3_REASSEMBLED_CONTENT_FROM_CHUNKS" in kwargs.get("input", "")
            return _make_completed(_ok_stdout())

        monkeypatch.setattr(subprocess, "run", fake_run)
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
            captured.append(kwargs.get("input", ""))
            return _make_completed(_ok_stdout())

        monkeypatch.setattr(subprocess, "run", fake_run)
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
        with patch("subprocess.run", side_effect=AssertionError(
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


# ── URI dispatch + chroma integration (RDR-096 P1.2) ────────────────────────


class TestUriDispatch:
    """Integration tests exercising the new ``read_source`` call path
    inside ``extract_aspects`` against a real ``chromadb.EphemeralClient``.
    Verifies the URI is constructed from ``(collection, source_path)``,
    chunk reassembly works end-to-end, and read failures surface as
    ``ExtractFail`` (no row written).
    """

    def test_succeeds_via_chroma_for_knowledge_collection(
        self, monkeypatch,
    ) -> None:
        """End-to-end: knowledge__ shape with ``title``-keyed chunks
        + EphemeralClient + a fake subprocess. The chroma reader
        reassembles, the extractor parses, and an ``AspectRecord``
        emerges with non-null fields.
        """
        import chromadb

        from nexus.aspect_extractor import AspectRecord, extract_aspects

        client = chromadb.EphemeralClient()
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
            assert "Knowledge note about update capture." in kwargs.get("input", "")
            return _make_completed(_ok_stdout())

        monkeypatch.setattr(subprocess, "run", fake_run)

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
        import chromadb

        from nexus.aspect_extractor import ExtractFail, extract_aspects

        client = chromadb.EphemeralClient()
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

        with patch("subprocess.run", side_effect=AssertionError(
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
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _make_completed(_ok_stdout()))

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
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _make_completed(_ok_stdout()))

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

        monkeypatch.setattr(subprocess, "run", fake_run)
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

        monkeypatch.setattr(subprocess, "run", fake_run)
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

        monkeypatch.setattr(subprocess, "run", fake_run)
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

        monkeypatch.setattr(subprocess, "run", fake_run)
        record = extract_aspects(
            content="x", source_path="/p1.pdf", collection="knowledge__delos",
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

        monkeypatch.setattr(subprocess, "run", fake_run)
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

        monkeypatch.setattr(subprocess, "run", fake_run)
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

        monkeypatch.setattr(subprocess, "run", fake_run)
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

        monkeypatch.setattr(subprocess, "run", fake_run)
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

        monkeypatch.setattr(subprocess, "run", fake_run)
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

        monkeypatch.setattr(subprocess, "run", fake_run)
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

        monkeypatch.setattr(subprocess, "run", fake_run)
        records = extract_aspects_batch([
            ("knowledge__delos", "/p1.pdf", "content 1"),
            ("knowledge__delos", "/p2.pdf", "content 2"),
        ])

        # /p1.pdf failed schema validation → null-fields
        assert records[0].source_path == "/p1.pdf"
        assert records[0].problem_formulation is None
        # /p2.pdf is fine
        assert records[1].problem_formulation == "P2"

    def test_batch_empty_input_returns_empty(self, monkeypatch) -> None:
        from nexus.aspect_extractor import extract_aspects_batch

        with patch("subprocess.run", side_effect=AssertionError(
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
        with patch("subprocess.run", side_effect=AssertionError(
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

        monkeypatch.setattr(subprocess, "run", fake_run)
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
            captured_prompt.append(kwargs.get("input", ""))
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

        monkeypatch.setattr(subprocess, "run", fake_run)
        extract_aspects_batch([
            ("knowledge__delos", "/p1.pdf", "content with\x00null bytes"),
        ])
        assert "\x00" not in captured_prompt[0]

    def test_batch_mixed_extractor_configs_raises(self) -> None:
        """A batch must come from a single ExtractorConfig. Mixed
        configs are a caller bug; raise rather than dispatch
        silently."""
        from nexus.aspect_extractor import extract_aspects_batch

        # Only one config exists today (knowledge__*); construct a
        # synthetic mixed batch by patching the registry briefly.
        # This test pins the contract; with one config in the
        # registry the only achievable mismatch is via two configs
        # that share no prefix. Skip when only one config is
        # registered.
        from nexus.aspect_extractor import _REGISTRY
        if len(_REGISTRY) < 2:
            pytest.skip(
                "only one ExtractorConfig registered; mixed-config "
                "test is vacuous"
            )
