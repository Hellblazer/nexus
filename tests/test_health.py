# SPDX-License-Identifier: AGPL-3.0-or-later
from nexus.health import HealthResult, format_health_for_cli


def test_health_result_fields():
    r = HealthResult(label="test", ok=True, detail="fine")
    assert r.label == "test"
    assert r.ok is True
    assert r.detail == "fine"
    assert r.fix_suggestions == []
    assert r.fatal is False
    assert r.warn is False


def test_health_result_with_fix_suggestions():
    r = HealthResult(
        label="missing key",
        ok=False,
        detail="not set",
        fix_suggestions=["nx config set key <value>", "https://example.com"],
        fatal=True,
    )
    assert r.fatal is True
    assert len(r.fix_suggestions) == 2


def test_format_check_ok():
    results = [HealthResult(label="Python ≥ 3.12", ok=True, detail="3.12.11")]
    output, failed = format_health_for_cli(results, local_mode=False)
    assert "✓ Python ≥ 3.12: 3.12.11" in output
    assert failed is False


def test_format_check_fail():
    results = [HealthResult(label="Python ≥ 3.12", ok=False, detail="3.11.0 — 3.12+ required")]
    output, failed = format_health_for_cli(results, local_mode=False)
    assert "✗ Python ≥ 3.12: 3.11.0 — 3.12+ required" in output
    # Non-fatal by default
    assert failed is False


def test_format_soft_warn_renders_warning_glyph():
    """RDR-129 B4 (nexus-uq8a4): a soft WARN (ok=False, warn=True) renders the
    ⚠ glyph — distinct from both ✓ (pass) and ✗ (hard fail) — and never marks
    the run as failed."""
    results = [
        HealthResult(
            label="T2 integrity",
            ok=False,
            warn=True,
            detail="FTS5: busy (write in progress, retry)",
        )
    ]
    output, failed = format_health_for_cli(results, local_mode=False)
    assert "⚠ T2 integrity: FTS5: busy (write in progress, retry)" in output
    assert "✗ T2 integrity" not in output
    assert failed is False


def test_format_fatal_fail():
    results = [
        HealthResult(label="Python ≥ 3.12", ok=False, detail="3.11.0", fatal=True),
    ]
    output, failed = format_health_for_cli(results, local_mode=False)
    assert failed is True


def test_format_fix_suggestions():
    results = [
        HealthResult(
            label="ripgrep",
            ok=False,
            detail="not found",
            fix_suggestions=["brew install ripgrep", "apt install ripgrep"],
            fatal=True,
        ),
    ]
    output, failed = format_health_for_cli(results, local_mode=False)
    assert "    Fix: brew install ripgrep" in output
    assert "         apt install ripgrep" in output
    assert failed is True


def test_format_header():
    results = [HealthResult(label="test", ok=True)]
    output, _ = format_health_for_cli(results, local_mode=False)
    assert output.startswith("Nexus health check:\n")


def test_format_footer_cloud_mode():
    results = [HealthResult(label="key", ok=False, fatal=True)]
    output, _ = format_health_for_cli(results, local_mode=False)
    assert "nx config init" in output


def test_format_footer_local_mode():
    results = [HealthResult(label="path", ok=False, fatal=True)]
    output, _ = format_health_for_cli(results, local_mode=True)
    assert "Run 'nx doctor' again" in output


def test_format_no_footer_when_passing():
    results = [HealthResult(label="all good", ok=True)]
    output, failed = format_health_for_cli(results, local_mode=False)
    assert "nx config init" not in output
    assert failed is False


def test_format_no_detail():
    results = [HealthResult(label="test", ok=True)]
    output, _ = format_health_for_cli(results, local_mode=False)
    # No colon after label when detail is empty
    assert "  ✓ test\n" in output or output.endswith("  ✓ test")


# ── Local collections empty-count surfacing (nexus-obp2) ──────────────────────


