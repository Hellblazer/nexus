# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus.config import backfill_install_mode_record, is_local_mode
from nexus.stranded_install import legacy_chroma_dir
from nexus.db.local_ef import LocalEmbeddingFunction
from nexus.db.t3 import T3Database
from tests.conftest import make_vector_test_client


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def local_ef() -> LocalEmbeddingFunction:
    return LocalEmbeddingFunction(model_name="all-MiniLM-L6-v2")


@pytest.fixture()
def local_db(tmp_path: Path, local_ef: LocalEmbeddingFunction) -> T3Database:
    # RDR-155 P4a.2 (nexus-1k8s1): the serving-path PersistentClient open is
    # retired — local-mode T3Database requires an injected client now.
    # EphemeralClient instances share one in-process backend, so clear any
    # collections left by earlier tests before handing the facade out.
    client = make_vector_test_client()
    for col in client.list_collections():
        client.delete_collection(col.name)
    return T3Database(
        local_mode=True,
        local_path=str(tmp_path / "chroma"),
        _client=client,
        _ef_override=local_ef,
    )


# ── config.py: is_local_mode ─────────────────────────────────────────────────


class TestIsLocalMode:
    @pytest.mark.parametrize(
        ("env", "expected"),
        [
            pytest.param({"NX_LOCAL": "1", "CHROMA_API_KEY": "k", "VOYAGE_API_KEY": "k"}, True, id="nx_local_1_overrides"),
            pytest.param({"NX_LOCAL": "0"}, False, id="nx_local_0_overrides"),
            pytest.param({}, True, id="no_credentials"),
            # RDR-155 P4b: the CHROMA_API_KEY inference died with the chroma
            # credential map — with no record/service_url/pg_credentials the
            # box is LOCAL regardless of legacy keys in the environment.
            pytest.param({"CHROMA_API_KEY": "k", "VOYAGE_API_KEY": "k"}, True, id="legacy_keys_are_inert"),
            pytest.param({"CHROMA_API_KEY": "k"}, True, id="chroma_key_is_inert"),
            pytest.param({"VOYAGE_API_KEY": "k"}, True, id="voyage_only"),
            # nexus-3k43p: a managed 6.0 user (service_url set, no chroma/voyage
            # key) must NOT be mis-detected as local. service_url presence wins
            # over the legacy CHROMA/VOYAGE-absent heuristic; NX_LOCAL still wins
            # over service_url.
            pytest.param({"NX_SERVICE_URL": "https://m.example"}, False, id="service_url_is_managed"),
            pytest.param({"NX_SERVICE_URL": "https://m.example", "NX_LOCAL": "1"}, True, id="nx_local_1_beats_service_url"),
            pytest.param({"NX_SERVICE_URL": "https://m.example", "VOYAGE_API_KEY": "k"}, False, id="service_url_beats_legacy_heuristic"),
        ],
    )
    def test_is_local_mode(
        self, env: dict[str, str], expected: bool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        for var in ("NX_LOCAL", "CHROMA_API_KEY", "VOYAGE_API_KEY", "NX_SERVICE_URL"):
            monkeypatch.delenv(var, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "cfg"))
        assert is_local_mode() is expected

    def _cfg(self, tmp_path, monkeypatch, *, pg_creds: bool):
        cfg = tmp_path / "cfg"
        cfg.mkdir(exist_ok=True)
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg))
        monkeypatch.setenv("HOME", str(tmp_path))
        for var in ("NX_LOCAL", "CHROMA_API_KEY", "VOYAGE_API_KEY", "NX_SERVICE_URL"):
            monkeypatch.delenv(var, raising=False)
        if pg_creds:
            from nexus.db.pg_provision import CREDENTIALS_FILENAME
            (cfg / CREDENTIALS_FILENAME).write_text("PGHOST=127.0.0.1\n")

    # ── RDR-188 P3.1 (nexus-9o6y2.13): explicit local signal + no voyage clause ──

    def test_pg_credentials_is_the_explicit_local_signal(self, tmp_path, monkeypatch):
        """A provisioned local service (pg_credentials present) is LOCAL even
        when legacy cloud keys sit in the environment (they are migration-
        source / engine-bootstrap material, not mode signals)."""
        self._cfg(tmp_path, monkeypatch, pg_creds=True)
        monkeypatch.setenv("CHROMA_API_KEY", "k")
        monkeypatch.setenv("VOYAGE_API_KEY", "k")
        assert is_local_mode() is True

    def test_service_url_beats_pg_credentials(self, tmp_path, monkeypatch):
        """A migrated local→managed install keeps its old pg_credentials on
        disk; the configured service_url must win (managed, not local)."""
        self._cfg(tmp_path, monkeypatch, pg_creds=True)
        monkeypatch.setenv("NX_SERVICE_URL", "https://m.example")
        assert is_local_mode() is False

    def _record(self, tmp_path, monkeypatch, mode, *, pg_creds=False):
        self._cfg(tmp_path, monkeypatch, pg_creds=pg_creds)
        import yaml
        cfg_file = tmp_path / "cfg" / "config.yml"
        cfg_file.write_text(yaml.safe_dump({"install": {"mode": mode}}))

    def test_pg_creds_plus_legacy_chroma_key_is_local(self, tmp_path, monkeypatch):
        """RDR-155 P4b: the ambiguous-corner warn died with the chroma
        credential map — pg_credentials resolves LOCAL, silently; a legacy
        chroma key in the env is inert."""
        self._cfg(tmp_path, monkeypatch, pg_creds=True)
        monkeypatch.setenv("CHROMA_API_KEY", "k")
        assert is_local_mode() is True

    def test_recorded_local_wins_over_artifact_inference(self, tmp_path, monkeypatch):
        """An explicit install.mode=local record resolves LOCAL with no
        artifacts and no warning — record beats inference."""
        self._record(tmp_path, monkeypatch, "local", pg_creds=False)
        monkeypatch.setenv("CHROMA_API_KEY", "k")  # would infer cloud without the record
        assert is_local_mode() is True

    def test_recorded_managed_resolves_false_without_service_url(self, tmp_path, monkeypatch):
        """install.mode=managed resolves managed even before service_url is
        configured (mid-onboarding shapes)."""
        self._record(tmp_path, monkeypatch, "managed", pg_creds=True)
        assert is_local_mode() is False

    def test_no_warning_with_record_and_legacy_key(self, tmp_path, monkeypatch):
        """RDR-155 P4b: the ambiguous-corner warn is gone entirely — a
        recorded mode plus a legacy chroma key resolves silently."""
        self._record(tmp_path, monkeypatch, "local", pg_creds=True)
        monkeypatch.setenv("CHROMA_API_KEY", "k")
        events: list[str] = []

        class _Cap:
            def warning(self, event, **kw):
                events.append(event)

        monkeypatch.setattr("structlog.get_logger", lambda *a, **k: _Cap())
        assert is_local_mode() is True
        assert events == []

    def test_stale_local_record_with_service_url_warns_and_service_url_wins(
        self, tmp_path, monkeypatch,
    ):
        """Contradiction: a configured service_url beside install.mode=local —
        the configured endpoint wins (nexus-3k43p posture) but LOUDLY."""
        import nexus.config as config_mod
        self._record(tmp_path, monkeypatch, "local", pg_creds=False)
        monkeypatch.setenv("NX_SERVICE_URL", "https://m.example")
        monkeypatch.setattr(config_mod, "_mode_record_contradiction_warned", False)
        events: list[str] = []

        class _Cap:
            def warning(self, event, **kw):
                events.append(event)

        monkeypatch.setattr("structlog.get_logger", lambda *a, **k: _Cap())
        assert is_local_mode() is False
        assert events == ["mode_record_contradicts_service_url"]

    def test_nx_local_env_beats_the_record(self, tmp_path, monkeypatch):
        self._record(tmp_path, monkeypatch, "managed", pg_creds=False)
        monkeypatch.setenv("NX_LOCAL", "1")
        assert is_local_mode() is True

    def test_garbage_record_value_falls_through_to_inference(self, tmp_path, monkeypatch):
        self._record(tmp_path, monkeypatch, "purple", pg_creds=True)
        assert is_local_mode() is True  # pg_credentials inference still applies

    def test_blanking_voyage_key_never_changes_mode(self, tmp_path, monkeypatch):
        """Gap 3's regression pin: the client no longer consumes the voyage
        key (RDR-188), so its presence/absence must have ZERO mode influence
        in every branch."""
        self._cfg(tmp_path, monkeypatch, pg_creds=False)
        monkeypatch.setenv("CHROMA_API_KEY", "k")
        monkeypatch.setenv("VOYAGE_API_KEY", "k")
        with_key = is_local_mode()
        monkeypatch.delenv("VOYAGE_API_KEY")
        assert is_local_mode() is with_key

        monkeypatch.delenv("CHROMA_API_KEY")
        monkeypatch.setenv("VOYAGE_API_KEY", "k")
        with_key_no_chroma = is_local_mode()
        monkeypatch.delenv("VOYAGE_API_KEY")
        assert is_local_mode() is with_key_no_chroma


