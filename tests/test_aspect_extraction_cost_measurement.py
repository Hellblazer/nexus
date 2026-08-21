# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-196 .p0b (bead nexus-nyry9.6) — measurement instrument and pin test
for ``commands/enrich.py``'s ``_PER_PAPER_COST_USD`` constant.

**Audit-fold F1 scope decision (Option B, nexus-nyry9.6):** aspect
extraction (``aspect_extractor.py``) is NOT rerouted through
``operators.dispatch.claude_dispatch`` -- it stays a synchronous,
independent ``claude -p`` call site (RDR-089 load-bearing sync contract +
the RF-8 ``PR_SET_PDEATHSIG`` daemon-death protection ``claude_dispatch``
does not have; see the bead's close-reason comment for the full
reasoning). That means aspect_extractor does NOT itself capture
``DispatchUsage`` telemetry (RDR-196 .p1a) at runtime, so there is no live
history to read the per-paper cost from.

Per the bead comment's own escape hatch ("under B, [.p1a] is the source of
the fixture/field knowledge"), this file uses ``claude_dispatch`` — already
landed, tested, and usage-instrumented — purely as a MEASUREMENT
INSTRUMENT: it dispatches one prompt/schema pair shaped exactly like a
real ``scholarly-paper-v1`` extraction (same prompt template, comparable
content length, no ``--model`` override -- matching production's actual
argv, which never passes ``--model`` despite ``ExtractorConfig.
model_version`` being only a label) and records the real
``DispatchUsage``. This is independent of whether the PRODUCTION
aspect-extraction path itself goes through ``claude_dispatch`` — it does
not, under Option B.

``TestLiveMeasurement`` is the one-shot measurement: run it manually,
read the recorded cost off the assertion/log, and hand-transcribe it (with
date + canonical model) into the ``_PER_PAPER_COST_USD`` provenance
comment. It also remains as a standing sanity check (skipped by default,
same convention as ``TestClaudeDispatchLiveUsage`` in
test_operator_dispatch.py) that a live dispatch really is non-free.

``TestPerPaperCostConstantIsMeasured`` is the bead's own required
falsifying pin: a value of ``0.01`` (the old fabricated literal) or
``0.0`` must fail it.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from nexus.aspect_extractor import _SCHOLARLY_PAPER_CONFIG, _SCHOLARLY_PAPER_PROMPT
from nexus.commands.enrich import _PER_PAPER_COST_USD