def test_t3_collections_census_via_service(tmp_path, monkeypatch) -> None:
    """RDR-155 P4a.2 (nexus-1k8s1): the collection census routes through the
    pgvector service handle (``make_t3()``) — the chroma-daemon probe and its
    empty-collection note retired with the Chroma serving path. The on-disk
    Chroma directory is reported as the legacy store awaiting the P5 ETL.
    """
    from nexus.health import _check_t3_local

    legacy = tmp_path / "chroma"
    legacy.mkdir()
    (legacy / "blob.bin").write_bytes(b"x" * 2048)
    monkeypatch.setenv("NX_LOCAL_CHROMA_PATH", str(legacy))

    class _StubServiceClient:
        def list_collections(self):
            return [{"name": "knowledge__a"}, {"name": "code__b"}, {"name": "rdr__c"}]

    monkeypatch.setattr("nexus.db.make_t3", lambda **kw: _StubServiceClient())
    monkeypatch.setattr("nexus.db.http_vector_client._get", lambda *a, **kw: [])
    results = _check_t3_local()
    line = next((r for r in results if r.label == "T3 collections"), None)
    assert line is not None
    assert "3 collections (pgvector service)" in line.detail
    assert "legacy Chroma store" in line.detail
    assert "awaiting P5 ETL" in line.detail


def test_t3_collections_census_degrades_when_service_unreachable(
    tmp_path, monkeypatch
) -> None:
    """An unreachable service degrades to an informational could-not-query
    line — doctor must not crash on a down service."""
    from nexus.health import _check_t3_local

    monkeypatch.setenv("NX_LOCAL_CHROMA_PATH", str(tmp_path / "chroma"))

    def _boom(**kw):
        raise RuntimeError("service down")

    monkeypatch.setattr("nexus.db.make_t3", _boom)
    monkeypatch.setattr("nexus.db.http_vector_client._get", lambda *a, **kw: [])
    results = _check_t3_local()
    line = next((r for r in results if r.label == "T3 collections"), None)
    assert line is not None
    assert line.ok is True
    assert "could not query" in line.detail


def test_pipeline_version_check_degrades_on_make_t3_failure(monkeypatch) -> None:
    """nexus-b6qlf regression: get_http_vector_client() (Phase 2, reached via
    make_t3()) now runs a cloud-mode engine-version probe before returning a
    handle. Every OTHER make_t3() call site in this module already degrades
    gracefully on failure (see test_t3_collections_census_degrades_when_
    service_unreachable above) -- the pipeline-version sweep's make_t3()
    call was the one exception, left unguarded, so a probe failure crashed
    the entire `nx doctor` run instead of reporting a soft-fail line like
    its siblings."""
    from nexus.health import _check_t3_cloud

    def _cred(key: str) -> str:
        # service_url empty -> _check_managed_service_probe() short-circuits
        # (not under test here); the three credentials below are truthy so
        # the pipeline-version-check branch is actually entered.
        return "" if key == "service_url" else "value"

    monkeypatch.setattr("nexus.config.get_credential", _cred)
    monkeypatch.setattr("nexus.db.http_vector_client._get", lambda *a, **kw: [])

    def _boom(**kw):
        raise RuntimeError("stale engine — below required floor")

    monkeypatch.setattr("nexus.db.make_t3", _boom)

    results = _check_t3_cloud()  # must not raise
    line = next((r for r in results if r.label == "pipeline versions"), None)
    assert line is not None
    assert line.ok is False
    assert "stale engine" in line.detail


def test_vector_service_probe_unconditional_and_fatal(monkeypatch, tmp_path) -> None:
    """RDR-155 P4a.2 dual-review finding 2 (substantive-critic): the vector
    service probe must fire in BOTH mode branches and without legacy Chroma
    credentials — a pgvector-only install with the service down must not
    doctor all-green. Service-down is a FATAL failure."""
    from nexus.health import _check_t3_cloud, _check_t3_local

    def _down(*a, **kw):
        raise RuntimeError("connection refused")

    monkeypatch.setattr("nexus.db.http_vector_client._get", _down)
    # Local branch (census stubbed; the probe is what's under test).
    monkeypatch.setattr("nexus.config._default_local_path", lambda: tmp_path / "nope")
    monkeypatch.setattr("nexus.db.make_t3", _down)
    local_line = next(
        (r for r in _check_t3_local() if r.label == "Vector service (/v1/vectors)"),
        None,
    )
    assert local_line is not None and local_line.ok is False and local_line.fatal

    # Cloud branch with NO chroma credentials at all (the fresh-install shape).
    monkeypatch.setattr("nexus.config.get_credential", lambda k: "")
    cloud_line = next(
        (r for r in _check_t3_cloud() if r.label == "Vector service (/v1/vectors)"),
        None,
    )
    assert cloud_line is not None and cloud_line.ok is False and cloud_line.fatal


