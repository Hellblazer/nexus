# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-wi1uv: post-extraction text-quality sanity gate.

``nx index pdf --extractor docling`` (the documented safe recovery from a
MinerU failure) can complete and report success while producing
space-stripped, unsearchable text ("istheasetofthe"). These tests cover
the calibrated signal function (:func:`assess_extraction_quality`) in
isolation and the gate wired into :meth:`PDFExtractor.extract`.
"""
from pathlib import Path
from unittest.mock import patch

import pytest

from nexus.errors import ExtractionQualityError
from nexus.pdf_extractor import (
    ExtractionResult,
    PDFExtractor,
    assess_extraction_quality,
)


@pytest.fixture
def extractor():
    return PDFExtractor()


@pytest.fixture
def dummy_pdf(tmp_path: Path) -> Path:
    p = tmp_path / "doc.pdf"
    p.write_bytes(b"dummy pdf bytes")
    return p


# Real healthy prose, repeated to clear the 500-char minimum — drawn from
# tests/fixtures/bft-to-smr.pdf's abstract (Docling extraction, verbatim).
_HEALTHY_PROSE = (
    "We present a new algorithm for state machine replication that is "
    "built around a leader-driven consensus primitive. This algorithm "
    "requires only two additional communication steps between replicas "
    "and clients if the consensus leader is correct and the system is "
    "synchronous, being thus the first latency-optimal transformation "
    "from Byzantine consensus to BFT state machine replication. We also "
    "discuss how to make simple modifications to leader-driven consensus "
    "algorithms in order to make them compatible with our transformation. "
)
# Joined with newlines (not a single unbroken paragraph) so the
# space-stripped garbage version below has multiple LINES -- one huge
# token per line -- clearing _MIN_TOKENS_FOR_RATIO_SIGNALS the way a real
# multi-paragraph/multi-page document's stripped output would, rather
# than collapsing the whole sample into a single giant token.
_HEALTHY_TEXT = "\n".join([_HEALTHY_PROSE] * 25)  # >=20 tokens per line, >=20 lines

# Real dense-LaTeX prose (Zoology paper, Appendix H — the exact incident
# document from nexus-5xn3k, hydrated from T3 knowledge__dt-papers) — the
# named false-positive hazard: legitimately dense math notation must PASS.
_DENSE_LATEX_TEXT = (
    r"""$ , previous layer's
output $\mathbf{O}_{\mathrm{prev}}[0\cdot\cdot\cdotN-\mathrm{\bar{\Phi}}]\in\mathbb{R}^{N\times3\bar{c}},$ , and linear bias $\mathbf{\overline{{B}}}\in\mathbb{R}^{N\timesN}$
1: Add ${\pmbu}_{\mathrm{curr}}\gets{\pmbu}+\mathbf{O}_{\mathrm{prev}}$ as an input to this layer.
2: $\mathbf{K},\mathbf{Q},\mathbf{V}\longleftarrow{\boldsymbol{u}}_{\mathrm{curr}}\mathbf{W}_{Q},{\boldsymbol{u}}_{\mathrm{curr}}\mathbf{W}_{K},{\boldsymbol{u}}_{\mathrm{curr}}\mathbf{W}_{V}.$
3: $\mathbf{O}\gets\left(\mathbf{Q}\mathbf{K}^{\top}+\mathbf{B}\right)\mathbf{V}$
4: return O as the output of this layer.

Proposition H.27. Given an input $\pmb{u}\in\{0,1\}^{N\timesd}$ (encoded as in Remark $H.26)$ where $d=3c$ , Attention with linear biases (even without using soft-max) solves Mqar for u using $\mathcal{O}(c^{2})$ parameters, $\mathcal{O}(Nc^{2}+N^{2}c)$ time complexity and $\mathcal{O}(1)$ layers.

Proof. We use two layers of attention. We will start by specifying the projection matrices for the first layer $\mathbf{W}_{Q}^{1},\mathbf{\bar{W}}_{K}^{1},\mathbf{W}_{V}^{1}\in\mathbb{R}^{d\timesd}$ as:

$$\mathbf{W}_{K}^{1}\equiv\mathbf{W}_{Q}^{1}\equiv\mathbf{0},\quad\mathbf{W}_{V}^{1}\equiv\left(\mathbf{0}\quad\mathbf{I}_{\mathbf{c}\times\mathbf{c}}\quad\mathbf{0}\right)$$

