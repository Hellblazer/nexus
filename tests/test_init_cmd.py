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
from nexus.db.local_ef import _TIER1_MODEL

#: Opaque non-None stand-in for a live LeaseRecord — init_cmd only checks
#: ``lease is None`` (cloud-internal) vs not-None (local service started).
_FAKE_LEASE = object()


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



# ── managed mode ──────────────────────────────────────────────────────────────


class TestManagedMode:
    """RDR-174 P1.3: when ``_resolve_init_mode()`` resolves MANAGED, plain
    ``nx init`` provisions nothing locally — it prints the managed-endpoint
    informational block (folded from the old cloud-mode early return; the
    RDR-166 credential wizard lands in P1.2) and returns.
    """

    def test_managed_mode_provisions_nothing_local(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """NX_LOCAL=0 → MANAGED: no provisioning, informational message, no
        local.embed_model written."""
        monkeypatch.setenv("NX_LOCAL", "0")
        called: list[str] = []
        monkeypatch.setattr(
            "nexus.commands.init.provision_and_start_service",
            lambda embedder=None: called.append("provisioned"),
        )

        result = CliRunner().invoke(init_cmd, [])

        assert result.exit_code == 0, result.output
        assert called == [], "MANAGED mode must not provision locally"
        assert "managed" in result.output.lower()
        assert "local" not in _read_config(cfg_dir)


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

    def test_local_mode_plain_init_provisions(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """RDR-174 P1.3: plain ``nx init`` (no ``--service``) in LOCAL mode now
        provisions the local service stack via the unified path — the explicit
        flag is no longer required."""
        monkeypatch.setenv("NX_LOCAL", "1")
        monkeypatch.delenv("NX_SERVICE_URL", raising=False)
        called: list[str | None] = []
        monkeypatch.setattr(
            "nexus.commands.init.provision_and_start_service",
            lambda embedder=None: called.append(embedder) or _FAKE_LEASE,
        )

        result = CliRunner().invoke(init_cmd, [])

        assert result.exit_code == 0, result.output
        assert called == [None], "LOCAL plain init must provision via the unified path"

    def test_nx_storage_backend_no_effect_on_dispatch(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """RDR-174 P1.1/P1.3: ``NX_STORAGE_BACKEND`` is no longer consulted by
        init dispatch (the ``_auto_service`` side-channel is gone). Dispatch is
        driven solely by ``_resolve_init_mode``; the env var has zero effect.
        In MANAGED mode it stays a no-op even when set to 'service'."""
        monkeypatch.setenv("NX_STORAGE_BACKEND", "service")
        monkeypatch.setenv("NX_LOCAL", "0")
        called: list[str] = []
        monkeypatch.setattr(
            "nexus.commands.init.provision_and_start_service",
            lambda embedder=None: called.append("provisioned"),
        )

        result = CliRunner().invoke(init_cmd, [])

        assert result.exit_code == 0, result.output
        assert called == [], "NX_STORAGE_BACKEND must not steer dispatch; MANAGED stays no-op"


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
    started, status-green service. nexus-qke1e: _start_service_step now routes
    through ensure_storage_supervisor (the PERSISTENT, heartbeated supervisor),
    not the transient start_storage_service — and fails loud on any error."""

    def test_start_step_reports_running_endpoint(self, monkeypatch) -> None:
        import types

        import nexus.commands.init as init_mod

        lease = types.SimpleNamespace(
            endpoint={"host": "127.0.0.1", "port": 18099, "pid": 4242},
            generation=3,
        )
        monkeypatch.setattr(
            "nexus.commands.daemon.ensure_storage_supervisor", lambda _cfg: lease
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

        def _boom(_cfg):
            raise StorageServiceStartError(
                "No nexus-service native binary found. Acquire one: "
                "nx daemon service install-binary <tag>"
            )

        monkeypatch.setattr(
            "nexus.commands.daemon.ensure_storage_supervisor", _boom
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

    def test_local_plain_init_wires_full_collapse(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """RDR-174 P1.3: plain ``nx init`` (no ``--service``) in LOCAL mode runs
        the FULL collapse pg→embed→binary→start through the unified path — the
        same body the explicit ``--service`` flag drives. NX_STORAGE_BACKEND is
        irrelevant; mode is forced LOCAL via NX_LOCAL=1."""
        monkeypatch.setenv("NX_LOCAL", "1")
        monkeypatch.delenv("NX_SERVICE_URL", raising=False)
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


class TestLocalDispatchP13:
    """RDR-174 P1.3: plain ``nx init`` LOCAL-path dispatch + picker removal."""

    def test_embedder_flag_threads_through_to_provisioning(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--embedder`` is the retained bge/minilm selector — it threads
        straight through to provision_and_start_service (which the service
        embedder step honors). No interactive picker in between."""
        monkeypatch.setenv("NX_LOCAL", "1")
        monkeypatch.delenv("NX_SERVICE_URL", raising=False)
        seen: list[str | None] = []
        monkeypatch.setattr(
            "nexus.commands.init.provision_and_start_service",
            lambda embedder=None: seen.append(embedder) or _FAKE_LEASE,
        )

        for choice in ("bge-768", "minilm-384"):
            seen.clear()
            result = CliRunner().invoke(init_cmd, ["--embedder", choice])
            assert result.exit_code == 0, result.output
            assert seen == [choice], f"--embedder {choice} must thread to provisioning"

    def test_no_interactive_picker_prompt(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The RDR-144 interactive 384-vs-768 picker is removed: plain LOCAL
        ``nx init`` never prints the on-device-model chooser and never blocks on
        an ``Embedder`` prompt."""
        monkeypatch.setenv("NX_LOCAL", "1")
        monkeypatch.delenv("NX_SERVICE_URL", raising=False)
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        monkeypatch.setattr("nexus.commands.init._provision_postgres_step", lambda: None)
        monkeypatch.setattr(
            "nexus.db.service_bge_model.fetch_service_bge_onnx",
            lambda **kw: Path("/fake/onnx"),
        )
        monkeypatch.setattr(
            "nexus.commands.init._ensure_service_binary_step", lambda cd: True
        )
        monkeypatch.setattr("nexus.commands.init._start_service_step", lambda: None)

        # No stdin supplied: if a prompt existed, the runner would error/hang.
        result = CliRunner().invoke(init_cmd, [])

        assert result.exit_code == 0, result.output
        out = result.output.lower()
        # The removed picker's prose + the click.prompt label must both be gone.
        assert "choose your on-device embedding model" not in out
        assert "minilm-384  all-minilm" not in out  # the picker's option listing
        assert "\nembedder" not in out and "embedder [" not in out  # prompt label

    def test_picker_helpers_are_removed(self) -> None:
        """The picker helper surface is gone (no silent dead code, OBS-1)."""
        import nexus.commands.init as init_mod

        for name in (
            "_offer_stale_migration",
            "_warmup_bge",
            "_ensure_local_extra",
            "_local_extra_installed",
            "_run_migration",
            "_uv_receipt_path",
            "_CHOICE_TO_MODEL",
            "_BGE_DOWNLOAD_HINT",
        ):
            assert not hasattr(init_mod, name), f"{name} must be removed in P1.3"

    def test_cloud_keys_no_service_url_does_not_provision_pg(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression guard (substantive-critic SIG-1): a cloud-Voyage user with
        cloud keys but NO service_url and NX_LOCAL unset resolves LOCAL by
        service_url-absence, yet must NOT have a local Postgres cluster silently
        provisioned (provision_and_start_service runs _provision_postgres_step
        BEFORE its internal is_local_mode guard). The secondary cloud guard in
        init_cmd short-circuits before provisioning and prints the cloud notice."""
        monkeypatch.delenv("NX_LOCAL", raising=False)
        monkeypatch.delenv("NX_SERVICE_URL", raising=False)
        monkeypatch.setenv("VOYAGE_API_KEY", "vk-test")
        monkeypatch.setenv("CHROMA_API_KEY", "ck-test")
        called: list[str] = []
        monkeypatch.setattr(
            "nexus.commands.init.provision_and_start_service",
            lambda embedder=None: called.append("provisioned"),
        )

        result = CliRunner().invoke(init_cmd, [])

        assert result.exit_code == 0, result.output
        assert called == [], "cloud-keys + no service_url must not provision a local PG"
        assert "cloud" in result.output.lower()

    def test_service_flag_forces_provisioning_in_managed_mode(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--service`` forces local provisioning even when mode resolves
        MANAGED (service_url set) — the explicit flag overrides dispatch."""
        monkeypatch.delenv("NX_LOCAL", raising=False)
        monkeypatch.setenv("NX_SERVICE_URL", "https://managed.example/api")
        called: list[str | None] = []
        monkeypatch.setattr(
            "nexus.commands.init.provision_and_start_service",
            lambda embedder=None: called.append(embedder) or _FAKE_LEASE,
        )

        result = CliRunner().invoke(init_cmd, ["--service"])

        assert result.exit_code == 0, result.output
        assert called == [None], "--service must force provisioning regardless of mode"

    def test_yes_flag_emits_noop_notice(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--yes`` is retained for compatibility but is a no-op; passing it
        emits an explicit notice rather than silently changing semantics."""
        monkeypatch.setenv("NX_LOCAL", "0")  # managed → no provisioning side effects
        result = CliRunner().invoke(init_cmd, ["--yes"])
        assert result.exit_code == 0, result.output
        assert "no-op" in result.output.lower()

    def test_guided_upgrade_default_serve_still_calls_provision(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DO-NOT-BREAK: migration._default_serve still routes through
        init.provision_and_start_service unchanged (signature intact)."""
        from nexus.migration import guided_upgrade

        called: list[str] = []
        monkeypatch.setattr(
            "nexus.commands.init.provision_and_start_service",
            lambda embedder=None: called.append("served") or _FAKE_LEASE,
        )

        result = guided_upgrade._default_serve()

        assert called == ["served"]
        assert result is _FAKE_LEASE


class TestResolveInitMode:
    """RDR-174 P1.1: explicit mode-detection precedence for init dispatch.

    Precedence (gate-locked, critic SIG-1): NX_LOCAL is orthogonal and WINS;
    otherwise dispatch on ``get_credential('service_url')``. Never ``is_local_mode``.
    """

    def test_nx_local_1_forces_local_even_with_service_url(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """NX_LOCAL=1 forces LOCAL even with a (stale) service_url set —
        preserves the migration / rollback-rehearsal pattern."""
        from nexus.commands.init import _resolve_init_mode

        monkeypatch.setenv("NX_LOCAL", "1")
        monkeypatch.setenv("NX_SERVICE_URL", "https://managed.example/api")
        assert _resolve_init_mode() == "local"

    def test_nx_local_0_forces_managed_without_service_url(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """NX_LOCAL=0 forces MANAGED even when no service_url is configured."""
        from nexus.commands.init import _resolve_init_mode

        monkeypatch.setenv("NX_LOCAL", "0")
        monkeypatch.delenv("NX_SERVICE_URL", raising=False)
        assert _resolve_init_mode() == "managed"

    def test_unset_nx_local_with_service_url_resolves_managed(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unset NX_LOCAL + service_url present → MANAGED."""
        from nexus.commands.init import _resolve_init_mode

        monkeypatch.delenv("NX_LOCAL", raising=False)
        monkeypatch.setenv("NX_SERVICE_URL", "https://managed.example/api")
        assert _resolve_init_mode() == "managed"

    def test_unset_nx_local_without_service_url_resolves_local(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unset NX_LOCAL + no service_url → LOCAL."""
        from nexus.commands.init import _resolve_init_mode

        monkeypatch.delenv("NX_LOCAL", raising=False)
        monkeypatch.delenv("NX_SERVICE_URL", raising=False)
        assert _resolve_init_mode() == "local"
