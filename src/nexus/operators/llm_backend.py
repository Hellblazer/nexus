# SPDX-License-Identifier: AGPL-3.0-or-later
"""LLM backend protocol, request/result types and backend selection (nexus-2k66t.3).

``claude_dispatch`` (``nexus.operators.dispatch``) is a thin front over an
``LLMBackend``. Three backends exist: ``cli`` (``dispatch.CliBackend``, the
``claude -p`` subprocess), ``anthropic`` (``llm_anthropic``, the Messages API on
raw ``httpx``) and ``openai_compat`` (``llm_openai``, Chat Completions for
Ollama, vLLM, OpenRouter).

Design of record: T2 ``nexus/design-direct-llm-provider-2026-10-23``.

Selection (:func:`select_backend`, pure): the setting is ``NX_LLM_BACKEND``,
then ``llm.backend`` in the GLOBAL ``~/.config/nexus/config.yml``, then ``auto``.
``auto`` is ``cli`` when ``claude`` is on PATH (today's behaviour and
subscription billing), else ``anthropic`` when an ``anthropic_api_key``
resolves, else no backend (:class:`NoBackendAvailableError`). A call that grants
tools always goes to ``cli``. An explicit ``anthropic`` or ``openai_compat``
forces that backend for every tool-free call.

Where settings are read: the environment and the GLOBAL config only (the
mtime-cached reader ``get_credential`` uses), never the per-repo ``.nexus.yml``:
a cloned repository must not be able to pick the backend, redirect prompts to an
endpoint of its choosing, or change which account is billed. ``load_config()``
merges the per-repo file, so it is not used here.

This module imports nothing from ``nexus.operators.dispatch`` and nothing from
the API backends at load time (both import it); the backends are built lazily.
"""
from __future__ import annotations

import math
import os
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

import structlog

from nexus.operators.llm_types import DispatchUsage, OperatorError

_log = structlog.get_logger(__name__)

#: Environment override for the backend. Wins over config ``llm.backend``.
NX_LLM_BACKEND_ENV: str = "NX_LLM_BACKEND"

#: Every value the setting accepts.
BACKEND_SETTINGS: tuple[str, ...] = ("auto", "cli", "anthropic", "openai_compat")

#: Backends this release implements. ``select_backend`` and ``backend_status``
#: both decide from this one constant; they raise / report unavailable for the rest.
IMPLEMENTED_BACKENDS: frozenset[str] = frozenset({"cli", "anthropic", "openai_compat"})

#: Credential names (``config.CREDENTIALS`` keys) the selection looks for.
ANTHROPIC_KEY: str = "anthropic_api_key"
OPENAI_KEY: str = "openai_compat_api_key"

#: Environment overrides for the ``openai_compat`` endpoint and model. They win
#: over ``llm.openai_compat.base_url`` / ``.model`` in the global config.
NX_LLM_OPENAI_BASE_URL_ENV: str = "NX_LLM_OPENAI_BASE_URL"
NX_LLM_OPENAI_MODEL_ENV: str = "NX_LLM_OPENAI_MODEL"

#: ``(credential name) -> is a non-empty value available``. The real one reads
#: the environment then the global config; the pure default reads an env mapping.
KeyPresent = Callable[[str], bool]

NO_BACKEND_MESSAGE: str = (
    "no LLM backend is available: the claude CLI is not on PATH and no anthropic_api_key is set. "
    "Install Claude Code, or run `nx config set anthropic_api_key <key>` (or export ANTHROPIC_API_KEY), "
    "or set llm.backend to openai_compat with a base_url and model in ~/.config/nexus/config.yml. "
    "Search and storage work without one."
)


class NoBackendAvailableError(OperatorError):
    """``auto`` found neither a ``claude`` on PATH nor an Anthropic key.
    A subclass of ``OperatorError``, so the runner's unavailable handling and
    every ``except OperatorError`` still catch it."""


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
    #: Transport retries an API backend may make on 429/529/5xx/connection
    #: failures. ``None`` is the backend default (2). A caller with its own
    #: retry loop (the aspect extractor) passes 0 so the two do not multiply.
    transport_retries: int | None = None
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
        raise OperatorError(
            f"{source}={raw!r} is not a known LLM backend; use one of: "
            f"{', '.join(BACKEND_SETTINGS)}"
        )
    return setting


