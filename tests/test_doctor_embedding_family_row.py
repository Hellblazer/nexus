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
        ("bge", [_BGE, "voyage-code-3", "voyage-context-3"]),
        ("voyage", ["voyage-code-3", "voyage-context-3"]),
        ("voyage", [_BGE, "voyage-code-3", "voyage-context-3"]),
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
    r = _row({"embedding_profile": "bge", "embedding_models": ["voyage-code-3", "voyage-context-3"]})
    assert r.ok is False and r.warn is True
    assert "bge" in r.detail and "degraded" in r.detail
    assert any("nx init --service" in s for s in r.fix_suggestions)


def test_an_engine_serving_no_model_at_all_warns() -> None:
    r = _row({"embedding_profile": "bge", "embedding_models": []})
    assert r.ok is False and r.warn is True


def test_the_engines_own_profile_decides_over_the_clients() -> None:
    """The engine seeded what it reports; the client's intent against it is the profile-vs-intent row's job."""
    r = _row({"embedding_profile": "voyage", "embedding_models": ["voyage-code-3"]}, client_profile="bge")
    assert r.ok is True and not r.warn


def test_an_engine_that_does_not_report_a_profile_is_read_against_the_clients_in_local_mode() -> None:
    body = {"embedding_models": ["voyage-code-3", "voyage-context-3"]}
    assert _row(body, client_profile="voyage").ok is True
    r = _row(body, client_profile="bge")
    assert r.ok is False and r.warn is True


def test_a_managed_engine_that_reports_no_profile_is_not_applicable() -> None:
    """The client's local profile says nothing about a managed engine, which has no bge files by design."""
    r = _row({"embedding_models": ["voyage-code-3", "voyage-context-3"]}, local=False, client_profile="bge")
    assert r.ok is True and not r.warn and "not applicable" in r.detail


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
