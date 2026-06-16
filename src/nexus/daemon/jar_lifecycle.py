# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""JAR lifecycle for installed users (bead nexus-pebfx.4).

pip/uv-installed users have no ``service/target`` directory, so repo-relative
JAR auto-discovery cannot work for them (hit 2026-06-10; the workaround was
``--jar``). This module provides:

- the **well-known JAR location** ``<config_dir>/service/nexus-service.jar``
  that ``nx daemon service install-jar`` populates and supervisor discovery
  prefers over repo-relative paths;
- a **provenance sidecar** (version, sha256, build date, bundled Liquibase
  changesets) written at install time;
- the **schema-skew gate**: Liquibase silently ignores applied changesets it
  does not know about, so an OLD JAR starts cleanly against a NEWER schema
  and fails undiagnosably at runtime. ``check_schema_skew`` refuses the spawn
  with an actionable message instead.

Distribution of the JAR to PyPI users (bundle vs GitHub-release download) is
a conexus RDR-001 decision — this module covers only local placement +
handshake mechanics.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import structlog

_log = structlog.get_logger(__name__)

__all__ = [
    "well_known_jar_path",
    "well_known_binary_path",
    "sidecar_path",
    "extract_jar_provenance",
    "install_jar",
    "read_installed_provenance",
    "fetch_service_version",
    "applied_changesets_via_psql",
    "check_schema_skew",
]

_WELL_KNOWN_SUBDIR = "service"
_WELL_KNOWN_JAR_NAME = "nexus-service.jar"
_WELL_KNOWN_BINARY_NAME = "nexus-service"
_SIDECAR_NAME = "nexus-service.jar.meta.json"

# Liquibase changeSet identity is (id, author); attribute order in the XML is
# not fixed, so match each attribute independently within the changeSet tag.
_CHANGESET_TAG_RE = re.compile(r"<changeSet\b([^>]*)>", re.S)
_ATTR_ID_RE = re.compile(r'\bid="([^"]+)"')
_ATTR_AUTHOR_RE = re.compile(r'\bauthor="([^"]+)"')

# Pinned to the dev.nexus group: the maven-shade fat JAR carries a
# pom.properties for EVERY dependency (21 of them); a generic match is
# last-write-wins and records a random dep's version (critic Critical,
# 2026-06-10: commons-compress 1.24.0 won over 1.0-SNAPSHOT).
_POM_PROPERTIES_RE = re.compile(
    r"^META-INF/maven/dev\.nexus/[^/]+/pom\.properties$",
)
_CHANGELOG_MEMBER_RE = re.compile(r"^db/changelog/[^/]+\.xml$")


def well_known_jar_path(config_dir: Path) -> Path:
    """``<config_dir>/service/nexus-service.jar`` — the installed-user JAR home."""
    return config_dir / _WELL_KNOWN_SUBDIR / _WELL_KNOWN_JAR_NAME


def well_known_binary_path(config_dir: Path) -> Path:
    """``<config_dir>/service/nexus-service`` — the installed-user NATIVE binary.

    RDR-157 ships per-OS/arch native-image binaries (no JVM). When one is
    positioned here (by the distribution launcher / ``nx init --service``), the
    storage-service supervisor execs it directly instead of ``java -jar``.
    """
    return config_dir / _WELL_KNOWN_SUBDIR / _WELL_KNOWN_BINARY_NAME


def sidecar_path(config_dir: Path) -> Path:
    """Provenance sidecar next to the well-known JAR."""
    return config_dir / _WELL_KNOWN_SUBDIR / _SIDECAR_NAME


