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

import os

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


@pytest.fixture(autouse=True)
def _no_real_stale_migration(monkeypatch: pytest.MonkeyPatch) -> None:
    """RDR-144 P4: the bge ``--yes`` path now calls ``_offer_stale_migration``,
    which touches the real local T3 store and (with ``--yes``) would AUTO-
    MIGRATE real collections. Default every test to a no-op offer; the
    dedicated P4 wiring tests override this with a controlled stub.
    """
    monkeypatch.setattr(
        "nexus.commands.init._offer_stale_migration", lambda assume_yes: None
    )


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

    def test_install_failure_prints_manual_fallback(
        self, cfg_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-zero uv exit (or timeout) during extra-add must be caught and
        converted to a manual-install fallback, never a raw traceback."""
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        monkeypatch.setattr("nexus.commands.init._local_extra_installed", lambda: False)
        receipt = tmp_path / "uv-receipt.toml"
        receipt.write_text("[tool]\n")
        monkeypatch.setattr("nexus.commands.init._uv_receipt_path", lambda: receipt)
        import subprocess

        def _boom(cmd, *a, **k):  # noqa: ANN001
            raise subprocess.CalledProcessError(1, cmd)

        monkeypatch.setattr(subprocess, "run", _boom)

        result = CliRunner().invoke(init_cmd, ["--yes"])

        assert result.exit_code == 0, result.output
        out = result.output.lower()
        assert "failed to install" in out
        assert "manually" in out

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


# ── P4: existing-384 detection + safe migration offer ─────────────────────────


from nexus.commands.init import _offer_stale_migration as _real_offer  # noqa: E402
from nexus.db.embed_migrate import MigrationOutcome, StaleCollection  # noqa: E402


def _reindexable(name: str) -> StaleCollection:
    return StaleCollection(
        name=name,
        count=4,
        source_paths=frozenset({"doc.md"}),
        sourceless=0,
        target_name=name.replace("minilm-l6-v2-384", "bge-base-en-v15-768"),
        kind="reindexable",
    )


class TestStaleMigrationWiring:
    def test_bge_yes_invokes_offer_with_assume_yes(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The bge ``--yes`` path reaches the migration offer, forwarding the
        ``--yes`` flag so the destructive confirms are auto-accepted."""
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        monkeypatch.setattr("nexus.commands.init._local_extra_installed", lambda: True)
        monkeypatch.setattr("nexus.commands.init._warmup_bge", lambda: None)
        seen: list[bool] = []
        monkeypatch.setattr(
            "nexus.commands.init._offer_stale_migration",
            lambda assume_yes: seen.append(assume_yes),
        )

        result = CliRunner().invoke(init_cmd, ["--yes"])

        assert result.exit_code == 0, result.output
        assert seen == [True]


class TestStaleMigrationOffer:
    """Direct tests of the real ``_offer_stale_migration`` (autouse stub is
    overridden by calling the module-level real reference)."""

    def _wire(self, monkeypatch, stale, migrate_spy):
        monkeypatch.setattr("nexus.commands.store._t3", lambda: object())
        monkeypatch.setattr(
            "nexus.db.embed_migrate.detect_stale_local_collections",
            lambda db, *, active_dim, active_token: stale,
        )
        monkeypatch.setattr(
            "nexus.db.embed_migrate.migrate_collection_safe", migrate_spy
        )

    def test_no_stale_is_silent_no_migrate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list = []
        self._wire(monkeypatch, [], lambda *a, **k: calls.append(a))

        runner = CliRunner()
        result = runner.invoke(_wrap(lambda: _real_offer(True)))

        assert result.exit_code == 0, result.output
        assert calls == []
        assert result.output.strip() == ""

    def test_reindexable_migrated_under_assume_yes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stale = [_reindexable("docs__proj__minilm-l6-v2-384__v1")]
        calls: list = []

        def _spy(db, s, *, dry_run, **k):
            calls.append((s.name, dry_run))
            return MigrationOutcome(
                name=s.name, target_name=s.target_name, status="migrated",
                before=4, after=4, reason="ok",
            )

        self._wire(monkeypatch, stale, _spy)
        result = CliRunner().invoke(_wrap(lambda: _real_offer(True)))

        assert result.exit_code == 0, result.output
        # migration actually ran, NOT a dry-run
        assert calls == [("docs__proj__minilm-l6-v2-384__v1", False)]
        assert "old collection removed" in result.output.lower() or "done" in result.output.lower()

    def test_code_collection_reported_never_migrated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stale = [
            StaleCollection(
                name="code__proj__minilm-l6-v2-384__v1", count=9,
                source_paths=frozenset({"a.py"}), sourceless=0,
                target_name="code__proj__bge-base-en-v15-768__v1", kind="code",
            )
        ]
        calls: list = []
        self._wire(monkeypatch, stale, lambda *a, **k: calls.append(a))

        result = CliRunner().invoke(_wrap(lambda: _real_offer(True)))

        assert result.exit_code == 0, result.output
        assert calls == []  # never auto-migrated
        assert "nx index repo" in result.output

    def test_double_confirm_decline_skips_migration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stale = [_reindexable("docs__proj__minilm-l6-v2-384__v1")]
        calls: list = []
        self._wire(monkeypatch, stale, lambda *a, **k: calls.append(a))

        # assume_yes=False -> first confirm prompts; "n" declines.
        result = CliRunner().invoke(_wrap(lambda: _real_offer(False)), input="n\n")

        assert result.exit_code == 0, result.output
        assert calls == []
        assert "skipped" in result.output.lower()

    def test_second_confirm_decline_skips_migration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Accept the first confirm, decline the second (the load-bearing
        delete gate) -> no migration."""
        stale = [_reindexable("docs__proj__minilm-l6-v2-384__v1")]
        calls: list = []
        self._wire(monkeypatch, stale, lambda *a, **k: calls.append(a))

        # "y" accepts first confirm, "n" declines the delete gate.
        result = CliRunner().invoke(_wrap(lambda: _real_offer(False)), input="y\nn\n")

        assert result.exit_code == 0, result.output
        assert calls == []
        assert "skipped" in result.output.lower()

    def _mixed(self, name: str, sourceless: int = 2) -> StaleCollection:
        return StaleCollection(
            name=name, count=5, source_paths=frozenset({"doc.md"}),
            sourceless=sourceless,
            target_name=name.replace("minilm-l6-v2-384", "bge-base-en-v15-768"),
            kind="reindexable",
        )

    def test_mixed_collection_deferred_under_assume_yes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A mixed (file + manual-note) collection is NEVER auto-migrated
        under --yes — we cannot auto-confirm a lossy delete."""
        stale = [self._mixed("knowledge__notes__minilm-l6-v2-384__v1")]
        calls: list = []
        self._wire(monkeypatch, stale, lambda *a, **k: calls.append(a))

        result = CliRunner().invoke(_wrap(lambda: _real_offer(True)))

        assert result.exit_code == 0, result.output
        assert calls == []  # not migrated
        out = result.output.lower()
        assert "cannot be re-embedded" in out or "skipped" in out

    def test_mixed_collection_migrates_after_explicit_loss_confirm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Interactive: confirming the note-loss prompt migrates the mixed
        collection with allow_sourceless_loss=True."""
        stale = [self._mixed("knowledge__notes__minilm-l6-v2-384__v1")]
        seen: list = []

        def _spy(db, s, *, dry_run, allow_sourceless_loss=False, **k):
            seen.append((s.name, allow_sourceless_loss))
            return MigrationOutcome(
                name=s.name, target_name=s.target_name, status="migrated",
                before=5, after=2, reason="ok",
            )

        self._wire(monkeypatch, stale, _spy)
        # "y" accepts the single note-loss confirmation.
        result = CliRunner().invoke(_wrap(lambda: _real_offer(False)), input="y\n")

        assert result.exit_code == 0, result.output
        assert seen == [("knowledge__notes__minilm-l6-v2-384__v1", True)]


# ── P5: --service / Postgres provisioning flag ────────────────────────────────


class TestServiceProvisioningFlag:
    """Unit tests for the ``--service`` flag wiring in init_cmd.

    These are pure unit tests — they stub out the provisioner so no real
    Postgres cluster is needed.  The full integration evidence lives in
    tests/db/test_pg_provision.py.
    """

    def _stub_provision(self, monkeypatch, *, already_provisioned: bool = False, fail: bool = False):
        """Patch nexus.db.pg_provision.provision with a stub."""
        from nexus.db.pg_provision import ProvisionResult

        stub_result = ProvisionResult(
            cluster_created=not already_provisioned,
            db_created=not already_provisioned,
            admin_role_created=not already_provisioned,
            svc_role_created=not already_provisioned,
            already_provisioned=already_provisioned,
            port=15432,
        )

        def _fake_provision(config_dir=None, **kw):
            if fail:
                from nexus.db.pg_provision import PgBinaryNotFoundError
                raise PgBinaryNotFoundError("No binaries. Install postgresql@16.")
            return stub_result

        monkeypatch.setattr("nexus.commands.init._provision_postgres_step",
                            lambda: _fake_provision())

        return stub_result

    def test_service_flag_triggers_provisioning(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--service`` triggers _provision_postgres_step."""
        called: list[str] = []
        monkeypatch.setattr(
            "nexus.commands.init._provision_postgres_step",
            lambda: called.append("called"),
        )
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)
        # Defensive: cloud mode returns before the start step, but stub it so a
        # future local-mode flip can't reach the real service starter.
        monkeypatch.setattr("nexus.commands.init._start_service_step", lambda: None)

        result = CliRunner().invoke(init_cmd, ["--service"])

        assert result.exit_code == 0, result.output
        assert called == ["called"], "provisioning step must be called with --service"

    def test_service_flag_not_triggered_without_flag(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without ``--service``, provisioning is NOT triggered."""
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        monkeypatch.setattr("nexus.commands.init._local_extra_installed", lambda: True)
        monkeypatch.setattr("nexus.commands.init._warmup_bge", lambda: None)
        called: list[str] = []
        monkeypatch.setattr(
            "nexus.commands.init._provision_postgres_step",
            lambda: called.append("called"),
        )

        result = CliRunner().invoke(init_cmd, ["--yes"])

        assert result.exit_code == 0, result.output
        assert called == [], "provisioning step must NOT be called without --service"

    def test_service_flag_auto_triggered_by_nx_storage_backend_env(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When ``NX_STORAGE_BACKEND=service`` is set, provisioning auto-triggers."""
        monkeypatch.setenv("NX_STORAGE_BACKEND", "service")
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)
        monkeypatch.setattr("nexus.commands.init._start_service_step", lambda: None)
        called: list[str] = []
        monkeypatch.setattr(
            "nexus.commands.init._provision_postgres_step",
            lambda: called.append("called"),
        )

        result = CliRunner().invoke(init_cmd, [])

        assert result.exit_code == 0, result.output
        assert called == ["called"], "auto-provisioning must fire when NX_STORAGE_BACKEND=service"

    def test_service_flag_no_auto_trigger_without_env(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without NX_STORAGE_BACKEND=service, no auto-trigger."""
        monkeypatch.delenv("NX_STORAGE_BACKEND", raising=False)
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        monkeypatch.setattr("nexus.commands.init._local_extra_installed", lambda: True)
        monkeypatch.setattr("nexus.commands.init._warmup_bge", lambda: None)
        called: list[str] = []
        monkeypatch.setattr(
            "nexus.commands.init._provision_postgres_step",
            lambda: called.append("called"),
        )

        result = CliRunner().invoke(init_cmd, ["--yes"])

        assert result.exit_code == 0, result.output
        assert called == [], "provisioning must NOT auto-trigger without env var"


def _wrap(fn):
    """Wrap a bare callable in a trivial Click command so CliRunner can drive
    its ``click.echo`` / ``click.confirm`` I/O."""
    import click as _click

    @_click.command()
    def _cmd() -> None:
        fn()

    return _cmd


# ── RDR-157 P3.4: first-run bundled-PG selection (bead nexus-vwvv5.13) ──────────


def test_select_bundled_pg_sets_env_from_bundle(tmp_path, monkeypatch, make_pg_bundle_txz) -> None:
    from nexus.commands.init import _select_bundled_pg
    from nexus.db import pg_bundle

    monkeypatch.delenv("NEXUS_PG_BIN", raising=False)
    archive = make_pg_bundle_txz(tmp_path, "nexus-pg-x.txz")
    monkeypatch.setenv(pg_bundle.BUNDLE_ENV, str(archive))
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()

    bin_dir = _select_bundled_pg(config_dir)

    assert bin_dir is not None
    assert os.environ["NEXUS_PG_BIN"] == str(bin_dir)
    assert str(bin_dir).startswith(str(config_dir))


def test_select_bundled_pg_none_without_bundle(tmp_path, monkeypatch) -> None:
    from nexus.commands.init import _select_bundled_pg
    from nexus.db import pg_bundle

    monkeypatch.delenv("NEXUS_PG_BIN", raising=False)
    monkeypatch.delenv(pg_bundle.BUNDLE_ENV, raising=False)
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()

    # Deterministic: explicit empty search dir (no reliance on what sits next to
    # the test interpreter).
    result = _select_bundled_pg(config_dir, search_dirs=[tmp_path / "empty"])
    assert result is None
    assert not os.environ.get("NEXUS_PG_BIN")


def test_select_bundled_pg_defaults_to_service_dir(tmp_path, monkeypatch, make_pg_bundle_txz) -> None:
    """RF-161-3 (nexus-06y9e): with no env override and no injected search_dirs,
    the default search dir must be <config_dir>/service/ — where the P2 acquire
    seam places the .txz — NOT the venv bin/. Before the fix _select_bundled_pg
    passed search_dirs=None straight through, so ensure_pg_bundle defaulted to
    [Path(sys.executable).parent] and a correctly-acquired bundle was never
    found (silent host-PG fallback)."""
    from nexus.commands.init import _select_bundled_pg
    from nexus.db import pg_bundle
    from nexus.db.pg_bundle import current_platform_tag

    monkeypatch.delenv("NEXUS_PG_BIN", raising=False)
    monkeypatch.delenv(pg_bundle.BUNDLE_ENV, raising=False)
    config_dir = tmp_path / "cfg"
    service_dir = config_dir / "service"
    service_dir.mkdir(parents=True)
    make_pg_bundle_txz(service_dir, f"nexus-pg-{current_platform_tag()}.txz")

    # No search_dirs, no env: must still find the bundle under <config_dir>/service.
    bin_dir = _select_bundled_pg(config_dir)
    assert bin_dir is not None, "bundle in <config_dir>/service/ must be found"
    assert os.environ["NEXUS_PG_BIN"] == str(bin_dir)
    assert str(bin_dir).startswith(str(config_dir))


def test_select_bundled_pg_respects_existing_env_override(tmp_path, monkeypatch, make_pg_bundle_txz) -> None:
    from nexus.commands.init import _select_bundled_pg
    from nexus.db import pg_bundle

    # Operator already pointed NEXUS_PG_BIN somewhere -> bundle not consulted.
    monkeypatch.setenv("NEXUS_PG_BIN", "/opt/my/pg/bin")
    archive = make_pg_bundle_txz(tmp_path, "nexus-pg-y.txz")
    monkeypatch.setenv(pg_bundle.BUNDLE_ENV, str(archive))
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()

    assert _select_bundled_pg(config_dir) is None
    assert os.environ["NEXUS_PG_BIN"] == "/opt/my/pg/bin"


def test_provision_step_aborts_loud_on_broken_bundle(tmp_path, monkeypatch) -> None:
    """M2: a set-but-missing NEXUS_PG_BUNDLE must SystemExit(1), never reach provision."""
    import nexus.commands.init as init_mod
    from nexus.db import pg_bundle

    monkeypatch.delenv("NEXUS_PG_BIN", raising=False)
    monkeypatch.setenv(pg_bundle.BUNDLE_ENV, str(tmp_path / "does-not-exist.txz"))
    monkeypatch.setattr(init_mod._config, "nexus_config_dir", lambda: tmp_path / "cfg")

    called = {"provision": False}

    def _fail_if_called(*a, **k):  # provision must NOT run when the bundle is broken
        called["provision"] = True
        raise AssertionError("provision() must not be reached on a broken bundle")

    monkeypatch.setattr("nexus.db.pg_provision.provision", _fail_if_called)

    with pytest.raises(SystemExit) as exc:
        init_mod._provision_postgres_step()
    assert exc.value.code == 1
    assert called["provision"] is False


def test_provision_step_idempotent_on_rerun(tmp_path, monkeypatch, make_pg_bundle_txz) -> None:
    """Composed re-run idempotency (RDR-157 §Approach P3 charge #1): running
    _provision_postgres_step twice extracts the ship-alongside bundle EXACTLY
    once (marker honored) and provisions each time, reporting already-provisioned
    on the rerun. The components are individually idempotent; this locks the
    composition (NEXUS_PG_BIN set on the first call must not force a re-extract)."""
    from types import SimpleNamespace

    import nexus.commands.init as init_mod
    from nexus.db import pg_bundle

    monkeypatch.delenv("NEXUS_PG_BIN", raising=False)
    archive = make_pg_bundle_txz(tmp_path, "nexus-pg-rerun.txz")
    monkeypatch.setenv(pg_bundle.BUNDLE_ENV, str(archive))
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    monkeypatch.setattr(init_mod._config, "nexus_config_dir", lambda: config_dir)

    extractions = {"n": 0}
    real_extract = pg_bundle.extract_bundle

    def _counting_extract(*a, **k):
        extractions["n"] += 1
        return real_extract(*a, **k)

    monkeypatch.setattr(pg_bundle, "extract_bundle", _counting_extract)

    provisions = {"n": 0}

    def _fake_provision(_cfg):
        provisions["n"] += 1
        return SimpleNamespace(already_provisioned=True, port=15432)

    monkeypatch.setattr("nexus.db.pg_provision.provision", _fake_provision)

    init_mod._provision_postgres_step()
    init_mod._provision_postgres_step()

    assert extractions["n"] == 1, "bundle re-extracted on rerun (marker not honored)"
    assert provisions["n"] == 2, "provision must run on each invocation"


class TestServiceLocalEmbedder:
    """RDR-160 P3.1/P3.2: a local --service install locks bge-768 and fetches
    the STANDARD ONNX the Java service reads. minilm-384 is non-operative here."""

    def _patch_common(self, monkeypatch):
        # No real Postgres, local mode, and a recording stub for the bge fetch.
        monkeypatch.setattr("nexus.commands.init._provision_postgres_step", lambda: None)
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        # RDR-157 P4.1: init --service now also starts the service. Stub it here
        # so the embedder-focused tests stay hermetic (start coverage is in
        # TestServiceStartStep below).
        monkeypatch.setattr("nexus.commands.init._start_service_step", lambda: None)
        # RDR-161 P1: init now gates start on a resolvable native binary. These
        # tests cover the embedder path, not binary acquisition (see
        # tests/test_init_service_binary.py), so report the binary as ready.
        monkeypatch.setattr(
            "nexus.commands.init._ensure_service_binary_step", lambda cd: True
        )
        calls: list[str] = []
        monkeypatch.setattr(
            "nexus.db.service_bge_model.fetch_service_bge_onnx",
            lambda **kw: (calls.append("fetch"), Path("/fake/onnx"))[1],
        )
        return calls

    def test_service_local_locks_bge_and_fetches(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = self._patch_common(monkeypatch)
        result = CliRunner().invoke(init_cmd, ["--service"])
        assert result.exit_code == 0, result.output
        assert calls == ["fetch"], "standard bge ONNX must be fetched for --service"
        assert _read_config(cfg_dir)["local"]["embed_model"] == _TIER1_MODEL
        assert "bge-768 only" in result.output
        # the interactive non-service prompt must NOT run
        assert "choose your on-device" not in result.output.lower()

    def test_service_local_minilm_gets_advisory_and_locks_bge(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = self._patch_common(monkeypatch)
        result = CliRunner().invoke(init_cmd, ["--service", "--embedder", "minilm-384"])
        assert result.exit_code == 0, result.output
        assert "non-operative" in result.output
        # locked to bge despite the minilm-384 request (no silent ignore)
        assert _read_config(cfg_dir)["local"]["embed_model"] == _TIER1_MODEL
        assert calls == ["fetch"]

    def test_service_local_fetch_offline_is_loud_and_fatal(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The Java service cannot boot without the model, so a fetch failure must
        # be FATAL (nonzero exit) — not a swallowed warning that makes the install
        # look successful. Re-run when online (idempotent).
        monkeypatch.setattr("nexus.commands.init._provision_postgres_step", lambda: None)
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)

        def _boom(**kw):
            raise RuntimeError("offline — the service will not boot without /x/model.onnx")

        monkeypatch.setattr("nexus.db.service_bge_model.fetch_service_bge_onnx", _boom)
        result = CliRunner().invoke(init_cmd, ["--service"])
        assert result.exit_code != 0  # fail-loud + fatal
        assert "will not boot" in result.output


class TestServiceStartStep:
    """RDR-157 P4.1 (nexus-vwvv5.17): nx init --service collapses through to a
    started, status-green service. _start_service_step calls the idempotent
    start_storage_service and fails loud (never a traceback) on any error."""

    def test_start_step_reports_running_endpoint(self, monkeypatch) -> None:
        import nexus.commands.init as init_mod

        monkeypatch.setattr(
            "nexus.daemon.storage_service_daemon.start_storage_service",
            lambda: {"host": "127.0.0.1", "port": 18099, "pid": 4242, "generation": 3},
        )
        runner_out: list[str] = []
        monkeypatch.setattr(init_mod.click, "echo", lambda *a, **k: runner_out.append(a[0] if a else ""))

        init_mod._start_service_step()

        joined = "\n".join(runner_out)
        assert "127.0.0.1:18099" in joined
        assert "serving" in joined.lower()

    def test_start_step_fails_loud_on_start_error(self, monkeypatch) -> None:
        import nexus.commands.init as init_mod
        from nexus.daemon.storage_service_daemon import StorageServiceStartError

        def _boom():
            raise StorageServiceStartError(
                "No nexus-service JAR found. Install one: nx daemon service install-jar <path>"
            )

        monkeypatch.setattr(
            "nexus.daemon.storage_service_daemon.start_storage_service", _boom
        )

        with pytest.raises(SystemExit) as exc:
            init_mod._start_service_step()
        assert exc.value.code == 1

    def test_init_service_local_wires_start_after_embedder(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The collapse order is provision -> embedder -> start, and start runs."""
        order: list[str] = []
        monkeypatch.setattr(
            "nexus.commands.init._provision_postgres_step",
            lambda: order.append("pg"),
        )
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        monkeypatch.setattr(
            "nexus.db.service_bge_model.fetch_service_bge_onnx",
            lambda **kw: (order.append("embed"), Path("/fake/onnx"))[1],
        )
        monkeypatch.setattr(
            "nexus.commands.init._ensure_service_binary_step",
            lambda cd: (order.append("binary"), True)[1],
        )
        monkeypatch.setattr(
            "nexus.commands.init._start_service_step",
            lambda: order.append("start"),
        )
        result = CliRunner().invoke(init_cmd, ["--service"])
        assert result.exit_code == 0, result.output
        assert order == ["pg", "embed", "binary", "start"]

    def test_auto_service_local_wires_start(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """NX_STORAGE_BACKEND=service auto-trigger in LOCAL mode runs the full
        collapse (pg -> embed -> start), not just provisioning. This is the
        re-run path an operator hits after a first `nx init --service`."""
        monkeypatch.setenv("NX_STORAGE_BACKEND", "service")
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        order: list[str] = []
        monkeypatch.setattr(
            "nexus.commands.init._provision_postgres_step", lambda: order.append("pg")
        )
        monkeypatch.setattr(
            "nexus.db.service_bge_model.fetch_service_bge_onnx",
            lambda **kw: (order.append("embed"), Path("/fake/onnx"))[1],
        )
        monkeypatch.setattr(
            "nexus.commands.init._ensure_service_binary_step",
            lambda cd: (order.append("binary"), True)[1],
        )
        monkeypatch.setattr(
            "nexus.commands.init._start_service_step", lambda: order.append("start")
        )
        result = CliRunner().invoke(init_cmd, [])
        assert result.exit_code == 0, result.output
        assert order == ["pg", "embed", "binary", "start"]
