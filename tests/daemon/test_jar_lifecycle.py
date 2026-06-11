# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bead nexus-pebfx.4 — JAR lifecycle for installed users.

(a) well-known JAR location + ``nx daemon service install-jar`` + discovery
order; (b) schema-skew gate: the supervisor refuses to start a JAR older
than the database schema it would connect to.

Hit 2026-06-10: pip/uv-installed users have no ``service/target`` — JAR
auto-discovery was repo-relative only, forcing the ``--jar`` workaround.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from nexus.daemon.jar_lifecycle import (
    applied_changesets_via_psql,
    check_schema_skew,
    extract_jar_provenance,
    install_jar,
    read_installed_provenance,
    sidecar_path,
    well_known_jar_path,
)
from nexus.daemon.storage_service_daemon import (
    StorageServiceStartError,
    _find_service_jar,
)

_CHANGELOG_A = """<?xml version="1.0" encoding="UTF-8"?>
<databaseChangeLog xmlns="http://www.liquibase.org/xml/ns/dbchangelog">
    <changeSet id="vectors-001" author="hal">
        <sql>CREATE TABLE t (i int)</sql>
    </changeSet>
    <changeSet id="vectors-002" author="hal">
        <sql>ALTER TABLE t ADD COLUMN j int</sql>
    </changeSet>
</databaseChangeLog>
"""

_CHANGELOG_B = """<?xml version="1.0" encoding="UTF-8"?>
<databaseChangeLog xmlns="http://www.liquibase.org/xml/ns/dbchangelog">
    <changeSet author="liam" id="catalog-001">
        <sql>CREATE TABLE c (i int)</sql>
    </changeSet>
</databaseChangeLog>
"""


def _make_fake_jar(
    path: Path,
    version: str = "1.0-SNAPSHOT",
    changelogs: dict[str, str] | None = None,
) -> Path:
    """Build a minimal JAR shaped like the real fat JAR (pom.properties +
    bundled db/changelog/*.xml)."""
    if changelogs is None:
        changelogs = {"a.xml": _CHANGELOG_A, "b.xml": _CHANGELOG_B}
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "META-INF/MANIFEST.MF",
            "Manifest-Version: 1.0\nMain-Class: dev.nexus.service.Main\n",
        )
        zf.writestr(
            "META-INF/maven/dev.nexus/nexus-service/pom.properties",
            f"artifactId=nexus-service\ngroupId=dev.nexus\nversion={version}\n",
        )
        # The maven-shade fat JAR merges every DEPENDENCY's pom.properties
        # too (21 in the real JAR). A generic matcher is last-write-wins and
        # records a dep's version — the 2026-06-10 critic Critical. Keep the
        # fixture faithful so the regression stays visible.
        zf.writestr(
            "META-INF/maven/org.apache.commons/commons-compress/pom.properties",
            "artifactId=commons-compress\ngroupId=org.apache.commons\nversion=1.24.0\n",
        )
        zf.writestr(
            "META-INF/maven/org.postgresql/postgresql/pom.properties",
            "artifactId=postgresql\ngroupId=org.postgresql\nversion=42.7.2\n",
        )
        for name, content in changelogs.items():
            zf.writestr(f"db/changelog/{name}", content)
    return path


class TestExtractJarProvenance:
    def test_extracts_version_sha_and_changesets(self, tmp_path: Path) -> None:
        jar = _make_fake_jar(tmp_path / "svc.jar", version="2.3.4")
        prov = extract_jar_provenance(jar)
        assert prov["version"] == "2.3.4"
        assert len(prov["sha256"]) == 64
        assert prov["size_bytes"] == jar.stat().st_size
        assert {(c["id"], c["author"]) for c in prov["changesets"]} == {
            ("vectors-001", "hal"),
            ("vectors-002", "hal"),
            ("catalog-001", "liam"),
        }

    def test_attribute_order_does_not_matter(self, tmp_path: Path) -> None:
        # _CHANGELOG_B uses author-before-id attribute order.
        jar = _make_fake_jar(
            tmp_path / "svc.jar", changelogs={"b.xml": _CHANGELOG_B},
        )
        prov = extract_jar_provenance(jar)
        assert {(c["id"], c["author"]) for c in prov["changesets"]} == {
            ("catalog-001", "liam"),
        }

    def test_missing_pom_properties_falls_back_to_unknown(self, tmp_path: Path) -> None:
        jar = tmp_path / "bare.jar"
        with zipfile.ZipFile(jar, "w") as zf:
            zf.writestr("db/changelog/a.xml", _CHANGELOG_A)
        prov = extract_jar_provenance(jar)
        assert prov["version"] == "unknown"

    def test_non_jar_fails_loud(self, tmp_path: Path) -> None:
        not_jar = tmp_path / "x.jar"
        not_jar.write_text("not a zip")
        with pytest.raises(StorageServiceStartError, match="not a valid JAR"):
            extract_jar_provenance(not_jar)


