# SPDX-License-Identifier: AGPL-3.0-or-later
"""LLM backend protocol, request/result types and backend selection (nexus-2k66t.3).

``claude_dispatch`` (``nexus.operators.dispatch``) is a thin front over an
``LLMBackend``. Phase 1 has one backend, ``cli`` (``dispatch.CliBackend``, the
former body of ``claude_dispatch``, moved without change). ``anthropic`` and
``openai_compat`` are named here so the setting can be validated, but selecting
either raises until the phase that implements it.

Design of record: T2 ``nexus/design-direct-llm-provider-2026-10-23``.

This module imports nothing from ``nexus.operators.dispatch`` at load time
(``dispatch`` imports it); the exception types are loaded when raised.
"""
from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, NoReturn, Protocol, runtime_checkable

import structlog

if TYPE_CHECKING:
    from nexus.operators.dispatch import DispatchUsage

_log = structlog.get_logger(__name__)

#: Environment override for the backend. Wins over config ``llm.backend``.
NX_LLM_BACKEND_ENV: str = "NX_LLM_BACKEND"

#: Every value the setting accepts.
BACKEND_SETTINGS: tuple[str, ...] = ("auto", "cli", "anthropic", "openai_compat")

#: Backends this release implements. ``select_backend`` raises for the rest.
IMPLEMENTED_BACKENDS: frozenset[str] = frozenset({"cli"})


@dataclass(frozen=True)
class DispatchRequest:
    """One LLM call, in backend-neutral form.

    ``json_schema=None`` means text mode (no structured output).
    ``usage_sink`` is the caller's out-param: a backend appends the parsed
    ``DispatchUsage`` to it (and to the ambient sink) for every call that
    reaches a parsed result, before any post-parse raise.
    ``max_budget_usd`` is already resolved (param over env) and validated.
    """

    prompt: str
    json_schema: dict[str, Any] | None
    timeout: float = 300.0
    deadline: float | None = None
    model: str | None = None
    operator: str | None = None
    max_budget_usd: float | None = None
    allowed_tools: list[str] | None = None
    mcp_servers: dict[str, Any] | None = None
    isolated: bool = True
    usage_sink: "list[DispatchUsage] | None" = field(default=None, compare=False)

    @property
    def grants_tool_access(self) -> bool:
        """Whether the call lets the model use tools (MCP servers or an allowlist)."""
        return bool(self.mcp_servers or self.allowed_tools)


@dataclass(frozen=True)
class DispatchResult:
    """What a backend returns: the schema-conforming object (or text), and usage."""

    structured: Any = None
    text: str | None = None
    usage: "DispatchUsage | None" = None


@dataclass(frozen=True)
class BackendStatus:
    """Whether a backend can serve a request now, and why or why not."""

    name: str
    available: bool
    reason: str = ""


@runtime_checkable
class LLMBackend(Protocol):
    """A way to run one LLM call. Stateless between calls."""

    name: str

    def available(self) -> BackendStatus: ...

    async def run(self, req: DispatchRequest) -> DispatchResult: ...


def configured_backend(config: Mapping[str, Any] | None, env: Mapping[str, str]) -> str:
    """The requested backend setting: ``NX_LLM_BACKEND``, then config ``llm.backend``,
    then ``auto``. Returns one of :data:`BACKEND_SETTINGS`; raises
    ``OperatorError`` for anything else, naming the source and the legal values."""
    raw = (env.get(NX_LLM_BACKEND_ENV) or "").strip()
    source = NX_LLM_BACKEND_ENV
    if not raw:
        section = (config or {}).get("llm")
        value = section.get("backend") if isinstance(section, Mapping) else None
        raw = str(value).strip() if value is not None else ""
        source = "config llm.backend"
    if not raw:
        return "auto"
    setting = raw.lower()
    if setting not in BACKEND_SETTINGS:
        from nexus.operators.dispatch import OperatorError  # noqa: PLC0415 - dispatch imports this module at load

        raise OperatorError(
            f"{source}={raw!r} is not a known LLM backend; use one of: "
            f"{', '.join(BACKEND_SETTINGS)}"
        )
    return setting