@dataclass(frozen=True)
class _Decision:
    """What the setting, the PATH and the credentials add up to for a
    tool-free request."""

    backend: str  # cli | anthropic | openai_compat | none
    reason: str
    problem: str | None = None  # why it cannot serve; None when it can
    nothing_available: bool = False  # auto found no backend at all


def _env_key_present(env: Mapping[str, str]) -> KeyPresent:
    """The pure default for *key_present*: a credential counts when its
    environment variable is non-empty in *env*."""
    from nexus.config import CREDENTIALS  # noqa: PLC0415 - deferred: config pulls yaml and the tuning tables

    return lambda name: bool((env.get(CREDENTIALS[name], "") or "").strip())


def config_error_fields(exc: BaseException) -> dict[str, Any]:
    """What a log line or an error may say about a failure to read the global
    config: the exception type and, for a YAML error, the 1-based line. Never
    ``str(exc)``: a YAML error quotes the offending line, and that line can be a
    credential value."""
    fields: dict[str, Any] = {"error_type": type(exc).__name__}
    mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
    line = getattr(mark, "line", None)
    if isinstance(line, int):
        fields["line"] = line + 1
    return fields


def _unreadable_config_message(exc: BaseException) -> str:
    line = config_error_fields(exc).get("line")
    return "global config.yml is unreadable" + (f" at line {line}" if line is not None else "")


def real_key_present(name: str) -> bool:
    """Whether credential *name* resolves from the real environment or the
    global config. A config that cannot be read counts as absent (and warns,
    with the exception type and line only: see :func:`config_error_fields`)."""
    try:
        from nexus.config import get_credential  # noqa: PLC0415 - deferred: config pulls yaml and the tuning tables

        return bool((get_credential(name) or "").strip())
    except Exception as exc:  # noqa: BLE001 - a broken config.yml must not turn a probe into a crash
        _log.warning("llm_backend_credential_unreadable", credential=name, **config_error_fields(exc))
        return False


def openai_compat_settings(config: Mapping[str, Any] | None, env: Mapping[str, str]) -> tuple[str, str]:
    """``(base_url, model)`` for the ``openai_compat`` backend: the
    ``NX_LLM_OPENAI_BASE_URL`` / ``NX_LLM_OPENAI_MODEL`` environment, then
    ``llm.openai_compat.base_url`` / ``.model`` in the GLOBAL config. Either is
    ``""`` when unset. Never read from a per-repo file."""
    section = (config or {}).get("llm")
    sub = section.get("openai_compat") if isinstance(section, Mapping) else None
    sub = sub if isinstance(sub, Mapping) else {}
    base = (env.get(NX_LLM_OPENAI_BASE_URL_ENV) or str(sub.get("base_url") or "")).strip()
    model = (env.get(NX_LLM_OPENAI_MODEL_ENV) or str(sub.get("model") or "")).strip()
    return base, model


def _openai_compat_problem(config: Mapping[str, Any] | None, env: Mapping[str, str]) -> str | None:
    base, model = openai_compat_settings(config, env)
    missing = [n for n, v in (("base_url", base), ("model", model)) if not v]
    if missing:
        return (
            f"LLM backend 'openai_compat' is selected but llm.openai_compat.{' and '.join(missing)} "
            f"is not set (global ~/.config/nexus/config.yml, or {NX_LLM_OPENAI_BASE_URL_ENV} / "
            f"{NX_LLM_OPENAI_MODEL_ENV}; a per-repo .nexus.yml is ignored)"
        )
    parts = urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return f"llm.openai_compat.base_url must be an http(s) URL with a host; got {parts.scheme or 'no'} scheme"
    if parts.username or parts.password:
        return "llm.openai_compat.base_url must not carry user:password@; set openai_compat_api_key instead"
    return None