class TestInstallJar:
    def test_installs_to_well_known_location_with_sidecar(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "cfg"
        (tmp_path / "build").mkdir()
        jar = _make_fake_jar(tmp_path / "build" / "svc.jar")

        dest, prov = install_jar(jar, config_dir, installed_by="conexus 9.9.9")

        assert dest == well_known_jar_path(config_dir)
        assert dest.is_file()
        sidecar = json.loads(sidecar_path(config_dir).read_text())
        assert sidecar["sha256"] == prov["sha256"]
        assert sidecar["installed_by"] == "conexus 9.9.9"
        assert sidecar["source_path"] == str(jar)
        assert sidecar["changesets"]

    def test_refuses_jar_without_changelogs(self, tmp_path: Path) -> None:
        # A JAR with no bundled changelog is not a nexus-service fat JAR —
        # installing it would disable the schema-skew gate silently.
        config_dir = tmp_path / "cfg"
        jar = tmp_path / "empty.jar"
        with zipfile.ZipFile(jar, "w") as zf:
            zf.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
        with pytest.raises(StorageServiceStartError, match="no bundled db/changelog"):
            install_jar(jar, config_dir)
        assert not well_known_jar_path(config_dir).exists()

    def test_reinstall_overwrites_atomically(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "cfg"
        old = _make_fake_jar(tmp_path / "old.jar", version="1.0")
        new = _make_fake_jar(tmp_path / "new.jar", version="2.0")
        install_jar(old, config_dir)
        install_jar(new, config_dir)
        assert read_installed_provenance(config_dir)["version"] == "2.0"

    def test_read_provenance_none_when_absent(self, tmp_path: Path) -> None:
        assert read_installed_provenance(tmp_path / "cfg") is None


class TestDiscoveryOrder:
    """--jar (caller) > NEXUS_SERVICE_JAR env > well-known > repo-relative."""

    def test_well_known_wins_when_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config_dir = tmp_path / "cfg"
        jar = _make_fake_jar(tmp_path / "svc.jar")
        install_jar(jar, config_dir)
        monkeypatch.delenv("NEXUS_SERVICE_JAR", raising=False)
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(config_dir))
        assert _find_service_jar() == well_known_jar_path(config_dir)

    def test_env_override_beats_well_known(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config_dir = tmp_path / "cfg"
        install_jar(_make_fake_jar(tmp_path / "wk.jar"), config_dir)
        explicit = _make_fake_jar(tmp_path / "explicit.jar")
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(config_dir))
        monkeypatch.setenv("NEXUS_SERVICE_JAR", str(explicit))
        assert _find_service_jar() == explicit

    def test_fail_message_names_install_jar(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("NEXUS_SERVICE_JAR", raising=False)
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "empty-cfg"))
        with patch(
            "nexus.daemon.storage_service_daemon.glob.glob", return_value=[],
        ), pytest.raises(StorageServiceStartError, match="install-jar"):
            _find_service_jar()