def select_backend(
    config: Mapping[str, Any] | None,
    env: Mapping[str, str],
    which: Callable[[str], str | None],
    req: DispatchRequest,
) -> str:
    """Pick the backend name for *req*. Pure: reads only its arguments.

    * The setting is ``NX_LLM_BACKEND`` over ``llm.backend`` over ``auto``.
    * A request that grants tools always routes to ``cli``.
    * Phase 1 implements ``cli`` only: ``auto`` resolves to it exactly as
      dispatch did before (a missing ``claude`` surfaces as the spawn error,
      not a new one); ``anthropic`` and ``openai_compat`` raise
      ``OperatorError`` saying the backend lands in a later phase.

    *which* is the PATH lookup (``shutil.which``); phase 1 does not consult it
    for the choice, phase 2's ``auto`` does.
    """
    setting = configured_backend(config, env)
    if req.grants_tool_access:
        return "cli"
    if setting in ("auto", "cli"):
        return "cli"
    _raise_not_implemented(setting)


def _raise_not_implemented(setting: str) -> NoReturn:
    from nexus.operators.dispatch import OperatorError  # noqa: PLC0415 - dispatch imports this module at load

    raise OperatorError(
        f"LLM backend {setting!r} is selected (NX_LLM_BACKEND or config llm.backend) "
        "but is not available in this release; it lands in a later phase. "
        "Unset the setting or use 'auto' or 'cli'."
    )


def resolve_backend_name(req: DispatchRequest) -> str:
    """:func:`select_backend` over the process's real inputs: ``os.environ``,
    the loaded config (read only when ``NX_LLM_BACKEND`` is unset) and
    ``shutil.which``."""
    config = None if (os.environ.get(NX_LLM_BACKEND_ENV) or "").strip() else _load_llm_config()
    return select_backend(config, os.environ, shutil.which, req)


def cli_status(which: Callable[[str], str | None] | None = None) -> BackendStatus:
    """Whether the ``claude`` binary is on PATH."""
    found = (which or shutil.which)("claude")
    if found:
        return BackendStatus("cli", True, "claude is on PATH")
    return BackendStatus("cli", False, "the claude CLI is not on PATH")


def backend_status(
    config: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> BackendStatus:
    """Status of the backend a tool-free request would use. Never raises: an
    unknown or unimplemented setting is reported as unavailable with the reason."""
    env = os.environ if env is None else env
    try:
        setting = configured_backend(config, env)
    except Exception as exc:  # noqa: BLE001 - OperatorError for a bad setting; report, never raise from a probe
        return BackendStatus("none", False, str(exc))
    if setting in ("auto", "cli"):
        return cli_status(which)
    return BackendStatus(
        setting, False, f"LLM backend {setting!r} is not available in this release"
    )


def _load_llm_config() -> Mapping[str, Any]:
    """Config for the setting lookup. Failure to read config is not a reason to
    fail a probe: ``auto`` applies."""
    try:
        from nexus.config import load_config  # noqa: PLC0415 - deferred: config pulls yaml and the tuning tables

        return load_config()
    except Exception as exc:  # noqa: BLE001 - a broken config.yml must not turn a probe into a crash
        _log.debug("llm_backend_config_unreadable", error=str(exc))
        return {}


def llm_backend_available(
    config: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> bool:
    """Whether some LLM backend can serve a tool-free request. Replaces the
    per-module ``_claude_available`` probes. Phase 1: true exactly when
    ``claude`` is on PATH and the setting is ``auto`` or ``cli``."""
    env = os.environ if env is None else env
    if config is None and not (env.get(NX_LLM_BACKEND_ENV) or "").strip():
        config = _load_llm_config()
    return backend_status(config, env, which).available