Above, $\mathbf{W_{V}^{1}}$ is meant to isolate the ${\mathbf{}}v_{i}$ embeddings. RetNet similar to a state-space-model (SSM) Gu et al. [2021], except for that the matrices A and C are input-dependent (i.e. they are functions of the input x). We note that RetNet is a special case of the recently proposed Mamba architectures, which replace the gamma term in RetNet with yet another input-dependent matrix.
"""
)


def _strip_spaces_per_line(text: str) -> str:
    """Reproduce the incident's exact failure signature: intra-line spaces
    removed, line breaks preserved (matches how docling's per-page markdown
    degrades when word-boundary whitespace collapses)."""
    return "\n".join(line.replace(" ", "") for line in text.split("\n"))


_GARBAGE_TEXT = _strip_spaces_per_line(_HEALTHY_TEXT)


# ── assess_extraction_quality: pure signal function ─────────────────────────


class TestAssessExtractionQuality:
    def test_healthy_prose_passes(self):
        report = assess_extraction_quality(_HEALTHY_TEXT)
        assert report.passed
        assert report.failing_signals == []

    def test_dense_latex_notation_passes(self):
        """The named false-positive hazard: real dense math/formula text
        must not trip the gate."""
        report = assess_extraction_quality(_DENSE_LATEX_TEXT)
        assert report.passed, f"dense notation wrongly flagged: {report.failing_signals}"

    def test_space_stripped_garbage_fails(self):
        """The bead's exact motivating signature: space-stripped text must
        fail the gate."""
        report = assess_extraction_quality(_GARBAGE_TEXT)
        assert not report.passed
        assert report.failing_signals

    def test_space_stripped_garbage_trips_whitespace_ratio(self):
        report = assess_extraction_quality(_GARBAGE_TEXT)
        assert any("whitespace_ratio" in s for s in report.failing_signals)

    def test_space_stripped_garbage_trips_mean_token_len(self):
        report = assess_extraction_quality(_GARBAGE_TEXT)
        assert any("mean_token_len" in s for s in report.failing_signals)

    def test_short_text_below_floor_passes_unconditionally(self):
        """Below the 500-char floor the signals are too noisy to trust —
        defers to the separately-enforced zero-chunks check downstream."""
        report = assess_extraction_quality("short abstract, real text.")
        assert report.passed
        assert report.failing_signals == []

    def test_empty_text_passes_unconditionally(self):
        report = assess_extraction_quality("")
        assert report.passed

    def test_all_whitespace_removed_single_token_fails(self):
        """A pathological all-one-token document (n_tokens==0 branch)."""
        report = assess_extraction_quality("x" * 1000)
        assert not report.passed

    def test_report_carries_measured_values(self):
        report = assess_extraction_quality(_HEALTHY_TEXT)
        assert report.n_chars == len(_HEALTHY_TEXT)
        assert report.n_tokens > 0
        assert 0.0 <= report.whitespace_ratio <= 1.0


# ── extract() wiring: the gate fires at the one choke point all backends
# route through ──────────────────────────────────────────────────────────


def _make_result(text: str, method: str = "docling") -> ExtractionResult:
    return ExtractionResult(
        text=text,
        metadata={"extraction_method": method, "page_count": 1, "page_boundaries": []},
    )


class TestExtractGateWiring:
    def test_docling_healthy_output_passes_through(self, extractor, dummy_pdf):
        with patch.object(extractor, "_extract_with_docling", return_value=_make_result(_HEALTHY_TEXT)):
            result = extractor.extract(dummy_pdf, extractor="docling")
        assert result.text == _HEALTHY_TEXT
        assert result.metadata["quality_gate_passed"] is True

    def test_docling_garbage_output_raises(self, extractor, dummy_pdf):
        with patch.object(extractor, "_extract_with_docling", return_value=_make_result(_GARBAGE_TEXT)):
            with pytest.raises(ExtractionQualityError, match="quality gate"):
                extractor.extract(dummy_pdf, extractor="docling")

    def test_mineru_garbage_output_raises(self, extractor, dummy_pdf):
        """The gate is extractor-agnostic — MinerU output is gated exactly
        like Docling's, never special-cased."""
        with patch.object(extractor, "_extract_with_mineru", return_value=_make_result(_GARBAGE_TEXT, "mineru")):
            with pytest.raises(ExtractionQualityError):
                extractor.extract(dummy_pdf, extractor="mineru")

    def test_pymupdf_fallback_garbage_output_raises(self, extractor, dummy_pdf):
        """docling raising falls back to PyMuPDF (_extract_normalized) —
        that fallback's output is gated too."""
        with patch.object(extractor, "_extract_with_docling", side_effect=RuntimeError("boom")):
            with patch.object(extractor, "_extract_normalized", return_value=_make_result(_GARBAGE_TEXT, "pymupdf_normalized")):
                with pytest.raises(ExtractionQualityError):
                    extractor.extract(dummy_pdf, extractor="docling")

    def test_allow_degraded_bypasses_the_raise(self, extractor, dummy_pdf):
        with patch.object(extractor, "_extract_with_docling", return_value=_make_result(_GARBAGE_TEXT)):
            result = extractor.extract(dummy_pdf, extractor="docling", allow_degraded=True)
        assert result.text == _GARBAGE_TEXT
        assert result.metadata["quality_gate_passed"] is False
        assert result.metadata["quality_gate_overridden"] is True

    def test_allow_degraded_default_is_false(self, extractor, dummy_pdf):
        """The override must be opt-in, never the default posture."""
        with patch.object(extractor, "_extract_with_docling", return_value=_make_result(_GARBAGE_TEXT)):
            with pytest.raises(ExtractionQualityError):
                extractor.extract(dummy_pdf, extractor="docling")

    def test_auto_mode_low_formula_docling_path_gated(self, extractor, dummy_pdf):
        """auto mode's <5-formula branch returns the Docling probe result
        directly — confirm that exit point is gated too."""
        with patch("nexus.pdf_extractor._has_formulas_quick", return_value=0):
            with patch.object(extractor, "_extract_with_docling", return_value=_make_result(_GARBAGE_TEXT)):
                with pytest.raises(ExtractionQualityError):
                    extractor.extract(dummy_pdf, extractor="auto")

    def test_error_message_names_remedy(self, extractor, dummy_pdf):
        with patch.object(extractor, "_extract_with_docling", return_value=_make_result(_GARBAGE_TEXT)):
            with pytest.raises(ExtractionQualityError) as exc_info:
                extractor.extract(dummy_pdf, extractor="docling")
        msg = str(exc_info.value)
        assert "--allow-degraded-extraction" in msg
        assert "mineru" in msg.lower()