def test_vector_service_probe_reachable(monkeypatch) -> None:
    """Probe reports reachable when the service answers."""
    from nexus.health import _check_vector_service

    monkeypatch.setattr("nexus.db.http_vector_client._get", lambda *a, **kw: [])
    line = _check_vector_service()
    assert line.ok is True
    assert "reachable" in line.detail
    assert "changeset" not in line.detail.lower()


# ── nexus-4m6i0.7: surface the failing Liquibase changeset on boot crash-loop ─

# Verbatim (modulo indentation) tail of storage_service_native.log from the
# real ms57z incident (GH #1390): engine-service v0.1.36 crash-looping on
# catalog-013-2's VALIDATE CONSTRAINT because chunks_384 never had the
# constraint created.
_REAL_INCIDENT_LOG_TAIL = """\
2026-07-08T10:22:01.123Z INFO  dev.nexus.service.Main - starting nexus-service v0.1.36
2026-07-08T10:22:01.456Z INFO  dev.nexus.service.db.SchemaMigrator - applying pending changesets
dev.nexus.service.db.SchemaMigrator$MigrationException: Liquibase migration failed
  at dev.nexus.service.db.SchemaMigrator.migrate(SchemaMigrator.java:111)
  at dev.nexus.service.Main.main(Main.java:83)
Caused by: liquibase.exception.MigrationFailedException:
  Migration failed for changeset db/changelog/catalog-013-chash-checks-validate.xml::catalog-013-2::nexus-e0hd2
Caused by: org.postgresql.util.PSQLException:
  ERROR: constraint "chunks_384_chash_len_check" of relation "chunks_384" does not exist
"""


def _write_service_log(config_dir, content: str) -> None:
    logs_dir = config_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "storage_service_native.log").write_text(content)


def test_last_boot_failure_detail_parses_real_incident_log(tmp_path) -> None:
    """Unit test of the tail-parser against the verbatim ms57z log shape."""
    from nexus.health import _last_boot_failure_detail

    log_path = tmp_path / "storage_service_native.log"
    log_path.write_text(_REAL_INCIDENT_LOG_TAIL)

    detail = _last_boot_failure_detail(log_path)
    assert detail is not None
    assert "catalog-013-2" in detail
    assert 'constraint "chunks_384_chash_len_check"' in detail
    assert "does not exist" in detail


def test_last_boot_failure_detail_missing_file_returns_none(tmp_path) -> None:
    from nexus.health import _last_boot_failure_detail

    detail = _last_boot_failure_detail(tmp_path / "does_not_exist.log")
    assert detail is None


def test_last_boot_failure_detail_no_marker_returns_none(tmp_path) -> None:
    from nexus.health import _last_boot_failure_detail

    log_path = tmp_path / "storage_service_native.log"
    log_path.write_text("2026-07-08T10:22:01.123Z INFO starting up\nready\n")

    detail = _last_boot_failure_detail(log_path)
    assert detail is None


def test_last_boot_failure_detail_directory_path_degrades(tmp_path) -> None:
    """A path that exists but is not a regular file (e.g. mis-resolved) must
    degrade to None, never raise."""
    from nexus.health import _last_boot_failure_detail

    dir_path = tmp_path / "storage_service_native.log"
    dir_path.mkdir()

    assert _last_boot_failure_detail(dir_path) is None


