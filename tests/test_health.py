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


def test_local_collections_reports_empty_count(tmp_path, monkeypatch) -> None:
    """nx doctor surfaces the count of empty local collections so deletion of
    every doc from a collection doesn't leave callers wondering why the
    collection still appears in `nx collection list`. (Empty collections are
    intentional — they preserve the embedding-model binding for fast next
    store_put.)

    RDR-120 P6 (nexus-qg86h): the probe now routes through the T3
    daemon's HttpClient; stub ``make_t3_client`` to return a wrapper
    over the test's PersistentClient so the test exercises the new
    routing path without requiring a running daemon.
    """
    import chromadb
    from nexus.health import _check_t3_local

    monkeypatch.setenv("NX_LOCAL_CHROMA_PATH", str(tmp_path / "chroma"))
    # Create one populated, two empty collections.
    client = chromadb.PersistentClient(path=str(tmp_path / "chroma"))
    populated = client.get_or_create_collection("knowledge__has_data")
    populated.add(ids=["a"], documents=["hello"], embeddings=[[0.1] * 384])
    client.get_or_create_collection("knowledge__empty1")
    client.get_or_create_collection("knowledge__empty2")

    class _Stub:
        _client = client

    monkeypatch.setattr(
        "nexus.daemon.t3_client.make_t3_client", lambda: _Stub(),
    )
    results = _check_t3_local()
    local_collections_line = next(
        (r for r in results if r.label == "Local collections"), None
    )
    assert local_collections_line is not None
    assert "3 collections" in local_collections_line.detail
    assert "(including 2 empty)" in local_collections_line.detail


def test_local_collections_omits_empty_note_when_none(tmp_path, monkeypatch) -> None:
    """No `(including N empty)` note when every collection has data.

    RDR-120 P6: same daemon-stub pattern as the preceding test.
    """
    import chromadb
    from nexus.health import _check_t3_local

    monkeypatch.setenv("NX_LOCAL_CHROMA_PATH", str(tmp_path / "chroma"))
    client = chromadb.PersistentClient(path=str(tmp_path / "chroma"))
    populated = client.get_or_create_collection("knowledge__has_data")
    populated.add(ids=["a"], documents=["hello"], embeddings=[[0.1] * 384])

    class _Stub:
        _client = client

    monkeypatch.setattr(
        "nexus.daemon.t3_client.make_t3_client", lambda: _Stub(),
    )
    results = _check_t3_local()
    local_collections_line = next(
        (r for r in results if r.label == "Local collections"), None
    )
    assert local_collections_line is not None
    assert "(including" not in local_collections_line.detail


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


# ── _check_t3_daemon_version (RDR-149 nexus-ymn76) ───────────────────────────


def test_check_t3_daemon_version_no_daemon(monkeypatch) -> None:
    from nexus import health

    monkeypatch.setattr(health, "find_t3_daemon", lambda: None, raising=False)
    monkeypatch.setattr(
        "nexus.daemon.discovery.find_t3_daemon", lambda config_dir=None: None
    )
    results = health._check_t3_daemon_version()
    assert len(results) == 1
    assert results[0].ok is True
    assert "no t3 daemon" in results[0].detail.lower()


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