def _decide(
    setting: str,
    config: Mapping[str, Any] | None,
    env: Mapping[str, str],
    which: Callable[[str], str | None],
    key_present: KeyPresent,
) -> _Decision:
    """The one decision :func:`select_backend`, :func:`backend_status` and
    :func:`llm_backend_available` all read, so they cannot disagree."""
    has_cli = bool(which("claude"))
    has_key = key_present(ANTHROPIC_KEY)
    if setting == "auto":
        if has_cli:
            return _Decision("cli", "claude is on PATH")
        if has_key:
            return _Decision("anthropic", "claude is not on PATH; the anthropic API is used (anthropic_api_key is set)")
        return _Decision("none", NO_BACKEND_MESSAGE, problem=NO_BACKEND_MESSAGE, nothing_available=True)
    if setting == "cli":
        if has_cli:
            return _Decision("cli", "claude is on PATH (llm.backend is cli)")
        return _Decision("cli", "the claude CLI is not on PATH", problem="the claude CLI is not on PATH (llm.backend is cli)")
    if setting == "anthropic":
        if has_key:
            return _Decision("anthropic", "llm.backend is anthropic and anthropic_api_key is set")
        msg = (
            "LLM backend 'anthropic' is selected but no anthropic_api_key is set: run "
            "`nx config set anthropic_api_key <key>` or export ANTHROPIC_API_KEY"
        )
        return _Decision("anthropic", msg, problem=msg)
    # openai_compat
    problem = _openai_compat_problem(config, env)
    if problem:
        return _Decision("openai_compat", problem, problem=problem)
    base, model = openai_compat_settings(config, env)
    return _Decision("openai_compat", f"llm.backend is openai_compat ({urlsplit(base).netloc}, model {model})")


def select_backend(
    config: Mapping[str, Any] | None,
    env: Mapping[str, str],
    which: Callable[[str], str | None],
    req: DispatchRequest,
    key_present: KeyPresent | None = None,
) -> str:
    """Pick the backend name for *req*. Pure: reads only its arguments.

    * The setting is ``NX_LLM_BACKEND`` over ``llm.backend`` over ``auto``.
    * A request that grants tools always routes to ``cli`` (a missing
      ``claude`` then surfaces when it is spawned, with a message that says so).
    * ``auto``: ``cli`` when ``which("claude")``, else ``anthropic`` when
      ``key_present("anthropic_api_key")``, else :class:`NoBackendAvailableError`.
    * An explicit ``cli`` returns ``cli`` whatever is on PATH (the spawn then
      fails with a clear message); an explicit ``anthropic`` without a key, or
      ``openai_compat`` without a base_url and model, raises ``OperatorError``.

    *which* is the PATH lookup (``shutil.which``). *key_present* defaults to a
    check of the ``env`` mapping alone; the real callers pass
    :func:`real_key_present`, which also reads the global config.
    """
    setting = configured_backend(config, env)
    if req.grants_tool_access:
        return "cli"
    decision = _decide(setting, config, env, which, key_present or _env_key_present(env))
    if decision.backend == "cli":
        return "cli"
    if decision.problem:
        raise (NoBackendAvailableError if decision.nothing_available else OperatorError)(decision.problem)
    return decision.backend


def resolve_backend_name(req: DispatchRequest) -> str:
    """:func:`select_backend` over the process's real inputs: ``os.environ``,
    the global config (always passed, even when ``NX_LLM_BACKEND`` is set: the
    ``openai_compat`` endpoint lives in it), ``shutil.which`` and the real
    credential lookup."""
    return select_backend(_load_llm_config(), os.environ, shutil.which, req, real_key_present)


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
    key_present: KeyPresent | None = None,
) -> BackendStatus:
    """Status of the backend a tool-free request would use. Never raises: a bad
    setting or a missing key is reported as unavailable with the reason, and no
    reason ever contains a key. With no *config* the global config is read; with
    no *env* the process environment and the real credential lookup are used, so
    an Anthropic key in ``config.yml`` counts."""
    real = env is None
    env = os.environ if real else env
    config = _load_llm_config() if config is None else config
    if key_present is None:
        key_present = real_key_present if real else _env_key_present(env)
    try:
        setting = configured_backend(config, env)
    except Exception as exc:  # noqa: BLE001 - OperatorError for a bad setting; report, never raise from a probe
        return BackendStatus("none", False, str(exc))
    decision = _decide(setting, config, env, which or shutil.which, key_present)
    return BackendStatus(decision.backend, decision.problem is None, decision.problem or decision.reason)