def test_last_boot_failure_detail_distant_unrelated_error_not_attributed(
    tmp_path,
) -> None:
    """nexus-4m6i0.7 critique: the ERROR-line association must be BOUNDED.
    A marker whose own error scrolled away, followed much later by an
    UNRELATED error (disk quota, OOM, ...), must NOT be glued into a
    fabricated 'changeset X: unrelated error' pairing — that actively
    misdirects the operator, strictly worse than the id-only form."""
    from nexus.health import _last_boot_failure_detail

    log_path = tmp_path / "storage_service_native.log"
    padding = "\n".join(f"routine log line {i}" for i in range(80))
    log_path.write_text(
        "Migration failed for changeset "
        "db/changelog/catalog-013-chash-checks-validate.xml::catalog-013-2::nexus-e0hd2\n"
        + padding
        + "\nERROR: disk quota exceeded on /var/lib/postgresql/data\n",
        encoding="utf-8",
    )

    detail = _last_boot_failure_detail(log_path)
    assert detail == "Liquibase changeset catalog-013-2", (
        "a distant, unrelated ERROR line must not be attributed to the "
        f"changeset marker: {detail!r}"
    )


def test_last_boot_failure_detail_bounded_tail_read(tmp_path) -> None:
    """The parser only considers a bounded tail WINDOW — a marker outside it
    (buried under older log) is not found. Note this pins the window's
    OUTPUT semantics, not the I/O mechanism: a read-all-then-slice
    implementation would produce identical output and also pass. The
    seek-from-end boundedness itself is an implementation property verified
    by reading ``_last_boot_failure_detail``, not regression-tested here."""
    from nexus.health import _BOOT_FAILURE_TAIL_BYTES, _last_boot_failure_detail

    log_path = tmp_path / "storage_service_native.log"
    filler = "x" * (_BOOT_FAILURE_TAIL_BYTES + 4096)
    log_path.write_text(
        "Migration failed for changeset db/changelog/x.xml::catalog-999-9::someone\n"
        + filler
    )

    # The marker was written before the filler, so it falls outside the tail
    # window once the file exceeds the cap -- degrade to None, don't crash.
    assert _last_boot_failure_detail(log_path) is None

    # Sanity: the same marker at the END of a large file IS found.
    log_path.write_text(
        filler
        + "\nMigration failed for changeset db/changelog/x.xml::catalog-999-9::someone\n"
        "Caused by: org.postgresql.util.PSQLException:\n"
        '  ERROR: relation "x" does not exist\n'
    )
    detail = _last_boot_failure_detail(log_path)
    assert detail is not None
    assert "catalog-999-9" in detail


def test_vector_service_unreachable_surfaces_boot_failure(monkeypatch, tmp_path) -> None:
    """Core acceptance test (nexus-4m6i0.7): when the vector service is
    unreachable AND the local service log carries a recent Liquibase
    VALIDATE failure, the HealthResult detail names the changeset and the
    SQL error one-liner instead of only 'not reachable'."""
    from nexus.health import _check_vector_service

    def _down(*a, **kw):
        raise RuntimeError("connection refused")

    monkeypatch.setattr("nexus.db.http_vector_client._get", _down)
    monkeypatch.setattr("nexus.config.nexus_config_dir", lambda: tmp_path)
    _write_service_log(tmp_path, _REAL_INCIDENT_LOG_TAIL)

    line = _check_vector_service()
    assert line.ok is False
    assert line.fatal is True
    assert "not reachable" in line.detail
    assert "catalog-013-2" in line.detail
    assert 'constraint "chunks_384_chash_len_check"' in line.detail


def test_vector_service_unreachable_no_log_degrades_to_bare_message(
    monkeypatch, tmp_path
) -> None:
    """Cloud-mode / no-local-service installs: no service log on disk ->
    the diagnostic must be a complete no-op, never crash doctor."""
    from nexus.health import _check_vector_service

    def _down(*a, **kw):
        raise RuntimeError("connection refused")

    monkeypatch.setattr("nexus.db.http_vector_client._get", _down)
    monkeypatch.setattr("nexus.config.nexus_config_dir", lambda: tmp_path)
    # No logs/ dir created at all.

    line = _check_vector_service()
    assert line.ok is False
    assert line.fatal is True
    assert line.detail == "not reachable"


