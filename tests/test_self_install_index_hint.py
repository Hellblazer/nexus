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
import shutil
import stat
from pathlib import Path

import click
import pytest

from nexus import pdf_stack
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


def _usable_bash() -> str:
    """The ``bash`` these tests hand a fake build script to.

    POSIX: ``bash`` on the PATH. Windows: the first ``bash`` on the PATH is
    usually ``System32\\bash.exe``, the WSL launcher, which runs the script in a
    Linux distribution (or fails when none is installed), so it is not used.
    Git for Windows' own bash, next to ``git.exe``, is; with neither the tests
    skip. Test-only: the Windows install path never runs bash."""
    if os.name != "nt":
        return "bash"
    git = shutil.which("git")
    if git:
        root = Path(git).resolve().parent.parent
        for candidate in (root / "bin" / "bash.exe", root / "usr" / "bin" / "bash.exe"):
            if candidate.is_file():
                return str(candidate)
    pytest.skip("no Git for Windows bash next to git.exe (the bash on PATH may be the WSL launcher)")


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
        return [_usable_bash(), str(script)]

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


# The plain command's text for two failure shapes, as printed before the
# bootstrap path (nexus-sv2wx.14) added a variant. Pinned whole so that a
# change to the shared function cannot move the plain path by a word.
_PLAIN_DNS_HINT = (
    "uv could not reach the package index at https://artifactory.infra.us-east-1.conductor.sh"
    "/artifactory/api/pypi/pypi-local/simple/conexus/\n"
    f"No setting naming {_HOST} was found (checked the UV_INDEX_URL, UV_DEFAULT_INDEX and "
    "UV_INDEX variables, uv.toml and pip.conf).\n"
    "The install you have was not changed.\n"
    "Reach that index (connect to the VPN it needs, or check your network).\n"
    "Or build this once from PyPI:\n"
    "    UV_INDEX_URL=https://pypi.org/simple nx self install\n"
    "A local-mode upgrade also downloads the pinned engine from GitHub releases, "
    "which a corporate network may block too."
)
_TLS_FAILURE = (
    "  Failed to fetch https://example.org/simple/conexus/\n"
    "  invalid peer certificate: UnknownIssuer\n"
)
_PLAIN_TLS_HINT = (
    "uv could not reach the package index at https://example.org/simple/conexus/\n"
    "No setting naming example.org was found (checked the UV_INDEX_URL, UV_DEFAULT_INDEX and "
    "UV_INDEX variables, uv.toml and pip.conf).\n"
    "The install you have was not changed.\n"
    "This looks like a certificate or TLS error, not an unreachable host. Check that the proxy "
    "(HTTPS_PROXY / ALL_PROXY) is reachable and correct. If the proxy or index uses a private "
    "certificate authority, point SSL_CERT_FILE at its CA bundle, or set UV_NATIVE_TLS=1 to use "
    "the system certificate store.\n"
    "A local-mode upgrade also downloads the pinned engine from GitHub releases, "
    "which a corporate network may block too."
)
_BOOTSTRAP_RERUN = (
    "Or re-run the same bootstrap command with UV_INDEX_URL=https://pypi.org/simple set."
)