class TestSchemaSkewGate:
    """The supervisor must refuse a JAR older than the applied schema:
    Liquibase silently ignores applied changesets it does not know, so an
    old JAR starts cleanly against a newer schema and fails undiagnosably
    at runtime. Refusal requires positive evidence (applied ⊄ bundled);
    an indeterminate applied-set (psql failure) logs and proceeds."""

    _CREDS = {
        "PG_PORT": "5499", "NX_DB_USER": "svc", "NX_DB_PASS": "pw",
        "NX_DB_URL": "jdbc:postgresql://127.0.0.1:5499/nexus",
    }

    def test_jar_matching_schema_passes(self, tmp_path: Path) -> None:
        jar = _make_fake_jar(tmp_path / "svc.jar")
        applied = {("vectors-001", "hal"), ("catalog-001", "liam")}
        with patch(
            "nexus.daemon.jar_lifecycle.applied_changesets_via_psql",
            return_value=applied,
        ):
            check_schema_skew(jar, self._CREDS)  # must not raise

    def test_jar_newer_than_schema_passes(self, tmp_path: Path) -> None:
        jar = _make_fake_jar(tmp_path / "svc.jar")
        with patch(
            "nexus.daemon.jar_lifecycle.applied_changesets_via_psql",
            return_value=set(),
        ):
            check_schema_skew(jar, self._CREDS)  # fresh DB: any JAR ok

    def test_jar_older_than_schema_refused(self, tmp_path: Path) -> None:
        jar = _make_fake_jar(tmp_path / "svc.jar")
        applied = {
            ("vectors-001", "hal"),
            ("vectors-099-future", "hal"),  # applied but unknown to this JAR
        }
        with patch(
            "nexus.daemon.jar_lifecycle.applied_changesets_via_psql",
            return_value=applied,
        ), pytest.raises(StorageServiceStartError, match="vectors-099-future"):
            check_schema_skew(jar, self._CREDS)

    def test_indeterminate_applied_set_proceeds(self, tmp_path: Path) -> None:
        jar = _make_fake_jar(tmp_path / "svc.jar")
        with patch(
            "nexus.daemon.jar_lifecycle.applied_changesets_via_psql",
            return_value=None,
        ):
            check_schema_skew(jar, self._CREDS)  # fail-open, logged

    def test_psql_output_parsing(self) -> None:
        fake = type("R", (), {
            "returncode": 0,
            "stdout": "vectors-001\thal\ncatalog-001\tliam\n\n",
            "stderr": "",
        })()
        with patch("nexus.daemon.jar_lifecycle.subprocess.run", return_value=fake), \
             patch(
                 "nexus.daemon.jar_lifecycle._psql_bin",
                 return_value="/fake/psql",
             ):
            applied = applied_changesets_via_psql(self._CREDS)
        assert applied == {("vectors-001", "hal"), ("catalog-001", "liam")}

    def test_psql_missing_table_is_empty_set(self) -> None:
        fake = type("R", (), {
            "returncode": 1,
            "stdout": "",
            "stderr": 'ERROR:  relation "databasechangelog" does not exist',
        })()
        with patch("nexus.daemon.jar_lifecycle.subprocess.run", return_value=fake), \
             patch(
                 "nexus.daemon.jar_lifecycle._psql_bin",
                 return_value="/fake/psql",
             ):
            assert applied_changesets_via_psql(self._CREDS) == set()

    def test_psql_connection_failure_is_none(self) -> None:
        fake = type("R", (), {
            "returncode": 2,
            "stdout": "",
            "stderr": "psql: error: connection refused",
        })()
        with patch("nexus.daemon.jar_lifecycle.subprocess.run", return_value=fake), \
             patch(
                 "nexus.daemon.jar_lifecycle._psql_bin",
                 return_value="/fake/psql",
             ):
            assert applied_changesets_via_psql(self._CREDS) is None