def _load_llm_config() -> Mapping[str, Any]:
    """``{"llm": <section>}`` from the GLOBAL config only, never the per-repo
    ``.nexus.yml``. A missing, unreadable or malformed global config is not a
    reason to fail a probe or a dispatch: ``auto`` applies."""
    try:
        from nexus.config import get_global_config_section  # noqa: PLC0415 - deferred: config pulls yaml and the tuning tables

        return {"llm": get_global_config_section("llm")}
    except Exception as exc:  # noqa: BLE001 - a broken config.yml must not turn a probe into a crash
        # Warning, not debug: a typo in config.yml must not silently move a
        # user who set llm.backend back to the default.
        _log.warning("llm_backend_config_unreadable", **config_error_fields(exc))
        return {}


def llm_backend_available(
    config: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
    key_present: KeyPresent | None = None,
) -> bool:
    """Whether some LLM backend can serve a tool-free request. Replaces the
    per-module ``_claude_available`` probes. True when ``backend_status`` says
    the chosen backend can serve: ``claude`` on PATH, or (``auto`` with no CLI)
    an ``anthropic_api_key``, or a configured ``openai_compat``, per the setting.

    *config* defaults to the global config's ``llm`` section, read even when
    ``NX_LLM_BACKEND`` is set."""
    return backend_status(config, env, which, key_present).available


def _credential(name: str) -> str:
    """Credential *name*, stripped; ``""`` when unset. A global ``config.yml`` that
    cannot be parsed raises a clean ``OperatorError`` naming the line and nothing
    else: the parser's own text quotes the offending line, which can be a
    credential. The error is raised outside the ``except`` so no exception chain
    carries that text either."""
    import yaml  # noqa: PLC0415 - deferred: only an API backend build reads credentials

    from nexus.config import get_credential  # noqa: PLC0415 - deferred: config pulls yaml and the tuning tables

    problem: str | None = None
    value = ""
    try:
        value = get_credential(name) or ""
    except (yaml.YAMLError, OSError) as exc:
        problem = _unreadable_config_message(exc)
    if problem is not None:
        raise OperatorError(problem)
    return value.strip()


def _config_number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise OperatorError(f"{label} must be a number of dollars per million tokens")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise OperatorError(f"{label} must be a number of dollars per million tokens") from None
    if not (math.isfinite(number) and number >= 0):
        raise OperatorError(f"{label} must be a finite number >= 0")
    return number


def openai_compat_pricing(config: Mapping[str, Any] | None) -> tuple[tuple[float, float] | None, bool]:
    """``((price_in, price_out) | None, unpriced_ok)`` from the GLOBAL config:
    ``llm.openai_compat.price_in_per_mtok`` / ``.price_out_per_mtok`` (USD per
    million tokens; both or neither) and ``.unpriced_ok`` (for a free local
    server: a budget ceiling is then accepted unenforced). Malformed values raise
    ``OperatorError``: a ceiling that silently stopped working is the failure
    this exists to prevent."""
    section = (config or {}).get("llm")
    sub = section.get("openai_compat") if isinstance(section, Mapping) else None
    sub = sub if isinstance(sub, Mapping) else {}
    p_in, p_out = sub.get("price_in_per_mtok"), sub.get("price_out_per_mtok")
    price: tuple[float, float] | None = None
    if p_in is not None or p_out is not None:
        if p_in is None or p_out is None:
            raise OperatorError(
                "llm.openai_compat.price_in_per_mtok and llm.openai_compat.price_out_per_mtok must be set together"
            )
        price = (
            _config_number(p_in, "llm.openai_compat.price_in_per_mtok"),
            _config_number(p_out, "llm.openai_compat.price_out_per_mtok"),
        )
    raw_ok = sub.get("unpriced_ok")
    if raw_ok is None:
        unpriced_ok = False
    elif isinstance(raw_ok, bool):
        unpriced_ok = raw_ok
    elif isinstance(raw_ok, str) and raw_ok.strip().lower() in ("true", "false"):
        unpriced_ok = raw_ok.strip().lower() == "true"  # `nx config set` writes strings
    else:
        raise OperatorError("llm.openai_compat.unpriced_ok must be true or false")
    return price, unpriced_ok


