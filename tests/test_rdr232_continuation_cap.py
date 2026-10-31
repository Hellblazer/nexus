# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-232 (nexus-jypmj): a long narrowed read goes to the headless path, not the continuation handoff.

A run confined to one document reserves up to 82 primary chunks under a 60,000-character budget; with their
attribution headers that is about 70,000 characters in the reducer's input, over
``bundle.MAX_CONTINUATION_PROMPT_CHARS`` (60,000, the cap of a prompt that lands in the CALLING session's context).
The continuation envelope is then not built (``continuation_oversized_fallback_to_headless``) and the whole answer
runs headless: no evidence is truncated. Sam's call whether narrowed runs get a separate cap. Tenant-free.
"""
from __future__ import annotations

import hashlib
from typing import Any

from structlog.testing import capture_logs

from nexus.plans.bundle import MAX_CONTINUATION_PROMPT_CHARS
from nexus.plans.continuation import classify_continuation_cut
from nexus.plans.continuation_envelope import assemble_continuation_envelope
from nexus.plans.primary_evidence import DocumentCoverage, PrimaryEvidence, SelectedChunk
from nexus.plans.reserved_stream import RESERVED_SHARE_NARROWED, ReservedStream, reserved_scope

DOC, COL = "1.2.4686", "rdr__1-2__bge-base-en-v15-768__v1"


def _h(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _narrowed_stream(chunks: int, chars: int) -> ReservedStream:
    items = tuple(
        SelectedChunk(
            source="RDR-084", doc_id=DOC, position=i, chash=_h(f"chunk {i}"), collection=COL,
            label=f"Section {i}", label_source="content", text=f"chunk {i} " + "w" * chars,
        )
        for i in range(chunks)
    )
    evidence = PrimaryEvidence(items=items, documents=(DocumentCoverage(
        source="RDR-084", doc_id=DOC, title="t", status="ok", manifest_rows=chunks, selected_rows=chunks, partial=False),))
    stream = ReservedStream.from_evidence(evidence, share_max=RESERVED_SHARE_NARROWED)
    assert stream is not None
    return stream


def _continuation(stream: ReservedStream) -> tuple[Any, list[dict]]:
    steps = [
        {"tool": "search", "args": {"query": "q", "corpus": COL}},
        {"tool": "summarize", "args": {"content": "$step1.contents"}},
    ]
    prefix = [{"ids": ["c1"], "tumblers": [DOC], "contents": ["a secondary chunk"], "chunk_collections": [COL]}]
    with capture_logs() as logs, reserved_scope(stream):
        envelope = assemble_continuation_envelope(
            cut=classify_continuation_cut(steps), steps=steps, bindings={}, step_outputs=prefix, plan_id=1, run_id=None,
        )
    return envelope, logs


def test_a_long_narrowed_read_exceeds_the_continuation_prompt_cap_and_falls_back_to_headless() -> None:
    assert MAX_CONTINUATION_PROMPT_CHARS == 60_000
    # the shape of the largest measured read: 73 chunks, about 59,800 characters, plus the headers
    envelope, logs = _continuation(_narrowed_stream(chunks=73, chars=815))
    fallback = [e for e in logs if e.get("event") == "continuation_oversized_fallback_to_headless"]
    assert envelope is None and len(fallback) == 1, [e.get("event") for e in logs]
    assert fallback[0]["prompt_chars"] > fallback[0]["max_chars"] == MAX_CONTINUATION_PROMPT_CHARS


def test_a_short_narrowed_read_still_hands_off_to_the_continuation() -> None:
    # the control: the same wiring with a small read builds the envelope, so the fallback above is the size and nothing else
    envelope, logs = _continuation(_narrowed_stream(chunks=20, chars=600))
    assert envelope is not None
    assert not [e for e in logs if e.get("event") == "continuation_oversized_fallback_to_headless"]