class TestTheBootstrapVariant:
    """On the bootstrap path there is no install to have been left unchanged and
    no `nx` to run: the hint names the same bootstrap command instead."""

    def test_the_plain_text_is_unchanged_word_for_word(self, tmp_path: Path):
        assert self_cmd.index_failure_hint(_UV_DNS_FAILURE, env={}, home=tmp_path) == _PLAIN_DNS_HINT
        assert self_cmd.index_failure_hint(_TLS_FAILURE, env={}, home=tmp_path) == _PLAIN_TLS_HINT

    def test_the_dns_hint_names_the_bootstrap_rerun_and_nothing_about_an_install(self, tmp_path: Path):
        hint = self_cmd.index_failure_hint(_UV_DNS_FAILURE, env={}, home=tmp_path, bootstrap=True)
        assert hint is not None
        assert _BOOTSTRAP_RERUN in hint
        assert "Reach that index" in hint
        for stale in ("not changed", "nx self install", "install you have", "upgrade"):
            assert stale not in hint, stale

    def test_the_tls_hint_says_nothing_about_an_install_either(self, tmp_path: Path):
        hint = self_cmd.index_failure_hint(_TLS_FAILURE, env={}, home=tmp_path, bootstrap=True)
        assert hint is not None
        assert "SSL_CERT_FILE" in hint
        for stale in ("not changed", "nx self install", "install you have", "upgrade"):
            assert stale not in hint, stale

    def test_a_pypi_failure_is_not_told_to_override_to_pypi(self, tmp_path: Path):
        stderr = (
            "  Failed to fetch: `https://pypi.org/simple/conexus/`\n"
            "  dns error: failed to lookup address\n"
        )
        hint = self_cmd.index_failure_hint(stderr, env={}, home=tmp_path, bootstrap=True)
        assert hint is not None
        assert "UV_INDEX_URL=https://pypi.org/simple" not in hint

    def test_the_build_failure_message_on_the_bootstrap_path_uses_the_variant(self, tmp_path, monkeypatch):
        for name in ("UV_INDEX_URL", "UV_DEFAULT_INDEX", "UV_INDEX", "HTTPS_PROXY", "https_proxy",
                     "ALL_PROXY", "all_proxy"):
            monkeypatch.delenv(name, raising=False)
        script = tmp_path / "fake_install_generation.sh"
        script.write_text(f"#!/usr/bin/env bash\ncat >&2 <<'EOF'\n{_UV_DNS_FAILURE}EOF\nexit 1\n")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        with pytest.raises(click.ClickException) as exc:
            self_cmd._build_flip_shims(
                [_usable_bash(), str(script)], install_dir=tmp_path, tools=tmp_path / "tools",
                bin_dir=tmp_path / "bin", bootstrap=True,
            )
        message = exc.value.message
        assert _BOOTSTRAP_RERUN in message
        assert "not changed" not in message and "nx self install" not in message
        assert message.index(_BOOTSTRAP_RERUN) < message.index("uv output:")


_MACOS_TAG_FAILURE = (
    "  x No solution found when resolving dependencies:\n"
    "  ╰─▶ onnxruntime==1.24.2 has no wheels with a matching platform tag "
    "(e.g., `macosx_13_0_arm64`)\n"
)


class TestTheMacosFloorHint:
    """The pinned dependency set includes onnxruntime 1.24.2, which ships only
    macOS 14 wheels; a macOS 13 or Intel Mac fails the resolve with a platform-tag
    message that reads like a nexus fault (nexus-2k66t.4.1, Sam 2026-10-23)."""

    def test_a_platform_tag_failure_on_darwin_names_the_floor(self):
        hint = self_cmd.platform_failure_hint(_MACOS_TAG_FAILURE, system="darwin")
        assert hint is not None and "macOS 14 or later on Apple Silicon" in hint
        assert "onnxruntime" in hint

    def test_the_same_failure_elsewhere_gets_no_macos_advice(self):
        assert self_cmd.platform_failure_hint(_MACOS_TAG_FAILURE, system="linux") is None
        assert self_cmd.platform_failure_hint(_MACOS_TAG_FAILURE, system="win32") is None

    def test_an_unrelated_darwin_failure_gets_none(self):
        assert self_cmd.platform_failure_hint(_RESOLVER_FAILURE, system="darwin") is None

    @pytest.mark.parametrize("stderr", [
        # A network failure on a supported Mac prints the macOS 14 wheel's URL.
        "error: Failed to download `onnxruntime==1.24.2`\n  Caused by: Request failed after 3 retries\n"
        "  Caused by: https://files.pythonhosted.org/packages/ab/cd/"
        "onnxruntime-1.24.2-cp312-cp312-macosx_14_0_arm64.whl: operation timed out\n",
        # Another package with no wheel for this platform is not the macOS floor.
        "error: Distribution `somepkg==1.0` can't be installed because it doesn't have a "
        "source distribution or wheel for the current platform\n",
        # A platform-tag failure that names a supported macOS is not the floor either.
        "error: `foo==2.0` has no wheels with a matching platform tag (e.g. `macosx_15_0_arm64`)\n",
    ])
    def test_a_darwin_failure_that_is_not_the_floor_gets_none(self, stderr):
        assert self_cmd.platform_failure_hint(stderr, system="darwin") is None

    def test_the_build_failure_message_carries_it_above_uvs_output(self, tmp_path, monkeypatch):
        monkeypatch.setattr(self_cmd, "_host_system", lambda: "darwin")
        script = tmp_path / "fake_install_generation.sh"
        script.write_text(f"#!/usr/bin/env bash\ncat >&2 <<'EOF'\n{_MACOS_TAG_FAILURE}EOF\nexit 1\n")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        with pytest.raises(click.ClickException) as exc:
            self_cmd._build_flip_shims(
                [_usable_bash(), str(script)], install_dir=tmp_path, tools=tmp_path / "tools",
                bin_dir=tmp_path / "bin",
            )
        message = exc.value.message
        assert message.startswith("generation build failed:")
        assert message.index("macOS 14 or later") < message.index("uv output:")
        assert "macosx_13_0_arm64" in message