# A synthetic but structurally realistic scholarly-paper body -- several
# paragraphs across abstract/intro/method/experiments/results, ~1000 words.
# Deliberately NOT 196-R2's trivial 'respond with exactly {"ok": true}'
# prompt (a different, much smaller input shape) -- the point of this
# fixture is a representative token count for a real extraction dispatch.
_SYNTHETIC_PAPER_BODY = """\
Title: Cross-Domain Retrieval Alignment for Long-Context Question Answering

Abstract

Retrieval-augmented generation (RAG) systems typically assume that the
retriever and the generator share a common notion of relevance: passages
ranked highly by the retriever are assumed to be the passages the
generator actually needs to answer a question correctly. In practice this
assumption breaks down whenever the retrieval corpus spans multiple
domains with different writing conventions, terminology, and structural
idioms -- for example a corpus mixing engineering design documents,
scholarly papers, and informal meeting notes. We introduce a lightweight
cross-domain alignment layer, Latent Domain Bridging (LDB), that learns a
small set of domain-conditioned projection matrices applied to retrieved
passage embeddings before they are ranked against the query embedding.
Unlike prior domain-adaptation approaches that require retraining the
base retriever, LDB is trained post-hoc on a small amount of weakly
supervised relevance data and adds negligible inference latency. Across
three heterogeneous benchmark corpora we show that LDB improves
answer-bearing passage recall by 6 to 11 points at fixed retrieval depth,
and that these gains propagate through to end-task exact-match accuracy
when paired with three different open-weight generator models.

1. Introduction

Long-context question answering over large, heterogeneous document
collections is an increasingly common deployment scenario: a single
retrieval index may need to serve queries against product documentation,
internal design records, and externally published research simultaneously.
Standard dense retrievers are trained on relatively homogeneous corpora
(web passages, Wikipedia, or a single curated QA dataset) and their
learned embedding space reflects the statistical regularities of that
training distribution. When such a retriever is applied unmodified to a
mixed corpus, passages from underrepresented domains are systematically
under-ranked relative to their true relevance, even when they contain the
literal answer span the query is looking for. This effect compounds in
retrieval-augmented generation pipelines: a generator conditioned on a
recall-degraded context set produces answers that are either incomplete,
hedged, or confidently wrong.

A natural fix is to fine-tune the retriever end-to-end on the target
mixed corpus. This is expensive, requires substantial labelled relevance
data per domain, and risks catastrophic forgetting of the retriever's
original strong performance on well-represented domains. We instead ask
whether a much smaller intervention -- a set of per-domain linear
projections applied only at inference time, after the base retriever has
already produced its embeddings -- can recover most of the available
recall improvement without touching the base model's weights at all.

2. Method

LDB operates in three stages. First, each document in the corpus is
tagged with a coarse domain label using a lightweight zero-shot
classifier (we use a distilled sentence-pair classifier trained on a
small labelled seed set, described in Section 3.1). Second, for each
domain we learn a projection matrix W_d by minimizing a contrastive
objective over (query, positive passage, negative passage) triples drawn
from that domain's weakly supervised relevance judgments; the objective
encourages the projected passage embedding W_d p to move closer to
queries known to be answered by p and further from queries known not to
be. Third, at retrieval time each candidate passage embedding is
projected through its domain's matrix before the final cosine-similarity
ranking against the (unmodified) query embedding.

Because W_d is a single dense matrix of the same dimensionality as the
base embedding space, applying it at inference time costs a single
matrix-vector multiply per candidate passage -- negligible relative to
the cost of the approximate nearest-neighbor search itself. Training each
W_d requires only a few thousand weakly supervised triples per domain,
which we obtain cheaply via a distant-supervision heuristic: query-passage
pairs where the passage's containing document is later cited, linked, or
directly quoted in the query's own source document are treated as weak
positives.

3. Experimental Setup

3.1 Corpora. We evaluate on three heterogeneous retrieval corpora
constructed by combining an engineering-documentation corpus, a
scholarly-paper corpus, and an internal-notes corpus, each independently
indexed with the same base retriever and then merged into a single
retrieval index. Domain labels for training LDB's classifier come from a
small hand-labelled seed set of 500 documents per corpus; the remaining
documents are labelled automatically at indexing time.

3.2 Baselines. We compare against the unmodified base retriever, a fully
fine-tuned variant of the same retriever on the merged corpus, and a
simple domain-reranking baseline that reorders top-k results using a
cross-encoder conditioned on the predicted domain label.

3.3 Metrics. We report Recall@10 for answer-bearing passages, and
downstream exact-match accuracy when the retrieved context is passed to
each of three open-weight generator models of varying size.

4. Results

LDB improves Recall@10 by 6 to 11 points over the unmodified base
retriever across all three corpora, closing roughly 70 percent of the gap
to the fully fine-tuned upper bound while requiring two orders of
magnitude less training data and no modification to the base retriever's
weights. The domain-reranking baseline recovers a smaller fraction of the
gap (2 to 4 points) and adds materially more inference latency than LDB's
single matrix multiply. Downstream exact-match accuracy improves in
proportion to the recall gain for all three generator models tested,
confirming that the retrieval-side improvement is not absorbed or
cancelled out by the generator.

We additionally ablate the domain-labelling step by replacing the learned
zero-shot classifier with ground-truth domain labels (available in our
evaluation setup but not in general deployment); this closes an
additional 1 to 2 points of recall gap, indicating that most of LDB's
benefit comes from the projection itself rather than from precise domain
boundaries.

5. Conclusion

We show that a lightweight, post-hoc, per-domain linear projection
applied to retrieved passage embeddings recovers most of the recall
benefit of full retriever fine-tuning on heterogeneous corpora, at a
fraction of the training cost and with no change to the base retriever.
This makes cross-domain retrieval alignment practical for deployments
where full retriever retraining is infeasible.
"""