def test_vector_service_unreachable_log_present_no_marker_degrades(
    monkeypatch, tmp_path
) -> None:
    """Service log exists but carries no Liquibase failure marker (e.g. the
    service crashed for an unrelated reason, or hasn't crashed at all) ->
    degrade to the bare message."""
    from nexus.health import _check_vector_service

    def _down(*a, **kw):
        raise RuntimeError("connection refused")

    monkeypatch.setattr("nexus.db.http_vector_client._get", _down)
    monkeypatch.setattr("nexus.config.nexus_config_dir", lambda: tmp_path)
    _write_service_log(tmp_path, "2026-07-08T10:22:01.123Z INFO starting up\n")

    line = _check_vector_service()
    assert line.ok is False
    assert line.detail == "not reachable"


def test_vector_service_reachable_never_surfaces_stale_boot_failure(
    monkeypatch, tmp_path
) -> None:
    """A healthy service must never carry a stale boot-failure line from an
    old log -- the diagnostic only fires in the unreachable branch."""
    from nexus.health import _check_vector_service

    monkeypatch.setattr("nexus.db.http_vector_client._get", lambda *a, **kw: [])
    monkeypatch.setattr("nexus.config.nexus_config_dir", lambda: tmp_path)
    # Stale log from a PRIOR crash still on disk even though the service is
    # currently reachable.
    _write_service_log(tmp_path, _REAL_INCIDENT_LOG_TAIL)

    line = _check_vector_service()
    assert line.ok is True
    assert line.detail == "reachable"
    assert "catalog-013-2" not in line.detail


def test_check_t3_local_surfaces_state2_degraded_bge(tmp_path, monkeypatch) -> None:
    """RDR-144 P5a integration: when the user chose bge-768 but the [local]
    extra is missing (EF resolves to 384), `_check_t3_local` emits the
    actionable degraded-embedder advisory — not just a structlog line.

    Uses a non-existent local path so the daemon collection-probe block is
    skipped; the advisory runs before it and needs no chroma.
    """
    from nexus.db.local_ef import _TIER0_MODEL, _TIER1_MODEL
    from nexus.health import _check_t3_local

    monkeypatch.setattr(
        "nexus.config._default_local_path", lambda: tmp_path / "does_not_exist"
    )
    # User chose bge; the active EF resolved to the 384 fallback (extra absent).
    monkeypatch.setattr(
        "nexus.config.local_embed_model_choice", lambda: _TIER1_MODEL
    )

    class _FakeEF:
        model_name = _TIER0_MODEL
        dimensions = 384

    monkeypatch.setattr(
        "nexus.db.local_ef.LocalEmbeddingFunction", lambda *a, **k: _FakeEF()
    )

    results = _check_t3_local()
    advisory = next((r for r in results if r.label == "Local embedder"), None)
    assert advisory is not None
    assert advisory.warn is True and advisory.fatal is False
    joined = advisory.detail + " " + " ".join(advisory.fix_suggestions)
    assert "bge-768" in joined and "384" in joined


def test_check_t3_local_suppresses_advisory_in_service_mode(tmp_path, monkeypatch) -> None:
    """nexus-ybw87: a --service install (pg_credentials present) embeds T3
    server-side in the Java service, so the PYTHON local-embedder advisory is
    suppressed even when it would otherwise fire (State-2 degraded-bge here) —
    it reflects fastembed, which does not serve a service user's T3.
    """
    from nexus.db.local_ef import _TIER0_MODEL, _TIER1_MODEL
    from nexus.health import _check_t3_local

    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg))
    (cfg / "pg_credentials").write_text("PG_PORT=15432\n")  # service mode configured

    monkeypatch.setattr(
        "nexus.config._default_local_path", lambda: tmp_path / "does_not_exist"
    )
    # Would normally trigger the State-2 degraded-bge advisory:
    monkeypatch.setattr("nexus.config.local_embed_model_choice", lambda: _TIER1_MODEL)

    class _FakeEF:
        model_name = _TIER0_MODEL
        dimensions = 384

    monkeypatch.setattr(
        "nexus.db.local_ef.LocalEmbeddingFunction", lambda *a, **k: _FakeEF()
    )

    results = _check_t3_local()
    assert not any(r.label == "Local embedder" for r in results), (
        "Python local-embedder advisory must be suppressed for a service install"
    )
    # The Python EF line is relabeled so it does not read as the T3 embedder
    # (which is the bge-768 service, reported by _check_service_bge_model).
    assert not any(r.label == "Embedding model" for r in results)
    ef_line = next((r for r in results if r.label.startswith("Embedding model")), None)
    assert ef_line is not None and "T1" in ef_line.label
    assert "server-side" in ef_line.detail


