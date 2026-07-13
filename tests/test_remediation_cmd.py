# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-182 P4 (nexus-ykzbj.14/.15): the ``nx forensics`` / ``nx remediate``
CLI surface + the durable-flag revocation audit.

Consent taxonomy under test:
- ``nx forensics``: display-only, UNGATED — never reads the durable flag
  (a human typing the command is the consent act).
- ``nx remediate``: display ungated; the RELEASE is a per-invocation
  interactive confirm (aborts non-interactive — a script cannot consent);
  accepted confirm audit-records BEFORE the playbook prints, fail-closed.
- ``nx config set claude_assisted_remediation.enabled <v>``: writes a
  grant/revoke audit row (best-effort — the flag write itself never blocks).
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest
from click.testing import CliRunner

from nexus.commands import remediation_cmd
from nexus.commands.remediation_cmd import forensics_cmd, remediate_cmd

_URL = "https://github.com/Hellblazer/nexus/blob/main/docs/migration-runbook.md"


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def isolated_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg))
    monkeypatch.chdir(tmp_path)
    return cfg


class _ConsentRecorder:
    def __init__(self):
        self.rows: list[dict] = []

    def record_consent(self, *, scope: str, ts: str, granted: bool) -> None:
        self.rows.append({"scope": scope, "ts": ts, "granted": granted})


@pytest.fixture()
def consent_recorder(monkeypatch):
    recorder = _ConsentRecorder()

    class _Db:
        telemetry = recorder

    @contextmanager
    def _ctx():
        yield _Db()

    import nexus.commands._helpers as helpers

    monkeypatch.setattr(helpers, "t2_handle", _ctx)
    return recorder


@pytest.fixture()
def no_diag(monkeypatch):
    monkeypatch.setattr(
        remediation_cmd, "_live_detail",
        lambda sql: "live diagnostics stub (test)",
    )


# ── nx forensics: display-only, ungated ─────────────────────────────────────

def _enable_flag(cfg):
    (cfg / "config.yml").write_text(
        "claude_assisted_remediation:\n  enabled: true\n"
    )


class TestForensicsCmd:
    def test_playbook_text_prints_ungated(self, runner, isolated_config, no_diag):
        """The guidance TEXT is display-only and ungated — prints with no
        flag set (the human-typed-command-is-consent taxonomy)."""
        result = runner.invoke(forensics_cmd, ["chash-poison"])
        assert result.exit_code == 0, result.output
        assert "[chash-poison]" in result.output
        assert _URL in result.output

    def test_live_diagnostics_gated_off_by_default(
        self, runner, isolated_config, monkeypatch
    ):
        """Taxonomy amendment (critic-p4): flag OFF -> the credentialed live
        probe does NOT run; the opt-in note appears instead. Mechanical: the
        _live_detail seam is never called."""
        touched: list = []
        monkeypatch.setattr(
            remediation_cmd, "_live_detail",
            lambda sql: touched.append("probe") or "LIVE COUNTS",
        )
        result = runner.invoke(forensics_cmd, ["chash-poison"])
        assert result.exit_code == 0, result.output
        assert touched == []  # the credentialed probe never ran
        assert "opt-in" in result.output
        assert "LIVE COUNTS" not in result.output
        assert "[chash-poison]" in result.output  # text still ungated

    def test_live_diagnostics_run_when_flag_enabled(
        self, runner, isolated_config, monkeypatch
    ):
        """Flag ON (global config.yml) -> the live probe runs and its counts
        embed. Confirms the CLI shares the same global-only reader as MCP."""
        _enable_flag(isolated_config)
        touched: list = []
        monkeypatch.setattr(
            remediation_cmd, "_live_detail",
            lambda sql: touched.append("probe") or "LIVE COUNTS",
        )
        result = runner.invoke(forensics_cmd, ["chash-poison"])
        assert result.exit_code == 0, result.output
        assert touched == ["probe"]
        assert "LIVE COUNTS" in result.output

    def test_repo_local_nexus_yml_cannot_enable_the_probe(
        self, runner, isolated_config, monkeypatch
    ):
        """The CLI live-diag leg shares the MCP gate's global-only provenance:
        a cwd .nexus.yml (git-pullable) must NOT unlock the credentialed probe."""
        from pathlib import Path

        Path(".nexus.yml").write_text(
            "claude_assisted_remediation:\n  enabled: true\n"
        )
        touched: list = []
        monkeypatch.setattr(
            remediation_cmd, "_live_detail",
            lambda sql: touched.append("probe") or "LIVE COUNTS",
        )
        result = runner.invoke(forensics_cmd, ["chash-poison"])
        assert result.exit_code == 0, result.output
        assert touched == []  # .nexus.yml does not unlock the probe
        assert "opt-in" in result.output

    def test_unknown_topic_fails_loud(self, runner, isolated_config):
        result = runner.invoke(forensics_cmd, ["nope"])
        assert result.exit_code != 0
        assert "unknown forensics topic" in result.output
        assert "chash-poison" in result.output