class TestInstallJarCli:
    def test_install_and_discovery_roundtrip(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from nexus.cli import main

        config_dir = tmp_path / "cfg"
        jar = _make_fake_jar(tmp_path / "svc.jar", version="3.1.4")
        runner = CliRunner()
        result = runner.invoke(main, [
            "daemon", "service", "install-jar", str(jar),
            "--config-dir", str(config_dir),
        ])
        assert result.exit_code == 0, result.output
        assert "3.1.4" in result.output
        assert well_known_jar_path(config_dir).is_file()
        prov = read_installed_provenance(config_dir)
        assert prov["version"] == "3.1.4"
        assert prov["installed_by"].startswith("conexus ")

    def test_requires_exactly_one_source(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from nexus.cli import main

        runner = CliRunner()
        neither = runner.invoke(main, ["daemon", "service", "install-jar"])
        assert neither.exit_code != 0
        both = runner.invoke(main, [
            "daemon", "service", "install-jar", "/x.jar", "--from-repo",
        ])
        assert both.exit_code != 0

    def test_garbage_jar_fails_loud(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from nexus.cli import main

        bad = tmp_path / "bad.jar"
        bad.write_text("nope")
        runner = CliRunner()
        result = runner.invoke(main, [
            "daemon", "service", "install-jar", str(bad),
            "--config-dir", str(tmp_path / "cfg"),
        ])
        assert result.exit_code == 2
        assert "not a valid JAR" in result.output


class TestStatusVersionHandshake:
    """`nx daemon service status` surfaces /version and warns on a stale
    running JAR (running app_version != installed sidecar version)."""

    def _invoke_status(self, tmp_path: Path, svc_version: dict | None):
        from unittest.mock import MagicMock

        from click.testing import CliRunner

        from nexus.cli import main

        config_dir = tmp_path / "cfg"
        config_dir.mkdir(parents=True, exist_ok=True)
        record = MagicMock()
        record.endpoint = {"host": "127.0.0.1", "port": 5999, "pid": 1234}
        record.generation = 1
        record.version = "5.10.6"
        record.heartbeat_epoch = 0.0
        record.status = "live"
        with patch(
            "nexus.daemon.service_registry.ServiceRegistry.discover",
            return_value=record,
        ), patch(
            "nexus.daemon.jar_lifecycle.fetch_service_version",
            return_value=svc_version,
        ):
            return CliRunner().invoke(main, [
                "daemon", "service", "status", "--config-dir", str(config_dir),
            ]), config_dir

    def test_status_shows_running_versions(self, tmp_path: Path) -> None:
        result, _ = self._invoke_status(tmp_path, {
            "app_version": "1.0-SNAPSHOT",
            "schema_latest_id": "vectors-002",
            "schema_changeset_count": 64,
        })
        assert result.exit_code == 0, result.output
        assert "1.0-SNAPSHOT" in result.output
        assert "vectors-002" in result.output
        assert "warning" not in result.output

    def test_status_warns_on_stale_running_jar(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "cfg"
        install_jar(_make_fake_jar(tmp_path / "new.jar", version="2.0"), config_dir)
        result, _ = self._invoke_status(tmp_path, {
            "app_version": "1.0-SNAPSHOT",
            "schema_latest_id": "x",
            "schema_changeset_count": 1,
        })
        assert result.exit_code == 0, result.output
        assert "warning" in result.output
        assert "restart" in result.output

    def test_status_tolerates_version_endpoint_absent(self, tmp_path: Path) -> None:
        # Older JAR without /version: status still works, no version fields.
        result, _ = self._invoke_status(tmp_path, None)
        assert result.exit_code == 0, result.output
        assert "service_app_version" not in result.output


class TestProvenanceReviewRegressions:
    """2026-06-10 stacked-review regressions on extract_jar_provenance and
    the gate's credential choice."""

    def test_version_pinned_to_service_artifact_not_dependency(
        self, tmp_path: Path,
    ) -> None:
        """Critic CRITICAL: the fat JAR merges ~21 dependency pom.properties;
        a generic matcher records a random dep's version (commons-compress
        1.24.0 beat 1.0-SNAPSHOT live). Only dev.nexus may win — regardless
        of zip member order."""
        jar = tmp_path / "svc.jar"
        with zipfile.ZipFile(jar, "w") as zf:
            # Dependency FIRST and LAST so any order-dependent scan fails.
            zf.writestr(
                "META-INF/maven/org.apache.commons/commons-compress/pom.properties",
                "version=1.24.0\n",
            )
            zf.writestr(
                "META-INF/maven/dev.nexus/nexus-service/pom.properties",
                "version=7.7.7\n",
            )
            zf.writestr(
                "META-INF/maven/org.postgresql/postgresql/pom.properties",
                "version=42.7.2\n",
            )
            zf.writestr("db/changelog/a.xml", _CHANGELOG_A)
        assert extract_jar_provenance(jar)["version"] == "7.7.7"

    def test_changeset_inside_xml_comment_ignored(self, tmp_path: Path) -> None:
        """CRE M1: a literal <changeSet> inside an XML comment must not
        inject a phantom changeset into the bundled set."""
        commented = """<?xml version="1.0"?>
<databaseChangeLog>
    <!-- Replaces <changeSet id="ghost-001" author="hal"> from before -->
    <changeSet id="real-001" author="hal">
        <sql>SELECT 1</sql>
    </changeSet>
</databaseChangeLog>
"""
        jar = _make_fake_jar(tmp_path / "svc.jar", changelogs={"c.xml": commented})
        ids = {(c["id"], c["author"])
               for c in extract_jar_provenance(jar)["changesets"]}
        assert ids == {("real-001", "hal")}

    def test_gate_prefers_admin_credentials(self) -> None:
        """Critic Significant 1 / CRE M2: the journal tables are owned by the
        migration role; with svc creds the gate is blind for exactly the
        first upgrade start (grants-002 not yet applied). Admin creds make
        it effective from run one."""
        creds = {
            "PG_PORT": "5499",
            "NX_DB_USER": "svc", "NX_DB_PASS": "svc-pw",
            "NX_DB_ADMIN_USER": "nexus_admin", "NX_DB_ADMIN_PASS": "admin-pw",
            "NX_DB_URL": "jdbc:postgresql://127.0.0.1:5499/nexus",
        }
        captured = {}

        def fake_run(cmd, env=None, **kw):
            captured["cmd"] = cmd
            captured["pgpassword"] = env.get("PGPASSWORD")
            return type("R", (), {
                "returncode": 0, "stdout": "", "stderr": "",
            })()

        with patch("nexus.daemon.jar_lifecycle.subprocess.run", side_effect=fake_run), \
             patch("nexus.daemon.jar_lifecycle._psql_bin", return_value="/fake/psql"):
            applied_changesets_via_psql(creds)
        assert "nexus_admin" in captured["cmd"]
        assert captured["pgpassword"] == "admin-pw"

    def test_gate_falls_back_to_svc_credentials(self) -> None:
        creds = {
            "PG_PORT": "5499",
            "NX_DB_USER": "svc", "NX_DB_PASS": "svc-pw",
            "NX_DB_URL": "jdbc:postgresql://127.0.0.1:5499/nexus",
        }
        captured = {}

        def fake_run(cmd, env=None, **kw):
            captured["cmd"] = cmd
            captured["pgpassword"] = env.get("PGPASSWORD")
            return type("R", (), {
                "returncode": 0, "stdout": "", "stderr": "",
            })()

        with patch("nexus.daemon.jar_lifecycle.subprocess.run", side_effect=fake_run), \
             patch("nexus.daemon.jar_lifecycle._psql_bin", return_value="/fake/psql"):
            applied_changesets_via_psql(creds)
        assert "svc" in captured["cmd"]
        assert captured["pgpassword"] == "svc-pw"

    def test_permission_denied_is_indeterminate_none(self) -> None:
        """CRE M2 explicit pin: permission denied (not 'does not exist') is
        INDETERMINATE — fail-open with a warning, never an empty applied set
        (an empty set would falsely declare the JAR compatible)."""
        fake = type("R", (), {
            "returncode": 1,
            "stdout": "",
            "stderr": "ERROR:  permission denied for table databasechangelog",
        })()
        creds = {
            "PG_PORT": "5499", "NX_DB_USER": "svc", "NX_DB_PASS": "pw",
            "NX_DB_URL": "jdbc:postgresql://127.0.0.1:5499/nexus",
        }
        with patch("nexus.daemon.jar_lifecycle.subprocess.run", return_value=fake), \
             patch("nexus.daemon.jar_lifecycle._psql_bin", return_value="/fake/psql"):
            assert applied_changesets_via_psql(creds) is None


class TestStatusNxMajorGapNote:
    def test_older_major_notes(self, monkeypatch) -> None:
        from nexus.commands.daemon import _nx_major_gap_note

        with patch("importlib.metadata.version", return_value="5.10.6"):
            note = _nx_major_gap_note("conexus 4.34.1")
        assert note is not None and "4.34.1" in note

    def test_same_major_silent(self) -> None:
        from nexus.commands.daemon import _nx_major_gap_note

        with patch("importlib.metadata.version", return_value="5.10.6"):
            assert _nx_major_gap_note("conexus 5.2.0") is None

    def test_unparseable_silent(self) -> None:
        from nexus.commands.daemon import _nx_major_gap_note

        assert _nx_major_gap_note("") is None
        assert _nx_major_gap_note("hand-rolled") is None
