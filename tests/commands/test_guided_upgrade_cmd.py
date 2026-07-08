# SPDX-License-Identifier: AGPL-3.0-or-later
"""ez5.10 — `nx guided-upgrade` command wiring.

Thin orchestrator: pre-flight detect → establish_verified_service → hand off to
migrate-to-service → advisory. Tests pin the control flow + exit codes; the
composed steps are patched (they have their own unit suites).
"""

from __future__ import annotations

from unittest.mock import patch

from click.testing import CliRunner

from nexus.commands.guided_upgrade_cmd import guided_upgrade_cmd
from nexus.migration.guided_upgrade import (
    AlreadyMigratedPlan,
    HealthGateResult,
    PreflightDetection,
    ProvisionResult,
    ServiceReadiness,
    StoreMigrationStatus,
    VoyageCapabilityOutcome,
)
from nexus.migration.detection import CollectionClassification, DetectionReport
from nexus.migration.etl_registry import LADDER_ORDER

_MOD = "nexus.commands.guided_upgrade_cmd"


def _preflight(needs: bool, data_bearing: int = 0) -> PreflightDetection:
    # A report whose legs_with_data drives needs_migration; we patch the
    # function anyway, so the report contents only feed the echo counts.
    return PreflightDetection(report=DetectionReport(classifications=()), needs_migration=needs)


def _preflight_voyage() -> PreflightDetection:
    # support="unsupported" is irrelevant to the gate (it keys on model+has_data),
    # included only to construct a valid classification.
    c = CollectionClassification(
        collection="knowledge__o__voyage-context-3__v1", leg="local",
        model="voyage-context-3", dim=None, support="unsupported",
        source_count=12, has_data=True,
    )
    return PreflightDetection(
        report=DetectionReport(classifications=(c,)), needs_migration=True
    )


def _ready(url: str = "http://127.0.0.1:8099") -> ServiceReadiness:
    return ServiceReadiness(
        ready=True, service_url=url, reason=None, version_ok=True,
        provision=ProvisionResult(service_url=url, host="127.0.0.1", port=8099,
                                   pid=1, generation=1),
        health=HealthGateResult(ready=True, attempts=1, last_status=200,
                                 last_error=None, waited_s=0.0),
    )


def _not_ready(reason: str) -> ServiceReadiness:
    return ServiceReadiness(
        ready=False, service_url=None, reason=reason, version_ok=False,
        provision=None, health=None,
    )