# ── _check_t3_daemon_version (RDR-149 nexus-ymn76) ───────────────────────────


def test_check_t3_daemon_version_no_daemon(monkeypatch) -> None:
    from nexus import health

    monkeypatch.setattr(
        "nexus.daemon.discovery.find_t3_daemon", lambda config_dir=None: None
    )
    results = health._check_t3_daemon_version()
    assert len(results) == 1
    assert results[0].ok is True
    assert "no t3 daemon" in results[0].detail.lower()


def test_check_t3_daemon_version_lease_without_version(monkeypatch) -> None:
    # A pre-RDR-149 daemon lease carries no version field — informational, ok.
    from nexus import health

    monkeypatch.setattr(
        "nexus.daemon.discovery.find_t3_daemon",
        lambda config_dir=None: {"tcp_host": "127.0.0.1", "tcp_port": 1},
    )
    results = health._check_t3_daemon_version()
    assert len(results) == 1
    assert results[0].ok is True
    assert "no version" in results[0].detail.lower()


def test_check_t3_daemon_version_match(monkeypatch) -> None:
    from importlib.metadata import version as _pkg_version

    from nexus import health

    cli = _pkg_version("conexus")
    monkeypatch.setattr(
        "nexus.daemon.discovery.find_t3_daemon",
        lambda config_dir=None: {"version": cli, "tcp_host": "127.0.0.1", "tcp_port": 1},
    )
    results = health._check_t3_daemon_version()
    assert len(results) == 1
    assert results[0].ok is True
    assert cli in results[0].detail


def test_check_t3_daemon_version_mismatch_warns(monkeypatch) -> None:
    from nexus import health

    monkeypatch.setattr(
        "nexus.daemon.discovery.find_t3_daemon",
        lambda config_dir=None: {"version": "0.0.1-stale", "tcp_host": "127.0.0.1", "tcp_port": 1},
    )
    results = health._check_t3_daemon_version()
    assert len(results) == 1
    r = results[0]
    assert r.ok is False and r.warn is True and r.fatal is False
    assert "0.0.1-stale" in r.detail
    assert any("daemon t3" in s for s in r.fix_suggestions)


# ── _check_managed_service_probe (RDR-001 nexus-o6fch) ───────────────────────