def extract_jar_provenance(jar_path: Path) -> dict:
    """Read version, sha256, size, build date, and bundled Liquibase
    changesets out of a nexus-service fat JAR.

    Raises :class:`~nexus.daemon.storage_service_daemon.StorageServiceStartError`
    when *jar_path* is not a readable zip — fail loud, never install garbage.
    """
    from nexus.daemon.storage_service_daemon import StorageServiceStartError

    if not jar_path.is_file() or not zipfile.is_zipfile(jar_path):
        raise StorageServiceStartError(
            f"{jar_path} is not a valid JAR (not a zip archive). "
            "Build one with: cd service && mvn package -DskipTests -q"
        )

    sha = hashlib.sha256()
    with jar_path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            sha.update(block)

    version = "unknown"
    changesets: list[dict[str, str]] = []
    with zipfile.ZipFile(jar_path) as zf:
        for name in zf.namelist():
            if _POM_PROPERTIES_RE.match(name):
                for line in zf.read(name).decode("utf-8", "replace").splitlines():
                    if line.startswith("version="):
                        version = line.split("=", 1)[1].strip()
                        break
            elif _CHANGELOG_MEMBER_RE.match(name):
                xml = zf.read(name).decode("utf-8", "replace")
                # A literal <changeSet ...> inside an XML comment would inject
                # a phantom changeset into the bundled set (CRE M1).
                xml = re.sub(r"<!--.*?-->", "", xml, flags=re.S)
                for tag in _CHANGESET_TAG_RE.finditer(xml):
                    attrs = tag.group(1)
                    cs_id = _ATTR_ID_RE.search(attrs)
                    cs_author = _ATTR_AUTHOR_RE.search(attrs)
                    if cs_id and cs_author:
                        changesets.append(
                            {"id": cs_id.group(1), "author": cs_author.group(1)},
                        )

    return {
        "version": version,
        "sha256": sha.hexdigest(),
        "size_bytes": jar_path.stat().st_size,
        "build_date": datetime.fromtimestamp(
            jar_path.stat().st_mtime, tz=UTC,
        ).isoformat(),
        "changesets": changesets,
    }


def install_jar(
    source: Path, config_dir: Path, installed_by: str = "",
) -> tuple[Path, dict]:
    """Copy *source* to the well-known location and write the provenance
    sidecar. Atomic (tmp + ``os.replace``) so a crashed install never leaves
    a half-written JAR where the supervisor will find it.

    Returns ``(installed_path, provenance)``.
    """
    from nexus.daemon.storage_service_daemon import StorageServiceStartError

    provenance = extract_jar_provenance(source)
    if not provenance["changesets"]:
        raise StorageServiceStartError(
            f"{source} has no bundled db/changelog/*.xml — not a nexus-service "
            "fat JAR (installing it would silently disable the schema-skew "
            "gate). Build one with: cd service && mvn package -DskipTests -q"
        )

    dest = well_known_jar_path(config_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)

    tmp_fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=".jar_install_")
    try:
        with os.fdopen(tmp_fd, "wb") as out, source.open("rb") as src:
            for block in iter(lambda: src.read(1 << 20), b""):
                out.write(block)
        os.replace(tmp_name, dest)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    sidecar = dict(provenance)
    sidecar["installed_at"] = datetime.now(UTC).isoformat()
    sidecar["installed_by"] = installed_by
    sidecar["source_path"] = str(source)

    tmp_fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=".jar_meta_")
    try:
        with os.fdopen(tmp_fd, "w") as out:
            json.dump(sidecar, out, indent=2)
        os.replace(tmp_name, sidecar_path(config_dir))
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    _log.info(
        "service_jar_installed",
        dest=str(dest),
        version=provenance["version"],
        sha256=provenance["sha256"][:12],
        changesets=len(provenance["changesets"]),
    )
    return dest, provenance