class TestBackfillInstallModeRecord:
    """nexus-g7ijj: ``set_config_value("install.mode", ...)`` is written only
    at ``nx init`` (local provisioning or managed onboarding) — an install
    that reached its current state purely via ``nx upgrade`` never got the
    record, so ``is_local_mode()`` fell through to ``pg_credentials``
    artifact inference forever. ``backfill_install_mode_record()`` closes
    that gap; precedence mirrors ``is_local_mode()``'s own reading of the
    record (service_url beats pg_credentials; a valid record is untouched;
    a garbage record is treated as unrecorded)."""

    def _cfg(self, tmp_path, monkeypatch, *, pg_creds: bool):
        cfg = tmp_path / "cfg"
        cfg.mkdir(exist_ok=True)
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg))
        monkeypatch.setenv("HOME", str(tmp_path))
        for var in ("NX_LOCAL", "CHROMA_API_KEY", "VOYAGE_API_KEY", "NX_SERVICE_URL"):
            monkeypatch.delenv(var, raising=False)
        if pg_creds:
            from nexus.db.pg_provision import CREDENTIALS_FILENAME
            (cfg / CREDENTIALS_FILENAME).write_text("PGHOST=127.0.0.1\n")
        return cfg

    def _record(self, tmp_path, monkeypatch, mode, *, pg_creds=False):
        cfg = self._cfg(tmp_path, monkeypatch, pg_creds=pg_creds)
        import yaml
        (cfg / "config.yml").write_text(yaml.safe_dump({"install": {"mode": mode}}))
        return cfg

    def _persist_service_url(self, cfg, url="https://m.example"):
        """Write ``credentials.service_url`` into config.yml directly (the
        FILE-backed evidence path), never via env — used to distinguish
        durable (file) evidence from a transient env override."""
        import yaml
        p = cfg / "config.yml"
        data = yaml.safe_load(p.read_text()) if p.exists() else {}
        data = data or {}
        data.setdefault("credentials", {})["service_url"] = url
        p.write_text(yaml.safe_dump(data))

    def test_recorded_local_is_a_noop(self, tmp_path, monkeypatch):
        self._record(tmp_path, monkeypatch, "local")
        assert backfill_install_mode_record() is None

    def test_recorded_managed_is_a_noop(self, tmp_path, monkeypatch):
        self._record(tmp_path, monkeypatch, "managed")
        assert backfill_install_mode_record() is None

    def test_no_record_with_persisted_service_url_stamps_managed(self, tmp_path, monkeypatch):
        cfg = self._cfg(tmp_path, monkeypatch, pg_creds=False)
        self._persist_service_url(cfg)
        assert backfill_install_mode_record() == "managed"
        assert is_local_mode() is False  # integration: the record now resolves it

    def test_no_record_with_pg_credentials_stamps_local(self, tmp_path, monkeypatch):
        self._cfg(tmp_path, monkeypatch, pg_creds=True)
        assert backfill_install_mode_record() == "local"
        assert is_local_mode() is True

    def test_virgin_box_stamps_nothing(self, tmp_path, monkeypatch):
        """Neither signal present: nx init owns first stamping, not this
        backfill."""
        self._cfg(tmp_path, monkeypatch, pg_creds=False)
        assert backfill_install_mode_record() is None

    def test_garbage_record_with_pg_credentials_restamps_local(self, tmp_path, monkeypatch):
        self._record(tmp_path, monkeypatch, "purple", pg_creds=True)
        assert backfill_install_mode_record() == "local"
        assert is_local_mode() is True

    def test_persisted_service_url_beats_pg_credentials_when_both_present(self, tmp_path, monkeypatch):
        cfg = self._cfg(tmp_path, monkeypatch, pg_creds=True)
        self._persist_service_url(cfg)
        assert backfill_install_mode_record() == "managed"
        assert is_local_mode() is False

    def test_unwritable_config_dir_returns_none_without_raising(self, tmp_path, monkeypatch):
        self._cfg(tmp_path, monkeypatch, pg_creds=True)
        with patch("nexus.config.set_config_value", side_effect=OSError("readonly fs")):
            assert backfill_install_mode_record() is None

    # ── nexus-g7ijj fix round: malformed config.yml must never raise ────────

    def test_malformed_config_yml_returns_none_without_raising(self, tmp_path, monkeypatch):
        cfg = self._cfg(tmp_path, monkeypatch, pg_creds=True)
        # Unclosed flow sequence: yaml.safe_load raises yaml.parser.ParserError.
        (cfg / "config.yml").write_text("foo: [1, 2\n")
        assert backfill_install_mode_record() is None

    # ── nexus-g7ijj fix round: NX_LOCAL takes precedence, same as is_local_mode ──

    def test_nx_local_1_skips_stamping_even_with_pg_credentials(self, tmp_path, monkeypatch):
        cfg = self._cfg(tmp_path, monkeypatch, pg_creds=True)
        monkeypatch.setenv("NX_LOCAL", "1")
        assert backfill_install_mode_record() is None
        assert not (cfg / "config.yml").exists()

    def test_nx_local_0_skips_stamping_even_with_service_url(self, tmp_path, monkeypatch):
        cfg = self._cfg(tmp_path, monkeypatch, pg_creds=False)
        self._persist_service_url(cfg)
        monkeypatch.setenv("NX_LOCAL", "0")
        import yaml
        before = yaml.safe_load((cfg / "config.yml").read_text())
        assert backfill_install_mode_record() is None
        after = yaml.safe_load((cfg / "config.yml").read_text())
        assert "install" not in after
        assert before == after  # untouched, not just "no install key added"

    # ── nexus-g7ijj fix round: only FILE-backed service_url counts as durable
    # evidence for the stamp; a transient NX_SERVICE_URL env var must not
    # permanently stamp "managed" onto a genuinely local box.

    def test_transient_service_url_env_with_pg_credentials_stamps_local(self, tmp_path, monkeypatch):
        self._cfg(tmp_path, monkeypatch, pg_creds=True)
        monkeypatch.setenv("NX_SERVICE_URL", "https://transient.example")
        assert backfill_install_mode_record() == "local"
        # is_local_mode()'s own runtime read is UNCHANGED: the env override
        # still wins for THIS session.
        assert is_local_mode() is False
        monkeypatch.delenv("NX_SERVICE_URL", raising=False)
        # once the transient env is gone, the durable "local" record resolves it.
        assert is_local_mode() is True

    def test_transient_service_url_env_without_pg_credentials_stamps_nothing(self, tmp_path, monkeypatch):
        self._cfg(tmp_path, monkeypatch, pg_creds=False)
        monkeypatch.setenv("NX_SERVICE_URL", "https://transient.example")
        assert backfill_install_mode_record() is None


