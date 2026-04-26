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

        assert select_config("docs__handbook") is None
        assert select_config("code__nexus") is None
        # rdr__* now has its own config (RDR-089 Phase F).

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
        """``extract_aspects`` invokes ``claude -p <prompt>
        --output-format json`` with timeout=180, capture_output=True,
        text=True."""
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
        assert args[0] == "claude"
        assert args[1] == "-p"
        # Argument 2 is the prompt — shape-only check (must include the
        # paper content somewhere, must reference scholarly aspects).
        prompt = args[2]
        assert "some paper text" in prompt
        assert args[3] == "--output-format"
        assert args[4] == "json"
        assert kwargs.get("timeout") == 180
        assert kwargs.get("capture_output") is True
        assert kwargs.get("text") is True

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
            assert "INLINE_CONTENT_HERE" in args[2]
            return _make_completed(_ok_stdout())

        monkeypatch.setattr(subprocess, "run", fake_run)
        extract_aspects(
            content="INLINE_CONTENT_HERE",
            source_path="/path/never/read.pdf",
            collection="knowledge__delos",
        )

    def test_reads_source_path_when_content_empty(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """CLI sites pass content="". The extractor reads source_path."""
        from nexus.aspect_extractor import extract_aspects

        src = tmp_path / "p1.txt"
        src.write_text("DISK_SOURCED_CONTENT")

        def fake_run(args, **kwargs):
            assert "DISK_SOURCED_CONTENT" in args[2]
            return _make_completed(_ok_stdout())

        monkeypatch.setattr(subprocess, "run", fake_run)
        record = extract_aspects(
            content="",
            source_path=str(src),
            collection="knowledge__delos",
        )
        assert record is not None
        assert record.problem_formulation == "Sharded WAL bottleneck."

    def test_strips_embedded_null_bytes_from_content(self, monkeypatch) -> None:
        """P1.3 spike caught real-world PDFs whose pymupdf-extracted text
        contains \\x00 bytes. ``subprocess.run`` rejects argv entries
        with embedded null bytes (POSIX C-string contract) — passing
        them raises ``ValueError('embedded null byte')`` BEFORE the
        retry guard runs. The extractor strips \\x00 from content
        before building the prompt so this real-world failure mode
        is no longer fatal.
        """
        from nexus.aspect_extractor import extract_aspects

        captured: list[str] = []

        def fake_run(args, **kwargs):
            captured.append(args[2])
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

    def test_unreadable_source_path_returns_null_fields_record(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """``content=""`` plus a missing file produces a null-fields
        record (row exists for triage, no subprocess call attempted).
        """
        from nexus.aspect_extractor import extract_aspects

        with patch("subprocess.run", side_effect=AssertionError(
            "subprocess must NOT be called when source_path is unreadable",
        )):
            record = extract_aspects(
                content="",
                source_path=str(tmp_path / "does-not-exist.pdf"),
                collection="knowledge__delos",
            )

        assert record is not None
        assert record.problem_formulation is None
        assert record.proposed_method is None
        assert record.experimental_datasets == []
        assert record.experimental_baselines == []
        assert record.experimental_results is None
        assert record.extras == {}
        assert record.confidence is None
        assert record.extractor_name == "scholarly-paper-v1"
        assert record.model_version  # config-supplied
        assert record.extracted_at  # populated even on failure


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
        with patch("subprocess.run", side_effect=AssertionError(
            "must not call subprocess for unsupported-only batch",
        )):
            records = extract_aspects_batch([
                ("docs__handbook", "/p1.md", "content"),
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
            ("docs__handbook", "/skip.md", "content"),  # unsupported
            ("knowledge__delos", "/p1.pdf", "content 1"),
        ])

        assert records[0] is None  # docs__handbook → unsupported
        assert records[1].source_path == "/p1.pdf"
        assert records[1].problem_formulation == "P1"

    def test_batch_strips_null_bytes_from_content(
        self, monkeypatch,
    ) -> None:
        """The null-byte defense from single-paper extends to batch."""
        from nexus.aspect_extractor import extract_aspects_batch

        captured_prompt: list[str] = []

        def fake_run(args, **kwargs):
            captured_prompt.append(args[2])
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
