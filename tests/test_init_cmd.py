# SPDX-License-Identifier: AGPL-3.0-or-later
"""`nx init` mode-detecting onboarding (RDR-144, collapsed by RDR-174).

Covers the RDR-174 dispatch: ``_resolve_init_mode`` precedence, the LOCAL path
(plain ``nx init`` provisions the local service stack; the RDR-144 embedder
picker was removed in P1.3), and the MANAGED path (reused RDR-166 wizard +
service probe). Dispatch is steered by NX_LOCAL / NX_SERVICE_URL env state;
the ``cfg_dir`` fixture clears those for isolation. The genuine-cloud arm still
pins ``nexus.config.is_local_mode`` (mem:feedback_pin_local_mode_in_cloud_tests).
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


def _fake_caps(url: str = "https://m.example"):  # noqa: ANN202
    """A ManagedCapabilities the patched probe returns on success."""
    from nexus.db.managed_endpoint import ManagedCapabilities

    return ManagedCapabilities(
        base_url=url,
        app_version="1.2.3",
        release_version="0.1.9",
        embedding_mode="voyage",
        embedding_models=["voyage-context-3"],
        schema_latest_id=None,
        schema_changeset_count=None,
    )


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
    # Self-isolate dispatch-steering env so ambient CI/dev values can't bleed in
    # (CRE LOW-3; mirrors the f15f6d45 fix to the fake_home fixture). Individual
    # tests re-set these as needed.
    for _var in ("NX_LOCAL", "NX_SERVICE_URL", "NX_SERVICE_TOKEN"):
        monkeypatch.delenv(_var, raising=False)
    return d



# ── managed mode ──────────────────────────────────────────────────────────────


class TestManagedMode:
    """RDR-174 P1.2/P1.3: when ``_resolve_init_mode()`` resolves MANAGED, plain
    ``nx init`` provisions nothing locally — it runs the reused RDR-166 wizard
    (creds) + service probe and returns. Full onboarding behaviour is covered by
    TestManagedOnboardingP12; this asserts only the no-local-provisioning +
    no-local.embed_model invariant.
    """

    def test_managed_mode_provisions_nothing_local(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """NX_LOCAL=0 + creds present → MANAGED: probe runs, no local
        provisioning, no local.embed_model written."""
        monkeypatch.setenv("NX_LOCAL", "0")
        monkeypatch.setenv("NX_SERVICE_URL", "https://m.example")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
        called: list[str] = []
        monkeypatch.setattr(
            "nexus.commands.init.provision_and_start_service",
            lambda embedder=None: called.append("provisioned"),
        )
        monkeypatch.setattr(
            "nexus.db.managed_endpoint.probe_managed_service",
            lambda **kw: _fake_caps(),
        )

        result = CliRunner().invoke(init_cmd, [])

        assert result.exit_code == 0, result.output
        assert called == [], "MANAGED mode must not provision locally"
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

    def test_service_flag_emits_deprecation_notice(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """RDR-174 P3.1 (§Approach 5): ``--service`` still runs the local
        provision path (now the default) but prints a deprecation notice."""
        monkeypatch.setenv("NX_LOCAL", "1")
        monkeypatch.delenv("NX_SERVICE_URL", raising=False)
        monkeypatch.setattr(
            "nexus.commands.init.provision_and_start_service",
            lambda embedder=None: _FAKE_LEASE,
        )

        result = CliRunner().invoke(init_cmd, ["--service"])

        assert result.exit_code == 0, result.output
        out = result.output.lower()
        assert "--service" in result.output and "deprecat" in out, (
            "--service must print a deprecation notice naming the flag"
        )

    def test_plain_init_no_deprecation_notice(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The deprecation notice fires ONLY for the explicit flag, not plain init."""
        monkeypatch.setenv("NX_LOCAL", "1")
        monkeypatch.delenv("NX_SERVICE_URL", raising=False)
        monkeypatch.setattr(
            "nexus.commands.init.provision_and_start_service",
            lambda embedder=None: _FAKE_LEASE,
        )

        result = CliRunner().invoke(init_cmd, [])

        assert result.exit_code == 0, result.output
        assert "deprecat" not in result.output.lower()

    def test_service_flag_same_provision_path_as_plain(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Behavior UNCHANGED: ``--service`` drives the SAME local provision path
        as plain ``nx init`` (both call provision_and_start_service identically)."""
        monkeypatch.setenv("NX_LOCAL", "1")
        monkeypatch.delenv("NX_SERVICE_URL", raising=False)
        seen: list[str | None] = []
        monkeypatch.setattr(
            "nexus.commands.init.provision_and_start_service",
            lambda embedder=None: seen.append(embedder) or _FAKE_LEASE,
        )

        CliRunner().invoke(init_cmd, [])
        CliRunner().invoke(init_cmd, ["--service"])

        assert seen == [None, None], (
            "--service and plain init must both call provision_and_start_service "
            "with the same args (behavior unchanged, only a notice added)"
        )

    def test_nx_storage_backend_no_effect_on_dispatch(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """RDR-174 P1.1/P1.3: ``NX_STORAGE_BACKEND`` is no longer consulted by
        init dispatch (the ``_auto_service`` side-channel is gone). Dispatch is
        driven solely by ``_resolve_init_mode``; the env var has zero effect.
        In MANAGED mode it stays a no-op even when set to 'service'."""
        monkeypatch.setenv("NX_STORAGE_BACKEND", "service")
        monkeypatch.setenv("NX_LOCAL", "0")
        monkeypatch.setenv("NX_SERVICE_URL", "https://m.example")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
        called: list[str] = []
        monkeypatch.setattr(
            "nexus.commands.init.provision_and_start_service",
            lambda embedder=None: called.append("provisioned"),
        )
        monkeypatch.setattr(
            "nexus.db.managed_endpoint.probe_managed_service",
            lambda **kw: _fake_caps(),
        )

        result = CliRunner().invoke(init_cmd, [])

        assert result.exit_code == 0, result.output
        assert called == [], "NX_STORAGE_BACKEND must not steer dispatch; MANAGED stays no-op"


class _FakeLeaseEP:
    """Minimal lease stand-in carrying the attrs init's autostart reporter reads."""
    endpoint = {"host": "127.0.0.1", "port": 18080, "pid": 1234}
    generation = 1


class TestAutostartPrompt:
    """RDR-174 P2.4 (nexus-3pfj0): nx init OFFERS to register the service
    autostart unit, with DECIDE-FIRST ordering (RDR-175 Gap 3) — never start a
    session supervisor under a unit.

    Decide-first contract verified per branch:
    - autostart=yes → install the unit as the SOLE starter (install_autostart
      called) + poll the lease; _start_service_step (session detach) NOT called.
    - autostart=no  → provision_and_start_service (session); install_autostart
      NOT called.
    - consent gate: no system unit written without an explicit yes / --yes.
    """

    def _local(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NX_LOCAL", "1")
        monkeypatch.delenv("NX_SERVICE_URL", raising=False)

    def _seams(self, monkeypatch: pytest.MonkeyPatch):
        """Patch the decide-first seams; return a calls recorder."""
        calls: dict[str, int] = {"stack": 0, "install": 0, "poll": 0, "session": 0}

        monkeypatch.setattr(
            "nexus.commands.init.provision_service_stack",
            lambda embedder=None: calls.__setitem__("stack", calls["stack"] + 1) or True,
        )
        monkeypatch.setattr(
            "nexus.commands.init._poll_service_lease",
            lambda config_dir, **kw: calls.__setitem__("poll", calls["poll"] + 1) or _FakeLeaseEP(),
        )
        monkeypatch.setattr(
            "nexus.commands.init._start_service_step",
            lambda: calls.__setitem__("session", calls["session"] + 1) or _FakeLeaseEP(),
        )

        from nexus.daemon import installer

        def _fake_install(*, tier: str, force: bool = False):  # noqa: ARG001
            calls["install"] += 1
            return installer.InstallResult(
                status=installer.InstallStatus.NEWLY_INSTALLED,
                dest=Path("/tmp/nexus-service.service"),
            )

        monkeypatch.setattr("nexus.daemon.installer.install_autostart", _fake_install)
        return calls

    def test_yes_flag_installs_decide_first(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--yes → install the unit as the sole starter; NEVER session-start."""
        self._local(monkeypatch)
        calls = self._seams(monkeypatch)

        result = CliRunner().invoke(init_cmd, ["--yes"])

        assert result.exit_code == 0, result.output
        assert calls["install"] == 1, "autostart unit must be installed on --yes"
        assert calls["poll"] == 1, "must poll the lease the unit's supervisor publishes"
        assert calls["session"] == 0, (
            "DECIDE-FIRST: must NOT start a session supervisor under the unit"
        )

    def test_no_autostart_flag_session_only(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--no-autostart → session supervisor only; no unit written."""
        self._local(monkeypatch)
        calls = self._seams(monkeypatch)
        served: list[str | None] = []
        monkeypatch.setattr(
            "nexus.commands.init.provision_and_start_service",
            lambda embedder=None: served.append(embedder) or _FAKE_LEASE,
        )

        result = CliRunner().invoke(init_cmd, ["--no-autostart"])

        assert result.exit_code == 0, result.output
        assert served == [None], "no-autostart must take the session provision path"
        assert calls["install"] == 0, "no system unit may be written on --no-autostart"

    def test_interactive_prompt_default_yes_installs(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Interactive TTY, default-yes prompt → installs decide-first."""
        self._local(monkeypatch)
        calls = self._seams(monkeypatch)
        monkeypatch.setattr("nexus.commands.init._stdin_is_interactive", lambda: True)
        monkeypatch.setattr("nexus.commands.init.click.confirm", lambda *a, **k: True)

        result = CliRunner().invoke(init_cmd, [])

        assert result.exit_code == 0, result.output
        assert calls["install"] == 1 and calls["session"] == 0

    def test_interactive_prompt_no_declines(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Explicit 'n' at the prompt → no unit, session supervisor still up."""
        self._local(monkeypatch)
        calls = self._seams(monkeypatch)
        monkeypatch.setattr("nexus.commands.init._stdin_is_interactive", lambda: True)
        monkeypatch.setattr("nexus.commands.init.click.confirm", lambda *a, **k: False)
        served: list[str | None] = []
        monkeypatch.setattr(
            "nexus.commands.init.provision_and_start_service",
            lambda embedder=None: served.append(embedder) or _FAKE_LEASE,
        )

        result = CliRunner().invoke(init_cmd, [])

        assert result.exit_code == 0, result.output
        assert calls["install"] == 0, "declining must not write a unit"
        assert served == [None], "decline → session provision path"

    def test_non_interactive_no_flag_consent_gate(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-interactive (no TTY) + no flag → NO unit (consent gate); session
        supervisor still starts; a notice is printed."""
        self._local(monkeypatch)
        calls = self._seams(monkeypatch)
        monkeypatch.setattr("nexus.commands.init._stdin_is_interactive", lambda: False)
        served: list[str | None] = []
        monkeypatch.setattr(
            "nexus.commands.init.provision_and_start_service",
            lambda embedder=None: served.append(embedder) or _FAKE_LEASE,
        )

        result = CliRunner().invoke(init_cmd, [])

        assert result.exit_code == 0, result.output
        assert calls["install"] == 0, "no silent unit write under non-interactive init"
        assert served == [None]
        assert "non-interactive" in result.output.lower(), (
            "the consent-gate decline must print an actionable notice"
        )

    def test_no_autostart_precedence_over_yes(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--no-autostart wins when both flags are passed (explicit decline)."""
        self._local(monkeypatch)
        calls = self._seams(monkeypatch)
        served: list[str | None] = []
        monkeypatch.setattr(
            "nexus.commands.init.provision_and_start_service",
            lambda embedder=None: served.append(embedder) or _FAKE_LEASE,
        )

        result = CliRunner().invoke(init_cmd, ["--yes", "--no-autostart"])

        assert result.exit_code == 0, result.output
        assert calls["install"] == 0, "--no-autostart must win over --yes"
        assert served == [None]

    def test_lease_timeout_exits_nonzero(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unit installed but no lease in the window → exit 1 (NOT confirmed
        serving), parity with the no-binary path. No session fallback (that would
        risk coexistence with the unit's eventual supervisor)."""
        self._local(monkeypatch)
        calls = self._seams(monkeypatch)
        monkeypatch.setattr(
            "nexus.commands.init._poll_service_lease", lambda config_dir, **kw: None
        )

        result = CliRunner().invoke(init_cmd, ["--yes"])

        assert result.exit_code == 1, result.output
        assert calls["install"] == 1
        assert calls["session"] == 0, "lease-timeout must NOT session-fall-back"
        assert "not confirmed serving" in result.output.lower()

    def _seams_already_present(self, monkeypatch: pytest.MonkeyPatch, calls: dict) -> None:
        """Override install_autostart to report ALREADY_PRESENT (re-run)."""
        from nexus.daemon import installer

        def _fake(*, tier: str, force: bool = False):  # noqa: ARG001
            calls["install"] += 1
            return installer.InstallResult(
                status=installer.InstallStatus.ALREADY_PRESENT,
                dest=Path("/tmp/nexus-service.service"),
            )

        monkeypatch.setattr("nexus.daemon.installer.install_autostart", _fake)

    def test_rerun_already_present_with_lease_succeeds(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Idempotent re-run (SIG-2): unit ALREADY_PRESENT + live lease → exit 0,
        reported as already registered (not 'Registered …')."""
        self._local(monkeypatch)
        calls = self._seams(monkeypatch)
        self._seams_already_present(monkeypatch, calls)

        result = CliRunner().invoke(init_cmd, ["--yes"])

        assert result.exit_code == 0, result.output
        assert "already registered" in result.output.lower()

    def test_rerun_already_present_lease_not_yet_published_exits_zero(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Idempotent re-run (SIG-2): unit ALREADY_PRESENT but the service has
        not published a lease yet (e.g. right after reboot) → exit 0, NOT 1. The
        OS unit owns bring-up; init has nothing to fix. (Contrast a FRESH install
        that never serves, which exits 1.)"""
        self._local(monkeypatch)
        calls = self._seams(monkeypatch)
        self._seams_already_present(monkeypatch, calls)
        monkeypatch.setattr(
            "nexus.commands.init._poll_service_lease", lambda config_dir, **kw: None
        )

        result = CliRunner().invoke(init_cmd, ["--yes"])

        assert result.exit_code == 0, result.output
        assert calls["session"] == 0, "must NOT session-fall-back on an existing unit"
        assert "not published a lease yet" in result.output.lower()

    def test_install_conflict_exits_nonzero_no_fallback(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-activation install conflict (ContentDiffersError) fails loud
        with a remedy and does NOT silently session-fall-back."""
        self._local(monkeypatch)
        calls = self._seams(monkeypatch)
        from nexus.daemon import installer

        def _conflict(*, tier: str, force: bool = False):  # noqa: ARG001
            calls["install"] += 1
            raise installer.ContentDiffersError("unit exists with different content")

        monkeypatch.setattr("nexus.daemon.installer.install_autostart", _conflict)

        result = CliRunner().invoke(init_cmd, ["--yes"])

        assert result.exit_code == 1, result.output
        assert calls["session"] == 0, "install conflict must not session-fall-back"
        assert "--force" in result.output, "must point at the --force remedy"

    def test_activation_error_falls_back_to_session(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """install_autostart ActivationError (headless/no session bus) → warn and
        fall back to a session supervisor so init still leaves a serving backend.
        Safe: ActivationError means nothing was started, so no coexistence."""
        self._local(monkeypatch)
        calls = self._seams(monkeypatch)
        from nexus.daemon import installer

        def _boom(*, tier: str, force: bool = False):  # noqa: ARG001
            calls["install"] += 1
            raise installer.ActivationError("no session bus")

        monkeypatch.setattr("nexus.daemon.installer.install_autostart", _boom)

        result = CliRunner().invoke(init_cmd, ["--yes"])

        assert result.exit_code == 0, result.output
        assert calls["install"] == 1, "install was attempted"
        assert calls["session"] == 1, "ActivationError falls back to a session supervisor"
        assert calls["poll"] == 0, "no lease poll when activation failed"


class TestT2DaemonDemotion:
    """RDR-174 P3.2 (§Approach 6): in the default all-SERVICE config no T2 store
    resolves to SQLite, so ``nx init`` must NOT register the SQLite T2 autostart
    unit. The ``nx daemon t2 install`` command stays available as an explicit
    opt-in (full deletion is RDR-158 P4, two-release window — NOT here).

    init.py contains no T2-registration call today; these tests PIN that
    invariant against regression rather than driving a production change."""

    def test_default_init_writes_no_t2_autostart_unit(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NX_LOCAL", "1")
        monkeypatch.delenv("NX_SERVICE_URL", raising=False)
        monkeypatch.setattr(
            "nexus.commands.init.provision_and_start_service",
            lambda embedder=None: _FAKE_LEASE,
        )
        tiers: list[str] = []
        monkeypatch.setattr(
            "nexus.daemon.installer.install_autostart",
            lambda *, tier, force=False: tiers.append(tier),
        )

        result = CliRunner().invoke(init_cmd, [])

        assert result.exit_code == 0, result.output
        assert "t2" not in tiers, (
            "nx init must NOT register the SQLite T2 autostart unit "
            f"(default config is all-SERVICE); installed tiers: {tiers}"
        )

    def test_t2_install_command_remains_optin(self) -> None:
        """Guard against accidental deletion: `nx daemon t2 install` stays
        registered (the demote-now / delete-at-RDR-158-P4 contract)."""
        from nexus.commands.daemon import t2_group

        assert "install" in t2_group.commands, (
            "nx daemon t2 install must remain an explicit opt-in — deletion is "
            "RDR-158 P4 (two-release window), not RDR-174 P3.2"
        )


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

    def test_service_with_nx_local_0_reports_no_local_service(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CRE LOW-2: ``--service`` with NX_LOCAL=0 provisions PG but
        provision_and_start_service returns None (is_local_mode False), so no
        local service starts. init must SAY so rather than exit silently."""
        monkeypatch.setenv("NX_LOCAL", "0")
        monkeypatch.setattr(
            "nexus.commands.init._provision_postgres_step", lambda: None
        )
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)

        result = CliRunner().invoke(init_cmd, ["--service"])

        assert result.exit_code == 0, result.output
        assert "no local service was started" in result.output.lower()

    def test_yes_flag_inert_in_managed_mode(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """RDR-174 P2.4: ``--yes`` now accepts service-autostart registration,
        but that only applies in LOCAL mode. In MANAGED mode there is no local
        supervisor to persist, so ``--yes`` has no autostart effect, registers no
        unit, and (correctly) no longer prints the old 'no-op' notice."""
        monkeypatch.setenv("NX_LOCAL", "0")  # managed → no local provisioning
        monkeypatch.setenv("NX_SERVICE_URL", "https://m.example")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
        monkeypatch.setattr(
            "nexus.db.managed_endpoint.probe_managed_service",
            lambda **kw: _fake_caps(),
        )
        installed: list[str] = []
        monkeypatch.setattr(
            "nexus.daemon.installer.install_autostart",
            lambda **kw: installed.append("called"),
        )
        result = CliRunner().invoke(init_cmd, ["--yes"])
        assert result.exit_code == 0, result.output
        assert installed == [], "managed --yes must not register an autostart unit"
        assert "no-op" not in result.output.lower(), "obsolete no-op notice removed"

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


class TestManagedOnboardingP12:
    """RDR-174 P1.2: managed-path dispatch — RDR-166 credential wizard (reused)
    + nx service probe, then STOP (no local provisioning).

    Tests patch ``nexus.db.managed_endpoint.probe_managed_service`` (the source
    module). Production reaches it via a deferred local import inside
    ``_managed_onboarding``, so the patch binds at call time — if that import is
    ever hoisted to module level, retarget to ``nexus.commands.init.*``.
    """

    def test_missing_creds_runs_wizard_and_persists_then_probes(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MANAGED with no creds → the reused RDR-166 wizard prompts and persists
        service_url + service_token, then the probe runs against the CONFIGURED
        url (not the default); no local provisioning."""
        monkeypatch.setenv("NX_LOCAL", "0")  # force MANAGED
        monkeypatch.delenv("NX_SERVICE_URL", raising=False)
        monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)
        provisioned: list[str] = []
        probed: list[str | None] = []
        monkeypatch.setattr(
            "nexus.commands.init.provision_and_start_service",
            lambda embedder=None: provisioned.append("x"),
        )
        monkeypatch.setattr(
            "nexus.db.managed_endpoint.probe_managed_service",
            lambda **kw: (probed.append(kw.get("base_url")), _fake_caps())[1],
        )

        result = CliRunner().invoke(
            init_cmd, [], input="https://m.example\ntoken123\n"
        )

        assert result.exit_code == 0, result.output
        assert provisioned == [], "managed path must not provision locally"
        creds = _read_config(cfg_dir).get("credentials", {})
        assert creds.get("service_url") == "https://m.example"
        assert creds.get("service_token") == "token123"
        # SIG-3: the probe must target the configured URL, not the default.
        assert probed == ["https://m.example"]

    def test_empty_wizard_input_fails_loud_no_default_probe(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SIG-1 regression: entering nothing in the wizard must NOT silently
        fall back to the default public endpoint and green-light init. It exits
        non-zero with a remedy and never probes."""
        monkeypatch.setenv("NX_LOCAL", "0")
        monkeypatch.delenv("NX_SERVICE_URL", raising=False)
        monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)
        probed: list[str | None] = []
        monkeypatch.setattr(
            "nexus.db.managed_endpoint.probe_managed_service",
            lambda **kw: (probed.append(kw.get("base_url")), _fake_caps())[1],
        )

        # Empty input for both wizard prompts.
        result = CliRunner().invoke(init_cmd, [], input="\n\n")

        assert result.exit_code != 0, result.output
        assert probed == [], "must not probe a default endpoint after empty input"
        assert "service_url" in result.output.lower()

    def test_url_set_but_token_empty_fails_loud(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The managed branch ensures BOTH creds: a URL with an empty token must
        fail loud rather than probe the unauthenticated /version and exit 0 with
        no bearer configured."""
        monkeypatch.setenv("NX_LOCAL", "0")
        monkeypatch.delenv("NX_SERVICE_URL", raising=False)
        monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)
        probed: list[str | None] = []
        monkeypatch.setattr(
            "nexus.db.managed_endpoint.probe_managed_service",
            lambda **kw: (probed.append(kw.get("base_url")), _fake_caps())[1],
        )

        # URL entered, token left empty.
        result = CliRunner().invoke(init_cmd, [], input="https://m.example\n\n")

        assert result.exit_code != 0, result.output
        assert probed == [], "must not probe with an unset token"
        assert "service_token" in result.output.lower()

    def test_probe_success_exits_zero_no_provision(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Creds present + probe OK → exit 0, reachable message, no provisioning."""
        monkeypatch.setenv("NX_SERVICE_URL", "https://m.example")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
        provisioned: list[str] = []
        monkeypatch.setattr(
            "nexus.commands.init.provision_and_start_service",
            lambda embedder=None: provisioned.append("x"),
        )
        monkeypatch.setattr(
            "nexus.db.managed_endpoint.probe_managed_service",
            lambda **kw: _fake_caps(),
        )

        result = CliRunner().invoke(init_cmd, [])

        assert result.exit_code == 0, result.output
        assert provisioned == []
        assert "reachable" in result.output.lower()

    def test_probe_failure_exits_nonzero_with_actionable_message(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Probe failure → non-zero exit + actionable remedy (fail loud)."""
        monkeypatch.setenv("NX_SERVICE_URL", "https://m.example")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
        from nexus.db.managed_endpoint import ManagedServiceUnreachable

        def _boom(**kw):  # noqa: ANN003, ANN202
            raise ManagedServiceUnreachable("connect timeout to https://m.example")

        monkeypatch.setattr(
            "nexus.db.managed_endpoint.probe_managed_service", _boom
        )

        result = CliRunner().invoke(init_cmd, [])

        assert result.exit_code != 0, result.output
        out = result.output.lower()
        assert "timeout" in out or "probe" in out
        assert "nx config init" in out or "re-run" in out

    def test_creds_present_skips_wizard(
        self, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both creds already set → wizard is skipped. With no stdin supplied,
        a resurrected prompt would abort non-zero; exit 0 proves the skip."""
        monkeypatch.setenv("NX_SERVICE_URL", "https://m.example")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
        monkeypatch.setattr(
            "nexus.db.managed_endpoint.probe_managed_service",
            lambda **kw: _fake_caps(),
        )

        result = CliRunner().invoke(init_cmd, [])  # no input — would hang if prompted

        assert result.exit_code == 0, result.output


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
