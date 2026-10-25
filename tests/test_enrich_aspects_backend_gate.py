# SPDX-License-Identifier: AGPL-3.0-or-later
"""`nx enrich aspects` up-front checks that need no catalog and no T2 tenant (7.86.1 shakeout).

The run stops before extracting when no LLM backend can run, including an explicit
`NX_LLM_BACKEND=cli` with no `claude` on PATH; `--dry-run` is not gated; the cost line
says what its figure is; the confidence floor printed is the engine's. Entry
selection is stubbed: these checks run before any entry is read. Kept out of
tests/test_enrich_aspects.py, whose tests each provision a T2 tenant.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.commands.enrich import _ASPECT_CONFIDENCE_FLOOR, enrich
from nexus.operators.llm_backend import NO_BACKEND_MESSAGE, NoBackendAvailableError


@pytest.fixture(autouse=True)
def _llm_backend_is_the_cli(monkeypatch):
    """The box these run on must not decide the backend: the resolver answers "cli"
    and a claude is on PATH unless a test says otherwise."""
    monkeypatch.setattr("nexus.operators.llm_backend.resolve_backend_name", lambda req: "cli")
    monkeypatch.setattr("nexus.commands.enrich._claude_on_path", lambda: True)


@pytest.fixture
def two_entries(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Entry selection stubbed: the gate and the cost line run before any entry is read."""
    calls: list[str] = []
    monkeypatch.setattr("nexus.commands.enrich._select_entries", lambda **_kw: [object(), object()])
    monkeypatch.setattr(
        "nexus.commands.enrich._run_extraction",
        lambda entries, collection, config: calls.append("ran") or [],
    )
    monkeypatch.setattr("nexus.commands.enrich._dry_run_predict_skips", lambda *a, **k: None)
    return calls


def test_the_floor_matches_the_engines() -> None:
    """The number printed is the engine's, not a copy that can drift."""
    java = (
        Path(__file__).resolve().parents[1]
        / "service/src/main/java/dev/nexus/service/db/AspectRepository.java"
    ).read_text(encoding="utf-8")
    assert f"MIN_CONFIDENCE = {_ASPECT_CONFIDENCE_FLOOR};" in java

def test_no_llm_backend_stops_before_extracting_and_exits_nonzero(
    two_entries, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _no_backend(req):
        raise NoBackendAvailableError(NO_BACKEND_MESSAGE)

    monkeypatch.setattr("nexus.operators.llm_backend.resolve_backend_name", _no_backend)
    result = CliRunner().invoke(
        enrich, ["aspects", "knowledge__delos", "--validate-sample", "0"],
    )
    assert result.exit_code != 0, result.output
    assert NO_BACKEND_MESSAGE in result.output, result.output
    assert two_entries == [], "nothing may be dispatched when no backend can run"

def test_explicit_cli_backend_with_no_claude_on_path_is_refused(
    two_entries, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NX_LLM_BACKEND=cli resolves to "cli" whatever is on PATH, so the real
    resolver passed this gate and every document then failed at the spawn."""
    monkeypatch.setenv("NX_LLM_BACKEND", "cli")
    monkeypatch.setattr("nexus.commands.enrich._claude_on_path", lambda: False)
    result = CliRunner().invoke(
        enrich, ["aspects", "knowledge__delos", "--validate-sample", "0"],
    )
    assert result.exit_code != 0, result.output
    assert NO_BACKEND_MESSAGE in result.output, result.output
    assert two_entries == []

def test_explicit_cli_backend_with_claude_on_path_passes_the_gate(
    two_entries, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive control for the refusal above: the same setup with a claude on PATH runs."""
    monkeypatch.setenv("NX_LLM_BACKEND", "cli")
    monkeypatch.setattr("nexus.commands.enrich._claude_on_path", lambda: True)
    result = CliRunner().invoke(
        enrich, ["aspects", "knowledge__delos", "--validate-sample", "0"],
    )
    assert NO_BACKEND_MESSAGE not in result.output, result.output
    assert two_entries == ["ran"]

def test_dry_run_is_not_gated_on_a_backend_and_names_its_cost_basis(
    two_entries, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--dry-run makes no LLM call, so a box with no backend can still ask
    what a run would cost; the cost line says what its figure is."""
    def _no_backend(req):
        raise NoBackendAvailableError(NO_BACKEND_MESSAGE)

    monkeypatch.setattr("nexus.operators.llm_backend.resolve_backend_name", _no_backend)
    result = CliRunner().invoke(enrich, ["aspects", "knowledge__delos", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "2 document(s) in 'knowledge__delos'" in result.output, result.output
    assert "typical for paper-sized documents" in result.output
    assert "mean of three measured papers, $0.13-0.31 each, 54k-109k prompt tokens" in result.output
    assert "far lower for short notes (one measured at $0.0035)" in result.output
    # $0.20 is a mean, not a ceiling: a claim of a bound is the defect this pins.
    assert "upper bound" not in result.output