class TestGuidedUpgradeCmd:
    def test_fresh_user_noops_without_provisioning(self, tmp_path) -> None:
        # nexus-ltix8: the fresh-user no-op requires BOTH legs clean — pin the
        # T2 SQLite path at a nonexistent location explicitly so the test does
        # not depend on the ambient config dir.
        with patch(f"{_MOD}.detect_pending_migration",
                   return_value=_preflight(False)) as det, \
             patch(f"{_MOD}.establish_verified_service") as est:
            result = CliRunner().invoke(
                guided_upgrade_cmd, ["--db", str(tmp_path / "absent" / "memory.db")])
        assert result.exit_code == 0, result.output
        assert "nothing to migrate" in result.output
        det.assert_called_once()
        est.assert_not_called()  # NEVER provision for an empty footprint

    def test_empty_chroma_with_t2_sqlite_proceeds_to_migration(self, tmp_path) -> None:
        """nexus-ltix8 (GH #1381): an empty Chroma footprint must NOT no-op
        when a local T2 SQLite store exists — pre-fix the command printed
        'you are already on the service stack' and stranded the T2 data on an
        unprovisioned service backend. With a present memory.db the command
        must proceed (provision + migrate; the T3 leg no-ops naturally)."""
        t2 = tmp_path / "memory.db"
        t2.write_bytes(b"stub")  # existence is the gate; content never read pre-provision
        from nexus.migration.guided_upgrade import (
            AlreadyMigratedPlan,
            StoreMigrationStatus,
        )

        plan = AlreadyMigratedPlan(statuses=(
            StoreMigrationStatus("memory", False, "memory: no covering report"),
        ))
        with patch(f"{_MOD}.detect_pending_migration",
                   return_value=_preflight(False)), \
             patch(f"{_MOD}.detect_already_migrated", return_value=plan), \
             patch(f"{_MOD}.establish_verified_service",
                   return_value=_ready()) as est, \
             patch("nexus.db.pg_provision.load_service_credentials_into_env",
                   return_value=True), \
             patch("nexus.commands.migrate_cmd._run_migration") as mig:
            result = CliRunner().invoke(
                guided_upgrade_cmd, ["--yes", "--db", str(t2)])
        assert result.exit_code == 0, result.output
        assert "T2 migration leg" in result.output
        est.assert_called_once()   # service IS provisioned for the T2 leg
        mig.assert_called_once()   # and the migration actually runs

    def test_fully_migrated_install_reaches_clean_noop(self, tmp_path) -> None:
        """nexus-ltix8 (critique M3): ordinary service-mode operation leaves a
        memory.db on disk (mcp_infra degraded-write backstop), so a fully-
        migrated install re-running bare guided-upgrade must reach a clean
        no-op — no re-prompt, no provisioning — when every evaluated T2 store
        is covered and there is no Chroma footprint."""
        from nexus.migration.guided_upgrade import (
            AlreadyMigratedPlan,
            StoreMigrationStatus,
        )

        t2 = tmp_path / "memory.db"
        t2.write_bytes(b"stub")
        plan = AlreadyMigratedPlan(statuses=(
            StoreMigrationStatus("memory", True, "memory: covered by report"),
            StoreMigrationStatus("catalog", True, "catalog: covered by report"),
        ))
        assert plan.all_skipped
        with patch(f"{_MOD}.detect_pending_migration",
                   return_value=_preflight(False)), \
             patch(f"{_MOD}.detect_already_migrated", return_value=plan), \
             patch(f"{_MOD}.establish_verified_service") as est, \
             patch("nexus.commands.migrate_cmd._run_migration") as mig:
            result = CliRunner().invoke(
                guided_upgrade_cmd, ["--db", str(t2)])
        assert result.exit_code == 0, result.output
        assert "nothing to do" in result.output
        est.assert_not_called()
        mig.assert_not_called()

    def test_not_ready_service_hard_fails_without_migrating(self) -> None:
        with patch(f"{_MOD}.detect_pending_migration",
                   return_value=_preflight(True, 3)), \
             patch(f"{_MOD}.establish_verified_service",
                   return_value=_not_ready("service v0.1.3 < required v0.1.8")), \
             patch("nexus.commands.migrate_cmd._run_migration") as mig:
            result = CliRunner().invoke(guided_upgrade_cmd, ["--yes"])
        assert result.exit_code == 1
        assert "NOT migrating" in result.output
        assert "v0.1.8" in result.output
        mig.assert_not_called()  # never migrate a not-ready service

    def test_happy_path_provisions_migrates_and_advises(self) -> None:
        with patch(f"{_MOD}.detect_pending_migration",
                   return_value=_preflight(True, 2)), \
             patch(f"{_MOD}.establish_verified_service",
                   return_value=_ready()) as est, \
             patch("nexus.db.pg_provision.load_service_credentials_into_env",
                   return_value=True), \
             patch("nexus.commands.migrate_cmd._run_migration") as mig:
            result = CliRunner().invoke(guided_upgrade_cmd, ["--yes"])
        assert result.exit_code == 0, result.output
        assert "Service verified" in result.output
        assert "nx doctor" in result.output  # advisory post-step (real command)
        # default provision path (no --service-url): no provision override passed
        _args, kwargs = est.call_args
        assert "provision" not in kwargs
        # wiring: the VERIFIED url + path overrides are handed to _run_migration.
        mig.assert_called_once_with(None, None, None, "http://127.0.0.1:8099")

    def test_pins_verified_endpoint_into_env_for_migration_legs(self) -> None:
        # nexus-qvemn: the migration's T2 store ETLs + count-source resolve the
        # endpoint via resolve_service_config() (NX_SERVICE_HOST/PORT-or-lease,
        # NEVER NX_SERVICE_URL). guided-upgrade must pin HOST/PORT from the VERIFIED
        # url so those legs resolve from env alone — no lease dependency, no 15s-TTL
        # race. Use a distinctive port to prove it derives from service_url.
        import os as _os
        from nexus.db.service_endpoint import resolve_service_config

        captured: dict[str, object] = {}

        def _capture(*_a, **_k) -> None:
            # At migration time the verified endpoint must resolve from env with NO
            # lease available (the failure mode was a reaped/expired lease).
            with patch("nexus.db.service_endpoint.discover_lease",
                       return_value=(None, None)), \
                 patch.dict(_os.environ, {"NX_SERVICE_TOKEN": "tok"}, clear=False):
                captured["host"] = _os.environ.get("NX_SERVICE_HOST")
                captured["port"] = _os.environ.get("NX_SERVICE_PORT")
                captured["resolved"] = resolve_service_config()

        with patch(f"{_MOD}.detect_pending_migration",
                   return_value=_preflight(True, 2)), \
             patch(f"{_MOD}.establish_verified_service",
                   return_value=_ready("http://127.0.0.1:52203")), \
             patch("nexus.db.pg_provision.load_service_credentials_into_env",
                   return_value=True), \
             patch("nexus.commands.migrate_cmd._run_migration", side_effect=_capture):
            result = CliRunner().invoke(guided_upgrade_cmd, ["--yes"])
        assert result.exit_code == 0, result.output
        assert captured["host"] == "127.0.0.1"
        assert captured["port"] == "52203"
        # resolve_service_config() resolves the verified endpoint from env, no lease.
        assert captured["resolved"] == ("127.0.0.1", 52203, "tok")
        # restored after the command — no leak of the pinned port into the process.
        assert _os.environ.get("NX_SERVICE_PORT") != "52203"

    def test_service_url_pins_supplied_endpoint_over_preset_env_and_restores(self) -> None:
        # nexus-qvemn (critic Sig-1): on the --service-url path, HOST/PORT derive from
        # the supplied (verified) URL and take precedence over a user's pre-set
        # NX_SERVICE_HOST/PORT for the migration's duration, then restore.
        import os as _os

        captured: dict[str, object] = {}

        def _capture(*_a, **_k) -> None:
            captured["host"] = _os.environ.get("NX_SERVICE_HOST")
            captured["port"] = _os.environ.get("NX_SERVICE_PORT")

        with patch(f"{_MOD}.detect_pending_migration",
                   return_value=_preflight(True, 1)), \
             patch(f"{_MOD}.establish_verified_service",
                   return_value=_ready("http://svc:9000")), \
             patch.dict(_os.environ,
                        {"NX_SERVICE_TOKEN": "user-tok",
                         "NX_SERVICE_HOST": "stale-host",
                         "NX_SERVICE_PORT": "9999"}, clear=False), \
             patch("nexus.commands.migrate_cmd._run_migration", side_effect=_capture):
            result = CliRunner().invoke(
                guided_upgrade_cmd, ["--yes", "--service-url", "http://svc:9000"]
            )
            # Assert INSIDE the patch.dict block so the pre-set values are still the
            # baseline (patch.dict reverts os.environ on exit).
            assert result.exit_code == 0, result.output
            # during migration: pinned to the supplied URL, not the stale pre-set env.
            assert captured["host"] == "svc"
            assert captured["port"] == "9000"
            # after the command: the user's pre-set values are restored (no leak/clobber).
            assert _os.environ.get("NX_SERVICE_HOST") == "stale-host"
            assert _os.environ.get("NX_SERVICE_PORT") == "9999"

    def test_abort_at_confirm_does_not_provision(self) -> None:
        with patch(f"{_MOD}.detect_pending_migration",
                   return_value=_preflight(True, 1)), \
             patch(f"{_MOD}.establish_verified_service") as est:
            result = CliRunner().invoke(guided_upgrade_cmd, [], input="n\n")
        assert result.exit_code == 0, result.output
        assert "Aborted" in result.output
        est.assert_not_called()

    def test_service_url_gates_existing_service_no_provision_thunk(self) -> None:
        captured = {}

        def fake_establish(*, timeout_s, provision=None, **kw):  # noqa: ANN001
            captured["has_provision"] = provision is not None
            if provision is not None:
                captured["url"] = provision().service_url
            return _ready("http://svc:9000")

        with patch(f"{_MOD}.detect_pending_migration",
                   return_value=_preflight(True, 1)), \
             patch(f"{_MOD}.establish_verified_service", side_effect=fake_establish), \
             patch("nexus.db.pg_provision.load_service_credentials_into_env") as loadc, \
             patch.dict("os.environ", {"NX_SERVICE_TOKEN": "user-tok"}, clear=False), \
             patch("nexus.commands.migrate_cmd._run_migration"):
            result = CliRunner().invoke(
                guided_upgrade_cmd, ["--yes", "--service-url", "http://svc:9000/"]
            )
        assert result.exit_code == 0, result.output
        assert captured["has_provision"] is True
        assert captured["url"] == "http://svc:9000"  # trailing slash stripped
        loadc.assert_not_called()  # --service-url never self-loads local creds

    def test_service_url_without_token_fails_before_migrating(self) -> None:
        import os as _os
        with patch(f"{_MOD}.detect_pending_migration",
                   return_value=_preflight(True, 1)), \
             patch(f"{_MOD}.establish_verified_service",
                   return_value=_ready("http://svc:9000")), \
             patch.dict(_os.environ, {}, clear=False), \
             patch("nexus.commands.migrate_cmd._run_migration") as mig:
            _os.environ.pop("NX_SERVICE_TOKEN", None)
            result = CliRunner().invoke(
                guided_upgrade_cmd, ["--yes", "--service-url", "http://svc:9000"]
            )
        assert result.exit_code == 1
        assert "NX_SERVICE_TOKEN" in result.output
        mig.assert_not_called()

    def test_storage_service_start_error_renders_remedy(self) -> None:
        from nexus.daemon.storage_service_daemon import StorageServiceStartError
        with patch(f"{_MOD}.detect_pending_migration",
                   return_value=_preflight(True, 1)), \
             patch(f"{_MOD}.establish_verified_service",
                   side_effect=StorageServiceStartError("no native binary available")), \
             patch("nexus.commands.migrate_cmd._run_migration") as mig:
            result = CliRunner().invoke(guided_upgrade_cmd, ["--yes"])
        assert result.exit_code == 1
        assert "Could not start the storage service" in result.output
        assert "no native binary available" in result.output
        mig.assert_not_called()

    def test_migration_block_relays_rollback_offer_and_nonzero(self) -> None:
        # _run_migration emits the migrated-failed + rollback offer to stderr,
        # then raises SystemExit(1). The command must let BOTH the output and the
        # exit code through (not swallow the relay). Simulate that realistically.
        import click as _click

        def _block(*_a, **_k):  # noqa: ANN002, ANN003
            _click.echo("Migration completed the copy but FAILED validation:", err=True)
            _click.echo("    nx storage migrate vectors --rollback", err=True)
            raise SystemExit(1)

        with patch(f"{_MOD}.detect_pending_migration",
                   return_value=_preflight(True, 1)), \
             patch(f"{_MOD}.establish_verified_service", return_value=_ready()), \
             patch("nexus.db.pg_provision.load_service_credentials_into_env",
                   return_value=True), \
             patch("nexus.commands.migrate_cmd._run_migration", side_effect=_block):
            result = CliRunner().invoke(guided_upgrade_cmd, ["--yes"])
        assert result.exit_code == 1
        # nexus-nb7hr secondary finding 2: a blocked guided run must print
        # the sentinel-recovery step (previously only migrate-to-service's
        # own failure text mentioned it).
        assert "nx migration --clear-state" in result.output
        assert "nx storage migrate vectors --rollback" in result.output
        assert "FAILED validation" in result.output

    def test_voyage_footprint_incapable_service_fails_before_migrating(self) -> None:
        with patch(f"{_MOD}.detect_pending_migration",
                   return_value=_preflight_voyage()), \
             patch(f"{_MOD}.establish_verified_service", return_value=_ready()), \
             patch(f"{_MOD}.verify_voyage_capability",
                   return_value=VoyageCapabilityOutcome(
                       ok=False, reason="target service embeds with ['bge-base-en-v15-768']")), \
             patch("nexus.db.pg_provision.load_service_credentials_into_env",
                   return_value=True), \
             patch("nexus.commands.migrate_cmd._run_migration") as mig:
            result = CliRunner().invoke(guided_upgrade_cmd, ["--yes"])
        assert result.exit_code == 1
        assert "cannot serve voyage" in result.output
        mig.assert_not_called()

    def test_voyage_footprint_capable_service_proceeds(self) -> None:
        with patch(f"{_MOD}.detect_pending_migration",
                   return_value=_preflight_voyage()), \
             patch(f"{_MOD}.establish_verified_service", return_value=_ready()), \
             patch(f"{_MOD}.verify_voyage_capability",
                   return_value=VoyageCapabilityOutcome(ok=True, reason=None)) as cap, \
             patch("nexus.db.pg_provision.load_service_credentials_into_env",
                   return_value=True), \
             patch("nexus.commands.migrate_cmd._run_migration") as mig:
            result = CliRunner().invoke(guided_upgrade_cmd, ["--yes"])
        assert result.exit_code == 0, result.output
        cap.assert_called_once()
        mig.assert_called_once()

    def test_service_url_voyage_footprint_incapable_fails(self) -> None:
        # The --service-url path runs the SAME voyage gate (it is not gated on
        # service_url) — exercise the combination explicitly (CR-L1).
        import os as _os
        with patch(f"{_MOD}.detect_pending_migration",
                   return_value=_preflight_voyage()), \
             patch(f"{_MOD}.establish_verified_service",
                   return_value=_ready("http://svc:9000")), \
             patch(f"{_MOD}.verify_voyage_capability",
                   return_value=VoyageCapabilityOutcome(
                       ok=False, reason="target service embeds with ['bge-base-en-v15-768']")), \
             patch.dict(_os.environ, {"NX_SERVICE_TOKEN": "tok"}, clear=False), \
             patch("nexus.commands.migrate_cmd._run_migration") as mig:
            result = CliRunner().invoke(
                guided_upgrade_cmd, ["--yes", "--service-url", "http://svc:9000"])
        assert result.exit_code == 1
        assert "cannot serve voyage" in result.output
        mig.assert_not_called()

    def test_no_voyage_footprint_skips_capability_check(self) -> None:
        with patch(f"{_MOD}.detect_pending_migration",
                   return_value=_preflight(True, 2)), \
             patch(f"{_MOD}.establish_verified_service", return_value=_ready()), \
             patch(f"{_MOD}.verify_voyage_capability") as cap, \
             patch("nexus.db.pg_provision.load_service_credentials_into_env",
                   return_value=True), \
             patch("nexus.commands.migrate_cmd._run_migration"):
            result = CliRunner().invoke(guided_upgrade_cmd, ["--yes"])
        assert result.exit_code == 0, result.output
        cap.assert_not_called()  # no voyage collections -> no capability probe

    def test_missing_token_after_provision_fails_before_migrating(self) -> None:
        # The one-command flow self-loads pg_credentials; if no token is available
        # afterwards, hard-fail BEFORE handing off (the manual path's source step
        # has no equivalent here).
        with patch(f"{_MOD}.detect_pending_migration",
                   return_value=_preflight(True, 1)), \
             patch(f"{_MOD}.establish_verified_service", return_value=_ready()), \
             patch("nexus.db.pg_provision.load_service_credentials_into_env",
                   return_value=False), \
             patch("nexus.commands.migrate_cmd._run_migration") as mig:
            result = CliRunner().invoke(guided_upgrade_cmd, ["--yes"])
        assert result.exit_code == 1
        assert "NX_SERVICE_TOKEN" in result.output
        mig.assert_not_called()

    def test_bad_service_url_is_rejected_before_migrating(self) -> None:
        with patch(f"{_MOD}.detect_pending_migration",
                   return_value=_preflight(True, 1)), \
             patch(f"{_MOD}.establish_verified_service") as est, \
             patch("nexus.commands.migrate_cmd._run_migration") as mig:
            result = CliRunner().invoke(
                guided_upgrade_cmd, ["--yes", "--service-url", "svc:9000"]
            )
        assert result.exit_code != 0
        assert "service-url" in result.output.lower()
        est.assert_not_called()
        mig.assert_not_called()