class TestLegacyChromaDir:
    # RDR-155 P4b: config._default_local_path retired; the FROZEN legacy
    # location lives in stranded_install.legacy_chroma_dir (detector probe).
    @pytest.mark.parametrize(
        ("env", "expected_suffix"),
        [
            pytest.param({}, ".local/share/nexus/chroma", id="default"),
            pytest.param({"XDG_DATA_HOME": "/custom/data"}, None, id="xdg"),
            pytest.param({"NX_LOCAL_CHROMA_PATH": "/my/chroma"}, None, id="env_override"),
        ],
    )
    def test_default_local_path(
        self, env: dict[str, str], expected_suffix: str | None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        monkeypatch.delenv("NX_LOCAL_CHROMA_PATH", raising=False)
        monkeypatch.setenv("HOME", "/home/testuser")
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        result = legacy_chroma_dir()
        if "NX_LOCAL_CHROMA_PATH" in env:
            assert result == Path(env["NX_LOCAL_CHROMA_PATH"])
        elif "XDG_DATA_HOME" in env:
            assert result == Path("/custom/data/nexus/chroma")
        else:
            assert result == Path(f"/home/testuser/{expected_suffix}")


# ── db/local_ef.py: LocalEmbeddingFunction ────────────────────────────────────


class TestLocalEmbeddingFunction:
    def test_tier0_no_fastembed(self) -> None:
        with patch.dict("sys.modules", {"fastembed": None}):
            ef = LocalEmbeddingFunction()
            assert ef.model_name == "all-MiniLM-L6-v2"
            assert ef.dimensions == 384

    @pytest.mark.parametrize("n_texts", [1, 3], ids=["single", "batch"])
    def test_tier0_embeds(self, local_ef: LocalEmbeddingFunction, n_texts: int) -> None:
        result = local_ef(["hello"] * n_texts)
        assert len(result) == n_texts
        for vec in result:
            assert len(vec) == 384

    def test_explicit_model_override(self) -> None:
        ef = LocalEmbeddingFunction(model_name="all-MiniLM-L6-v2")
        assert ef.model_name == "all-MiniLM-L6-v2"
        assert ef.dimensions == 384

    def test_unknown_model_name_raises_naming_the_model(self) -> None:
        """nexus-r4fdv: an unrecognised model name used to silently default
        to 384 dimensions — the least-used dim in this deployment, so the
        fallback was the value least likely to be right, and a wrong width
        is silently-wrong data, not a degraded result (no-silent-fallbacks
        directive). Must raise, naming the model and the known set."""
        with pytest.raises(ValueError, match="totally-made-up-model"):
            LocalEmbeddingFunction(model_name="totally-made-up-model")


# ── db/t3.py: T3Database local_mode ──────────────────────────────────────────


class TestT3DatabaseLocalMode:
    def test_local_mode_init(self, local_db: T3Database) -> None:
        from nexus.db.inmemory_vector_store import InMemoryVectorClient

        assert local_db._local_mode is True
        # RDR-155 P4b P0a: the injected test substrate is the in-memory
        # client (was chromadb.ClientAPI).
        assert isinstance(local_db._client, InMemoryVectorClient)
        # nexus-sghyo (2026-08-06): T3Database no longer constructs a
        # Voyage client at all — client-side embedding is retired (Hal
        # determination 2026-07-28). The always-None ``_voyage_client``
        # attribute this used to assert on is gone, not just unset.
        assert not hasattr(local_db, "_voyage_client")

    def test_local_mode_no_cloud_probe(self, tmp_path: Path, local_ef: LocalEmbeddingFunction) -> None:
        # RDR-155 P4a.2: local mode without an injected client fails loud (the
        # PersistentClient serving open is retired) and never probes the cloud.
        #
        # P3: the `patch("nexus.db.t3.chromadb.CloudClient")` tripwire that
        # wrapped this is GONE — there is no chromadb to patch, so "never
        # constructs a CloudClient" is structural rather than observable. That
        # half is now owned by test_rdr155_p4b_deletion_gate.py (module
        # absence) and test_p4b_collection_not_found_contract.py
        # (test_chromadb_is_not_importable). What remains is the live
        # behaviour: local mode without an injected client fails loud.
        with pytest.raises(RuntimeError, match="RDR-155 Phase 4a"):
            T3Database(local_mode=True, local_path=str(tmp_path / "chroma"), _ef_override=local_ef)

    def test_local_mode_put_and_search(self, local_db: T3Database) -> None:
        doc_id = local_db.put(
            collection="knowledge__test",
            content="Python is a programming language",
            title="python-fact",
        )
        assert doc_id
        results = local_db.search("programming language", collection_names=["knowledge__test"])
        assert len(results) == 1
        assert any("Python" in r.get("content", "") for r in results)

    def test_local_mode_put_does_not_emit_source_path(
        self, local_db: T3Database,
    ) -> None:
        """RDR-102 D2 / Phase B per-writer absence guard for the MCP
        ``store_put`` path (db/t3.py:627). MCP-stored docs are
        single-chunk and route through ``make_chunk_metadata`` like
        every other writer; after Phase B they MUST land without
        source_path. Closes the RF-4 inventory: 6 indexer call sites
        + this MCP put site = 7 writer paths verified absent.
        """
        local_db.put(
            collection="knowledge__source_path_check",
            content="MCP put has no on-disk source",
            title="mcp-no-source-path",
        )
        col = local_db.get_or_create_collection(
            "knowledge__source_path_check",
        )
        rows = col.get(include=["metadatas"])
        assert rows["metadatas"], "expected MCP put to land at least one chunk"
        leaked = [m for m in rows["metadatas"] if "source_path" in m]
        assert not leaked, (
            f"{len(leaked)}/{len(rows['metadatas'])} MCP store_put "
            f"chunks still carry source_path. Phase B dropped the "
            f"source_path= kwarg from db/t3.py:627; if this test fails "
            f"a regression has re-added it OR ALLOWED_TOP_LEVEL has "
            f"re-acquired source_path."
        )

    def test_local_mode_search_skips_cce(self, local_db: T3Database) -> None:
        # nexus-sghyo (2026-08-06): see test_local_mode_init — the client
        # constructs no Voyage client at all now (client-side embedding
        # retired), so there is no CCE branch left for local mode to skip
        # via a null voyage client; this test's remaining job is proving
        # local-mode search still works (the CCE branch was deleted, not
        # merely bypassed).
        local_db.put(collection="knowledge__test", content="test content", title="t1")
        results = local_db.search("test", collection_names=["knowledge__test"])
        assert isinstance(results, list)

    def test_local_mode_skips_max_query_results_clamping(self, local_db: T3Database) -> None:
        for i in range(5):
            local_db.put(collection="knowledge__test", content=f"document {i}", title=f"doc-{i}")
        results = local_db.search("document", collection_names=["knowledge__test"], n_results=500)
        assert isinstance(results, list)

    def test_cloud_mode_still_works(self) -> None:
        mock_client = MagicMock()
        db = T3Database(_client=mock_client, _ef_override=MagicMock())
        assert db._local_mode is False

    def test_local_mode_creates_path(self, tmp_path: Path, local_ef: LocalEmbeddingFunction) -> None:
        chroma_dir = tmp_path / "nonexistent" / "chroma"
        T3Database(
            local_mode=True,
            local_path=str(chroma_dir),
            _client=make_vector_test_client(),
            _ef_override=local_ef,
        )
        assert chroma_dir.exists()


# ── db/__init__.py: make_t3 local path ────────────────────────────────────────


class TestMakeT3Local:
    def test_make_t3_local_mode_routes_to_service(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """RDR-155 P4a.2 (nexus-1k8s1): local mode without injected
        ``_client`` routes T3 serving to the pgvector-backed
        nexus-service — ``make_t3`` returns the HttpVectorClient
        singleton (the chroma-daemon leg is retired)."""
        monkeypatch.setenv("NX_LOCAL", "1")
        monkeypatch.setenv("NX_LOCAL_CHROMA_PATH", str(tmp_path / "chroma"))
        monkeypatch.setenv("HOME", str(tmp_path))

        from nexus.db import make_t3
        from nexus.db.http_vector_client import (
            HttpVectorClient,
            get_http_vector_client,
        )

        result = make_t3()
        assert isinstance(result, HttpVectorClient)
        assert result is get_http_vector_client()

    def test_make_t3_local_mode_with_injected_client_skips_service(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The ``_client`` injection path stays open as the canonical
        test substitute (EphemeralClient / MagicMock). Verifies that
        passing ``_client`` short-circuits service dispatch and returns
        the T3Database facade."""
        monkeypatch.setenv("NX_LOCAL", "1")
        monkeypatch.setenv("NX_LOCAL_CHROMA_PATH", str(tmp_path / "chroma"))
        monkeypatch.setenv("HOME", str(tmp_path))
        from nexus.db import make_t3

        result = make_t3(_client=MagicMock(), _ef_override=MagicMock())
        assert isinstance(result, T3Database)
        assert result._local_mode is False  # injected path = cloud-like construction

    def test_make_t3_cloud_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NX_LOCAL", "0")
        from nexus.db import make_t3
        assert make_t3(_client=MagicMock(), _ef_override=MagicMock())._local_mode is False


# ── Staleness round-trip ──────────────────────────────────────────────────────


class TestLocalStaleness:
    @pytest.mark.parametrize(
        ("query_hash", "query_model", "expected"),
        [
            ("abc123", "all-MiniLM-L6-v2", True),
            ("def456", "all-MiniLM-L6-v2", False),
            ("abc123", "voyage-code-3", False),
        ],
        ids=["same-skip", "changed-hash", "changed-model"],
    )
    def test_staleness(
        self, local_db: T3Database, query_hash: str, query_model: str, expected: bool
    ) -> None:
        from nexus.indexer_utils import check_staleness

        # RDR-103 Phase 5: pre-create with strict=False so the test
        # can keep its legacy 2-segment fixture name.
        col = local_db.get_or_create_collection("code__test", strict=False)
        # RDR-102 D2 dropped source_path from ALLOWED_TOP_LEVEL, so
        # normalize() filters it out at write time — the staleness
        # check now keys on doc_id (the catalog tumbler) rather than
        # source_path. Stamp doc_id at upsert and pass it to
        # check_staleness so the test exercises the post-Phase-A /
        # post-Phase-B identity path.
        local_db.upsert_chunks(
            "code__test",
            ids=["chunk1"],
            documents=["def hello(): pass"],
            metadatas=[{
                "doc_id": "1.7.42",
                "content_hash": "abc123",
                "embedding_model": "all-MiniLM-L6-v2",
            }],
        )
        assert check_staleness(
            col, "/repo/hello.py", query_hash, query_model,
            doc_id="1.7.42",
        ) is expected


# ── Collection lifecycle ──────────────────────────────────────────────────────


class TestLocalCollectionLifecycle:
    def test_collection_lifecycle(self, local_db: T3Database) -> None:
        doc_id = local_db.put(
            collection="knowledge__lifecycle",
            content="Rust is a systems programming language",
            title="rust-fact",
            tags="rust,systems",
        )
        assert doc_id

        results = local_db.search("systems programming", collection_names=["knowledge__lifecycle"])
        assert len(results) == 1
        assert any("Rust" in r.get("content", "") for r in results)

        names = [c["name"] for c in local_db.list_collections()]
        assert "knowledge__lifecycle" in names
        assert len(local_db.list_store("knowledge__lifecycle")) == 1
        assert local_db.delete_by_id("knowledge__lifecycle", doc_id) is True

    def test_expire_ttl_entries(self, local_db: T3Database) -> None:
        """``expire()`` deletes entries whose ``indexed_at + ttl_days``
        is in the past; permanent entries (omitted ``ttl_days``, i.e.
        ``None``) are kept. ``expires_at`` was removed from the schema;
        expiry is derived Python-side via ``metadata_schema.is_expired``.

        nexus-tk070.p6b fix-pass (nexus-24rof, RDR-194 D5): ``put()`` now
        REJECTS an explicit ``ttl_days=0`` — the permanent case below omits
        the argument entirely (the new default, ``None``) rather than
        passing ``0``.
        """
        from datetime import UTC, datetime, timedelta
        from nexus.metadata_schema import make_chunk_metadata
        import hashlib

        # Backdate the indexed_at by 100 days with ttl_days=1 → expired.
        old = (datetime.now(UTC) - timedelta(days=100)).isoformat()
        col = local_db.get_or_create_collection("knowledge__expire_test", strict=False)
        h_temp = hashlib.sha256(b"temporary data").hexdigest()
        col.upsert(
            ids=["temp-id"],
            documents=["temporary data"],
            metadatas=[make_chunk_metadata(
                content_type="prose",
                chunk_text_hash=h_temp, content_hash=h_temp,
                chunk_end_char=14,
                indexed_at=old, ttl_days=1,
                embedding_model="local-onnx-minilm-l6-v2",
                title="temp",
            )],
        )
        # Permanent entry — still alive after expire(). ttl_days omitted
        # (defaults to None); an explicit 0 is now rejected (nexus-24rof).
        local_db.put(
            collection="knowledge__expire_test",
            content="permanent data",
            title="perm",
        )
        assert local_db.expire() == 1
        assert len(local_db.search("permanent", collection_names=["knowledge__expire_test"])) == 1


# ── Corpus model consistency ──────────────────────────────────────────────────


class TestCorpusLocalModels:
    def test_local_ef_model_name_consistent(self, local_ef: LocalEmbeddingFunction) -> None:
        assert local_ef.model_name
        assert isinstance(local_ef.model_name, str)

    @pytest.mark.parametrize(
        ("collection", "expected_model"),
        [("code__test", "voyage-code-3"), ("docs__test", "voyage-context-3")],
    )
    def test_corpus_functions_return_cloud_names(self, collection: str, expected_model: str) -> None:
        from nexus.corpus import index_model_for_collection, embedding_model_for_collection
        assert index_model_for_collection(collection) == expected_model
        assert embedding_model_for_collection(collection) == expected_model


# ── Frecency-only local mode ─────────────────────────────────────────────────


class TestFrecencyOnlyLocalMode:
    def test_frecency_only_no_crash(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NX_LOCAL", "1")
        monkeypatch.setenv("NX_LOCAL_CHROMA_PATH", str(tmp_path / "chroma"))
        monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
        monkeypatch.delenv("CHROMA_API_KEY", raising=False)

        # RDR-155 P4a.2 (nexus-1k8s1): local mode without injected _client
        # routes to the pgvector service. Stub make_t3 so this test
        # exercises the frecency code path without a running service.
        from nexus.db.t3 import T3Database

        def _stub_make_t3(*, _client=None, _ef_override=None):
            ef = MagicMock()
            ef.return_value = [[0.1, 0.2, 0.3]]
            return T3Database(
                _client=make_vector_test_client(),
                _ef_override=ef,
                local_mode=True,
            )

        monkeypatch.setattr("nexus.db.make_t3", _stub_make_t3)

        from nexus.indexer import _run_index_frecency_only

        # RDR-103 Phase 5: registry values are conformant 4-segment
        # post-flip; the frecency path goes through the strict
        # get_or_create_collection which rejects legacy 2-segment.
        registry = MagicMock()
        registry.get.return_value = {
            "collection": "code__repo__voyage-code-3__v1",
            "code_collection": "code__repo__voyage-code-3__v1",
            "docs_collection": "docs__repo__voyage-context-3__v1",
        }
        with patch("nexus.frecency.batch_frecency", return_value={}):
            _run_index_frecency_only(tmp_path, registry)


# ── Check local path writable ────────────────────────────────────────────────


class TestCheckLocalPathWritable:
    def test_writable_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NX_LOCAL_CHROMA_PATH", str(tmp_path / "chroma"))
        from nexus.indexer_utils import check_local_path_writable
        check_local_path_writable()

    def test_unwritable_path_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NX_LOCAL_CHROMA_PATH", "/proc/nonexistent/chroma")
        from nexus.indexer_utils import check_local_path_writable
        from nexus.errors import CredentialsMissingError
        with pytest.raises(CredentialsMissingError, match="not writable"):
            check_local_path_writable()


@pytest.fixture(autouse=True)
def _legacy_vector_backend(monkeypatch):
    """nexus-tawx0: service mode is the post-P4a DEFAULT (no-Python-embed
    stubs fire unless opted out). This module tests the legacy
    chroma/local embed pipeline, which is exactly the chroma-injected
    configuration the opt-out exists for."""
    monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "chroma")