class TestTheCu128Hint:
    """NX_TORCH_BACKEND=cu128 (set, or picked by auto for a CUDA 12.8 driver)
    fails with uv's bare "unsatisfiable" text; the build failure names cu126
    (nexus-husxe)."""

    _UNSATISFIABLE = "  x No solution found when resolving dependencies:\n  torch==2.13.0 is unusable\n"

    def test_an_explicit_cu128_names_cu126_and_the_variable(self):
        hint = self_cmd.torch_backend_hint(
            self._UNSATISFIABLE, env={"NX_TORCH_BACKEND": "cu128"}, system="linux",
        )
        assert hint is not None and "NX_TORCH_BACKEND=cu126" in hint and "12.6 to 12.8" in hint

    def test_auto_counts_only_when_uv_names_cu128(self):
        env = {"NX_TORCH_BACKEND": "auto"}
        assert self_cmd.torch_backend_hint(self._UNSATISFIABLE, env=env, system="linux") is None
        assert self_cmd.torch_backend_hint("torch==2.13.0+cu128", env=env, system="linux") is not None

    def test_other_backends_and_hosts_get_none(self):
        assert self_cmd.torch_backend_hint(self._UNSATISFIABLE, env={}, system="linux") is None
        assert self_cmd.torch_backend_hint(
            self._UNSATISFIABLE, env={"NX_TORCH_BACKEND": "cu130"}, system="linux",
        ) is None
        assert self_cmd.torch_backend_hint(
            self._UNSATISFIABLE, env={"NX_TORCH_BACKEND": "cu128"}, system="darwin",
        ) is None

    def test_the_build_failure_message_carries_it_above_uvs_output(self, tmp_path, monkeypatch):
        monkeypatch.setattr(self_cmd, "_host_system", lambda: "linux")
        monkeypatch.setenv("NX_TORCH_BACKEND", "cu128")
        script = tmp_path / "fake_install_generation.sh"
        script.write_text(f"#!/usr/bin/env bash\ncat >&2 <<'EOF'\n{self._UNSATISFIABLE}EOF\nexit 1\n")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        with pytest.raises(click.ClickException) as exc:
            self_cmd._build_flip_shims(
                [_usable_bash(), str(script)], install_dir=tmp_path, tools=tmp_path / "tools",
                bin_dir=tmp_path / "bin",
            )
        message = exc.value.message
        assert message.index("NX_TORCH_BACKEND=cu126") < message.index("uv output:")


