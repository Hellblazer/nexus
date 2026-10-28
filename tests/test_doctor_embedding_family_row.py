# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx doctor's "Embedding family" row (nexus-wypwq, RDR-210 Phase 3 Step 1, Technical Design 2).

An engine that cannot serve the profile it was told (``voyage`` with no key, ``bge`` with no bge files) boots
DEGRADED, not refused: it seeds the chosen profile, serves the family it can, and answers 422 for the other
family's collections. ``GET /version`` reports ``embedding_models``, the model tokens the engine serves. The row
reads the profile against that set and warns when the profile's family is absent. It stays green and
"not applicable" where it cannot tell: the engine is unreachable, or an older engine does not report the field.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.health import _check_embedding_family

_GET = "nexus.db.http_vector_client._get"
_LABEL = "Embedding family"
_BGE = "bge-base-en-v15-768"


def _row(version_body, *, local: bool = True, client_profile: str = "bge"):
    with (
        patch("nexus.config.is_local_mode", return_value=local),
        patch("nexus.config.local_embed_profile", return_value=client_profile),
    ):
        rows = _check_embedding_family(version_body)
    assert len(rows) == 1
    assert rows[0].label == _LABEL
    return rows[0]


def test_an_unreachable_engine_is_not_applicable_and_green() -> None:
    r = _row(None)
    assert r.ok is True and not r.warn and "not applicable" in r.detail


def test_a_probe_that_raises_is_not_applicable_and_green() -> None:
    with patch(_GET, side_effect=RuntimeError("boom")):
        (r,) = _check_embedding_family()
    assert r.ok is True and not r.warn and "not applicable" in r.detail


def test_an_engine_without_embedding_models_is_not_applicable_and_green() -> None:
    r = _row({"embedding_mode": "onnx-local", "embedding_profile": "bge"})
    assert r.ok is True and not r.warn
    assert "not applicable" in r.detail and "does not report" in r.detail


def test_a_malformed_embedding_models_warns_as_unreadable_never_as_served() -> None:
    for bad in ("bge-base-en-v15-768", {"bge": 1}, [1, 2], None):
        r = _row({"embedding_models": bad, "embedding_profile": "bge"})
        assert r.ok is False and r.warn is True, bad
        assert "could not be read" in r.detail


@pytest.mark.parametrize(
    ("profile", "models"),
    [
        ("bge", [_BGE]),
        ("bge", [_BGE, "voyage-model-a", "voyage-model-b"]),
        ("voyage", ["voyage-model-a", "voyage-model-b"]),
        ("voyage", [_BGE, "voyage-model-a", "voyage-model-b"]),
    ],
)
def test_a_served_profile_family_is_green_and_names_the_profile(profile, models) -> None:
    r = _row({"embedding_profile": profile, "embedding_models": models})
    assert r.ok is True and not r.warn
    assert profile in r.detail and "served" in r.detail


def test_a_voyage_profile_without_voyage_models_warns_and_points_at_the_key() -> None:
    r = _row({"embedding_profile": "voyage", "embedding_models": [_BGE]})
    assert r.ok is False and r.warn is True
    assert "voyage" in r.detail and "degraded" in r.detail
    assert any("voyage_api_key" in s for s in r.fix_suggestions)
    assert any("restart" in s for s in r.fix_suggestions)


def test_a_bge_profile_without_bge_models_warns_and_points_at_the_files() -> None:
    r = _row({"embedding_profile": "bge", "embedding_models": ["voyage-model-a", "voyage-model-b"]})
    assert r.ok is False and r.warn is True
    assert "bge" in r.detail and "degraded" in r.detail
    assert any("nx init" in s and "--service" not in s for s in r.fix_suggestions)


def test_an_engine_serving_no_model_at_all_warns() -> None:
    r = _row({"embedding_profile": "bge", "embedding_models": []})
    assert r.ok is False and r.warn is True


def test_the_engines_own_profile_decides_over_the_clients() -> None:
    """The engine seeded what it reports; the client's intent against it is the profile-vs-intent row's job."""
    r = _row({"embedding_profile": "voyage", "embedding_models": ["voyage-model-a"]}, client_profile="bge")
    assert r.ok is True and not r.warn


def test_an_engine_that_does_not_report_a_profile_is_read_against_the_clients_in_local_mode() -> None:
    body = {"embedding_models": ["voyage-model-a", "voyage-model-b"]}
    assert _row(body, client_profile="voyage").ok is True
    r = _row(body, client_profile="bge")
    assert r.ok is False and r.warn is True


def test_a_voyage_only_engine_that_reports_no_profile_is_not_read_against_the_clients_bge() -> None:
    """A pre-profile engine in voyage mode serves Voyage whatever this client intends: the client's
    profile is no evidence about what that engine was told, so the row cannot tell (never warn)."""
    body = {"embedding_mode": "voyage", "embedding_models": ["voyage-model-a", "voyage-model-b"]}
    r = _row(body, client_profile="bge")
    assert r.ok is True and not r.warn and "not applicable" in r.detail