# ── quality_gate_overridden durability (nexus-wi1uv round-2) ────────────────
#
# code-review-expert + substantive-critic Critical (both independently,
# 2026-08-06): docs/cli-reference.md claimed chunk metadata carries
# quality_gate_overridden:true, which was FALSE — the flag lived only on
# the transient ExtractionResult.metadata (a CLI echo + structlog WARNING,
# discarded once the process exits). These tests cover the two write
# paths that now stamp it onto persisted chunk metadata: the batch/
# incremental path (doc_indexer._pdf_chunks -> make_chunk_metadata) and
# the streaming path's post-pass (pipeline_stages._enrich_metadata_from_
# extraction -> t3.update_chunks). See tests/test_metadata_quality_gate_
# overridden.py for the metadata_schema-level contract.


from unittest.mock import MagicMock  # noqa: E402 — grouped with this section's imports


class TestQualityGateOverriddenPropagation:
    def test_pdf_chunks_stamps_override_from_extraction_result(self, tmp_path: Path):
        """Batch/incremental path: PDFExtractor.extract()'s metadata carries
        quality_gate_overridden (set by _enforce_extraction_quality when
        allow_degraded=True bypassed a real gate failure) — _pdf_chunks
        must thread it into every produced chunk's metadata."""
        from nexus.doc_indexer import _pdf_chunks

        pdf = tmp_path / "x.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")

        with (
            patch("nexus.doc_indexer.PDFExtractor") as ext_cls,
            patch("nexus.doc_indexer.PDFChunker") as chk_cls,
        ):
            ext_cls.return_value.extract.return_value = ExtractionResult(
                text=_GARBAGE_TEXT,
                metadata={
                    "extraction_method": "docling",
                    "page_count": 1,
                    "format": "markdown",
                    "page_boundaries": [],
                    "quality_gate_passed": False,
                    "quality_gate_overridden": True,
                },
            )
            chunk = MagicMock()
            chunk.text = "some chunk text"
            chunk.metadata = {"chunk_start_char": 0, "chunk_end_char": 15, "page_number": 1}
            chk_cls.return_value.chunk.return_value = [chunk]

            prepared = _pdf_chunks(
                pdf, content_hash="deadbeef" * 8, target_model="voyage-context-3",
                now_iso="2026-08-06T00:00:00+00:00", corpus="default",
                allow_degraded_extraction=True,
            )
        assert len(prepared) == 1
        _id, _text, meta = prepared[0]
        assert meta["quality_gate_overridden"] is True

    def test_pdf_chunks_omits_key_on_healthy_extraction(self, tmp_path: Path):
        """The common case (gate passed, no override) must NOT carry the
        key — the sparse-key design in metadata_schema.normalize()."""
        from nexus.doc_indexer import _pdf_chunks

        pdf = tmp_path / "x.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")

        with (
            patch("nexus.doc_indexer.PDFExtractor") as ext_cls,
            patch("nexus.doc_indexer.PDFChunker") as chk_cls,
        ):
            ext_cls.return_value.extract.return_value = ExtractionResult(
                text=_HEALTHY_TEXT,
                metadata={
                    "extraction_method": "docling",
                    "page_count": 1,
                    "format": "markdown",
                    "page_boundaries": [],
                    "quality_gate_passed": True,
                },
            )
            chunk = MagicMock()
            chunk.text = "some chunk text"
            chunk.metadata = {"chunk_start_char": 0, "chunk_end_char": 15, "page_number": 1}
            chk_cls.return_value.chunk.return_value = [chunk]

            prepared = _pdf_chunks(
                pdf, content_hash="deadbeef" * 8, target_model="voyage-context-3",
                now_iso="2026-08-06T00:00:00+00:00", corpus="default",
            )
        assert len(prepared) == 1
        _id, _text, meta = prepared[0]
        assert "quality_gate_overridden" not in meta

    def test_streaming_enrichment_includes_override_when_true(self):
        """Streaming path post-pass: _enrich_metadata_from_extraction must
        merge quality_gate_overridden=True into the update_chunks payload
        when the ExtractionResult carries it."""
        from nexus.pipeline_stages import _enrich_metadata_from_extraction

        result = ExtractionResult(
            text=_GARBAGE_TEXT,
            metadata={
                "extraction_method": "docling", "page_count": 1,
                "quality_gate_overridden": True,
            },
        )
        col = MagicMock()
        col.get.return_value = {"ids": ["c1"], "metadatas": [{"title": "old"}]}
        t3 = MagicMock()

        ok = _enrich_metadata_from_extraction(
            "hash123", result, Path("/repo/x.pdf"), t3, col, "docs__test",
        )
        assert ok is True
        t3.update_chunks.assert_called_once()
        _, call_args, _ = t3.update_chunks.mock_calls[0]
        updated_metas = call_args[2]
        assert updated_metas[0]["quality_gate_overridden"] is True

    def test_streaming_enrichment_omits_key_when_false(self):
        """The common case must not merge the key in at all (verified
        fix: the enrichment dict does NOT rely on a downstream normalize()
        that the HttpVectorClient.update_chunks production path does not
        actually run)."""
        from nexus.pipeline_stages import _enrich_metadata_from_extraction

        result = ExtractionResult(
            text=_HEALTHY_TEXT,
            metadata={"extraction_method": "docling", "page_count": 1},
        )
        col = MagicMock()
        col.get.return_value = {"ids": ["c1"], "metadatas": [{"title": "old"}]}
        t3 = MagicMock()

        ok = _enrich_metadata_from_extraction(
            "hash123", result, Path("/repo/x.pdf"), t3, col, "docs__test",
        )
        assert ok is True
        t3.update_chunks.assert_called_once()
        _, call_args, _ = t3.update_chunks.mock_calls[0]
        updated_metas = call_args[2]
        assert "quality_gate_overridden" not in updated_metas[0]