# ── nx remediate: interactive consent, audited release ──────────────────────

class TestRemediateCmd:
    def test_declined_confirm_is_safe(self, runner, isolated_config, no_diag, consent_recorder):
        result = runner.invoke(remediate_cmd, ["chash-poison"], input="n\n")
        assert result.exit_code == 0, result.output
        assert "Declined" in result.output
        assert consent_recorder.rows == []
        assert _URL in result.output  # runbook URL remains on screen
        # The recovery steps were NOT released:
        assert "roll back the poisoned pgvector target" not in result.output

    def test_accepted_confirm_records_consent_and_releases(
        self, runner, isolated_config, no_diag, consent_recorder
    ):
        result = runner.invoke(remediate_cmd, ["chash-poison"], input="y\n")
        assert result.exit_code == 0, result.output
        assert len(consent_recorder.rows) == 1
        row = consent_recorder.rows[0]
        assert row["scope"] == "remediate:chash-poison"
        assert row["granted"] is True
        assert "roll back the poisoned pgvector target" in result.output

    def test_non_interactive_aborts_without_consent(
        self, runner, isolated_config, no_diag, consent_recorder
    ):
        """A script cannot consent: EOF on stdin aborts before any release."""
        result = runner.invoke(remediate_cmd, ["chash-poison"])  # no input
        assert result.exit_code != 0
        assert consent_recorder.rows == []
        assert "roll back the poisoned pgvector target" not in result.output

    def test_audit_unavailable_refuses_release(
        self, runner, isolated_config, no_diag, monkeypatch
    ):
        class _NoConsent:
            pass

        class _Db:
            telemetry = _NoConsent()

        @contextmanager
        def _ctx():
            yield _Db()

        import nexus.commands._helpers as helpers

        monkeypatch.setattr(helpers, "t2_handle", _ctx)
        result = runner.invoke(remediate_cmd, ["chash-poison"], input="y\n")
        assert result.exit_code != 0
        assert "nexus-ng2sy" in result.output
        assert "roll back the poisoned pgvector target" not in result.output

    def test_unknown_topic_fails_loud(self, runner, isolated_config, consent_recorder):
        result = runner.invoke(remediate_cmd, ["nope"], input="y\n")
        assert result.exit_code != 0
        assert "unknown remediate topic" in result.output
        assert consent_recorder.rows == []

    def test_generic_audit_failure_refuses_release(
        self, runner, isolated_config, no_diag, monkeypatch
    ):
        """(review-p4 Low-1) The CLI's generic except-Exception branch: a
        non-attribute audit failure (disk-full class) refuses the release
        with the contract's message — symmetric with the MCP tool's test."""
        class _Failing:
            def record_consent(self, *, scope, ts, granted):
                raise RuntimeError("disk full")

        class _Db:
            telemetry = _Failing()

        @contextmanager
        def _ctx():
            yield _Db()

        import nexus.commands._helpers as helpers

        monkeypatch.setattr(helpers, "t2_handle", _ctx)
        result = runner.invoke(remediate_cmd, ["chash-poison"], input="y\n")
        assert result.exit_code != 0
        assert "unaudited" in result.output.lower()
        assert "disk full" in result.output
        assert "roll back the poisoned pgvector target" not in result.output

    def test_history_prints_the_audit_trail(
        self, runner, isolated_config, monkeypatch
    ):
        """(review-p4 Low-2) The read surface has a real operator consumer:
        nx remediate --history prints grants and revokes in order."""
        class _Reader:
            def list_consents(self):
                return [
                    {"scope": "flag:claude_assisted_remediation",
                     "ts": "2026-07-13T00:00:00Z", "granted": True},
                    {"scope": "remediate:chash-poison",
                     "ts": "2026-07-13T00:01:00Z", "granted": True},
                    {"scope": "flag:claude_assisted_remediation",
                     "ts": "2026-07-13T00:02:00Z", "granted": False},
                ]

        class _Db:
            telemetry = _Reader()

        @contextmanager
        def _ctx():
            yield _Db()

        import nexus.commands._helpers as helpers

        monkeypatch.setattr(helpers, "t2_handle", _ctx)
        result = runner.invoke(remediate_cmd, ["--history"])
        assert result.exit_code == 0, result.output
        lines = [ln for ln in result.output.splitlines() if ln.strip()]
        assert lines == [
            "2026-07-13T00:00:00Z  GRANT   flag:claude_assisted_remediation",
            "2026-07-13T00:01:00Z  GRANT   remediate:chash-poison",
            "2026-07-13T00:02:00Z  REVOKE  flag:claude_assisted_remediation",
        ]

    def test_history_empty_trail(self, runner, isolated_config, monkeypatch):
        class _Reader:
            def list_consents(self):
                return []

        class _Db:
            telemetry = _Reader()

        @contextmanager
        def _ctx():
            yield _Db()

        import nexus.commands._helpers as helpers

        monkeypatch.setattr(helpers, "t2_handle", _ctx)
        result = runner.invoke(remediate_cmd, ["--history"])
        assert result.exit_code == 0
        assert "No consent events recorded." in result.output


