# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``nx doctor`` rows for the LLM backend (nexus-2k66t.3 P2): which backend
runs the LLM steps and why, and whether the key a GUI-spawned server would need
is persisted. Neither row ever prints a key."""
from __future__ import annotations

import inspect
import os
from pathlib import Path

import pytest

from nexus import health
from tests._module_seam import setattr_in
from nexus.health import _check_llm_backend, _check_llm_key_persistence

KEY = "sk-ant-api03-DOCTORSECRET0123456789abcdef"


def _write_global_config(text: str) -> None:
    cfg_dir = Path(os.environ["NEXUS_CONFIG_DIR"])
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yml").write_text(text, encoding="utf-8")


@pytest.fixture
def no_claude(monkeypatch):
    setattr_in(monkeypatch, "nexus.operators.llm_backend", "shutil.which", lambda name: None)
    setattr_in(monkeypatch, "nexus.health", "shutil.which", lambda name: None)


@pytest.fixture
def claude(monkeypatch):
    setattr_in(monkeypatch, "nexus.operators.llm_backend", "shutil.which", lambda name: "/usr/bin/claude")
    setattr_in(monkeypatch, "nexus.health", "shutil.which", lambda name: "/usr/bin/claude")


class TestBackendRow:
    def test_cli_on_path(self, claude, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
        (row,) = _check_llm_backend()
        assert row.label == "LLM backend" and row.ok and not row.warn
        assert row.detail.startswith("cli: claude is on PATH") and KEY not in row.detail
        assert "need the Claude Code CLI" not in row.detail  # the CLI is there

    def test_key_only_names_the_api_and_the_tool_limit(self, no_claude, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
        (row,) = _check_llm_backend()
        assert row.ok and row.detail.startswith("anthropic:") and KEY not in row.detail
        assert "nx_enrich_beads" in row.detail and "nx_plan_audit" in row.detail

    def test_key_from_the_global_config_counts_and_is_not_printed(self, no_claude) -> None:
        _write_global_config("credentials:\n  anthropic_api_key: " + KEY + "\n")
        (row,) = _check_llm_backend()
        assert row.ok and row.detail.startswith("anthropic:") and KEY not in row.detail + "".join(row.fix_suggestions)

    def test_a_box_with_neither_is_not_applicable_not_a_warning(self, no_claude) -> None:
        # A virgin box (search and storage only) must stay clean: the fresh-install
        # bar is zero warnings, and "no LLM backend" is a supported state.
        (row,) = _check_llm_backend()
        assert row.ok and not row.warn and "not applicable" in row.detail
        assert "search and storage work" in row.detail

    def test_explicit_anthropic_without_a_key_warns_with_the_remedy(self, claude, monkeypatch) -> None:
        monkeypatch.setenv("NX_LLM_BACKEND", "anthropic")
        (row,) = _check_llm_backend()
        assert not row.ok and row.warn and not row.fatal
        assert "anthropic_api_key" in row.detail
        assert any("nx config set anthropic_api_key" in f for f in row.fix_suggestions)

    def test_an_unknown_setting_warns(self, claude, monkeypatch) -> None:
        monkeypatch.setenv("NX_LLM_BACKEND", "gemini")
        (row,) = _check_llm_backend()
        assert not row.ok and row.warn and "gemini" in row.detail

    def test_openai_compat_names_the_host_and_model_not_the_key(self, no_claude, monkeypatch) -> None:
        monkeypatch.setenv("NX_LLM_BACKEND", "openai_compat")
        monkeypatch.setenv("NX_LLM_OPENAI_BASE_URL", "http://gpu-box:8000")
        monkeypatch.setenv("NX_LLM_OPENAI_MODEL", "qwen2.5")
        monkeypatch.setenv("NX_LLM_OPENAI_API_KEY", "oa-secret-key-value")
        (row,) = _check_llm_backend()
        assert row.ok and "gpu-box:8000" in row.detail and "qwen2.5" in row.detail
        assert "oa-secret-key-value" not in row.detail

    def test_a_broken_probe_is_a_warning_not_a_crash(self, monkeypatch) -> None:
        def boom(*a, **k):
            raise RuntimeError("probe broke")

        monkeypatch.setattr("nexus.operators.llm_backend.backend_status", boom)
        (row,) = _check_llm_backend()
        assert not row.ok and row.warn and "probe broke" in row.detail


class TestKeyPersistenceRow:
    def test_env_only_key_that_serves_the_calls_warns_without_printing_it(self, no_claude, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
        (row,) = _check_llm_key_persistence()
        assert not row.ok and row.warn and not row.fatal
        assert "anthropic_api_key" in row.detail and "ANTHROPIC_API_KEY" in row.detail
        assert KEY not in row.detail + "".join(row.fix_suggestions)
        assert any(f.startswith('nx config set anthropic_api_key "$ANTHROPIC_API_KEY"') for f in row.fix_suggestions)

    def test_a_persisted_key_is_fine(self, no_claude, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
        _write_global_config("credentials:\n  anthropic_api_key: " + KEY + "\n")
        assert _check_llm_key_persistence() == []

    def test_an_env_key_the_cli_makes_irrelevant_is_not_nagged(self, claude, monkeypatch) -> None:
        # A Claude Code user who exports ANTHROPIC_API_KEY for other reasons.
        monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
        assert _check_llm_key_persistence() == []

    def test_no_key_no_row(self, no_claude) -> None:
        assert _check_llm_key_persistence() == []

    def test_forcing_the_api_makes_the_env_key_relevant_even_with_the_cli(self, claude, monkeypatch) -> None:
        monkeypatch.setenv("NX_LLM_BACKEND", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
        (row,) = _check_llm_key_persistence()
        assert row.warn


def test_both_rows_are_wired_into_run_health_checks() -> None:
    src = inspect.getsource(health.run_health_checks)
    assert "_check_llm_backend()" in src and "_check_llm_key_persistence()" in src
