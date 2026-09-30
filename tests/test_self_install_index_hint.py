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

    def test_userinfo_query_and_fragment_are_never_echoed(self, tmp_path: Path):
        """An index URL can carry a token in any of three places."""
        leaky = _UV_DNS_FAILURE.replace(
            f"https://{_HOST}/artifactory/api/pypi/pypi-local/simple/conexus/",
            f"https://alice:s3cret@{_HOST}/artifactory/api/pypi/pypi-local/simple/conexus/"
            "?token=QSECRET&x=1#FRAGSECRET",
        )
        assert "QSECRET" in leaky  # the fixture really carries them
        env = {"UV_INDEX_URL": f"https://alice:s3cret@{_HOST}/simple?token=QSECRET"}
        hint = self_cmd.index_failure_hint(leaky, env=env, home=tmp_path)
        assert hint is not None
        for secret in ("s3cret", "alice", "QSECRET", "token=", "FRAGSECRET", "#"):
            assert secret not in hint
        assert f"https://{_HOST}/artifactory/api/pypi/pypi-local/simple/conexus/" in hint

    def test_a_backticked_url_is_read_without_the_backtick(self, tmp_path: Path):
        """uv's real form is ``Failed to fetch: `https://...` ``."""
        stderr = (
            "  Request failed after 3 retries\n"
            "  Failed to fetch: `https://x.example/simple/conexus/`\n"
            "  dns error: failed to lookup address\n"
        )
        hint = self_cmd.index_failure_hint(stderr, env={}, home=tmp_path)
        assert hint is not None
        assert "`" not in hint
        assert "https://x.example/simple/conexus/" in hint

    @pytest.mark.parametrize(
        "url", ["https://x.example:abc/simple/conexus/", "https://[abc/simple/conexus/"]
    )
    def test_an_unparseable_index_url_degrades_instead_of_raising(self, tmp_path: Path, url: str):
        """A bad port or a broken IPv6 literal makes urllib raise ValueError;
        that must not turn the ClickException into a traceback."""
        stderr = f"  Failed to fetch: `{url}`\n  dns error: failed to lookup address\n"
        hint = self_cmd.index_failure_hint(stderr, env={}, home=tmp_path)
        assert hint is not None
        assert "the package index it was using" in hint
        assert "not changed" in hint

    def test_a_certificate_failure_blames_certificates_not_reachability(self, tmp_path: Path):
        stderr = (
            "  Failed to fetch: `https://x.example/simple/conexus/`\n"
            "  error sending request: invalid peer certificate: UnknownIssuer\n"
        )
        hint = self_cmd.index_failure_hint(stderr, env={}, home=tmp_path)
        assert hint is not None
        for name in ("HTTPS_PROXY", "ALL_PROXY", "SSL_CERT_FILE", "UV_NATIVE_TLS"):
            assert name in hint
        assert "VPN" not in hint
        assert "pypi.org/simple" not in hint
        assert "not changed" in hint

    def test_a_configured_proxy_gets_the_proxy_advice_and_the_variable_name(self, tmp_path: Path):
        env = {"HTTPS_PROXY": "http://user:pw@proxy.corp:3128"}
        hint = self_cmd.index_failure_hint(_UV_DNS_FAILURE, env=env, home=tmp_path)
        assert hint is not None
        assert "HTTPS_PROXY" in hint and "SSL_CERT_FILE" in hint
        assert "VPN" not in hint
        assert "pw@" not in hint and "proxy.corp" not in hint  # the value is never echoed

    def test_a_pypi_failure_is_not_told_to_override_to_pypi(self, tmp_path: Path):
        stderr = (
            "  Failed to fetch: `https://pypi.org/simple/conexus/`\n"
            "  dns error: failed to lookup address\n"
        )
        hint = self_cmd.index_failure_hint(stderr, env={}, home=tmp_path)
        assert hint is not None
        assert "UV_INDEX_URL=https://pypi.org/simple" not in hint
        assert "not changed" in hint

    def test_a_host_is_matched_exactly_not_as_a_substring(self, tmp_path: Path):
        """``pypi.org`` must not be reported as named by a test.pypi.org or
        pypi.org.evil setting."""
        stderr = (
            "  Failed to fetch: `https://pypi.org/simple/conexus/`\n"
            "  dns error: failed to lookup address\n"
        )
        cfg = tmp_path / ".config" / "uv" / "uv.toml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text('index-url = "https://test.pypi.org/simple"\n')
        env = {"UV_INDEX_URL": "https://pypi.org.evil.example/simple"}
        hint = self_cmd.index_failure_hint(stderr, env=env, home=tmp_path)
        assert hint is not None
        assert "That host is named by" not in hint
        assert "No setting naming pypi.org" in hint

    def test_an_exact_host_in_a_config_file_still_matches(self, tmp_path: Path):
        stderr = (
            "  Failed to fetch: `https://pypi.org/simple/conexus/`\n"
            "  dns error: failed to lookup address\n"
        )
        cfg = tmp_path / ".config" / "uv" / "uv.toml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text('index-url = "https://pypi.org/simple"\n')
        hint = self_cmd.index_failure_hint(stderr, env={}, home=tmp_path)
        assert hint is not None and str(cfg) in hint

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
        for name in ("UV_INDEX_URL", "UV_DEFAULT_INDEX", "UV_INDEX", "HTTPS_PROXY", "https_proxy",
                     "ALL_PROXY", "all_proxy"):
            monkeypatch.delenv(name, raising=False)
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

    def test_an_unparseable_url_still_ends_in_a_click_exception(self, tmp_path, monkeypatch):
        stderr = "  Failed to fetch: `https://[abc/simple/`\n  dns error: failed to lookup address\n"
        message = self._fail(tmp_path, stderr, monkeypatch)
        assert "the package index it was using" in message
        assert message.index("not changed") < message.index("uv output:")

    def test_nothing_was_flipped(self, tmp_path, monkeypatch):
        tools = tmp_path / "tools"
        self._fail(tmp_path, _UV_DNS_FAILURE, monkeypatch)
        assert not (tools / "current").exists()
        assert not any(p.name.startswith("gen-") for p in tools.iterdir())
        assert os.path.isdir(tools)