# ── nx config set: durable-flag grant/revoke audit ───────────────────────────

class TestFlagConsentAudit:
    def _set(self, runner, key_value: str):
        from nexus.commands.config_cmd import config_group

        return runner.invoke(config_group, ["set", key_value])

    def test_enable_writes_grant_row(self, runner, isolated_config, consent_recorder):
        result = self._set(runner, "claude_assisted_remediation.enabled=true")
        assert result.exit_code == 0, result.output
        assert consent_recorder.rows == [
            {"scope": "flag:claude_assisted_remediation",
             "ts": consent_recorder.rows[0]["ts"], "granted": True}
        ]

    def test_disable_writes_revoke_row(self, runner, isolated_config, consent_recorder):
        result = self._set(runner, "claude_assisted_remediation.enabled=false")
        assert result.exit_code == 0, result.output
        assert len(consent_recorder.rows) == 1
        assert consent_recorder.rows[0]["granted"] is False

    def test_other_keys_write_no_row(self, runner, isolated_config, consent_recorder):
        result = self._set(runner, "pdf.extractor=mineru")
        assert result.exit_code == 0, result.output
        assert consent_recorder.rows == []

    def test_audit_failure_warns_but_flag_still_set(
        self, runner, isolated_config, monkeypatch
    ):
        @contextmanager
        def _boom():
            raise RuntimeError("no service")
            yield  # pragma: no cover

        import nexus.commands._helpers as helpers

        monkeypatch.setattr(helpers, "t2_handle", _boom)
        result = self._set(runner, "claude_assisted_remediation.enabled=true")
        assert result.exit_code == 0, result.output
        assert "WARNING: consent audit not recorded" in result.output
        assert (isolated_config / "config.yml").exists()
        assert "enabled" in (isolated_config / "config.yml").read_text()


# ── the read surface (real SQLite) ───────────────────────────────────────────

def test_list_consents_reads_back_in_order(tmp_path):
    from nexus.db.t2 import T2Database

    db = T2Database(tmp_path / "memory.db", run_migrations=True)
    db.telemetry.record_consent(
        scope="flag:claude_assisted_remediation", ts="2026-07-13T00:00:00Z",
        granted=True,
    )
    db.telemetry.record_consent(
        scope="remediate:chash-poison", ts="2026-07-13T00:01:00Z", granted=True,
    )
    db.telemetry.record_consent(
        scope="flag:claude_assisted_remediation", ts="2026-07-13T00:02:00Z",
        granted=False,
    )
    assert db.telemetry.list_consents() == [
        {"scope": "flag:claude_assisted_remediation",
         "ts": "2026-07-13T00:00:00Z", "granted": True},
        {"scope": "remediate:chash-poison",
         "ts": "2026-07-13T00:01:00Z", "granted": True},
        {"scope": "flag:claude_assisted_remediation",
         "ts": "2026-07-13T00:02:00Z", "granted": False},
    ]