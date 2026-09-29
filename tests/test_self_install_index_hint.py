# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx self install`` names the package index when a generation build cannot reach it (nexus-12pyx).

Reported 2026-09-28 on a work Mac: ``nx self install`` failed with
``Error: generation build failed:`` and uv's raw ``Request failed after 3
retries ... Failed to fetch https://artifactory.../simple/conexus/ ... dns
error``. The cause was a corporate index (``UV_INDEX_URL`` or a uv config
file) unreachable off VPN. Nothing on the nexus side was wrong and the
running generation was untouched, and the message said neither.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import click
import pytest

from nexus.commands import self_cmd

# The shape uv printed on the reporting machine (host and path from the bead).
_UV_DNS_FAILURE = (
    "  x Failed to build conexus\n"
    "  Request failed after 3 retries\n"
    "  Failed to fetch https://artifactory.infra.us-east-1.conductor.sh"
    "/artifactory/api/pypi/pypi-local/simple/conexus/\n"
    "  dns error: failed to lookup address information: "
    "nodename nor servname provided, or not known\n"
)
_HOST = "artifactory.infra.us-east-1.conductor.sh"
_RESOLVER_FAILURE = (
    "  x No solution found when resolving dependencies:\n"
    "  Because conexus==99 was not found in the package registry\n"
)


class TestTheHint:
    def test_a_dns_failure_names_the_index_and_where_it_is_set(self, tmp_path: Path):
        env = {"UV_INDEX_URL": f"https://{_HOST}/artifactory/api/pypi/pypi-local/simple"}
        hint = self_cmd.index_failure_hint(_UV_DNS_FAILURE, env=env, home=tmp_path)
        assert hint is not None
        assert _HOST in hint
        assert "UV_INDEX_URL" in hint

    def test_it_says_the_install_is_unchanged_and_offers_both_remedies(self, tmp_path: Path):
        hint = self_cmd.index_failure_hint(_UV_DNS_FAILURE, env={}, home=tmp_path)
        assert hint is not None
        assert "not changed" in hint
        assert "VPN" in hint
        assert "UV_INDEX_URL=https://pypi.org/simple nx self install" in hint

    def test_it_mentions_the_engine_download(self, tmp_path: Path):
        hint = self_cmd.index_failure_hint(_UV_DNS_FAILURE, env={}, home=tmp_path)
        assert hint is not None and "GitHub" in hint

    def test_a_uv_config_file_that_names_the_host_is_reported_by_path(self, tmp_path: Path):
        cfg = tmp_path / "xdg" / "uv" / "uv.toml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text(f'index-url = "https://{_HOST}/artifactory/api/pypi/pypi-local/simple"\n')
        env = {"XDG_CONFIG_HOME": str(tmp_path / "xdg")}
        hint = self_cmd.index_failure_hint(_UV_DNS_FAILURE, env=env, home=tmp_path)
        assert hint is not None
        assert str(cfg) in hint

    def test_a_pip_config_naming_the_host_is_reported_with_the_caveat(self, tmp_path: Path):
        cfg = tmp_path / ".config" / "pip" / "pip.conf"
        cfg.parent.mkdir(parents=True)
        cfg.write_text(f"[global]\nindex-url = https://{_HOST}/simple\n")
        hint = self_cmd.index_failure_hint(_UV_DNS_FAILURE, env={}, home=tmp_path)
        assert hint is not None
        assert str(cfg) in hint
        assert "uv does not read" in hint

    def test_an_unlocated_setting_is_said_to_be_unlocated_not_invented(self, tmp_path: Path):
        hint = self_cmd.index_failure_hint(_UV_DNS_FAILURE, env={}, home=tmp_path)
        assert hint is not None
        assert _HOST in hint
        assert "No setting naming" in hint

    def test_credentials_in_the_index_url_are_never_echoed(self, tmp_path: Path):
        leaky = _UV_DNS_FAILURE.replace("https://", "https://alice:s3cret@")
        env = {"UV_INDEX_URL": f"https://alice:s3cret@{_HOST}/simple"}
        hint = self_cmd.index_failure_hint(leaky, env=env, home=tmp_path)
        assert hint is not None
        assert "s3cret" not in hint and "alice" not in hint

    @pytest.mark.parametrize(
        "line",
        [
            "dns error: failed to lookup address information",
            "error sending request for url (https://x.example/simple/conexus/)",
            "tcp connect error: Connection refused (os error 61)",
            "Request failed after 3 retries",
            "operation timed out",
        ],
    )
    def test_every_network_shape_is_recognised(self, tmp_path: Path, line: str):
        stderr = f"  Failed to fetch https://x.example/simple/conexus/\n  {line}\n"
        assert self_cmd.index_failure_hint(stderr, env={}, home=tmp_path) is not None

    def test_a_non_network_failure_gets_no_hint(self, tmp_path: Path):
        """A resolver failure is not the index being unreachable; blaming the
        network there would send the user after the wrong cause."""
        assert self_cmd.index_failure_hint(_RESOLVER_FAILURE, env={}, home=tmp_path) is None

    def test_a_network_failure_with_no_url_still_hints_without_a_host(self, tmp_path: Path):
        hint = self_cmd.index_failure_hint("dns error: failed to lookup address", env={}, home=tmp_path)
        assert hint is not None
        assert "not changed" in hint


class TestTheBuildFailureMessage:
    """Through the real ``_build_flip_shims``, driving a real failing script."""

    def _failing_build(self, tmp_path: Path, stderr: str) -> list[str]:
        script = tmp_path / "fake_install_generation.sh"
        script.write_text(f"#!/usr/bin/env bash\ncat >&2 <<'EOF'\n{stderr}EOF\nexit 1\n")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        return ["bash", str(script)]

    def _fail(self, tmp_path: Path, stderr: str, monkeypatch) -> str:
        monkeypatch.delenv("UV_INDEX_URL", raising=False)
        with pytest.raises(click.ClickException) as exc:
            self_cmd._build_flip_shims(
                self._failing_build(tmp_path, stderr),
                install_dir=tmp_path, tools=tmp_path / "tools", bin_dir=tmp_path / "bin",
            )
        return exc.value.message

    def test_the_summary_comes_first_and_uvs_own_output_stays_below_it(self, tmp_path, monkeypatch):
        message = self._fail(tmp_path, _UV_DNS_FAILURE, monkeypatch)
        assert message.startswith("generation build failed:")
        assert _HOST in message
        assert message.index("not changed") < message.index("uv output:")
        assert message.index("uv output:") < message.index("Request failed after 3 retries")

    def test_a_non_network_failure_keeps_the_original_shape(self, tmp_path, monkeypatch):
        message = self._fail(tmp_path, _RESOLVER_FAILURE, monkeypatch)
        assert message == f"generation build failed:\n{_RESOLVER_FAILURE.strip()}"

    def test_nothing_was_flipped(self, tmp_path, monkeypatch):
        tools = tmp_path / "tools"
        self._fail(tmp_path, _UV_DNS_FAILURE, monkeypatch)
        assert not (tools / "current").exists()
        assert not any(p.name.startswith("gen-") for p in tools.iterdir())
        assert os.path.isdir(tools)