# ── Round-2 findings (code-review-expert + substantive-critic, both
# independently, 2026-08-06) ─────────────────────────────────────────────


class TestBoundaryBehaviorShortDocuments:
    """Significant #4 (critic): the round-1 500-char auto-pass floor
    reproduced the bead's own incident signature UNDETECTED at 169 chars
    -- short garbage still yields >=1 real chunk, so "defer to the
    zero-chunks check downstream" was false for this case. Fixed via a
    token-count floor that gates only whitespace_ratio/long_token_fraction
    (which need enough samples); mean_token_len applies unconditionally.
    """

    # Real prose (Docling extraction of tests/fixtures/bft-to-smr.pdf's
    # abstract), truncated to the critic's reported boundary length.
    _SHORT_HEALTHY = (
        "We present a new algorithm for state machine replication that is "
        "built around a leader-driven consensus primitive. This algorithm "
    )[:169]

    def test_short_healthy_text_passes(self):
        report = assess_extraction_quality(self._SHORT_HEALTHY)
        assert report.passed
        assert report.n_chars <= 169

    def test_short_garbage_at_boundary_length_fails(self):
        """The critic's exact reproduction: space-stripped at ~169 chars
        must now FAIL (round-1 passed this unconditionally)."""
        garbage = _strip_spaces_per_line(self._SHORT_HEALTHY)
        report = assess_extraction_quality(garbage)
        assert not report.passed
        assert any("mean_token_len" in s for s in report.failing_signals)

    def test_short_garbage_fails_via_mean_token_len_specifically(self):
        """mean_token_len is the signal that must not wait for a
        sample-size floor -- verify it fires even with very few tokens."""
        garbage = _strip_spaces_per_line(self._SHORT_HEALTHY)
        report = assess_extraction_quality(garbage)
        assert report.n_tokens < 20  # below _MIN_TOKENS_FOR_RATIO_SIGNALS
        assert report.mean_token_len > 20.0

    def test_extremely_short_single_word_passes(self):
        """A trivially short, genuinely real single word must not
        false-positive just for being short."""
        report = assess_extraction_quality("Abstract")
        assert report.passed

    def test_empty_text_passes_unconditionally_round2(self):
        report = assess_extraction_quality("")
        assert report.passed
        assert report.n_chars == 0