def test_the_clients_profile_still_reads_an_engine_in_another_mode_that_reports_no_profile() -> None:
    body = {"embedding_mode": "onnx-local", "embedding_models": ["voyage-model-a", "voyage-model-b"]}
    r = _row(body, client_profile="bge")
    assert r.ok is False and r.warn is True


def test_the_probe_uses_a_short_timeout() -> None:
    """`_get` defaults to 30 s; `nx doctor` must not hang that long on one row."""
    seen: dict = {}

    def _fake_get(path, **kwargs):
        seen.update(kwargs)
        return {"embedding_profile": "bge", "embedding_models": [_BGE]}

    with patch(_GET, _fake_get):
        (r,) = _check_embedding_family()
    assert r.ok is True
    assert 0 < seen["timeout"] <= 5, seen


def test_a_hung_endpoint_reads_not_applicable_quickly(monkeypatch) -> None:
    """A real socket that accepts and never answers: the row gives up inside the probe budget."""
    import socket
    import threading
    import time

    import nexus.health as health

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    held: list = []
    threading.Thread(target=lambda: held.append(srv.accept()), daemon=True).start()
    monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
    monkeypatch.setenv("NX_SERVICE_PORT", str(port))
    monkeypatch.setenv("NX_SERVICE_TOKEN", "t")
    monkeypatch.setattr(health, "_EMBEDDING_FAMILY_PROBE_TIMEOUT_S", 0.5)
    start = time.monotonic()
    try:
        (r,) = _check_embedding_family()
    finally:
        srv.close()
        for conn, _ in held:
            conn.close()
    assert time.monotonic() - start < 5
    assert r.ok is True and not r.warn and "not applicable" in r.detail


def test_a_managed_engine_that_reports_no_profile_is_not_applicable() -> None:
    """The client's local profile says nothing about a managed engine, which has no bge files by design."""
    r = _row({"embedding_models": ["voyage-model-a", "voyage-model-b"]}, local=False, client_profile="bge")
    assert r.ok is True and not r.warn and "not applicable" in r.detail


def test_a_managed_dual_mode_engine_serving_both_families_is_green_and_says_so() -> None:
    """nexus-njn1a finding 10: the cloud /version reports embedding_mode "dual" and all four model tokens but no
    embedding_profile. Whichever profile the engine seeded, its family is served, so the row answers instead of
    reading "could not be determined" on every managed box."""
    body = {
        "embedding_mode": "dual",
        "embedding_models": [_BGE, "voyage-3", "voyage-code-3", "voyage-context-3"],
    }
    r = _row(body, local=False, client_profile="bge")
    assert r.ok is True and not r.warn
    assert "not applicable" not in r.detail
    assert "dual" in r.detail and "both" in r.detail and _BGE in r.detail


def test_a_dual_mode_engine_missing_one_family_is_not_called_served() -> None:
    """Both families must be in the served list for the profile-free answer: a degraded dual engine that lost
    its Voyage key lists bge only, and the row cannot say whether the seeded profile was served."""
    r = _row({"embedding_mode": "dual", "embedding_models": [_BGE]}, local=False, client_profile="bge")
    assert r.ok is True and not r.warn and "not applicable" in r.detail
    assert "both" not in r.detail


def test_the_not_applicable_detail_names_the_real_cause_on_a_managed_engine() -> None:
    """It used to say "the embedding profile could not be determined for this engine" on every managed box. The
    cause is that the engine's /version names no embedding_profile and a managed engine is never read against
    this client's local profile."""
    r = _row({"embedding_mode": "voyage", "embedding_models": ["voyage-3"]}, local=False, client_profile="bge")
    assert r.ok is True and not r.warn and "not applicable" in r.detail
    assert "embedding_profile" in r.detail
    assert "embedding_mode voyage" in r.detail
    assert "managed" in r.detail
    assert "could not be determined" not in r.detail


def test_an_unrecognised_profile_value_is_not_applicable_never_a_guess() -> None:
    r = _row({"embedding_profile": "minilm", "embedding_models": [_BGE]})
    assert r.ok is True and not r.warn and "not applicable" in r.detail


@pytest.mark.t2_tenant
def test_the_default_sweep_prints_the_row(monkeypatch) -> None:
    """The row is registered in the real `nx doctor` run: unregistering it from run_health_checks fails this."""
    calls = {"n": 0}

    def _fake_get(path, **kwargs):
        if path == "/version":
            calls["n"] += 1
            return {"embedding_profile": "voyage", "embedding_models": [_BGE], "embedding_mode": "onnx-local"}
        raise RuntimeError(f"unexpected {path}")

    monkeypatch.setattr(_GET, _fake_get)
    result = CliRunner().invoke(main, ["doctor"])
    assert calls["n"] >= 1, result.output
    assert _LABEL in result.output
    assert "degraded" in result.output