# The 5 required-field JSON Schema mirroring ExtractorConfig.required_fields
# + the optional extras/confidence fields the prompt also asks for.
_ASPECT_SCHEMA = {
    "type": "object",
    "required": [
        "problem_formulation",
        "proposed_method",
        "experimental_datasets",
        "experimental_baselines",
        "experimental_results",
    ],
    "properties": {
        "problem_formulation": {"type": "string"},
        "proposed_method": {"type": "string"},
        "experimental_datasets": {"type": "array", "items": {"type": "string"}},
        "experimental_baselines": {"type": "array", "items": {"type": "string"}},
        "experimental_results": {"type": "string"},
        "extras": {"type": "object"},
        "confidence": {"type": "number"},
    },
}


def _claude_auth_available() -> bool:
    """Same probe as TestClaudeDispatchLiveUsage in test_operator_dispatch.py
    (duplicated rather than imported -- that file is under a sibling review
    fence for RDR-196 .p2b and must not be touched or depended on here)."""
    try:
        result = subprocess.run(
            ["claude", "auth", "status", "--json"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return False
        data = json.loads(result.stdout)
        return bool(data.get("loggedIn") or data.get("isLoggedIn"))
    except Exception:
        return False


class TestPerPaperCostConstantIsMeasured:
    """Falsifying pin (bead VERIFICATION): the constant must not be the old
    fabricated literal, and must not be free."""

    def test_not_the_old_fabricated_literal(self) -> None:
        assert _PER_PAPER_COST_USD != 0.01

    def test_not_zero(self) -> None:
        assert _PER_PAPER_COST_USD != 0.0
        assert _PER_PAPER_COST_USD > 0


@pytest.mark.integration
class TestLiveMeasurement:
    """The measurement instrument (run manually to (re)derive the constant).

    Run with:
      uv run pytest -m integration tests/test_aspect_extraction_cost_measurement.py -k LiveMeasurement -s
    """

    @pytest.mark.asyncio
    async def test_live_aspect_extraction_shaped_dispatch_records_nonzero_cost(self) -> None:
        if not _claude_auth_available():
            pytest.skip("claude CLI not on PATH or not authenticated -- live dispatch skipped")

        from nexus.operators.dispatch import DispatchUsage, claude_dispatch

        prompt = _SCHOLARLY_PAPER_PROMPT.format(content=_SYNTHETIC_PAPER_BODY)
        assert prompt == _SCHOLARLY_PAPER_CONFIG.prompt_template.format(
            content=_SYNTHETIC_PAPER_BODY,
        ), "prompt must be built from the SAME template production uses"

        sink: list[DispatchUsage] = []
        result = await claude_dispatch(
            prompt,
            _ASPECT_SCHEMA,
            timeout=180.0,  # matches _invoke_once's single-paper budget
            usage_sink=sink,
            # No model= override -- production's _run_claude_isolated never
            # passes --model either (ExtractorConfig.model_version is a
            # storage label, not an actual CLI argument), so this measures
            # the SAME ambient-default-model cost production actually pays.
        )

        assert isinstance(result, dict)
        assert len(sink) == 1, f"expected exactly one usage record, got {sink}"
        usage = sink[0]
        assert usage.cost_usd is not None and usage.cost_usd > 0, (
            f"live aspect-extraction-shaped dispatch must record non-zero cost, got {usage.cost_usd!r}"
        )
        assert usage.model is not None and usage.model != ""
        print(  # noqa: T201 -- manual measurement instrument; the operator reads this
            f"\nMEASURED aspect-extraction dispatch: cost_usd={usage.cost_usd} "
            f"model={usage.model} input_tokens={usage.input_tokens} "
            f"output_tokens={usage.output_tokens} duration_ms={usage.duration_ms}",
        )
