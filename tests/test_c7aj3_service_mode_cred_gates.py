# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-c7aj3 / nexus-nmw3i: legacy Chroma-credential gates must not block
service-backed installs.

The repro these pin: a migrated (or born-cloud) install — ``service_url``
persisted, NO legacy Chroma credentials anywhere. Pre-fix: every CLI T3
verb (``nx search`` / ``store`` / ``collection`` — 22 call sites through
``store._t3`` — plus ``memory promote``) hard-failed "chroma_api_key not
set"; ``nx doctor`` exit-1'd on three fatal credential lines; and the
credential-persistence check false-flagged shell-env-only legacy creds as
a mode-misdetection risk even though the persisted ``service_url`` anchors
the mode.

Resolution (critic-c7aj3 review): the credential pre-flight is DELETED —
``make_t3()`` is service-backed unconditionally (RDR-155 P4a.2), so no
call site could reach a direct-Chroma client and the gate only ever
produced false failures. Doctor's credential lines are informational
(migration-source config), never fatal. End-to-end coverage of the victim
scenario lives here (full ``CliRunner`` invocations with an empty
credential environment, only the backend handle mocked).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.commands.store import _t3


@pytest.fixture()
def no_cred_env(monkeypatch: pytest.MonkeyPatch):
    """The victim environment: no legacy cloud creds anywhere, non-local."""
    for var in ("CHROMA_API_KEY", "CHROMA_TENANT", "CHROMA_DATABASE", "VOYAGE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NX_LOCAL", "0")
    monkeypatch.delenv("NX_STORAGE_BACKEND_VECTORS", raising=False)
    with patch("nexus.config.get_credential", side_effect=lambda k: {
        "service_url": "https://svc.example",
    }.get(k)):
        yield


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _fake_t3() -> MagicMock:
    db = MagicMock()
    db.__enter__ = MagicMock(return_value=db)
    db.__exit__ = MagicMock(return_value=False)
    db.put.return_value = "doc-id-1"
    db.search.return_value = []
    db.list_store.return_value = []
    db.collection_info.return_value = {"count": 0}
    return db


class TestNoPreflight:
    def test_t3_constructs_without_any_creds(self, no_cred_env):
        """The 22-call-site entry point reaches make_t3() with no
        credential pre-flight — the gate is gone, not conditioned."""
        sentinel = object()
        with patch("nexus.commands.store.make_t3", return_value=sentinel):
            assert _t3() is sentinel

    def test_no_inline_cred_block_resurrected(self):
        """Neither store.py nor memory.py may regrow a Chroma-cred
        pre-flight (the c7aj3 bug class). make_t3()'s own errors are the
        only sanctioned construction failure."""
        import inspect

        from nexus.commands import memory as memory_mod
        from nexus.commands import store as store_mod

        for mod in (store_mod, memory_mod):
            src = inspect.getsource(mod)
            assert "chroma_api_key not set" not in src, mod.__name__
            assert '"chroma_api_key", "voyage_api_key", "chroma_database"' not in src, mod.__name__


class TestVictimScenarioEndToEnd:
    """Full CliRunner invocations in the no-creds environment — only the
    backend handle is mocked; the command wiring (including any gate that
    might be reintroduced) runs for real."""

    def test_search_works(self, runner, no_cred_env):
        db = _fake_t3()
        with patch("nexus.commands.store.make_t3", return_value=db), \
             patch("nexus.commands.search_cmd._search_collections", create=True) as _:
            result = runner.invoke(main, ["search", "anything"])
        assert "chroma_api_key" not in result.output.lower()
        # The command may fail later on mock-shape details; the bug class
        # is specifically the credential pre-flight message.

    def test_store_put_works(self, runner, no_cred_env, tmp_path):
        src = tmp_path / "f.txt"
        src.write_text("content")
        db = _fake_t3()
        with patch("nexus.commands.store.make_t3", return_value=db), \
             patch("nexus.corpus.t3_collection_name", return_value="knowledge__t__x__v1"), \
             patch("nexus.commands.store._catalog_store_hook", return_value=""), \
             patch("nexus.commands.store._single_chunk_manifest_metadata",
                   return_value=("cid", [{}])), \
             patch("nexus.hook_registry.HookRegistry"), \
             patch("nexus.hook_registry.install_default_hooks"):
            result = runner.invoke(main, ["store", "put", str(src), "--title", "t"])
        assert result.exit_code == 0, result.output

    def test_store_list_works(self, runner, no_cred_env):
        db = _fake_t3()
        with patch("nexus.commands.store.make_t3", return_value=db), \
             patch("nexus.corpus.t3_collection_name", return_value="knowledge__t__x__v1"):
            result = runner.invoke(main, ["store", "list"])
        assert result.exit_code == 0, result.output

    def test_memory_promote_reaches_t3(self, runner, no_cred_env):
        fake_t2 = MagicMock()
        fake_t2.__enter__ = MagicMock(return_value=fake_t2)
        fake_t2.__exit__ = MagicMock(return_value=False)
        fake_t2.memory.get.return_value = {
            "id": 1, "project": "p", "title": "note.md", "content": "hello",
            "tags": "", "timestamp": "2026-07-18T00:00:00+00:00", "ttl": 30,
        }
        db = _fake_t3()
        with patch("nexus.commands.memory.t2_handle", return_value=fake_t2), \
             patch("nexus.db.make_t3", return_value=db), \
             patch("nexus.corpus.t3_collection_name", return_value="knowledge__p__x__v1"):
            result = runner.invoke(
                main, ["memory", "promote", "1", "--collection", "knowledge__p"],
            )
        assert "chroma_api_key" not in result.output.lower()
        assert "voyage_api_key not set" not in result.output.lower()