class TestTheCu128HintSurvivesTheOnDemandInstall:
    """The on-demand PDF install keeps only the last lines of a build failure, and the hint sits at
    the HEAD of the message: `NX_TORCH_BACKEND=cu128 nx index pdf` exited 70 with uv's tail and no
    cu126 advice (7.86.1 shakeout, nexus-husxe follow-up). The build here is a real
    ``_build_flip_shims`` over a script that prints a long uv failure, the same shape as the leg's
    log (a dozen lines of venv chatter, then the unsatisfiable resolve)."""

    _UV_FAILURE = (
        "Using CPython 3.12.15\n"
        "Creating virtual environment at: /tools/gen-20261025T151428Z\n"
        "Activate with: source /tools/gen-20261025T151428Z/bin/activate\n"
        "Using Python 3.12.15 environment at: /tools/gen-20261025T151428Z\n"
        "Resolved 1 package in 0.87ms\n"
        "Installed 1 package in 9ms\n"
        " + conexus==7.86.1\n"
        "Using Python 3.12.15 environment at: /tools/gen-20261025T151428Z\n"
        "error: No solution found when resolving dependencies\n"
        "  cause: Because there is no version of torch==2.13.0 and conexus[pdf]>=7.86.1 depends on\n"
        "         torch{sys_platform == 'linux'}==2.13.0, we can conclude that conexus[pdf]>=7.86.1\n"
        "         cannot be used.\n"
        "         And because you require conexus[pdf]==7.86.1, we can conclude that your\n"
        "         requirements are unsatisfiable.\n"
        "UV-LAST-LINE\n"
    )

    def _failing_build(self, tmp_path, monkeypatch, *, progress: bool):
        monkeypatch.setattr(self_cmd, "_host_system", lambda: "linux")
        monkeypatch.setenv("NX_TORCH_BACKEND", "cu128")
        script = tmp_path / "fake_install_generation.sh"
        script.write_text(f"#!/usr/bin/env bash\ncat >&2 <<'EOF'\n{self._UV_FAILURE}EOF\nexit 1\n")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        with pytest.raises(click.ClickException) as exc:
            self_cmd._build_flip_shims(
                [_usable_bash(), str(script)], install_dir=tmp_path, tools=tmp_path / "tools",
                bin_dir=tmp_path / "bin", progress=progress,
            )
        return exc.value

    @pytest.mark.parametrize("progress", [True, False])
    def test_the_exit_70_message_carries_the_advice_and_uvs_tail(self, tmp_path, monkeypatch, progress):
        failure = self._failing_build(tmp_path, monkeypatch, progress=progress)
        classified = pdf_stack._classify_failure(failure.message)
        assert isinstance(classified, pdf_stack.PdfStackInstallError)
        assert classified.exit_code == pdf_stack.EXIT_INSTALLER == 70
        text = str(classified)
        assert "NX_TORCH_BACKEND=cu126" in text and "12.6 to 12.8" in text
        assert "UV-LAST-LINE" in text, "uv's own tail is still shown"
        assert text.index("NX_TORCH_BACKEND=cu126") < text.index("UV-LAST-LINE")

    def test_ensure_exits_70_with_the_advice(self, tmp_path, monkeypatch):
        """`nx index pdf` on the on-demand path: ensure() classifies the build's ClickException."""
        failure = self._failing_build(tmp_path, monkeypatch, progress=True)
        monkeypatch.setattr(pdf_stack, "state", lambda: pdf_stack.StackState("absent", ""))
        monkeypatch.setattr(
            pdf_stack, "install_plan",
            lambda: pdf_stack.InstallPlan("generation", True, pdf_stack.INSTALL_COMMAND),
        )
        monkeypatch.setenv(pdf_stack.POLICY_ENV, "install")
        monkeypatch.setattr(pdf_stack, "_windows_runtime_missing", lambda: False)
        monkeypatch.setattr(pdf_stack, "current_generation_has_stack", lambda: False)

        def _build(**_kw):
            raise failure

        monkeypatch.setattr(pdf_stack, "_install_generation", _build)
        with pytest.raises(pdf_stack.PdfStackInstallError) as err:
            pdf_stack.ensure("nx index pdf")
        assert err.value.exit_code == 70
        assert "NX_TORCH_BACKEND=cu126" in str(err.value)

    def test_a_failure_with_no_advice_still_shows_the_last_twelve_lines(self):
        """The control: no hint applies, so the message is uv's text and the tail rule is unchanged."""
        body = "\n".join(f"uv line {n}" for n in range(1, 31))
        text = str(pdf_stack._classify_failure(f"generation build failed:\n{body}"))
        assert "uv line 30" in text and "uv line 19" in text, "the last twelve lines are shown"
        assert "uv line 18" not in text, "and no more than twelve"
