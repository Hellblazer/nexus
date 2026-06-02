# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-144 P2: `nx init` guided onboarding verb (skeleton + detect + persist).

Phase 2 scope is intentionally narrow: detect cloud-vs-local, present the
local embedder choice (bge-768 recommended, one-time download cost stated,
minilm-384 as the explicit alternative), and persist the choice to
config.yml. NO model fetch and NO extra-add happen here — that is P3.

Cloud-path tests pin ``nexus.config.is_local_mode`` because CI runners lack
cloud credentials and ``is_local_mode()`` defaults to True there
(mem:feedback_pin_local_mode_in_cloud_tests).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from nexus.commands.init import init_cmd
from nexus.db.local_ef import _TIER0_MODEL, _TIER1_MODEL


def _read_config(cfg_dir: Path) -> dict:
    p = cfg_dir / "config.yml"
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text()) or {}


@pytest.fixture()
def cfg_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "cfg"
    d.mkdir()
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(d))
    return d


# ── cloud mode ────────────────────────────────────────────────────────────────


class TestCloudMode:
    def test_cloud_mode_provisions_nothing_local(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cloud mode: no embedder prompt, no local.embed_model written."""
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)

        result = CliRunner().invoke(init_cmd, [])

        assert result.exit_code == 0, result.output
        assert "cloud" in result.output.lower()
        assert "local" not in _read_config(cfg_dir)


# ── local mode ────────────────────────────────────────────────────────────────


class TestLocalMode:
    def test_local_recommends_bge_with_download_cost_stated(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Local mode states the one-time download cost up front and names
        bge-768 as recommended."""
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)

        result = CliRunner().invoke(init_cmd, ["--yes"])

        assert result.exit_code == 0, result.output
        out = result.output.lower()
        assert "bge" in out and "768" in out
        assert "recommend" in out
        # one-time download cost must be stated up front
        assert "download" in out and "mb" in out

    def test_default_choice_persists_bge_768(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--yes accepts the recommended bge-768 and writes it to config.yml."""
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)

        result = CliRunner().invoke(init_cmd, ["--yes"])

        assert result.exit_code == 0, result.output
        assert _read_config(cfg_dir)["local"]["embed_model"] == _TIER1_MODEL

    def test_explicit_minilm_persists_384(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--embedder minilm-384 records the explicit lower-quality choice."""
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)

        result = CliRunner().invoke(init_cmd, ["--embedder", "minilm-384"])

        assert result.exit_code == 0, result.output
        assert _read_config(cfg_dir)["local"]["embed_model"] == _TIER0_MODEL

    def test_explicit_bge_flag_persists_768(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)

        result = CliRunner().invoke(init_cmd, ["--embedder", "bge-768"])

        assert result.exit_code == 0, result.output
        assert _read_config(cfg_dir)["local"]["embed_model"] == _TIER1_MODEL

    def test_interactive_prompt_accepts_choice(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Interactive (no flag) prompt records the typed choice."""
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)

        result = CliRunner().invoke(init_cmd, [], input="minilm-384\n")

        assert result.exit_code == 0, result.output
        assert _read_config(cfg_dir)["local"]["embed_model"] == _TIER0_MODEL

    def test_choice_round_trips_through_config_reader(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The persisted choice is readable back through the config layer."""
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        CliRunner().invoke(init_cmd, ["--yes"])

        from nexus.config import load_config

        assert load_config()["local"]["embed_model"] == _TIER1_MODEL

    def test_no_model_fetch_in_p2(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """P2 scope guard: nx init must NOT construct a fastembed model or
        create the cache dir — fetch/extra-add is P3. Make both fatal."""
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        import nexus.db.local_ef as local_ef

        monkeypatch.setattr(
            local_ef.LocalEmbeddingFunction,
            "_init_ef",
            lambda self: (_ for _ in ()).throw(
                AssertionError("nx init must not construct an EF in P2")
            ),
        )

        result = CliRunner().invoke(init_cmd, ["--yes"])
        assert result.exit_code == 0, result.output