class TestDoctorCredLines:
    def _cloud_results(self, *, creds_present: bool):
        from nexus.health import HealthResult, _check_t3_cloud

        def cred(key: str):
            return "x" if creds_present else None

        stub = HealthResult(label="stub", ok=True, detail="")
        with patch("nexus.config.get_credential", side_effect=cred), \
             patch("nexus.health._check_vector_service", return_value=stub), \
             patch("nexus.health._check_managed_service_probe", return_value=[stub]):
            return _check_t3_cloud()

    def test_absent_creds_informational_never_fatal(self):
        """nmw3i: absent legacy creds are migration-source status, never a
        failing/fatal doctor line."""
        results = self._cloud_results(creds_present=False)
        cred_lines = [r for r in results if "CHROMA" in r.label or "VOYAGE" in r.label.upper()]
        assert cred_lines, "credential lines disappeared from doctor output"
        for r in cred_lines:
            assert r.ok, f"{r.label} flagged failing"
            assert not getattr(r, "fatal", False)
        assert any("migration-source only" in r.detail for r in cred_lines)

    def test_pipeline_version_line_reports_retired(self):
        """reviewer-c7aj3 Medium: the pipeline-version line must not vanish
        without legacy creds — it reports the sweep as retired."""
        results = self._cloud_results(creds_present=False)
        pipeline_lines = [r for r in results if r.label == "pipeline versions"]
        assert pipeline_lines and pipeline_lines[0].ok
        assert "retired" in pipeline_lines[0].detail


class TestCredentialPersistenceCheck:
    """critic-c7aj3 Critical: the shell-env-only warning's premise
    (GUI spawn misdetects mode) is void when service_url is persisted —
    is_local_mode() anchors on it before any credential heuristic."""

    def _run(self, *, file_creds: dict, env: dict, monkeypatch, tmp_path):
        import yaml

        from nexus.health import _check_credential_persistence

        for var in ("CHROMA_API_KEY", "VOYAGE_API_KEY", "CHROMA_TENANT", "CHROMA_DATABASE"):
            monkeypatch.delenv(var, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        cfg = tmp_path / "config.yml"
        cfg.write_text(yaml.safe_dump({"credentials": file_creds}))
        with patch("nexus.config._global_config_path", return_value=cfg):
            return _check_credential_persistence()

    def test_persisted_service_url_silences_the_warning(self, monkeypatch, tmp_path):
        results = self._run(
            file_creds={"service_url": "https://api.example"},
            env={"CHROMA_API_KEY": "shell-only-key"},
            monkeypatch=monkeypatch, tmp_path=tmp_path,
        )
        assert results == [], (
            "shell-env-only legacy creds must not warn when service_url is "
            "persisted (the nmw3i false-flag)"
        )

    def test_no_persisted_service_url_still_warns(self, monkeypatch, tmp_path):
        """Polarity: without a persisted mode anchor the original
        nexus-m7evs warning keeps its teeth."""
        results = self._run(
            file_creds={},
            env={"CHROMA_API_KEY": "shell-only-key"},
            monkeypatch=monkeypatch, tmp_path=tmp_path,
        )
        assert len(results) == 1 and not results[0].ok
        assert "shell env only" in results[0].detail