class TestManagedServiceProbeCheck:
    """Doctor cloud-branch probe of a MANAGED endpoint — only when NX_SERVICE_URL
    is explicitly set (never default-probes the public api.conexus-nexus.com)."""

    def test_no_service_url_does_not_probe(self, monkeypatch):
        monkeypatch.delenv("NX_SERVICE_URL", raising=False)
        from nexus.health import _check_managed_service_probe
        assert _check_managed_service_probe() == []

    def test_whitespace_service_url_does_not_probe(self, monkeypatch):
        monkeypatch.setenv("NX_SERVICE_URL", "   ")
        from nexus.health import _check_managed_service_probe
        assert _check_managed_service_probe() == []

    def test_config_yml_service_url_does_probe(self, monkeypatch):
        # nexus-v3p0x: a greenfield user who set the endpoint via `nx config set
        # service_url` (NO shell export) must still get the doctor probe — the
        # guard resolves via get_credential (env, then config.yml), not bare env.
        monkeypatch.delenv("NX_SERVICE_URL", raising=False)
        from nexus.config import set_credential
        set_credential("service_url", "https://api.conexus-nexus.com")
        from nexus.db import managed_endpoint as me

        seen = {}

        caps = me.ManagedCapabilities(
            base_url="https://api.conexus-nexus.com", app_version="1.0",
            release_version="0.1.8",
            embedding_mode="voyage", embedding_models=[],
            schema_latest_id=None, schema_changeset_count=None,
        )

        def _capture(**kw):
            seen["base_url"] = kw.get("base_url")
            return caps

        monkeypatch.setattr(me, "probe_managed_service", _capture)
        from nexus.health import _check_managed_service_probe
        res = _check_managed_service_probe()
        # It probed (non-empty result) against the config.yml endpoint, not [].
        assert res != []
        assert seen["base_url"] == "https://api.conexus-nexus.com"

    def test_probes_explicit_url_never_the_public_default(self, monkeypatch):
        # SAFETY INVARIANT: the probe receives the EXPLICIT NX_SERVICE_URL, never
        # base_url=None (which would default to https://api.conexus-nexus.com).
        monkeypatch.setenv("NX_SERVICE_URL", "https://staging.example.com")
        from nexus.db import managed_endpoint as me

        seen = {}
        caps = me.ManagedCapabilities(
            base_url="https://staging.example.com", app_version="1.0",
            release_version="0.1.8",
            embedding_mode="voyage", embedding_models=[],
            schema_latest_id=None, schema_changeset_count=None,
        )

        def _capture(**kw):
            seen.update(kw)
            return caps
        monkeypatch.setattr(me, "probe_managed_service", _capture)
        from nexus.health import _check_managed_service_probe
        _check_managed_service_probe()
        assert seen.get("base_url") == "https://staging.example.com"

    def test_compatible_is_ok(self, monkeypatch):
        monkeypatch.setenv("NX_SERVICE_URL", "https://api.conexus-nexus.com")
        from nexus.db import managed_endpoint as me

        caps = me.ManagedCapabilities(
            base_url="https://api.conexus-nexus.com", app_version="1.0-SNAPSHOT",
            release_version="0.1.8",
            embedding_mode="voyage", embedding_models=[],
            schema_latest_id="vectors-002", schema_changeset_count=64,
        )
        monkeypatch.setattr(me, "probe_managed_service", lambda **kw: caps)
        from nexus.health import _check_managed_service_probe
        res = _check_managed_service_probe()
        assert len(res) == 1 and res[0].ok is True
        assert "1.0-SNAPSHOT" in res[0].detail

    def test_incompatible_is_soft_warn(self, monkeypatch):
        monkeypatch.setenv("NX_SERVICE_URL", "https://x")
        from nexus.db import managed_endpoint as me

        def _boom(**kw):
            raise me.ManagedServiceIncompatible("app_version 0.9.0 below minimum 1.0.0")
        monkeypatch.setattr(me, "probe_managed_service", _boom)
        from nexus.health import _check_managed_service_probe
        res = _check_managed_service_probe()
        assert len(res) == 1
        assert res[0].ok is False and res[0].warn is True and res[0].fatal is False
        assert "0.9.0" in res[0].detail

    def test_unreachable_is_soft_warn_not_double_fatal(self, monkeypatch):
        # reachability fatal is _check_vector_service's domain — stay soft here
        monkeypatch.setenv("NX_SERVICE_URL", "https://x")
        from nexus.db import managed_endpoint as me

        def _boom(**kw):
            raise me.ManagedServiceUnreachable("unreachable")
        monkeypatch.setattr(me, "probe_managed_service", _boom)
        from nexus.health import _check_managed_service_probe
        res = _check_managed_service_probe()
        assert len(res) == 1 and res[0].ok is False and res[0].warn is True and res[0].fatal is False


