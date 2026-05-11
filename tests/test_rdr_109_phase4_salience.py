# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-109 Phase 4 prototype: salient-sentence extraction + boost.

Tests cover the deterministic pieces (sentence split, token overlap)
and the salience extractor with a stub cross-encoder. The full
calibration sweep is integration work tracked separately in the
``data/calibration/rdr-109/`` artefacts and the
``scripts/rdr-109-calibrate.py`` harness.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# scripts/ is not a package; insert it on sys.path for the import.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from rdr_109_salience import (  # noqa: E402
    extract_salient_sentences,
    load_seed_queries,
    split_sentences,
    token_overlap_boost,
)


# ── split_sentences ──────────────────────────────────────────────────


def test_split_sentences_drops_empty() -> None:
    assert split_sentences("") == []
    assert split_sentences("   \n\t  ") == []


def test_split_sentences_basic() -> None:
    text = "First sentence. Second sentence. Third sentence."
    out = split_sentences(text)
    assert len(out) == 3
    assert out[0].startswith("First")
    assert out[-1].startswith("Third")


def test_split_sentences_truncates_long_run_on() -> None:
    text = "X" * 5000 + ". " + "Y" * 100 + "."
    out = split_sentences(text, max_sentence_chars=600)
    assert all(len(s) <= 600 for s in out)


# ── token_overlap_boost ──────────────────────────────────────────────


def test_token_overlap_zero_weight_short_circuits() -> None:
    assert token_overlap_boost("query text", ["query"], weight=0.0) == 0.0


def test_token_overlap_no_overlap_returns_zero() -> None:
    assert token_overlap_boost(
        "alpha beta",
        ["gamma delta"],
        weight=0.5,
    ) == 0.0


def test_token_overlap_full_match() -> None:
    # 2/2 tokens overlap; boost = 0.5 * 1.0 = 0.5
    score = token_overlap_boost(
        "alpha beta",
        ["alpha beta"],
        weight=0.5,
    )
    assert score == pytest.approx(0.5)


def test_token_overlap_partial_match() -> None:
    # query tokens: {alpha, beta, gamma} (3)
    # salient tokens: {alpha, beta} (2 of query overlap)
    score = token_overlap_boost(
        "alpha beta gamma",
        ["alpha beta"],
        weight=0.3,
    )
    assert score == pytest.approx(0.3 * 2 / 3)


def test_token_overlap_handles_empty_inputs() -> None:
    assert token_overlap_boost("", ["alpha"], weight=0.5) == 0.0
    assert token_overlap_boost("alpha", [], weight=0.5) == 0.0
    assert token_overlap_boost("alpha", [""], weight=0.5) == 0.0


# ── extract_salient_sentences ────────────────────────────────────────


class _StubCrossEncoder:
    """Returns scores keyed on substring presence, deterministic and
    cheap. Lets the test pin which sentences a given seed prefers."""

    def __init__(self, scores_by_substring: dict[str, float]) -> None:
        self._scores_by_substring = scores_by_substring

    def score(self, query: str, documents: list[str]) -> list[float]:
        out: list[float] = []
        for doc in documents:
            best = 0.0
            for substr, val in self._scores_by_substring.items():
                if substr in doc:
                    best = max(best, val)
            out.append(best)
        return out


def test_extract_salient_returns_top_n_by_score() -> None:
    chunk = "Alpha sentence one. Beta sentence two. Gamma sentence three."
    ce = _StubCrossEncoder({"Beta": 5.0, "Gamma": 4.0, "Alpha": 1.0})
    out = extract_salient_sentences(
        chunk, seed_queries=["seed"], top_n=2, cross_encoder=ce,
    )
    # Top 2 by max-pooled score; tie-broken stable-by-original-order.
    assert len(out) == 2
    assert any("Beta" in s for s in out)
    assert any("Gamma" in s for s in out)


def test_extract_salient_max_pools_across_seeds() -> None:
    chunk = "Alpha alpha. Beta beta."
    # seed-1 prefers Alpha; seed-2 prefers Beta. With max-pool both
    # tie at high score; both kept.
    ce = _StubCrossEncoder({"Alpha": 9.0, "Beta": 9.0})
    out = extract_salient_sentences(
        chunk, seed_queries=["s1", "s2"], top_n=5, cross_encoder=ce,
    )
    assert len(out) == 2


def test_extract_salient_empty_chunk_returns_empty() -> None:
    ce = _StubCrossEncoder({})
    assert extract_salient_sentences(
        "", seed_queries=["s"], top_n=3, cross_encoder=ce,
    ) == []


# ── load_seed_queries integration ────────────────────────────────────


def test_load_seed_queries_round_trip(tmp_path: Path) -> None:
    payload = {
        "content_type": "code",
        "seeds": ["What does this do?", "How is it used?"],
    }
    (tmp_path / "seed_queries_code.json").write_text(json.dumps(payload))
    out = load_seed_queries(tmp_path, "code")
    assert out == ["What does this do?", "How is it used?"]
