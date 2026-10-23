# SPDX-License-Identifier: AGPL-3.0-or-later
"""LLM backend protocol, selection and availability (nexus-2k66t.3 Phase 1).

Phase 1 implements the ``cli`` backend only. These tests pin: the pure
``select_backend`` matrix, ``claude_dispatch`` as a front over an injected
backend, and the five availability probes routing through
``llm_backend_available``. The byte-identity of the CLI path itself is pinned in
``test_llm_backend_cli_identity.py``.
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
from tests._module_seam import patch_in

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
    BackendStatus,
    DispatchRequest,
    DispatchResult,
    LLMBackend,
    backend_status,
    configured_backend,
    llm_backend_available,
    resolve_backend_name,
    select_backend,
)

_SCHEMA = {"type": "object", "properties": {"result": {"type": "string"}}}


def _on_path(name: str) -> str | None:
    return "/usr/local/bin/claude" if name == "claude" else None


def _off_path(name: str) -> str | None:
    return None


def _req(**kw) -> DispatchRequest:
    return DispatchRequest(prompt="p", json_schema=_SCHEMA, **kw)


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
    @pytest.mark.parametrize("which", [_on_path, _off_path], ids=["cli-on-path", "cli-absent"])
    @pytest.mark.parametrize("config", [None, {}, {"llm": {}}, {"llm": {"backend": "auto"}}], ids=str)
    def test_auto_is_cli_whatever_is_on_path(self, which, config) -> None:
        # auto resolves to cli exactly as dispatch did before this change; a
        # missing binary stays the spawn error, not a new one.
        assert select_backend(config, {}, which, _req()) == "cli"

    def test_explicit_cli(self) -> None:
        assert select_backend({"llm": {"backend": "cli"}}, {}, _off_path, _req()) == "cli"
        assert select_backend(None, {"NX_LLM_BACKEND": "cli"}, _off_path, _req()) == "cli"

    @pytest.mark.parametrize("setting", ["anthropic", "openai_compat"])
    @pytest.mark.parametrize("via", ["env", "config"])
    def test_unimplemented_backend_raises_naming_the_later_phase(self, setting, via) -> None:
        config = {"llm": {"backend": setting}} if via == "config" else None
        env = {"NX_LLM_BACKEND": setting} if via == "env" else {}
        with pytest.raises(OperatorError, match=r"later phase") as ei:
            select_backend(config, env, _on_path, _req())
        assert setting in str(ei.value)

    def test_env_beats_config(self) -> None:
        config = {"llm": {"backend": "anthropic"}}
        assert select_backend(config, {"NX_LLM_BACKEND": "cli"}, _on_path, _req()) == "cli"
        with pytest.raises(OperatorError):
            select_backend({"llm": {"backend": "cli"}}, {"NX_LLM_BACKEND": "anthropic"}, _on_path, _req())

    def test_blank_env_falls_through_to_config(self) -> None:
        with pytest.raises(OperatorError, match="later phase"):
            select_backend({"llm": {"backend": "anthropic"}}, {"NX_LLM_BACKEND": "  "}, _on_path, _req())

    def test_case_and_whitespace_are_normalised(self) -> None:
        assert select_backend(None, {"NX_LLM_BACKEND": " CLI "}, _on_path, _req()) == "cli"

    @pytest.mark.parametrize("grant", [
        {"allowed_tools": ["Read"]},
        {"mcp_servers": {"nx": {"command": "nx-mcp"}}},
        {"allowed_tools": ["Read"], "mcp_servers": {"nx": {"command": "nx-mcp"}}},
    ], ids=["allowed_tools", "mcp_servers", "both"])
    @pytest.mark.parametrize("setting", ["auto", "cli", "anthropic", "openai_compat"])
    def test_tool_grant_always_routes_to_cli(self, grant, setting) -> None:
        env = {"NX_LLM_BACKEND": setting}
        assert select_backend(None, env, _off_path, _req(**grant)) == "cli"

    def test_empty_grant_is_not_a_grant(self) -> None:
        assert _req(allowed_tools=[], mcp_servers={}).grants_tool_access is False
        with pytest.raises(OperatorError, match="later phase"):
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
        env = {"NX_LLM_BACKEND": "cli"}
        config = {"llm": {"backend": "cli"}}
        calls: list[str] = []

        def which(name: str) -> str | None:
            calls.append(name)
            return None

        select_backend(config, env, which, _req())
        assert env == {"NX_LLM_BACKEND": "cli"} and config == {"llm": {"backend": "cli"}}
        assert calls == []  # phase 1 never consults PATH for the choice

    @pytest.mark.parametrize("bad", [None, "x", 3, []])
    def test_malformed_llm_section_is_auto(self, bad) -> None:
        assert configured_backend({"llm": bad}, {}) == "auto"


# ── availability ────────────────────────────────────────────────────────────


class TestAvailability:
    def test_same_answer_as_the_old_probe(self) -> None:
        assert llm_backend_available({}, {}, _on_path) is True
        assert llm_backend_available({}, {}, _off_path) is False

    def test_default_inputs_follow_shutil_which(self) -> None:
        with patch_in("nexus.operators.llm_backend", "shutil.which", side_effect=lambda n: "/x/claude" if n == "claude" else None):
            assert llm_backend_available({}, {}) is True
        with patch_in("nexus.operators.llm_backend", "shutil.which", return_value=None):
            assert llm_backend_available({}, {}) is False

    @pytest.mark.parametrize("setting", ["anthropic", "openai_compat"])
    def test_unimplemented_setting_is_unavailable_with_a_reason(self, setting) -> None:
        status = backend_status(None, {"NX_LLM_BACKEND": setting}, _on_path)
        assert status.available is False and setting in status.reason
        assert llm_backend_available(None, {"NX_LLM_BACKEND": setting}, _on_path) is False

    def test_unknown_setting_does_not_raise_from_a_probe(self) -> None:
        status = backend_status(None, {"NX_LLM_BACKEND": "gemini"}, _on_path)
        assert status.available is False and "gemini" in status.reason

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
        # Pins the json_schema=None guard: a later phase routes text mode to the
        # API backend, and until then the cli backend must not spawn claude with
        # a schema of "null" (what the pre-P1 code would have sent).
        with _spawn_raises():
            with pytest.raises(OperatorError, match="json_schema"):
                asyncio.run(CliBackend().run(DispatchRequest(prompt="p", json_schema=None)))

    def test_backends_satisfy_the_protocol(self) -> None:
        assert isinstance(CliBackend(), LLMBackend)
        assert isinstance(FakeBackend(), LLMBackend)


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

    def test_unimplemented_backend_setting_fails_before_spawning(self, monkeypatch) -> None:
        monkeypatch.setenv("NX_LLM_BACKEND", "anthropic")
        with _spawn_raises():
            with pytest.raises(OperatorError, match="later phase"):
                asyncio.run(claude_dispatch("p", _SCHEMA))

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

    def test_global_config_anthropic_makes_claude_dispatch_raise_the_later_phase_error(self) -> None:
        _write_global_config("llm:\n  backend: anthropic\n")
        with _spawn_raises():
            with pytest.raises(OperatorError, match="later phase") as ei:
                asyncio.run(claude_dispatch("p", _SCHEMA))
        assert "anthropic" in str(ei.value)

    def test_env_cli_overrides_a_config_value_of_anthropic(self, monkeypatch) -> None:
        _write_global_config("llm:\n  backend: anthropic\n")
        with pytest.raises(OperatorError, match="later phase"):
            resolve_backend_name(_req())  # config alone: raises
        monkeypatch.setenv("NX_LLM_BACKEND", "cli")
        assert resolve_backend_name(_req()) == "cli"  # env wins

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

        def spy(config, env, which, req):
            seen["config"] = config
            return "cli"

        with patch("nexus.operators.llm_backend.select_backend", side_effect=spy):
            resolve_backend_name(_req())
        assert seen["config"] == {"llm": {"backend": "anthropic"}}  # a later phase reads its key from it


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
