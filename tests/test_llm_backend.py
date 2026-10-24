# SPDX-License-Identifier: AGPL-3.0-or-later
"""LLM backend protocol, selection and availability (nexus-2k66t.3 Phases 1-2).

These tests pin: the pure ``select_backend`` matrix over setting x ``claude`` on
PATH x key x tool grant, ``backend_status`` and ``llm_backend_available`` agreeing
with it (they flip together), ``claude_dispatch`` as a front over an injected
backend, the five availability probes routing through ``llm_backend_available``,
and where the settings are read (env and GLOBAL config, never a per-repo file).
The byte-identity of the CLI path itself is pinned in
``test_llm_backend_cli_identity.py``; the API backends have their own files.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import os
import re
from pathlib import Path
from unittest.mock import patch

import pytest
from tests._module_seam import patch_in, setattr_in

from nexus.commands import taxonomy_cmd
from nexus.daemon import aspect_worker_daemon as awd
from nexus.operators import dispatch as dispatch_mod
from nexus.operators.dispatch import (
    CliBackend,
    DispatchUsage,
    OperatorError,
    claude_dispatch,
)
from nexus.operators.llm_backend import (
    BACKEND_SETTINGS,
    IMPLEMENTED_BACKENDS,
    NO_BACKEND_MESSAGE,
    BackendStatus,
    DispatchRequest,
    DispatchResult,
    LLMBackend,
    NoBackendAvailableError,
    backend_status,
    build_api_backend,
    configured_backend,
    llm_backend_available,
    openai_compat_settings,
    resolve_backend_name,
    run_text_sync,
    select_backend,
    selected_api_backend,
)

_SCHEMA = {"type": "object", "properties": {"result": {"type": "string"}}}


def _on_path(name: str) -> str | None:
    return "/usr/local/bin/claude" if name == "claude" else None


def _off_path(name: str) -> str | None:
    return None


def _req(**kw) -> DispatchRequest:
    return DispatchRequest(prompt="p", json_schema=_SCHEMA, **kw)


_KEY = "sk-ant-api03-SELECTIONSECRET0123456789"
_HAS_KEY = {"ANTHROPIC_API_KEY": _KEY}
_OPENAI_OK = {"NX_LLM_OPENAI_BASE_URL": "http://localhost:11434", "NX_LLM_OPENAI_MODEL": "llama3.1"}


@pytest.fixture
def claude_on_path(monkeypatch):
    """``shutil.which`` as llm_backend and dispatch (the CliBackend spawn check) see it: claude present."""
    setattr_in(monkeypatch, ["nexus.operators.llm_backend", "nexus.operators.dispatch"], "shutil.which", _on_path)


@pytest.fixture
def claude_off_path(monkeypatch):
    setattr_in(monkeypatch, ["nexus.operators.llm_backend", "nexus.operators.dispatch"], "shutil.which", _off_path)


class _SpawnReached(Exception):
    """Raised by the fake spawn: the code under test got as far as starting ``claude``."""


def _spawn_raises():
    """Patch the dispatch module's subprocess spawn with a fake that raises.

    A bare ``patch`` here makes ``proc`` a MagicMock whose ``stdout.read`` returns
    a truthy mock forever, and the drain loop appends it without bound (a pytest
    process grew to ~40 GB when a mutant let a test reach the spawn). A raising
    fake turns "the code reached the spawn" into an immediate, bounded failure.
    """

    async def spawn(*_a, **_k):
        raise _SpawnReached

    return patch_in("nexus.operators.dispatch", "asyncio.create_subprocess_exec", side_effect=spawn)


def _write_global_config(text: str) -> None:
    """Write the (per-test, conftest-isolated) global ``config.yml``."""
    cfg_dir = Path(os.environ["NEXUS_CONFIG_DIR"])
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yml").write_text(text, encoding="utf-8")


class FakeBackend:
    """Implements the protocol; records the request, returns a canned result."""

    name = "fake"

    def __init__(self, structured=None, usage: DispatchUsage | None = None) -> None:
        self.requests: list[DispatchRequest] = []
        self._structured = {"result": "fake"} if structured is None else structured
        self._usage = usage

    def available(self) -> BackendStatus:
        return BackendStatus("fake", True)

    async def run(self, req: DispatchRequest) -> DispatchResult:
        self.requests.append(req)
        return DispatchResult(structured=self._structured, usage=self._usage)


# ── select_backend matrix ───────────────────────────────────────────────────


class TestSelectBackend:
    # auto = cli when claude is on PATH, else anthropic when a key resolves, else
    # no backend. Every cell of the matrix, so a change to any one fails by name.
    @pytest.mark.parametrize("which,env,expected", [
        (_on_path, {}, "cli"),
        (_on_path, _HAS_KEY, "cli"),  # a key never displaces the subscription CLI
        (_off_path, _HAS_KEY, "anthropic"),
    ], ids=["cli", "cli-and-key", "key-only"])
    @pytest.mark.parametrize("config", [None, {}, {"llm": {}}, {"llm": {"backend": "auto"}}], ids=str)
    def test_auto_matrix(self, which, env, expected, config) -> None:
        assert select_backend(config, env, which, _req()) == expected

    def test_auto_with_neither_is_a_named_error_with_the_remedy(self) -> None:
        with pytest.raises(NoBackendAvailableError) as ei:
            select_backend(None, {}, _off_path, _req())
        assert isinstance(ei.value, OperatorError) and str(ei.value) == NO_BACKEND_MESSAGE
        assert "nx config set anthropic_api_key" in NO_BACKEND_MESSAGE and "Claude Code" in NO_BACKEND_MESSAGE

    def test_a_blank_key_is_no_key(self) -> None:
        with pytest.raises(NoBackendAvailableError):
            select_backend(None, {"ANTHROPIC_API_KEY": "   "}, _off_path, _req())

    def test_the_key_predicate_is_what_decides_not_the_env_mapping(self) -> None:
        # The real caller passes a lookup that also reads config.yml.
        assert select_backend(None, {}, _off_path, _req(), key_present=lambda n: n == "anthropic_api_key") == "anthropic"
        with pytest.raises(NoBackendAvailableError):
            select_backend(None, _HAS_KEY, _off_path, _req(), key_present=lambda n: False)

    def test_explicit_cli_is_cli_whatever_is_on_path(self) -> None:
        for which in (_on_path, _off_path):
            assert select_backend({"llm": {"backend": "cli"}}, {}, which, _req()) == "cli"
            assert select_backend(None, {"NX_LLM_BACKEND": "cli", **_HAS_KEY}, which, _req()) == "cli"

    @pytest.mark.parametrize("which", [_on_path, _off_path], ids=["cli-on-path", "cli-absent"])
    @pytest.mark.parametrize("via", ["env", "config"])
    def test_explicit_anthropic_forces_the_api_even_with_the_cli_present(self, which, via) -> None:
        config = {"llm": {"backend": "anthropic"}} if via == "config" else None
        env = {**_HAS_KEY, **({"NX_LLM_BACKEND": "anthropic"} if via == "env" else {})}
        assert select_backend(config, env, which, _req()) == "anthropic"

    @pytest.mark.parametrize("via", ["env", "config"])
    def test_explicit_anthropic_without_a_key_names_the_remedy(self, via) -> None:
        config = {"llm": {"backend": "anthropic"}} if via == "config" else None
        env = {"NX_LLM_BACKEND": "anthropic"} if via == "env" else {}
        with pytest.raises(OperatorError, match=r"anthropic_api_key") as ei:
            select_backend(config, env, _on_path, _req())
        assert not isinstance(ei.value, NoBackendAvailableError)
        assert "nx config set anthropic_api_key" in str(ei.value)

    def test_openai_compat_needs_a_base_url_and_a_model(self) -> None:
        env = {"NX_LLM_BACKEND": "openai_compat"}
        assert select_backend(None, {**env, **_OPENAI_OK}, _off_path, _req()) == "openai_compat"
        with pytest.raises(OperatorError, match=r"base_url and model|base_url"):
            select_backend(None, env, _on_path, _req())
        with pytest.raises(OperatorError, match="model"):
            select_backend(None, {**env, "NX_LLM_OPENAI_BASE_URL": "http://h:1"}, _on_path, _req())

    def test_openai_compat_settings_come_from_env_then_global_config(self) -> None:
        cfg = {"llm": {"openai_compat": {"base_url": "http://cfg:1", "model": "cfg-model"}}}
        assert openai_compat_settings(cfg, {}) == ("http://cfg:1", "cfg-model")
        assert openai_compat_settings(cfg, {"NX_LLM_OPENAI_MODEL": "env-model"}) == ("http://cfg:1", "env-model")
        assert openai_compat_settings(None, {}) == ("", "")
        assert openai_compat_settings({"llm": {"openai_compat": "junk"}}, {}) == ("", "")
        assert select_backend({"llm": {"backend": "openai_compat", **cfg["llm"]}}, {}, _off_path, _req()) == "openai_compat"

    @pytest.mark.parametrize("url,why", [
        ("ftp://h/", "http"), ("localhost:11434", "http"), ("http://u:p@h:1", "user:password"),
    ])
    def test_openai_compat_base_url_is_validated(self, url, why) -> None:
        env = {"NX_LLM_BACKEND": "openai_compat", "NX_LLM_OPENAI_BASE_URL": url, "NX_LLM_OPENAI_MODEL": "m"}
        with pytest.raises(OperatorError, match=why) as ei:
            select_backend(None, env, _on_path, _req())
        assert "p@" not in str(ei.value)  # a password in the URL is never echoed

    def test_env_beats_config(self) -> None:
        config = {"llm": {"backend": "anthropic"}}
        assert select_backend(config, {"NX_LLM_BACKEND": "cli"}, _on_path, _req()) == "cli"
        with pytest.raises(OperatorError):
            select_backend({"llm": {"backend": "cli"}}, {"NX_LLM_BACKEND": "anthropic"}, _on_path, _req())

    def test_blank_env_falls_through_to_config(self) -> None:
        with pytest.raises(OperatorError, match="anthropic_api_key"):
            select_backend({"llm": {"backend": "anthropic"}}, {"NX_LLM_BACKEND": "  "}, _on_path, _req())

    def test_case_and_whitespace_are_normalised(self) -> None:
        assert select_backend(None, {"NX_LLM_BACKEND": " CLI "}, _on_path, _req()) == "cli"

    @pytest.mark.parametrize("grant", [
        {"allowed_tools": ["Read"]},
        {"mcp_servers": {"nx": {"command": "nx-mcp"}}},
        {"allowed_tools": ["Read"], "mcp_servers": {"nx": {"command": "nx-mcp"}}},
    ], ids=["allowed_tools", "mcp_servers", "both"])
    @pytest.mark.parametrize("setting", ["auto", "cli", "anthropic", "openai_compat"])
    @pytest.mark.parametrize("which", [_on_path, _off_path], ids=["cli-on-path", "cli-absent"])
    def test_tool_grant_always_routes_to_cli(self, grant, setting, which) -> None:
        env = {"NX_LLM_BACKEND": setting, **_HAS_KEY, **_OPENAI_OK}
        assert select_backend(None, env, which, _req(**grant)) == "cli"

    def test_empty_grant_is_not_a_grant(self) -> None:
        assert _req(allowed_tools=[], mcp_servers={}).grants_tool_access is False
        with pytest.raises(OperatorError, match="anthropic_api_key"):
            select_backend(None, {"NX_LLM_BACKEND": "anthropic"}, _on_path, _req(allowed_tools=[], mcp_servers={}))

    def test_unknown_value_names_source_and_legal_values(self) -> None:
        with pytest.raises(OperatorError) as ei:
            select_backend(None, {"NX_LLM_BACKEND": "gemini"}, _on_path, _req())
        msg = str(ei.value)
        assert "NX_LLM_BACKEND" in msg and "gemini" in msg
        assert all(s in msg for s in BACKEND_SETTINGS)
        with pytest.raises(OperatorError, match="config llm.backend"):
            select_backend({"llm": {"backend": "gemini"}}, {}, _on_path, _req())

    def test_unknown_value_is_loud_even_for_a_tool_grant(self) -> None:
        with pytest.raises(OperatorError):
            select_backend(None, {"NX_LLM_BACKEND": "gemini"}, _on_path, _req(allowed_tools=["Read"]))

    def test_is_pure(self) -> None:
        env = {"NX_LLM_BACKEND": "auto", **_HAS_KEY}
        config = {"llm": {"backend": "auto"}}
        snapshot = (dict(env), {"llm": dict(config["llm"])})
        calls: list[str] = []

        def which(name: str) -> str | None:
            calls.append(name)
            return None

        assert select_backend(config, env, which, _req()) == "anthropic"
        assert (env, config) == snapshot
        assert calls == ["claude"]  # the one PATH lookup, for the one binary

    def test_every_implemented_backend_is_selectable(self) -> None:
        # IMPLEMENTED_BACKENDS and the matrix cannot drift apart.
        assert IMPLEMENTED_BACKENDS == {"cli", "anthropic", "openai_compat"}
        reached = {
            select_backend(None, {"NX_LLM_BACKEND": s, **_HAS_KEY, **_OPENAI_OK}, _off_path, _req())
            for s in ("cli", "anthropic", "openai_compat")
        }
        assert reached == IMPLEMENTED_BACKENDS

    @pytest.mark.parametrize("bad", [None, "x", 3, []])
    def test_malformed_llm_section_is_auto(self, bad) -> None:
        assert configured_backend({"llm": bad}, {}) == "auto"


# ── availability ────────────────────────────────────────────────────────────


class TestAvailability:
    def test_same_answer_as_the_old_probe_when_no_key_is_involved(self) -> None:
        assert llm_backend_available({}, {}, _on_path) is True
        assert llm_backend_available({}, {}, _off_path) is False

    def test_a_key_makes_a_cli_less_box_available(self) -> None:
        assert llm_backend_available({}, _HAS_KEY, _off_path) is True
        status = backend_status({}, _HAS_KEY, _off_path)
        assert status == BackendStatus("anthropic", True, status.reason) and "anthropic" in status.reason
        assert _KEY not in status.reason

    def test_default_inputs_follow_shutil_which(self) -> None:
        with patch_in("nexus.operators.llm_backend", "shutil.which", side_effect=lambda n: "/x/claude" if n == "claude" else None):
            assert llm_backend_available({}, {}) is True
        with patch_in("nexus.operators.llm_backend", "shutil.which", return_value=None):
            assert llm_backend_available({}, {}) is False

    def test_the_real_lookup_reads_the_credential_from_the_global_config(self, monkeypatch) -> None:
        # No env mapping given: the process environment AND config.yml's credentials count.
        _write_global_config("credentials:\n  anthropic_api_key: " + _KEY + "\n")
        setattr_in(monkeypatch, "nexus.operators.llm_backend", "shutil.which", _off_path)
        assert llm_backend_available() is True
        status = backend_status()
        assert status.name == "anthropic" and _KEY not in status.reason
        _write_global_config("")
        assert llm_backend_available() is False

    def test_openai_compat_status(self) -> None:
        env = {"NX_LLM_BACKEND": "openai_compat"}
        ok = backend_status(None, {**env, **_OPENAI_OK}, _off_path)
        assert ok.available and ok.name == "openai_compat" and "llama3.1" in ok.reason
        bad = backend_status(None, env, _on_path)
        assert bad.available is False and "base_url" in bad.reason

    def test_anthropic_setting_without_a_key_is_unavailable_with_the_remedy(self) -> None:
        status = backend_status(None, {"NX_LLM_BACKEND": "anthropic"}, _on_path)
        assert status.available is False and "nx config set anthropic_api_key" in status.reason
        assert llm_backend_available(None, {"NX_LLM_BACKEND": "anthropic"}, _on_path) is False

    def test_unknown_setting_does_not_raise_from_a_probe(self) -> None:
        status = backend_status(None, {"NX_LLM_BACKEND": "gemini"}, _on_path)
        assert status.available is False and "gemini" in status.reason

    @pytest.mark.parametrize("setting", ["auto", "cli", "anthropic", "openai_compat"])
    @pytest.mark.parametrize("which", [_on_path, _off_path], ids=["cli-on-path", "cli-absent"])
    @pytest.mark.parametrize("env_extra", [{}, _HAS_KEY, _OPENAI_OK, {**_HAS_KEY, **_OPENAI_OK}],
                             ids=["bare", "key", "openai", "both"])
    def test_selection_status_and_availability_flip_together(self, setting, which, env_extra) -> None:
        # One decision feeds all three, so they cannot disagree (the P1 review's
        # "flip llm_backend_available, backend_status and selection TOGETHER").
        env = {"NX_LLM_BACKEND": setting, **env_extra}
        status = backend_status(None, env, which)
        assert llm_backend_available(None, env, which) is status.available
        try:
            chosen = select_backend(None, env, which, _req())
        except OperatorError:
            assert status.available is False
            return
        assert chosen == status.name
        if chosen == "cli":
            assert status.available is (which("claude") is not None)  # selection leaves a missing binary to the spawn
        else:
            assert status.available is True

    def test_global_config_is_read_when_no_config_is_passed(self) -> None:
        _write_global_config("llm:\n  backend: anthropic\n")
        assert llm_backend_available(env={}, which=_on_path) is False
        _write_global_config("llm:\n  backend: cli\n")
        assert llm_backend_available(env={}, which=_on_path) is True

    def test_unreadable_config_means_auto(self) -> None:
        with patch("nexus.config.get_global_config_section", side_effect=OSError("boom")):
            assert llm_backend_available(env={}, which=_on_path) is True

    def test_cli_backend_reports_its_own_status(self) -> None:
        assert CliBackend(_on_path).available() == BackendStatus("cli", True, "claude is on PATH")
        assert CliBackend(_off_path).available().available is False

    def test_cli_backend_refuses_text_mode_before_spawning(self) -> None:
        # Pins the json_schema=None guard: text mode goes to an API backend, and
        # the cli backend must not spawn claude with a schema of "null" (what the
        # pre-P1 code would have sent).
        with _spawn_raises():
            with pytest.raises(OperatorError, match="json_schema"):
                asyncio.run(CliBackend().run(DispatchRequest(prompt="p", json_schema=None)))

    def test_backends_satisfy_the_protocol(self) -> None:
        from nexus.operators.llm_anthropic import AnthropicBackend
        from nexus.operators.llm_openai import OpenAICompatBackend

        assert isinstance(CliBackend(), LLMBackend)
        assert isinstance(FakeBackend(), LLMBackend)
        assert isinstance(AnthropicBackend(api_key="k"), LLMBackend)
        assert isinstance(OpenAICompatBackend(base_url="http://h", model="m"), LLMBackend)


# ── claude_dispatch is a front over a backend ───────────────────────────────


class TestClaudeDispatchFront:
    def test_delegates_to_the_injected_backend_and_returns_its_structured(self) -> None:
        fake = FakeBackend(structured={"result": "from-fake"})
        got = asyncio.run(claude_dispatch("hello", _SCHEMA, 12.5, backend=fake))
        assert got == {"result": "from-fake"}
        (req,) = fake.requests
        assert req == DispatchRequest(
            prompt="hello", json_schema=_SCHEMA, timeout=12.5, deadline=None, model=None,
            operator=None, max_budget_usd=None, allowed_tools=None, mcp_servers=None, isolated=True,
        )

    def test_every_parameter_reaches_the_request(self) -> None:
        fake = FakeBackend()
        sink: list[DispatchUsage] = []
        asyncio.run(claude_dispatch(
            "p", _SCHEMA, 7.0, backend=fake, allowed_tools=["Read"], mcp_servers={"nx": {}},
            usage_sink=sink, model="haiku", operator="operator_rank", max_budget_usd=0.25,
            isolated=False, deadline=123.0,
        ))
        (req,) = fake.requests
        assert (req.timeout, req.deadline, req.model, req.operator, req.max_budget_usd) == (
            7.0, 123.0, "haiku", "operator_rank", 0.25,
        )
        assert (req.allowed_tools, req.mcp_servers, req.isolated) == (["Read"], {"nx": {}}, False)
        assert req.usage_sink is sink  # the backend, not the front, appends to it
        assert sink == []

    def test_blank_model_is_normalised_before_the_backend_sees_it(self) -> None:
        fake = FakeBackend()
        asyncio.run(claude_dispatch("p", _SCHEMA, backend=fake, model="   "))
        assert fake.requests[0].model is None

    def test_env_budget_is_resolved_in_the_front(self, monkeypatch) -> None:
        monkeypatch.setenv("NX_DISPATCH_MAX_BUDGET_USD", "0.5")
        fake = FakeBackend()
        asyncio.run(claude_dispatch("p", _SCHEMA, backend=fake))
        asyncio.run(claude_dispatch("p", _SCHEMA, backend=fake, max_budget_usd=0.1))
        assert [r.max_budget_usd for r in fake.requests] == [0.5, 0.1]

    @pytest.mark.parametrize("bad", ["nope", "nan", "-1", "0"])
    def test_bad_env_budget_raises_before_any_backend_runs(self, monkeypatch, bad) -> None:
        monkeypatch.setenv("NX_DISPATCH_MAX_BUDGET_USD", bad)
        fake = FakeBackend()
        with pytest.raises(ValueError):
            asyncio.run(claude_dispatch("p", _SCHEMA, backend=fake))
        assert fake.requests == []

    def test_a_backend_exception_propagates_unchanged(self) -> None:
        class Boom(FakeBackend):
            async def run(self, req):  # noqa: ARG002
                raise dispatch_mod.OperatorTimeoutError("t")

        with pytest.raises(dispatch_mod.OperatorTimeoutError):
            asyncio.run(claude_dispatch("p", _SCHEMA, backend=Boom()))

    def test_signature_keeps_its_parameters_and_adds_backend_last(self) -> None:
        params = list(inspect.signature(claude_dispatch).parameters)
        assert params == [
            "prompt", "json_schema", "timeout", "allowed_tools", "mcp_servers", "usage_sink",
            "model", "operator", "max_budget_usd", "isolated", "deadline", "backend",
        ]

    def test_anthropic_without_a_key_fails_before_spawning(self, monkeypatch) -> None:
        monkeypatch.setenv("NX_LLM_BACKEND", "anthropic")
        with _spawn_raises():
            with pytest.raises(OperatorError, match="anthropic_api_key"):
                asyncio.run(claude_dispatch("p", _SCHEMA))

    def test_auto_with_neither_claude_nor_a_key_fails_with_the_clear_error(self, claude_off_path) -> None:
        # The clear error comes from the spawn finding no binary (the unit tests'
        # mocked spawn is unaffected), not from a raw FileNotFoundError.
        async def spawn(*_a, **_k):
            raise FileNotFoundError("claude")

        with patch_in("nexus.operators.dispatch", "asyncio.create_subprocess_exec", side_effect=spawn):
            with pytest.raises(OperatorError) as ei:
                asyncio.run(claude_dispatch("p", _SCHEMA))
        assert str(ei.value) == NO_BACKEND_MESSAGE and isinstance(ei.value.__cause__, FileNotFoundError)

    def test_a_tool_grant_without_claude_says_which_operator_needs_it(self, claude_off_path, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", _KEY)

        async def spawn(*_a, **_k):
            raise FileNotFoundError("claude")

        with (
            patch_in("nexus.operators.dispatch", "asyncio.create_subprocess_exec", side_effect=spawn),
            patch("nexus.db.t1.mint_t1_session_token", return_value={"session_token": "t"}),
            patch("nexus.operators.dispatch._close_dispatch_session"),
        ):
            with pytest.raises(OperatorError) as ei:
                asyncio.run(claude_dispatch("p", _SCHEMA, allowed_tools=["Read"], operator="nx_plan_audit"))
        assert "nx_plan_audit needs the Claude Code CLI" in str(ei.value)
        assert "API key cannot serve it" in str(ei.value) and _KEY not in str(ei.value)

    def test_a_missing_binary_that_is_not_the_cause_keeps_the_raw_error(self, claude_on_path) -> None:
        async def spawn(*_a, **_k):
            raise FileNotFoundError("some-other-path")

        with patch_in("nexus.operators.dispatch", "asyncio.create_subprocess_exec", side_effect=spawn):
            with pytest.raises(FileNotFoundError):
                asyncio.run(claude_dispatch("p", _SCHEMA))

    def test_explicit_cli_with_no_binary_names_the_setting(self, claude_off_path, monkeypatch) -> None:
        monkeypatch.setenv("NX_LLM_BACKEND", "cli")

        async def spawn(*_a, **_k):
            raise FileNotFoundError("claude")

        with patch_in("nexus.operators.dispatch", "asyncio.create_subprocess_exec", side_effect=spawn):
            with pytest.raises(OperatorError, match="llm.backend"):
                asyncio.run(claude_dispatch("p", _SCHEMA))

    def test_a_selected_api_backend_never_spawns_claude(self, monkeypatch) -> None:
        monkeypatch.setenv("NX_LLM_BACKEND", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", _KEY)
        fake = FakeBackend(structured={"result": "via-api"})
        with _spawn_raises(), patch("nexus.operators.dispatch.build_api_backend", return_value=fake) as build:
            got = asyncio.run(claude_dispatch("p", _SCHEMA))
        assert got == {"result": "via-api"} and build.call_args.args == ("anthropic",) and len(fake.requests) == 1

    def test_tool_grant_still_spawns_the_cli_under_an_api_setting(self, monkeypatch) -> None:
        monkeypatch.setenv("NX_LLM_BACKEND", "anthropic")

        class Sentinel(Exception):
            pass

        async def spawn(*a, **k):
            raise Sentinel

        # The CLI backend reached its spawn: routed to cli despite the setting.
        # Spawn is faked on this module's seam only, and the mint is stubbed.
        with (
            patch_in("nexus.operators.dispatch", "asyncio.create_subprocess_exec", side_effect=spawn),
            patch("nexus.db.t1.mint_t1_session_token", return_value={"session_token": "t"}),
            patch("nexus.operators.dispatch._close_dispatch_session"),
        ):
            with pytest.raises(Sentinel):
                asyncio.run(claude_dispatch("p", _SCHEMA, allowed_tools=["Read"]))


# ── where the setting is read (global config + env, never per-repo) ────────


class TestWhereTheSettingIsRead:
    """Ship-blocker pins: ``llm.backend`` comes from the env and the GLOBAL
    config. The mutants these kill: global config never read; per-repo
    ``.nexus.yml`` merged into the lookup; env not winning over config."""

    @pytest.fixture(autouse=True)
    def _no_ambient_setting(self, monkeypatch) -> None:
        monkeypatch.delenv("NX_LLM_BACKEND", raising=False)

    def test_global_config_anthropic_makes_claude_dispatch_demand_a_key(self) -> None:
        _write_global_config("llm:\n  backend: anthropic\n")
        with _spawn_raises():
            with pytest.raises(OperatorError, match="anthropic_api_key") as ei:
                asyncio.run(claude_dispatch("p", _SCHEMA))
        assert "anthropic" in str(ei.value)

    def test_env_cli_overrides_a_config_value_of_anthropic(self, monkeypatch) -> None:
        _write_global_config("llm:\n  backend: anthropic\n")
        with pytest.raises(OperatorError, match="anthropic_api_key"):
            resolve_backend_name(_req())  # config alone: raises
        monkeypatch.setenv("NX_LLM_BACKEND", "cli")
        assert resolve_backend_name(_req()) == "cli"  # env wins

    @pytest.fixture(autouse=True)
    def _claude_present(self, monkeypatch) -> None:
        # These tests are about WHICH SOURCE the setting comes from, not the box's PATH.
        setattr_in(monkeypatch, "nexus.operators.llm_backend", "shutil.which", _on_path)

    def test_a_per_repo_nexus_yml_cannot_pick_the_backend(self, tmp_path, monkeypatch) -> None:
        from nexus.config import load_config

        repo = tmp_path / "cloned-repo"
        repo.mkdir()
        (repo / ".nexus.yml").write_text("llm:\n  backend: anthropic\n", encoding="utf-8")
        monkeypatch.delenv("NEXUS_REPO_ROOT", raising=False)
        monkeypatch.chdir(repo)
        # Non-vacuity: the merged loader DOES see the per-repo setting here.
        assert load_config()["llm"]["backend"] == "anthropic"
        assert resolve_backend_name(_req()) == "cli"
        assert llm_backend_available(env={}, which=_on_path) is True
        # And a per-repo file cannot override a global one either.
        _write_global_config("llm:\n  backend: cli\n")
        assert resolve_backend_name(_req()) == "cli"

    @pytest.mark.parametrize("broken", ["llm: [unterminated\n", ":\n  - : :\n\t bad", "- just\n- a list\n", "llm: 3\n"])
    def test_a_broken_global_config_is_auto(self, broken) -> None:
        _write_global_config(broken)
        assert resolve_backend_name(_req()) == "cli"
        assert llm_backend_available(env={}, which=_on_path) is True

    def test_the_config_is_passed_even_when_the_env_is_set(self, monkeypatch) -> None:
        monkeypatch.setenv("NX_LLM_BACKEND", "cli")
        _write_global_config("llm:\n  backend: anthropic\n")
        seen = {}

        def spy(config, env, which, req, key_present=None):
            seen["config"] = config
            return "cli"

        with patch("nexus.operators.llm_backend.select_backend", side_effect=spy):
            resolve_backend_name(_req())
        assert seen["config"] == {"llm": {"backend": "anthropic"}}  # the openai_compat endpoint lives in it


# ── the five probe sites ────────────────────────────────────────────────────

_ROOT = Path(__file__).resolve().parent.parent / "src" / "nexus"
_SITE_FILES = [
    _ROOT / "commands" / "taxonomy_cmd.py",
    _ROOT / "commands" / "index.py",
    _ROOT / "daemon" / "aspect_worker_daemon.py",
    _ROOT / "commands" / "doctor.py",
]


class TestProbeSites:
    def test_the_delegating_probes_follow_llm_backend_available(self) -> None:
        for probe in (taxonomy_cmd._claude_available, awd._claude_available):
            with patch("nexus.operators.llm_backend.llm_backend_available", return_value=True):
                assert probe() is True
            with patch("nexus.operators.llm_backend.llm_backend_available", return_value=False):
                assert probe() is False

    def test_the_daemon_entrypoint_guard_follows_llm_backend_available(self) -> None:
        with patch("nexus.operators.llm_backend.llm_backend_available", return_value=False):
            with pytest.raises(RuntimeError, match="claude"):
                awd._require_extraction_credentials()
        with patch("nexus.operators.llm_backend.llm_backend_available", return_value=True):
            awd._require_extraction_credentials()  # no raise

    def test_the_spawner_declines_when_no_backend_is_available(self, tmp_path) -> None:
        with patch("nexus.operators.llm_backend.llm_backend_available", return_value=False):
            assert awd.ensure_aspect_worker_daemon(config_dir=tmp_path, tenant="t-llm-backend") is False

    @pytest.mark.parametrize("path", _SITE_FILES, ids=lambda p: p.name)
    def test_no_site_probes_the_claude_binary_itself(self, path: Path) -> None:
        src = path.read_text()
        assert not re.search(r"""which\(\s*["']claude["']\s*\)""", src), (
            f"{path.name} still probes PATH for claude directly; route through llm_backend_available"
        )

    def test_doctor_and_index_use_the_delegating_probes(self) -> None:
        # Both import the probe by name at call time, so a test (or a later
        # phase) that moves the answer moves every site together.
        doctor = (_ROOT / "commands" / "doctor.py").read_text()
        index = (_ROOT / "commands" / "index.py").read_text()
        assert "from nexus.daemon.aspect_worker_daemon import _claude_available" in doctor
        assert "from nexus.commands.taxonomy_cmd import _claude_available" in index

    def test_all_five_call_sites_exist(self) -> None:
        # Non-vacuity: the sites this phase rewired are really there.
        counts = {}
        for path in _SITE_FILES:
            tree = ast.parse(path.read_text())
            counts[path.name] = sum(
                1 for n in ast.walk(tree)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_claude_available"
            )
        assert counts == {
            "taxonomy_cmd.py": 2, "index.py": 1, "aspect_worker_daemon.py": 2, "doctor.py": 1,
        }


# ── building the API backends: credentials and endpoints come from the right places ─


class TestBuildApiBackend:
    def test_anthropic_key_from_the_environment(self, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", _KEY)
        backend = build_api_backend("anthropic")
        assert backend.name == "anthropic" and backend.available().available

    def test_anthropic_key_from_the_global_config_and_env_wins(self, monkeypatch) -> None:
        _write_global_config("credentials:\n  anthropic_api_key: from-config-key\n")
        assert build_api_backend("anthropic")._api_key == "from-config-key"
        monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env-key")
        assert build_api_backend("anthropic")._api_key == "from-env-key"

    def test_the_key_never_reaches_the_process_environment(self, monkeypatch) -> None:
        _write_global_config("credentials:\n  anthropic_api_key: " + _KEY + "\n")
        before = dict(os.environ)
        build_api_backend("anthropic")
        assert dict(os.environ) == before and "ANTHROPIC_API_KEY" not in os.environ

    def test_a_config_only_key_is_never_handed_to_a_claude_child(self, monkeypatch) -> None:
        # Injecting the config.yml key into the child's env would silently move a
        # subscription user to API billing.
        _write_global_config("credentials:\n  anthropic_api_key: " + _KEY + "\n")
        monkeypatch.setenv("NX_LLM_BACKEND", "cli")
        for kwargs in ({}, {"grants_tool_access": True}):
            env = dispatch_mod._build_dispatch_env(**kwargs)
            assert _KEY not in "".join(env.values())
            assert "ANTHROPIC_API_KEY" not in env

    def test_the_key_is_not_in_the_backends_repr_or_status(self) -> None:
        _write_global_config("credentials:\n  anthropic_api_key: " + _KEY + "\n")
        backend = build_api_backend("anthropic")
        assert _KEY not in repr(backend.available()) and _KEY not in str(backend.available().reason)

    def test_openai_compat_from_global_config(self) -> None:
        _write_global_config(
            "llm:\n  backend: openai_compat\n  openai_compat:\n    base_url: http://gpu-box:8000\n    model: qwen\n"
            "credentials:\n  openai_compat_api_key: secret-oa-key\n"
        )
        backend = build_api_backend("openai_compat")
        assert backend._url() == "http://gpu-box:8000/v1/chat/completions"
        assert backend._model == "qwen" and backend._api_key == "secret-oa-key"

    def test_openai_compat_endpoint_is_not_read_from_a_per_repo_nexus_yml(self, tmp_path, monkeypatch) -> None:
        # A cloned repository must not be able to point prompts at a server it names.
        from nexus.config import load_config

        repo = tmp_path / "cloned"
        repo.mkdir()
        (repo / ".nexus.yml").write_text(
            "llm:\n  openai_compat:\n    base_url: http://attacker.example\n    model: evil\n", encoding="utf-8")
        monkeypatch.delenv("NEXUS_REPO_ROOT", raising=False)
        monkeypatch.chdir(repo)
        assert load_config()["llm"]["openai_compat"]["base_url"] == "http://attacker.example"  # non-vacuity
        _write_global_config("llm:\n  backend: openai_compat\n")
        with pytest.raises(OperatorError, match="base_url"):
            build_api_backend("openai_compat")
        monkeypatch.setenv("NX_LLM_OPENAI_BASE_URL", "http://trusted:1")
        monkeypatch.setenv("NX_LLM_OPENAI_MODEL", "good")
        assert build_api_backend("openai_compat")._url() == "http://trusted:1/v1/chat/completions"

    def test_openai_key_has_its_own_variable_and_ignores_openai_api_key(self, monkeypatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "must-never-be-sent-to-a-configured-server")
        monkeypatch.setenv("NX_LLM_OPENAI_BASE_URL", "http://h:1")
        monkeypatch.setenv("NX_LLM_OPENAI_MODEL", "m")
        assert build_api_backend("openai_compat")._api_key is None
        monkeypatch.setenv("NX_LLM_OPENAI_API_KEY", "right-one")
        assert build_api_backend("openai_compat")._api_key == "right-one"

    def test_unknown_backend_name(self) -> None:
        with pytest.raises(OperatorError):
            build_api_backend("cli")

    def test_credentials_are_registered_and_masked(self) -> None:
        from nexus.config import CREDENTIALS, NON_SECRET_CREDENTIALS

        assert CREDENTIALS["anthropic_api_key"] == "ANTHROPIC_API_KEY"
        assert CREDENTIALS["openai_compat_api_key"] == "NX_LLM_OPENAI_API_KEY"
        assert not ({"anthropic_api_key", "openai_compat_api_key"} & NON_SECRET_CREDENTIALS)  # masked by `nx config get`


class TestSelectedApiBackendAndTextRun:
    def test_none_when_the_cli_serves_or_nothing_does(self, claude_on_path, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", _KEY)
        assert selected_api_backend() is None  # cli wins on a box with both
        setattr_in(monkeypatch, "nexus.operators.llm_backend", "shutil.which", _off_path)
        assert selected_api_backend() == "anthropic"
        monkeypatch.delenv("ANTHROPIC_API_KEY")
        assert selected_api_backend() is None  # nothing: the caller's CLI path says so

    def test_a_misconfiguration_raises(self, claude_on_path, monkeypatch) -> None:
        monkeypatch.setenv("NX_LLM_BACKEND", "anthropic")
        with pytest.raises(OperatorError, match="anthropic_api_key"):
            selected_api_backend()

    def test_run_text_sync_runs_a_text_call_with_transport_retries_off(self, monkeypatch) -> None:
        fake = FakeBackend()
        seen = []

        async def run(req):
            seen.append(req)
            return DispatchResult(text="hello")

        fake.run = run  # type: ignore[method-assign]
        with patch("nexus.operators.llm_backend.build_api_backend", return_value=fake) as build:
            result = run_text_sync("anthropic", "p", model="haiku", timeout=9.0, operator="aspect_extractor")
        assert result.text == "hello" and build.call_args.args == ("anthropic",)
        (req,) = seen
        assert (req.json_schema, req.transport_retries, req.timeout, req.model) == (None, 0, 9.0, "haiku")

    def test_run_text_sync_works_from_inside_a_running_loop(self) -> None:
        fake = FakeBackend()

        async def run(req):
            return DispatchResult(text="from-thread")

        fake.run = run  # type: ignore[method-assign]

        async def caller():
            with patch("nexus.operators.llm_backend.build_api_backend", return_value=fake):
                return run_text_sync("anthropic", "p", model=None, timeout=5.0)

        assert asyncio.run(caller()).text == "from-thread"


# ── nexus-2k66t.3 P2 fix round ──────────────────────────────────────────────


_BROKEN_KEY_LINE = "sk-ant-FAKEBROKEN0123456789"
#: A YAML scanner error whose offending line (line 3) carries a credential value.
_BROKEN_CONFIG = "credentials:\n  other: 1\n  anthropic_api_key: " + _BROKEN_KEY_LINE + ": [x\n"
_BROKEN_OPENAI_CONFIG = (
    "credentials:\n  other: 1\n  openai_compat_api_key: " + _BROKEN_KEY_LINE + ": [x\n"
)


def _log_text(logs) -> str:
    import json

    return json.dumps(logs, default=str)


class TestAMalformedConfigNeverLeaksACredential:
    """A YAML error's text quotes the offending line, which can be a credential
    value. Only the exception type and the line number may be logged or raised."""

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch) -> None:
        for var in ("NX_LLM_BACKEND", "ANTHROPIC_API_KEY", "NX_LLM_OPENAI_API_KEY"):
            monkeypatch.delenv(var, raising=False)

    def test_the_fixture_really_quotes_the_key(self) -> None:
        # Non-vacuity: the raw parser error DOES carry the value, so a pass below means the scrub worked.
        import yaml

        with pytest.raises(yaml.YAMLError) as ei:
            yaml.safe_load(_BROKEN_CONFIG)
        assert _BROKEN_KEY_LINE in str(ei.value)

    def test_the_credential_probe_logs_the_type_and_line_only(self) -> None:
        import structlog

        from nexus.operators.llm_backend import real_key_present

        _write_global_config(_BROKEN_CONFIG)
        with structlog.testing.capture_logs() as logs:
            assert real_key_present("anthropic_api_key") is False
        events = [e for e in logs if e["event"] == "llm_backend_credential_unreadable"]
        assert len(events) == 1 and events[0]["error_type"] == "ScannerError" and events[0]["line"] == 3
        assert _BROKEN_KEY_LINE not in _log_text(logs)

    def test_the_section_reader_logs_the_type_and_line_only(self) -> None:
        import structlog

        _write_global_config(_BROKEN_CONFIG)
        with structlog.testing.capture_logs() as logs:
            resolve_backend_name(_req())  # auto: the broken config means auto, and says so
        events = [e for e in logs if e["event"] == "llm_backend_config_unreadable"]
        assert len(events) == 1 and events[0]["error_type"] == "ScannerError" and events[0]["line"] == 3
        assert _BROKEN_KEY_LINE not in _log_text(logs)

    @pytest.mark.parametrize("name,text,extra_env", [
        ("anthropic", _BROKEN_CONFIG, {}),
        ("openai_compat", _BROKEN_OPENAI_CONFIG, _OPENAI_OK),
    ])
    def test_building_a_backend_raises_a_clean_operator_error(self, name, text, extra_env, monkeypatch) -> None:
        import structlog

        for k, v in extra_env.items():
            monkeypatch.setenv(k, v)
        _write_global_config(text)
        with structlog.testing.capture_logs() as logs, pytest.raises(OperatorError) as ei:
            build_api_backend(name)
        assert str(ei.value) == "global config.yml is unreadable at line 3"
        everything = _log_text(logs) + str(ei.value) + repr(ei.value) + repr(ei.value.__cause__) + repr(ei.value.__context__)
        assert _BROKEN_KEY_LINE not in everything
        assert ei.value.__cause__ is None and ei.value.__context__ is None

    def test_an_explicit_anthropic_dispatch_over_a_broken_config_leaks_nothing(self, monkeypatch) -> None:
        import structlog

        monkeypatch.setenv("NX_LLM_BACKEND", "anthropic")
        _write_global_config(_BROKEN_CONFIG)
        with structlog.testing.capture_logs() as logs, _spawn_raises(), pytest.raises(OperatorError) as ei:
            asyncio.run(claude_dispatch("p", _SCHEMA))
        assert _BROKEN_KEY_LINE not in _log_text(logs) + str(ei.value) + repr(ei.value)


class TestTheDispatchEnvKeepsAnOpenAiCompatKeyOutOfClaudeChildren:
    @pytest.mark.parametrize("kwargs", [{}, {"ephemeral": True}, {"ephemeral": True, "grants_tool_access": True}],
                             ids=["owned", "ephemeral", "ephemeral-tools"])
    def test_nx_llm_openai_api_key_is_dropped(self, monkeypatch, kwargs) -> None:
        monkeypatch.setenv("NX_LLM_OPENAI_API_KEY", "oa-secret-must-not-reach-claude")
        monkeypatch.setattr("nexus.db.t1.mint_t1_session_token", lambda sid, *, context: {"session_token": "t"})
        env = dispatch_mod._build_dispatch_env(**kwargs)
        assert "NX_LLM_OPENAI_API_KEY" not in env
        assert "oa-secret-must-not-reach-claude" not in "".join(env.values())

    def test_an_exported_anthropic_api_key_is_still_inherited(self, monkeypatch) -> None:
        # Unchanged CLI semantics: claude -p honours an exported ANTHROPIC_API_KEY.
        monkeypatch.setenv("ANTHROPIC_API_KEY", _KEY)
        assert dispatch_mod._build_dispatch_env()["ANTHROPIC_API_KEY"] == _KEY


class TestNoBackendIsTheNamedErrorAtTheSpawnToo:
    def test_auto_with_neither_raises_no_backend_available_error(self, claude_off_path) -> None:
        async def spawn(*_a, **_k):
            raise FileNotFoundError("claude")

        with patch_in("nexus.operators.dispatch", "asyncio.create_subprocess_exec", side_effect=spawn):
            with pytest.raises(NoBackendAvailableError) as ei:
                asyncio.run(claude_dispatch("p", _SCHEMA))
        assert str(ei.value) == NO_BACKEND_MESSAGE

    def test_an_explicit_cli_without_claude_stays_a_plain_operator_error(self, claude_off_path, monkeypatch) -> None:
        monkeypatch.setenv("NX_LLM_BACKEND", "cli")

        async def spawn(*_a, **_k):
            raise FileNotFoundError("claude")

        with patch_in("nexus.operators.dispatch", "asyncio.create_subprocess_exec", side_effect=spawn):
            with pytest.raises(OperatorError) as ei:
                asyncio.run(claude_dispatch("p", _SCHEMA))
        assert not isinstance(ei.value, NoBackendAvailableError)


class TestOpenAiCompatPriceSettings:
    @pytest.fixture(autouse=True)
    def _endpoint(self, monkeypatch) -> None:
        for k, v in _OPENAI_OK.items():
            monkeypatch.setenv(k, v)

    def test_a_configured_price_reaches_the_backend(self) -> None:
        _write_global_config("llm:\n  openai_compat:\n    price_in_per_mtok: 0.5\n    price_out_per_mtok: 1.5\n")
        price = build_api_backend("openai_compat")._price("llama3.1")
        assert (price.input, price.output) == (0.5, 1.5)

    def test_no_price_means_none_and_not_unpriced_ok(self) -> None:
        backend = build_api_backend("openai_compat")
        assert backend._price("llama3.1") is None and backend._unpriced_ok is False

    def test_unpriced_ok_true_reaches_the_backend(self) -> None:
        _write_global_config("llm:\n  openai_compat:\n    unpriced_ok: true\n")
        assert build_api_backend("openai_compat")._unpriced_ok is True

    @pytest.mark.parametrize("text", [
        "llm:\n  openai_compat:\n    price_in_per_mtok: 0.5\n",                       # half a price
        "llm:\n  openai_compat:\n    price_in_per_mtok: x\n    price_out_per_mtok: 1\n",
        "llm:\n  openai_compat:\n    price_in_per_mtok: -1\n    price_out_per_mtok: 1\n",
        "llm:\n  openai_compat:\n    price_in_per_mtok: .inf\n    price_out_per_mtok: 1\n",
        "llm:\n  openai_compat:\n    unpriced_ok: maybe\n",
        "llm:\n  openai_compat:\n    price_in_per_mtok: 0\n    price_out_per_mtok: 0\n",   # free: say unpriced_ok
    ])
    def test_a_malformed_price_setting_fails_loud(self, text) -> None:
        _write_global_config(text)
        with pytest.raises(OperatorError, match="llm.openai_compat"):
            build_api_backend("openai_compat")
