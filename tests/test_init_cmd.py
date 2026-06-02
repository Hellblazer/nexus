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

        # repo_root=cfg_dir so the repo's own .nexus.yml does not bleed in.
        assert load_config(repo_root=cfg_dir)["local"]["embed_model"] == _TIER1_MODEL

    def test_no_warmup_when_extra_absent(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the [local] extra is not installed in-process, nx init must
        NOT attempt an in-process warmup (it cannot — fastembed is absent). It
        takes the extra-add / instruction path instead."""
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        monkeypatch.setattr("nexus.commands.init._local_extra_installed", lambda: False)
        monkeypatch.setattr("nexus.commands.init._uv_receipt_path", lambda: None)
        monkeypatch.setattr(
            "nexus.commands.init._warmup_bge",
            lambda: (_ for _ in ()).throw(
                AssertionError("must not warmup when extra absent")
            ),
        )

        result = CliRunner().invoke(init_cmd, ["--yes"])
        assert result.exit_code == 0, result.output


# ── P3: extra-add (A) + warmup pre-fetch (B) ──────────────────────────────────


class TestExtraAddAndWarmup:
    def test_bge_with_extra_present_warms_up(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fresh-local + fastembed available in-process → warmup-embed runs."""
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        monkeypatch.setattr("nexus.commands.init._local_extra_installed", lambda: True)
        called: list[list[str]] = []
        monkeypatch.setattr(
            "nexus.commands.init._warmup_bge", lambda: called.append(["warm"])
        )

        result = CliRunner().invoke(init_cmd, ["--yes"])

        assert result.exit_code == 0, result.output
        assert called == [["warm"]]

    def test_offline_warmup_is_graceful_not_crash(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Offline / cache-miss during warmup → actionable message, exit 0,
        never a crash or hang (CA-1 Refinement B)."""
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        monkeypatch.setattr("nexus.commands.init._local_extra_installed", lambda: True)

        # Real _warmup_bge, but the embed call raises (simulated offline).
        import nexus.db.local_ef as local_ef

        def _boom(self, *a, **kw):  # noqa: ANN001
            raise RuntimeError("offline: could not fetch model")

        monkeypatch.setattr(local_ef.LocalEmbeddingFunction, "__call__", _boom)

        result = CliRunner().invoke(init_cmd, ["--yes"])

        assert result.exit_code == 0, result.output
        out = result.output.lower()
        assert "could not" in out
        # the cache-path line is the ACTIONABLE part — assert it specifically,
        # not just the exception message text.
        assert "cache location:" in out

    def test_editable_tree_no_receipt_prints_manual_no_reinstall(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Editable/dev tree (no uv-receipt.toml) → manual instruction, and
        the reinstall subprocess is NEVER shelled (clobber-a-dev-tree guard)."""
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        monkeypatch.setattr("nexus.commands.init._local_extra_installed", lambda: False)
        monkeypatch.setattr("nexus.commands.init._uv_receipt_path", lambda: None)
        import subprocess

        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("must not shell reinstall on a dev tree")
            ),
        )

        result = CliRunner().invoke(init_cmd, ["--yes"])

        assert result.exit_code == 0, result.output
        assert "pip install" in result.output.lower() or "conexus[local]" in result.output

    def test_receipt_present_shells_reinstall(
        self, cfg_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """uv-tool install (receipt present) → shell the editable-safe
        reinstall adding [local]; no in-process warmup (new venv)."""
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        monkeypatch.setattr("nexus.commands.init._local_extra_installed", lambda: False)
        receipt = tmp_path / "uv-receipt.toml"
        receipt.write_text("[tool]\n")
        monkeypatch.setattr("nexus.commands.init._uv_receipt_path", lambda: receipt)
        calls: list[list[str]] = []
        import subprocess

        def _fake_run(cmd, *a, **k):  # noqa: ANN001
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(subprocess, "run", _fake_run)

        result = CliRunner().invoke(init_cmd, ["--yes"])

        assert result.exit_code == 0, result.output
        assert len(calls) == 1
        cmd = calls[0]
        assert "uv" in cmd[0] and "install" in cmd and "--reinstall" in cmd
        assert "--from" in cmd
        assert any("[local]" in part for part in cmd)

    def test_minilm_choice_does_not_fetch_or_install(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Choosing the bundled 384 model fetches/installs nothing."""
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        monkeypatch.setattr(
            "nexus.commands.init._warmup_bge",
            lambda: (_ for _ in ()).throw(AssertionError("no warmup for minilm")),
        )
        monkeypatch.setattr(
            "nexus.commands.init._ensure_local_extra",
            lambda: (_ for _ in ()).throw(AssertionError("no extra-add for minilm")),
        )

        result = CliRunner().invoke(init_cmd, ["--embedder", "minilm-384"])
        assert result.exit_code == 0, result.output