def build_api_backend(name: str) -> LLMBackend:
    """The API backend named *name* (``anthropic`` or ``openai_compat``), built
    from the real credentials and global config. The key is read here, held by
    the backend, and goes nowhere else: not into ``os.environ``, not into a log
    line, not into a child process."""
    if name == "anthropic":
        from nexus.operators.llm_anthropic import AnthropicBackend  # noqa: PLC0415 - deferred: httpx stays off the CLI-only path

        return AnthropicBackend(api_key=_credential(ANTHROPIC_KEY) or None)
    if name == "openai_compat":
        from nexus.operators.llm_openai import OpenAICompatBackend  # noqa: PLC0415 - deferred, as above

        config = _load_llm_config()
        problem = _openai_compat_problem(config, os.environ)
        if problem:
            raise OperatorError(problem)
        base, model = openai_compat_settings(config, os.environ)
        price, unpriced_ok = openai_compat_pricing(config)
        return OpenAICompatBackend(
            base_url=base, model=model, api_key=_credential(OPENAI_KEY) or None,
            price=price, unpriced_ok=unpriced_ok,
        )
    raise OperatorError(f"LLM backend {name!r} has no API implementation")


def selected_api_backend() -> str | None:
    """The API backend (``anthropic`` / ``openai_compat``) a tool-free call would
    use right now, or ``None`` when it would use the ``claude`` CLI or no backend
    is available (the caller's CLI path then surfaces that). A misconfiguration
    (an explicit ``anthropic`` with no key) raises ``OperatorError``."""
    try:
        name = resolve_backend_name(DispatchRequest(prompt="", json_schema=None))
    except NoBackendAvailableError:
        return None
    return None if name == "cli" else name


def api_model_name(backend_name: str, requested: str | None) -> str | None:
    """The model a call to the API backend *backend_name* with *requested* as its
    model would run on: the configured server model for ``openai_compat`` (which
    ignores the Claude alias it is handed), the resolved id for ``anthropic``.
    ``None`` when it cannot be told. Used to label what an extraction ran on."""
    try:
        if backend_name == "openai_compat":
            return openai_compat_settings(_load_llm_config(), os.environ)[1] or None
        from nexus.operators.llm_anthropic import resolved_model_id  # noqa: PLC0415 - deferred: httpx stays off the CLI-only path

        return resolved_model_id(requested)
    except Exception:  # noqa: BLE001 - a label must never fail an extraction
        return None


def run_text_sync(
    backend_name: str, prompt: str, *, model: str | None, timeout: float, operator: str | None = None,
) -> DispatchResult:
    """Run one text-mode call (no schema) on the API backend *backend_name* from
    synchronous code, with transport retries off: the caller (the aspect
    extractor) has its own retry loop, and the two must not multiply. Returns the
    :class:`DispatchResult` (``.text``, ``.usage``).

    Works whether or not the calling thread already runs an event loop: with one
    running, the call is made on a fresh loop in a helper thread."""
    import asyncio  # noqa: PLC0415 - only the sync entry needs it
    import concurrent.futures  # noqa: PLC0415 - as above

    req = DispatchRequest(
        prompt=prompt, json_schema=None, timeout=timeout, model=model, operator=operator, transport_retries=0,
    )
    backend = build_api_backend(backend_name)

    def _run() -> DispatchResult:
        return asyncio.run(backend.run(req))

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _run()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_run).result()