def test_run_health_checks_survives_unresolvable_catalog_endpoint(monkeypatch, tmp_path) -> None:
    """nexus-<bead> regression: run_health_checks()'s OWN make_catalog_reader()
    call (feeding _check_catalog, distinct from _check_git_hooks' already-
    guarded call) was unguarded. In service mode with no reachable
    nexus-service — e.g. a bare `nx doctor` before `nx daemon service start`
    — resolve_service_config() raises RuntimeError, which propagated
    uncaught out of run_health_checks() and crashed the whole `nx doctor`
    command instead of degrading gracefully like every sibling check.

    Discovered live via upgrade-shakeout.sh (10/12 FAIL) during the 6.1.0
    release gate.
    """
    import nexus.health as health_mod

    def _boom():
        raise RuntimeError("nexus-service endpoint is not resolvable (NX_STORAGE_BACKEND=service): ...")

    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", _boom)
    monkeypatch.chdir(tmp_path)

    # Must not raise — this is the exact call site that crashed `nx doctor`.
    results, _is_local = health_mod.run_health_checks()

    catalog_results = [r for r in results if r.label == "Catalog"]
    assert catalog_results, "Catalog check must still report, degraded, not vanish"
    assert catalog_results[0].ok is True


# ── _check_dimension_orphans (nexus-9tsdf / GH #1113 AC2) ────────────────────


def _patch_orphan_finder(monkeypatch, *, mismatches, skipped=0, active="voyage"):
    """Stub the shared finder + T3 handle so the health check is exercised
    without a live service. The finder is THE shared source of truth with
    `nx collection prune` — doctor and the remedy command can never
    disagree about what counts as an orphan."""
    from nexus import health

    monkeypatch.setattr("nexus.db.make_t3", lambda: object())
    monkeypatch.setattr(
        "nexus.commands.collection._find_dimension_mismatched_collections",
        lambda t3: (mismatches, skipped, active),
    )
    return health


def test_check_dimension_orphans_names_collection_and_suggests_prune(
    monkeypatch,
) -> None:
    """AC2 (GH #1113): doctor names the specific mismatched collection(s)
    and suggests the prune command."""
    orphan = "knowledge__shakedown-scratch__minilm-l6-v2-384__v1"
    health = _patch_orphan_finder(
        monkeypatch,
        mismatches=[{
            "name": orphan, "declared_model": "minilm-l6-v2-384",
            "declared_dim": 384, "active_dim": 1024, "count": 1,
        }],
    )
    results = health._check_dimension_orphans()
    assert len(results) == 1
    r = results[0]
    assert r.ok is False and r.warn is True, "orphans are a soft warn, never fatal"
    assert orphan in r.detail
    assert "384" in r.detail and "1024" in r.detail
    assert any("nx collection prune" in s for s in r.fix_suggestions)


def test_check_dimension_orphans_clean_passes(monkeypatch) -> None:
    health = _patch_orphan_finder(monkeypatch, mismatches=[])
    results = health._check_dimension_orphans()
    assert len(results) == 1
    assert results[0].ok is True
    assert "voyage" in results[0].detail


def test_check_dimension_orphans_unknown_embedder_never_guesses(
    monkeypatch,
) -> None:
    """An unresolved active-embedder probe must skip, not guess — a wrong
    guess here would tell the operator to delete healthy collections."""
    health = _patch_orphan_finder(monkeypatch, mismatches=[], active="unknown")
    results = health._check_dimension_orphans()
    assert len(results) == 1
    assert results[0].ok is True
    assert "skipped" in results[0].detail.lower()


def test_check_dimension_orphans_degrades_on_t3_failure(monkeypatch) -> None:
    from nexus import health

    def _boom():
        raise RuntimeError("service unreachable")

    monkeypatch.setattr("nexus.db.make_t3", _boom)
    results = health._check_dimension_orphans()
    assert len(results) == 1
    assert results[0].ok is True
    assert "skipped" in results[0].detail.lower()


def test_check_dimension_orphans_wired_into_run_health_checks() -> None:
    """Falsification pin: deleting the run_health_checks call site must
    fail this test, not silently drop AC2 from `nx doctor`."""
    import inspect

    from nexus import health

    src = inspect.getsource(health.run_health_checks)
    assert "_check_dimension_orphans" in src
