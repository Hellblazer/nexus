# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx enrich aspects --dry-run`` is bounded. nexus-bocft (the "hang").

``_dry_run_predict_skips`` made one ``read_source`` call per entry -- one
HTTPS round-trip to the vector service each, serially, printing nothing in
between. On a 407-entry knowledge collection in cloud mode it ran past 300
seconds and was killed twice as a hang. It was not hung; it was doing 407
round-trips after printing "--dry-run: skipping extraction."

The prediction now reads a fixed-size prefix and projects the skip rate.
These tests count the calls, which is the property that matters.
"""
from __future__ import annotations

from types import SimpleNamespace

import click
import pytest

import nexus.commands.enrich as enrich_mod
from nexus import aspect_readers
from nexus.commands.enrich import _DRY_RUN_PREDICT_SAMPLE, _dry_run_predict_skips

def _entries(n: int) -> list:
    return [SimpleNamespace(title=f"note-{i:03d}", file_path="", source_uri="") for i in range(n)]


@pytest.fixture()
def wiring(monkeypatch):
    """A read_source that counts its calls and fails every third read."""
    calls: list[str] = []

    def fake_read_source(uri, *, t3=None, doc_id_lookup=None, manifest_lookup=None):
        calls.append(uri)
        if len(calls) % 3 == 0:
            return aspect_readers.ReadFail(reason="empty")
        return SimpleNamespace(text="ok")

    monkeypatch.setattr(aspect_readers, "read_source", fake_read_source)
    monkeypatch.setattr("nexus.mcp_infra.get_t3", lambda: object())
    monkeypatch.setattr(enrich_mod, "_build_catalog_doc_id_lookup", lambda: {})
    monkeypatch.setattr(enrich_mod, "_build_catalog_manifest_lookup", lambda: {})
    monkeypatch.setattr(enrich_mod, "_chroma_source_id_for_entry", lambda e: e.title)
    out: list[str] = []
    monkeypatch.setattr(enrich_mod.click, "echo", lambda s="", *a, **k: out.append(str(s)))
    return calls, out


def test_a_large_collection_reads_only_the_sample(wiring) -> None:
    calls, out = wiring
    _dry_run_predict_skips(_entries(407), "knowledge__x")

    assert len(calls) == _DRY_RUN_PREDICT_SAMPLE, (
        f"{len(calls)} round-trips for a dry-run; the 407-entry case took >300s this way"
    )
    text = "\n".join(out)
    assert f"samples the first {_DRY_RUN_PREDICT_SAMPLE} of 407" in text, text


def test_the_skip_rate_is_projected_onto_the_population(wiring) -> None:
    calls, out = wiring
    _dry_run_predict_skips(_entries(300), "knowledge__x")

    sampled_skips = _DRY_RUN_PREDICT_SAMPLE // 3
    projected = round(sampled_skips * 300 / _DRY_RUN_PREDICT_SAMPLE)
    text = "\n".join(out)
    assert f"{sampled_skips} of {_DRY_RUN_PREDICT_SAMPLE} sampled" in text, text
    assert f"projected ~{projected} of 300" in text, text
    # The cost line uses the PROJECTED count, not the sampled one.
    assert f"Predicted actual cost (excluding skips): ~${(300 - projected) * enrich_mod._PER_PAPER_COST_USD:.2f}" in text, text


def test_a_small_collection_is_read_in_full_and_reported_exactly(wiring) -> None:
    """When the sample is the population, nothing is projected and the
    wording is the exact one; the pre-fix behaviour for small collections."""
    calls, out = wiring
    _dry_run_predict_skips(_entries(9), "knowledge__x")

    assert len(calls) == 9
    text = "\n".join(out)
    assert "samples the first" not in text
    assert "Planned skips: 3 of 9 document(s)" in text, text
    assert "projected" not in text


def test_all_readable_says_so_without_projecting(wiring, monkeypatch) -> None:
    calls, out = wiring
    monkeypatch.setattr(
        aspect_readers, "read_source",
        lambda uri, **kw: (calls.append(uri), SimpleNamespace(text="ok"))[1],
    )
    _dry_run_predict_skips(_entries(100), "knowledge__x")

    assert len(calls) == _DRY_RUN_PREDICT_SAMPLE
    assert any("All sampled entries readable" in s for s in out), out