class TestAlreadyMigratedWiring:
    """RDR-178 Gap 7 (nexus-1sx01): already-migrated T2 stores are echoed and
    threaded through to ``_run_migration`` as ``skip_t2_stores``."""

    def _plan(self, *, skip: frozenset[str]) -> AlreadyMigratedPlan:
        return AlreadyMigratedPlan(
            statuses=tuple(
                StoreMigrationStatus(
                    s, s in skip,
                    f"{s}: already migrated 2026-07-01, no newer local writes"
                    if s in skip else f"{s}: no migration report found — will migrate",
                )
                for s in LADDER_ORDER
            )
        )

    def test_skip_stores_are_echoed_and_forwarded(self) -> None:
        plan = self._plan(skip=frozenset({"memory", "plans"}))
        with patch(f"{_MOD}.detect_pending_migration",
                   return_value=_preflight(True, 2)), \
             patch(f"{_MOD}.detect_already_migrated", return_value=plan) as det, \
             patch(f"{_MOD}.establish_verified_service", return_value=_ready()), \
             patch("nexus.db.pg_provision.load_service_credentials_into_env",
                   return_value=True), \
             patch("nexus.commands.migrate_cmd._run_migration") as mig:
            result = CliRunner().invoke(guided_upgrade_cmd, ["--yes"])
        assert result.exit_code == 0, result.output
        assert "already migrated 2026-07-01" in result.output
        det.assert_called_once()
        assert det.call_args.kwargs["force"] is False
        mig.assert_called_once_with(
            None, None, None, "http://127.0.0.1:8099",
            skip_t2_stores=frozenset({"memory", "plans"}),
        )

    def test_nothing_skipped_reproduces_the_exact_prior_call_shape(self) -> None:
        plan = self._plan(skip=frozenset())
        with patch(f"{_MOD}.detect_pending_migration",
                   return_value=_preflight(True, 2)), \
             patch(f"{_MOD}.detect_already_migrated", return_value=plan), \
             patch(f"{_MOD}.establish_verified_service", return_value=_ready()), \
             patch("nexus.db.pg_provision.load_service_credentials_into_env",
                   return_value=True), \
             patch("nexus.commands.migrate_cmd._run_migration") as mig:
            result = CliRunner().invoke(guided_upgrade_cmd, ["--yes"])
        assert result.exit_code == 0, result.output
        assert "already migrated" not in result.output
        # No skip_t2_stores kwarg at all when nothing is skipped.
        mig.assert_called_once_with(None, None, None, "http://127.0.0.1:8099")

    def test_force_flag_is_forwarded_to_detection(self) -> None:
        plan = self._plan(skip=frozenset())
        with patch(f"{_MOD}.detect_pending_migration",
                   return_value=_preflight(True, 2)), \
             patch(f"{_MOD}.detect_already_migrated", return_value=plan) as det, \
             patch(f"{_MOD}.establish_verified_service", return_value=_ready()), \
             patch("nexus.db.pg_provision.load_service_credentials_into_env",
                   return_value=True), \
             patch("nexus.commands.migrate_cmd._run_migration"):
            result = CliRunner().invoke(guided_upgrade_cmd, ["--yes", "--force"])
        assert result.exit_code == 0, result.output
        det.assert_called_once()
        assert det.call_args.kwargs["force"] is True

    def test_all_skipped_still_migrates_for_the_t3_leg(self) -> None:
        # T3 (Chroma) is out of scope for this bead (nexus-s3dd4, Wave 2) —
        # even when every T2 store is covered, the command still proceeds
        # because `pre.needs_migration` is True (Chroma data still present).
        plan = self._plan(skip=frozenset(LADDER_ORDER))
        with patch(f"{_MOD}.detect_pending_migration",
                   return_value=_preflight(True, 2)), \
             patch(f"{_MOD}.detect_already_migrated", return_value=plan), \
             patch(f"{_MOD}.establish_verified_service", return_value=_ready()) as est, \
             patch("nexus.db.pg_provision.load_service_credentials_into_env",
                   return_value=True), \
             patch("nexus.commands.migrate_cmd._run_migration") as mig:
            result = CliRunner().invoke(guided_upgrade_cmd, ["--yes"])
        assert result.exit_code == 0, result.output
        est.assert_called_once()  # still provisions — T3 leg is untouched by this bead
        mig.assert_called_once_with(
            None, None, None, "http://127.0.0.1:8099",
            skip_t2_stores=frozenset(LADDER_ORDER),
        )
