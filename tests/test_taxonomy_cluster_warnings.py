# SPDX-License-Identifier: AGPL-3.0-or-later
"""Observability fix (2026-07-15): the MiniBatchKMeans path emitted spurious
macOS-Accelerate FP RuntimeWarnings (divide by zero / overflow / invalid in
matmul) on provably clean unit-norm input — verified by a full norm census of
code__1-1 (28,164 x 1024: zero NaN/inf/zero-norm rows) taken while the
warnings fired and the resulting topics were valid. They read as data
corruption and cost a diagnosis detour.

_cluster now suppresses FP-state warnings ONLY when the input is provably
finite; genuinely non-finite input emits a loud
``clustering_nonfinite_embeddings`` structured event and then fails loudly
in sklearn's own NaN validation (fail-loud discipline — no silent clustering
of garbage).
"""
from __future__ import annotations

import numpy as np
import pytest

import nexus.db.t2.taxonomy_compute as tc


class _RecordingLog:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    def info(self, event: str, **kw) -> None:
        self.events.append(("info", event, kw))

    def warning(self, event: str, **kw) -> None:
        self.events.append(("warning", event, kw))

    def debug(self, event: str, **kw) -> None:
        self.events.append(("debug", event, kw))


@pytest.fixture
def rec_log(monkeypatch: pytest.MonkeyPatch) -> _RecordingLog:
    rec = _RecordingLog()
    monkeypatch.setattr(tc, "_log", rec)
    return rec


def _kmeans_sized_embeddings(nonfinite_rows: int = 0) -> np.ndarray:
    # Just over the threshold so _cluster takes the MiniBatchKMeans branch.
    n = tc.LARGE_COLLECTION_THRESHOLD + 1
    rng = np.random.default_rng(42)
    emb = rng.normal(size=(n, 8)).astype(np.float32)
    emb /= np.linalg.norm(emb, axis=1, keepdims=True)
    for i in range(nonfinite_rows):
        emb[i, 0] = np.nan
    return emb


def test_finite_input_clusters_without_nonfinite_event(rec_log: _RecordingLog) -> None:
    emb = _kmeans_sized_embeddings()

    labels, centroids = tc._cluster(emb, len(emb), "code__t__m__v1")

    assert labels.shape == (len(emb),)
    assert centroids.shape[0] >= 10  # k = max(10, ...)
    events = [e for _lvl, e, _kw in rec_log.events]
    assert "clustering_minibatch_kmeans" in events
    assert "clustering_nonfinite_embeddings" not in events


def test_finite_input_suppresses_fp_runtime_warnings(rec_log: _RecordingLog) -> None:
    # Critique Critical-2: the discriminating regression test for the actual
    # suppression. numpy FP-state warnings escalate to errors here, so if a
    # future refactor drops the np.errstate block, any spurious Accelerate
    # divide/overflow/invalid warning ABORTS the fit and this test fails.
    # (On platforms whose BLAS never emits the spurious warnings this is
    # vacuously green — the incident platform is macOS Accelerate, where the
    # pre-fix code demonstrably warned on this exact input shape.)
    import warnings

    emb = _kmeans_sized_embeddings()
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        labels, _centroids = tc._cluster(emb, len(emb), "code__t__m__v1")
    assert labels.shape == (len(emb),)


def test_nan_centroids_tripwire_fires_loud(rec_log: _RecordingLog, monkeypatch) -> None:
    # Degenerate-but-finite defense: if the suppressed fit ever produces NaN
    # centroids (a genuine 0/0 the errstate would otherwise hide), the loud
    # clustering_nan_centroids event must fire.
    class _NanKm:
        cluster_centers_ = np.full((10, 8), np.nan, dtype=np.float32)

        def __init__(self, **_kw):
            pass

        def fit_predict(self, X):
            return np.zeros(len(X), dtype=int)

    import sklearn.cluster as skc

    monkeypatch.setattr(skc, "MiniBatchKMeans", _NanKm)
    emb = _kmeans_sized_embeddings()
    tc._cluster(emb, len(emb), "code__t__m__v1")

    assert any(
        e == "clustering_nan_centroids" and lvl == "warning"
        for lvl, e, _kw in rec_log.events
    )


def test_nonfinite_input_emits_loud_event_then_fails_loud(rec_log: _RecordingLog) -> None:
    emb = _kmeans_sized_embeddings(nonfinite_rows=3)

    # sklearn's own input validation rejects NaN — the structured event must
    # fire FIRST so the operator sees row counts, then the failure propagates
    # (never silent clustering of garbage).
    with pytest.raises(ValueError):
        tc._cluster(emb, len(emb), "code__t__m__v1")

    warn = [
        (lvl, kw) for lvl, e, kw in rec_log.events
        if e == "clustering_nonfinite_embeddings"
    ]
    assert len(warn) == 1
    lvl, kw = warn[0]
    assert lvl == "warning"
    assert kw["nonfinite_rows"] == 3
    assert kw["collection"] == "code__t__m__v1"
