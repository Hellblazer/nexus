# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-f9duo (indexing-brittleness P0.4): ``nx doctor --check-embeddings``.

The probe re-embeds sampled chunk text with the collection's model and
compares it with the stored vector. nexus-tysei found stored vectors at
cosine 0.25 to 0.87 against their own text, invisible to every audit.

The drift case is built, not mocked: a chunk is stored with the engine's
real embedding of a DIFFERENT text, which is the shape tysei found (a real
vector, finite and unit-norm, that does not represent its text).
"""
from __future__ import annotations

import random
from datetime import UTC, datetime

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.db import make_t3
from nexus.doctor_embeddings import (
    COSINE_FLOOR,
    CollectionDrift,
    _cosine,
    default_seed,
    format_report,
    probe_collection,
    window_offsets,
)

_CLEAN = "knowledge__f9duo-clean__bge-base-en-v15-768__v1"
_DRIFT = "knowledge__f9duo-drift__bge-base-en-v15-768__v1"

_TEXTS = [
    "Hilbert curves preserve locality when mapping a cube onto a line.",
    "Postgres advisory locks serialise a critical section across sessions.",
    "The rain in the valley fed three rivers before the spring thaw.",
    "Voyage context embeddings take the surrounding document into account.",
]


def _ids(prefix: str, n: int) -> list[str]:
    return [f"{prefix}{i:063x}" for i in range(1, n + 1)]


def _seed_clean(t3) -> None:
    t3.upsert_chunks_with_embeddings(
        _CLEAN, ids=_ids("a", len(_TEXTS)), documents=_TEXTS, embeddings=[],
        metadatas=[{"content_type": "prose", "title": "t"}] * len(_TEXTS),
    )


def _seed_drift(t3) -> None:
    """Every chunk's vector is the real embedding of the NEXT text, stored
    through the same-model passthrough (``upsert_chunks(embeddings=...)``,
    nexus-hxry2); ``upsert_chunks_with_embeddings`` would discard it and
    embed server-side. Seeds the clean collection first, because the embed
    route resolves the model from a registered collection.
    """
    _seed_clean(t3)
    shifted = _TEXTS[1:] + _TEXTS[:1]
    wrong = t3.embed_for_collection(_CLEAN, shifted)
    t3.upsert_chunks(
        _DRIFT, ids=_ids("b", len(_TEXTS)), documents=_TEXTS, embeddings=wrong,
        metadatas=[{"content_type": "prose", "title": "t"}] * len(_TEXTS),
    )


# ── pure parts ────────────────────────────────────────────────────────────────


def test_window_offsets_whole_collection_when_small() -> None:
    assert window_offsets(5, 20, random.Random(1)) == [(0, 5)]
    assert window_offsets(0, 20, random.Random(1)) == []


def test_window_offsets_spread_and_in_range() -> None:
    windows = window_offsets(10_000, 20, random.Random(7))
    assert len(windows) == 4
    assert len({o for o, _ in windows}) == 4
    assert all(0 <= o and o + n <= 10_000 for o, n in windows)
    assert sum(n for _, n in windows) >= 20


def test_window_offsets_is_deterministic_per_seed() -> None:
    assert window_offsets(5000, 20, random.Random(3)) == window_offsets(5000, 20, random.Random(3))


def test_default_seed_is_the_utc_date() -> None:
    assert default_seed(datetime(2026, 9, 25, 23, 0, tzinfo=UTC)) == 20260925


def test_report_is_not_clean_when_nothing_was_compared() -> None:
    lines, ok = format_report([CollectionDrift(collection="c", size=3)], sample=20, seed=1)
    assert not ok
    assert any("not a clean result" in line for line in lines)


def test_report_names_unprobed_collections() -> None:
    probed = CollectionDrift(collection="good", size=2, cosines={"x": 0.9999})
    failed = CollectionDrift(collection="bad", size=2, error="VectorServiceError: 503")
    lines, ok = format_report([probed, failed], sample=20, seed=1)
    assert not ok
    assert any("bad: NOT PROBED" in line for line in lines)


def test_a_nan_cosine_is_below_the_floor_not_a_pass() -> None:
    r = CollectionDrift(collection="c", size=2, cosines={"n" * 64: float("nan"), "y" * 64: 0.9999})
    assert [cid for cid, _ in r.below_floor] == ["n" * 64]
    _, ok = format_report([r], sample=20, seed=1)
    assert not ok


def test_cosine_of_a_zero_vector_is_nan() -> None:
    assert _cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert _cosine([0.0, 0.0], [1.0, 0.0]) != _cosine([0.0, 0.0], [1.0, 0.0])


def test_report_flags_rows_below_the_floor() -> None:
    r = CollectionDrift(collection="c", size=2, cosines={"x" * 64: 0.45, "y" * 64: 0.9999})
    lines, ok = format_report([r], sample=20, seed=1)
    assert not ok
    assert "1/2 below floor" in lines[1] and "xxxxxxxxxxxx 0.450" in lines[1]


# ── real engine substrate ─────────────────────────────────────────────────────


def test_engine_written_chunks_clear_the_floor(t2_service_env) -> None:
    t3 = make_t3()
    _seed_clean(t3)

    r = probe_collection(t3, _CLEAN, len(_TEXTS), 20, random.Random(1))

    assert r.error is None, r.error
    assert len(r.cosines) == len(_TEXTS)
    assert min(r.cosines.values()) >= COSINE_FLOOR, r.cosines


def test_a_vector_of_other_text_is_below_the_floor(t2_service_env) -> None:
    t3 = make_t3()
    _seed_drift(t3)

    r = probe_collection(t3, _DRIFT, len(_TEXTS), 20, random.Random(1))

    assert r.error is None, r.error
    assert len(r.below_floor) == len(_TEXTS), r.cosines


def test_cli_exit_codes(t2_service_env) -> None:
    t3 = make_t3()
    _seed_drift(t3)
    runner = CliRunner()

    clean = runner.invoke(main, ["doctor", "--check-embeddings", "--embeddings-collection", _CLEAN])
    assert clean.exit_code == 0, clean.output
    assert "4 chunk(s) sampled in 1 collection(s)" in clean.output

    drift = runner.invoke(main, ["doctor", "--check-embeddings", "--embeddings-collection", _DRIFT])
    assert drift.exit_code == 1, drift.output
    assert f"{_DRIFT}: 4/4 below floor" in drift.output

    unknown = runner.invoke(
        main, ["doctor", "--check-embeddings", "--embeddings-collection", "knowledge__nope__bge-base-en-v15-768__v1"],
    )
    assert unknown.exit_code == 1, unknown.output
    assert "no such collection" in unknown.output


def test_json_is_refused_with_check_embeddings() -> None:
    result = CliRunner().invoke(main, ["doctor", "--check-embeddings", "--json"])
    assert result.exit_code != 0
    assert "--check-embeddings" in result.output


@pytest.mark.parametrize("sample", [0, 301])
def test_sample_size_is_bounded(sample: int) -> None:
    result = CliRunner().invoke(main, ["doctor", "--check-embeddings", "--embeddings-sample", str(sample)])
    assert result.exit_code == 2, result.output
