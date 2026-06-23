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