class TestCJKNonSpacedScript:
    """Significant #5 (critic): str.split() cannot segment CJK text at
    all (no inter-word spaces) -- a real CJK document is structurally
    identical to the space-stripped-garbage signature on every signal.
    Detected via non-spaced-script dominance and SKIPPED (capability-
    honest), never force-fit into a threshold that cannot discriminate."""

    # Real Chinese prose (not per-character-repeated filler) about NLP.
    _CJK_PROSE = (
        "自然语言处理是计算机科学与人工智能领域的一个重要方向，"
        "研究能实现人与计算机之间用自然语言进行有效通信的各种理论和方法。"
        "近年来，深度学习技术在这一领域取得了显著的进展，"
        "特别是在机器翻译、文本摘要和问答系统等任务上。"
        "本文综述了近期的研究成果，并讨论了未来可能的发展方向。"
    ) * 3

    def test_cjk_prose_passes(self):
        report = assess_extraction_quality(self._CJK_PROSE)
        assert report.passed

    def test_cjk_prose_is_skipped_not_silently_evaluated(self):
        """Capability-honest: the report must SAY it skipped evaluation,
        not just happen to pass -- distinguishes a real pass from a class
        of input this gate cannot judge."""
        report = assess_extraction_quality(self._CJK_PROSE)
        assert report.skipped_reason != ""
        assert "non-spaced-script" in report.skipped_reason.lower()

    def test_latin_prose_is_not_skipped(self):
        """Regression: the skip path must not fire on ordinary English."""
        report = assess_extraction_quality(_HEALTHY_TEXT)
        assert report.skipped_reason == ""

    def test_mixed_cjk_and_latin_below_threshold_not_skipped(self):
        """A document that is mostly Latin with a few CJK terms must not
        trip the skip path -- only CJK-DOMINANT text should."""
        mixed = _HEALTHY_TEXT + " 附录 "
        report = assess_extraction_quality(mixed)
        assert report.skipped_reason == ""

    def test_space_stripped_cjk_still_reported_passed(self):
        """Even a 'stripped' CJK sample (already unspaced by nature) must
        not be flagged -- there is no meaningful stripped/healthy
        distinction for this script, which is exactly why it is skipped
        rather than evaluated."""
        report = assess_extraction_quality(self._CJK_PROSE)
        assert report.passed

    def test_enforce_extraction_quality_logs_skip_not_warning(self, tmp_path: Path):
        """_enforce_extraction_quality must not raise, and must not log
        the failure-path WARNING/ERROR events, for a skipped CJK doc."""
        from nexus.pdf_extractor import _enforce_extraction_quality

        pdf = tmp_path / "cjk.pdf"
        pdf.write_bytes(b"dummy")
        result = _make_result(self._CJK_PROSE)
        _enforce_extraction_quality(result, pdf, allow_degraded=False)  # must not raise
        assert result.metadata["quality_gate_passed"] is True
        assert "quality_gate_overridden" not in result.metadata


