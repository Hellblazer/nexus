# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-hixe9: startup diagnostic for resolved T3 mode + embedder + config path.

Steve's field report: Desktop MCPB resolves LOCAL mode (bge-768) against
Hal's cloud (voyage-1024) collections, silently zero-ing every vector search
via the dimension-mismatch skip. A clean CLI simulation with the same
credentials resolves cloud correctly, so the GUI-spawned .mcpb's runtime
context (HOME / NEXUS_CONFIG_DIR / NX_LOCAL / which config.yml it reads)
must differ in a way not yet reproduced headlessly. This is not a fix for
that divergence (needs live Desktop access to diagnose) -- it is the
one-line structured startup log the bead calls for, so the next live run
leaves a durable on-disk trail instead of requiring reproduction.

nexus-smd1k (substantive-critic round-1 finding): the initial diagnostic
only evidenced ``is_local_mode()``'s NX_LOCAL branch, leaving the
service_url/legacy-key branches -- the more likely root-cause location per
the bead's own CLI-vs-GUI divergence evidence -- dark. Fixed by adding
``service_url_found``/``chroma_key_found``/``voyage_key_found`` booleans
(never the credential values themselves).
"""
from __future__ import annotations

from nexus.mcp.core import _resolve_mode_diagnostics


class TestResolveModeDiagnostics:
    def test_local_mode_returns_local_embedder_token(self, monkeypatch, tmp_path) -> None:
        monkeypatch.delenv("NX_LOCAL", raising=False)
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        monkeypatch.setattr(
            "nexus.db.local_ef.local_model_token", lambda: "minilm-l6-v2-384"
        )
        monkeypatch.setattr("nexus.config.nexus_config_dir", lambda: tmp_path)
        monkeypatch.setattr("nexus.config.get_credential", lambda name: "")

        result = _resolve_mode_diagnostics()

        assert result["mode"] == "local"
        assert result["local_embedder"] == "minilm-l6-v2-384"

    def test_cloud_mode_returns_none_local_embedder(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)
        monkeypatch.setattr("nexus.config.nexus_config_dir", lambda: tmp_path)
        monkeypatch.setattr("nexus.config.get_credential", lambda name: "")

        result = _resolve_mode_diagnostics()

        assert result["mode"] == "cloud"
        assert result["local_embedder"] is None

    def test_credential_presence_booleans_reflect_which_branch_resolved(
        self, monkeypatch, tmp_path
    ) -> None:
        # nexus-smd1k (substantive-critic finding on nexus-hixe9): is_local_mode()
        # has three decision branches (NX_LOCAL, service_url, legacy chroma/voyage
        # keys) -- the diagnostic must evidence WHICH one fired, as booleans only,
        # never the credential values themselves.
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)
        monkeypatch.setattr("nexus.config.nexus_config_dir", lambda: tmp_path)

        def _fake_credential(name: str) -> str:
            return {
                "service_url": "https://svc.example",
                "chroma_api_key": "",
                "voyage_api_key": "vk_live",
            }.get(name, "")

        monkeypatch.setattr("nexus.config.get_credential", _fake_credential)

        result = _resolve_mode_diagnostics()

        assert result["service_url_found"] is True
        # RDR-155 P4b: chroma_key_found dropped with the chroma credential map.
        assert "chroma_key_found" not in result
        assert result["voyage_key_found"] is True

    def test_credential_booleans_never_leak_the_actual_values(
        self, monkeypatch, tmp_path
    ) -> None:
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        monkeypatch.setattr(
            "nexus.db.local_ef.local_model_token", lambda: "minilm-l6-v2-384"
        )
        monkeypatch.setattr("nexus.config.nexus_config_dir", lambda: tmp_path)
        monkeypatch.setattr(
            "nexus.config.get_credential", lambda name: "super-secret-token-value"
        )

        result = _resolve_mode_diagnostics()

        assert "super-secret-token-value" not in repr(result)
        assert result["service_url_found"] is True

    def test_config_dir_reflects_resolved_path(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)
        monkeypatch.setattr("nexus.config.nexus_config_dir", lambda: tmp_path)
        monkeypatch.setattr("nexus.config.get_credential", lambda name: "")

        result = _resolve_mode_diagnostics()

        assert result["config_dir"] == str(tmp_path)

    def test_returns_home_and_nx_local_env_verbatim(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)
        monkeypatch.setattr("nexus.config.nexus_config_dir", lambda: tmp_path)
        monkeypatch.setattr("nexus.config.get_credential", lambda name: "")
        monkeypatch.setenv("HOME", "/Users/probe-home")
        monkeypatch.setenv("NX_LOCAL", "1")

        result = _resolve_mode_diagnostics()

        assert result["home"] == "/Users/probe-home"
        assert result["nx_local_env"] == "1"

    def test_never_raises_on_resolution_failure(self, monkeypatch) -> None:
        def _boom() -> bool:
            raise RuntimeError("credential store unreachable")

        monkeypatch.setattr("nexus.config.is_local_mode", _boom)

        result = _resolve_mode_diagnostics()

        assert result["mode"] == "unknown"
        assert "error" in result