def read_installed_provenance(config_dir: Path) -> dict | None:
    """Parsed sidecar for the well-known JAR, or ``None`` when not installed."""
    path = sidecar_path(config_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        _log.warning("service_jar_sidecar_unreadable", path=str(path))
        return None


def fetch_service_version(host: str, port: int, timeout: float = 3.0) -> dict | None:
    """GET the running service's /version handshake, or ``None`` when
    unreachable (older JAR without the endpoint, service down)."""
    import urllib.request

    try:
        with urllib.request.urlopen(
            f"http://{host}:{port}/version", timeout=timeout,
        ) as resp:
            data = json.loads(resp.read())
            return data if isinstance(data, dict) else None
    except Exception as exc:
        _log.debug("service_version_unreachable", host=host, port=port, error=str(exc))
        return None


# ── Schema-skew gate (bead item b) ───────────────────────────────────────────


def _psql_bin() -> str | None:
    """psql from the same discovery the supervisor uses for pg_ctl."""
    try:
        from nexus.db.pg_provision import discover_pg_binaries
        # discover_pg_binaries validates all four binaries incl. psql.
        return str(discover_pg_binaries().psql)
    except Exception:
        import shutil
        return shutil.which("psql")


def _db_name_from_creds(creds: dict) -> str:
    """Database name from the jdbc URL, defaulting to ``nexus``."""
    url = creds.get("NX_DB_URL", "")
    m = re.search(r"postgresql://[^/]+/([^?]+)", url)
    return m.group(1) if m else "nexus"


def applied_changesets_via_psql(creds: dict) -> set[tuple[str, str]] | None:
    """``(id, author)`` pairs applied in the database's Liquibase journal.

    Returns the empty set when ``databasechangelog`` does not exist yet
    (fresh cluster — any JAR is acceptable) and ``None`` when the applied
    set cannot be determined (psql missing, connection refused). ``None``
    means *indeterminate*: the gate logs and proceeds, because refusal
    requires positive evidence of skew.
    """
    psql = _psql_bin()
    if psql is None:
        _log.warning("schema_skew_psql_not_found")
        return None
    port = creds.get("PG_PORT", "")
    # Prefer ADMIN creds: the journal tables are owned by the migration role,
    # and the nexus_svc read grant (grants-002) only exists on databases that
    # have already run a pebfx.4-era JAR — with svc creds the gate would be
    # blind for exactly the first upgrade start (critic Significant 1 /
    # CRE M2). Falls back to svc creds when admin creds are absent.
    user = creds.get("NX_DB_ADMIN_USER", "") or creds.get("NX_DB_USER", "")
    password = (
        creds.get("NX_DB_ADMIN_PASS", "")
        if creds.get("NX_DB_ADMIN_USER", "")
        else creds.get("NX_DB_PASS", "")
    )
    if not port or not user:
        _log.warning("schema_skew_creds_incomplete")
        return None

    env = dict(os.environ)
    env["PGPASSWORD"] = password
    try:
        result = subprocess.run(
            [
                psql, "-h", "127.0.0.1", "-p", str(port), "-U", user,
                "-d", _db_name_from_creds(creds),
                "-t", "-A", "-F", "\t", "-X", "-v", "ON_ERROR_STOP=1",
                "-c", "SELECT id, author FROM databasechangelog",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _log.warning("schema_skew_psql_failed", error=str(exc))
        return None

    if result.returncode != 0:
        if "does not exist" in result.stderr:
            return set()
        _log.warning(
            "schema_skew_psql_failed",
            returncode=result.returncode,
            stderr=result.stderr.strip()[:300],
        )
        return None

    applied: set[tuple[str, str]] = set()
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) >= 2:
            applied.add((parts[0], parts[1]))
    return applied


def check_schema_skew(jar_path: Path, creds: dict) -> None:
    """Refuse to start a JAR older than the schema it would connect to.

    Liquibase does NOT fail on applied changesets absent from its bundled
    changelog — it silently ignores them, so the old JAR boots cleanly and
    then breaks undiagnosably at runtime (missing columns, wrong shapes).
    This gate turns that into an actionable startup refusal.

    Indeterminate applied-set (psql unavailable) logs and proceeds —
    refusal requires positive evidence.
    """
    from nexus.daemon.storage_service_daemon import StorageServiceStartError

    bundled = {
        (c["id"], c["author"])
        for c in extract_jar_provenance(jar_path)["changesets"]
    }
    applied = applied_changesets_via_psql(creds)
    if applied is None:
        _log.warning(
            "schema_skew_check_indeterminate",
            jar=str(jar_path),
            hint="could not read databasechangelog; proceeding without the gate",
        )
        return
    missing = applied - bundled
    if missing:
        sample = ", ".join(
            f"{cs_id} (by {author})" for cs_id, author in sorted(missing)[:5]
        )
        raise StorageServiceStartError(
            f"JAR {jar_path} is OLDER than the database schema: the database "
            f"has {len(missing)} applied Liquibase changeset(s) this JAR does "
            f"not know about (e.g. {sample}). Starting it would fail "
            "undiagnosably at runtime. Install a newer JAR:\n"
            "  nx daemon service install-jar <path-to-newer-jar>\n"
            "or rebuild from the repo: cd service && mvn package -DskipTests -q"
        )
    _log.debug(
        "schema_skew_check_passed",
        bundled=len(bundled),
        applied=len(applied),
    )