class TestCodeIdentifierDenseText:
    """Significant #6 (critic): synthetic Python-identifier-dense text
    tripped long_token_fraction=0.42 at the round-1 ceiling (0.10) -- a
    real false-positive class the bead's own "dense-notation" hazard
    language covers but round-1 only calibrated against LaTeX. Recalibrated
    (see module docstring) with this repo's own identifiers worked into
    realistic prose -- round-2 ceiling raised to 0.5 with verified margin.
    """

    # Realistic systems-paper-with-code-listing prose, using this repo's
    # own real identifiers (not cherry-picked to fail) -- long_token_
    # fraction measures ~0.20 on this sample.
    _CODE_DENSE_PROSE = """
The containment wrapper _contain_extraction_quality_gate wraps _index_pdf_file
and records failures into _quality_gate_failed, mirroring _contain_transient_upsert's
shape. Both compose inside _index_one_pdf via run_file_loop's FIRST_EXCEPTION
semantics. The metadata_schema.make_chunk_metadata factory threads
quality_gate_overridden through ALLOWED_TOP_LEVEL, and normalize() drops the
False default in metadata_schema.normalize before HttpVectorClient.update_chunks
posts to /v1/vectors/update-metadata. See PER_RECORD_SURVIVABLE_EXCEPTIONS in
src/nexus/errors.py and test_rlkgu_per_record_catch_tripwire.py for the
classification contract enforced by test_every_nexuserror_subclass_is_classified.
"""

    # Denser, pseudocode/algorithm-listing style -- long_token_fraction ~0.25.
    _PSEUDOCODE_DENSE = """
Algorithm _contain_extraction_quality_gate(fn, file, failed):
  try: return fn()
  except ExtractionQualityError as exc:
    _log.error("index_file_quality_gate_failed", file=str(file), error=str(exc))
    failed.append(str(file)); return 0
Called from _index_one_pdf via _contain_transient_upsert(lambda: _contain_extraction_quality_gate(...), file)
"""

    def test_code_dense_prose_passes(self):
        report = assess_extraction_quality(self._CODE_DENSE_PROSE)
        assert report.passed, f"code-dense prose wrongly flagged: {report.failing_signals}"

    def test_pseudocode_dense_passes(self):
        report = assess_extraction_quality(self._PSEUDOCODE_DENSE)
        assert report.passed, f"pseudocode-dense text wrongly flagged: {report.failing_signals}"

    def test_code_dense_prose_long_token_fraction_has_margin(self):
        """Verify the calibration margin claimed in the module docstring
        is real and load-bearing, not decorative -- confirms this sample
        genuinely exercises long_token_fraction (not trivially near zero)
        while staying under the revised ceiling."""
        report = assess_extraction_quality(self._CODE_DENSE_PROSE)
        assert 0.1 < report.long_token_fraction < 0.5

    def test_ceiling_tightened_to_round1_value_flags_code_dense(self):
        """Kill control: prove the round-2 threshold is load-bearing by
        confirming the round-1 ceiling (0.10) WOULD have flagged this
        legitimate sample -- i.e. this is a genuine recalibration, not a
        threshold that never mattered."""
        report = assess_extraction_quality(self._CODE_DENSE_PROSE)
        assert report.long_token_fraction > 0.10

    def test_worst_case_identifiers_only_still_fails(self):
        """An unrealistic worst case (nothing but long identifiers, no
        connecting prose at all) should still fail -- the raised ceiling
        must not blind the gate to genuinely pathological input."""
        identifiers = " ".join([
            "_contain_extraction_quality_gate", "PER_RECORD_SURVIVABLE_EXCEPTIONS",
            "test_every_nexuserror_subclass_is_classified", "quality_gate_overridden",
            "_QUALITY_GATE_MIN_CHARS_removed", "ExtractionQualityError",
            "_enforce_extraction_quality", "make_chunk_metadata",
            "MAX_SAFE_TOP_LEVEL_KEYS", "_LONG_TOKEN_FRACTION_CEILING",
        ] * 3)
        report = assess_extraction_quality(identifiers)
        assert not report.passed
