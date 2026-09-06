#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-121 routing-hook framework.

Helpers every routing hook imports. The hook protocol is:

* Read JSON from stdin (the Claude Code PreToolUse payload).
* Print exactly one JSON envelope to stdout.
* Exit 0 on every code path including unexpected exceptions.

Decision envelope shape (PreToolUse):

    allow:
        {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "additionalContext": "..."   # only when allow carries advisory text
        }}

    deny (see ``deny_envelope`` — the reason rides in two audience-specific
    fields, with ``reason`` kept for legacy compatibility):
        {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "<full reason>",   # what the MODEL reads
            "reason": "<full reason>"                       # legacy alias
         },
         "systemMessage": "<short summary>"}                # the USER's transcript banner

Fail-open is the default. Hooks opt in to fail-closed by passing
``fail_closed=True`` to ``run_hook``; the registry.yaml ``fail_closed:
true`` flag is the source of truth and the hook script reads its own
rule entry to decide.

Escape token: a command may include ``# routing-allow: <reason>``
(reason >= 8 characters) to bypass any routing hook. The token is
audited in the telemetry log so over-use is visible.

WRITER SWAP (nexus-gjv9b PART 2, Sam directive 2026-08-20):
``log_routing_event`` records to the engine's ``routing_events`` table
now (best-effort POST via ``urllib``, ~250ms timeout), not the JSONL
log below -- see that function's own docstring for the full design
decision (metered drop on service-down, never a JSONL fallback). The
JSONL append and rotation machinery was deleted at PART 3 (2026-09-05):
this script only ever ships to plugin installs that no longer write
``routing_log.jsonl``, so nothing here could protect a pre-swap install.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import pathlib
import re
import sys
import time
from typing import Any, Callable

ESCAPE_TOKEN = "# routing-allow:"
ESCAPE_REASON_MIN_LENGTH = 8

# ---------------------------------------------------------------------------
# Envelope builders (pure — return JSON strings)
# ---------------------------------------------------------------------------


def allow_envelope(context: str = "") -> str:
    """Return an allow envelope as a JSON string."""
    payload: dict[str, Any] = {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
    }
    if context:
        payload["additionalContext"] = context
    return json.dumps({"hookSpecificOutput": payload})


def deny_envelope(reason: str, summary: str | None = None) -> str:
    """Return a deny envelope as a JSON string.

    The reason rides in three fields for cross-version robustness:

    * ``permissionDecisionReason`` -- the canonical PreToolUse field
      current Claude Code feeds back to the model on a deny. Carries the
      *full* ``reason`` (cause + remediation) so the model can correct.
    * ``systemMessage`` (top-level) -- surfaced in the user transcript.
      Carries the short ``summary`` so the banner stays a one-liner
      instead of the full remediation essay.
    * ``reason`` -- the legacy key earlier envelopes used.

    Earlier envelopes carried *only* ``reason``, which current Claude
    Code does not read: a deny then arrived as a bare "denied" with no
    cause and no remediation, leaving the model to guess what to do
    next. Emitting the canonical field is what makes the redirect
    message actually reach the model.

    ``summary`` decouples the two audiences. When omitted, the first
    non-empty line of ``reason`` is used so callers that don't supply a
    summary still get a terse banner rather than the whole block.
    """
    # Strip BEFORE the truthiness check: a whitespace-only reason is truthy, so
    # ``reason or default`` would keep it, and ``"".splitlines()[0]`` would then
    # IndexError. deny_envelope is on every routing hook's deny path, so it must
    # never raise. Stripping makes the guard fire and keeps the first-line slice
    # safe (reason is now non-empty).
    reason = reason.strip() or "(no reason provided)"
    system_message = summary or reason.splitlines()[0]
    payload = {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
        "reason": reason,
    }
    return json.dumps(
        {"hookSpecificOutput": payload, "systemMessage": system_message}
    )


def warn_envelope(message: str) -> str:
    """Semantic alias for ``allow_envelope`` that signals advisory intent.

    Routing hooks emit warnings when a pattern looks suspicious but the
    command should proceed. The permission decision stays ``allow``;
    the message rides in ``additionalContext`` so the user sees it.
    """
    return allow_envelope(message)


# ---------------------------------------------------------------------------
# Stdout writers (impure — print then exit 0)
# ---------------------------------------------------------------------------


def allow(context: str = "") -> None:
    """Emit allow envelope to stdout and ``exit 0``."""
    sys.stdout.write(allow_envelope(context) + "\n")
    sys.stdout.flush()
    sys.exit(0)


def deny(reason: str, summary: str | None = None) -> None:
    """Emit deny envelope to stdout and ``exit 0`` (never exit 2).

    ``summary`` rides in ``systemMessage`` (the transcript banner);
    ``reason`` rides in ``permissionDecisionReason`` (the model-facing
    feedback). See :func:`deny_envelope`.
    """
    sys.stdout.write(deny_envelope(reason, summary) + "\n")
    sys.stdout.flush()
    sys.exit(0)


def warn(message: str) -> None:
    """Emit warn envelope (allow + additionalContext) and ``exit 0``."""
    sys.stdout.write(warn_envelope(message) + "\n")
    sys.stdout.flush()
    sys.exit(0)


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------


def parse_stdin(raw: str) -> dict[str, Any]:
    """Parse the Claude Code hook payload; return ``{}`` on any failure."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def get_bash_command(payload: dict[str, Any]) -> str:
    """Extract the Bash ``command`` field; ``""`` if not a Bash call."""
    if payload.get("tool_name") != "Bash":
        return ""
    tool_input = payload.get("tool_input") or {}
    cmd = tool_input.get("command") if isinstance(tool_input, dict) else ""
    return cmd if isinstance(cmd, str) else ""


# ---------------------------------------------------------------------------
# Escape token
# ---------------------------------------------------------------------------

_ESCAPE_RE = re.compile(
    r"#\s*routing-allow\s*:\s*(?P<reason>.+?)\s*$",
    re.MULTILINE,
)


def extract_escape_reason(command: str) -> str:
    """Return the ``# routing-allow:`` reason text, or ``""`` when absent.

    nexus-mzvwa.9: the reason trails the command, so the 200-char
    ``command_fragment`` cap in :func:`log_routing_event` routinely cut
    it — making escape reasons un-auditable from the log. Callers pass
    this as the dedicated ``escape_reason`` field instead.
    """
    if not command or ESCAPE_TOKEN not in command:
        return ""
    match = _ESCAPE_RE.search(command)
    return match.group("reason").strip() if match else ""


def should_skip_for_reason(command: str) -> bool:
    """Return True iff ``command`` carries a valid ``# routing-allow:`` escape.

    Valid means: token present and the trailing reason text is at least
    ``ESCAPE_REASON_MIN_LENGTH`` characters after stripping whitespace.
    """
    if not command or ESCAPE_TOKEN not in command:
        return False
    match = _ESCAPE_RE.search(command)
    if not match:
        return False
    reason = match.group("reason").strip()
    return len(reason) >= ESCAPE_REASON_MIN_LENGTH


def degraded_token_variants(segment: str) -> list[list[str]]:
    """Rough tokenizations of a segment ``shlex`` rejected for unbalanced
    quoting (nexus-2e874). The old ``except ValueError: continue`` silently
    DROPPED the whole segment, so a single stray quote anywhere in a gated
    command fully bypassed the guard (``git push origin main
    --receive-pack="x`` was ALLOWed with zero warning).

    Two variants, because neither alone keeps every anchor visible
    (review Important-1): quote-chars-as-whitespace keeps a quote glued to
    a token BOUNDARY splitting (``--receive-pack="x`` -> ``--receive-pack=``,
    ``x``), while quote-chars-removed keeps a quote INSIDE a verb from
    fracturing it (``gi"t push`` -> ``git``, ``push``). Callers must treat a
    match in EITHER variant as a match — the safe, over-inclusive
    direction; only quoting fidelity inside VALUES is lost.
    """
    blanked = segment.replace('"', " ").replace("'", " ").split()
    stripped = segment.replace('"', "").replace("'", "").split()
    return [blanked] if blanked == stripped else [blanked, stripped]


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


#: Matches ServiceRegistry's tier name for the shared nexus-service engine
#: (src/nexus/daemon/service_registry.py TIER_TTLS) -- the lease file is
#: ``<config_dir>/storage_service_addr.<uid>``. Mirrors
#: ``conexus/hooks/scripts/t2_prefix_scan.py``'s identical constant.
_STORAGE_SERVICE_TIER = "storage_service"

#: nexus-znvjd: the client's cross-process DATA-token lease, written by
#: ``nexus.db.data_token.DataTokenManager._write_lease`` at
#: ``<config_dir>/data_token_lease.<sha256(host[:port]\x00tenant)>``.
#: Mirrors ``t2_prefix_scan.py``'s identical constants.
_DATA_TOKEN_LEASE_PREFIX = "data_token_lease."
_DATA_TOKEN_LEASE_FORMAT_VERSION = 1


def _default_config_dir() -> pathlib.Path:
    """Stdlib-only mirror of ``nexus.config.nexus_config_dir`` (same
    resolution ``conexus/hooks/scripts/t2_prefix_scan.py``'s identically-
    named function uses)."""
    config_dir = os.environ.get("NEXUS_CONFIG_DIR") or os.environ.get("NX_CONFIG_DIR")
    if config_dir:
        return pathlib.Path(config_dir)
    return pathlib.Path.home() / ".config" / "nexus"


def _read_service_lease(config_dir: pathlib.Path) -> dict | None:
    """Best-effort read of the local supervisor's ServiceRegistry lease.

    Ported verbatim (nexus-gjv9b PART 2 CRITICAL review fix) from
    ``conexus/hooks/scripts/t2_prefix_scan.py``'s ``_read_lease`` --
    see that function's own docstring for the full design rationale
    (this hook cannot import ``nexus.daemon.service_registry`` either,
    RDR-121 § Contract mirroring nexus-vg6d4's identical constraint for
    the T2 prefix scan). ``tests/test_routing_hooks.py``'s
    ``test_parity_read_service_lease_*`` suite (nexus-gjv9b review
    fold-in round 3, code-review item 2 -- this docstring claimed the
    suite before it existed) runs BOTH implementations against the SAME
    on-disk lease fixture and asserts identical return values across
    fresh/expired/malformed/missing -- edit both functions, or edit one
    and let the parity test catch the drift. A source-level byte-diff
    would false-positive: this file uses ``import pathlib`` /
    ``pathlib.Path``, ``t2_prefix_scan.py`` uses ``from pathlib import
    Path`` -- behavioral parity is the actual contract, not textual
    identity.
    """
    path = config_dir / f"{_STORAGE_SERVICE_TIER}_addr.{os.getuid()}"
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    try:
        if str(data.get("status", "live")) != "live":
            return None
        heartbeat_epoch = float(data["heartbeat_epoch"])
        ttl = float(data["ttl"])
        endpoint = data["endpoint"]
        host = str(endpoint.get("host", "127.0.0.1"))
        port = int(endpoint.get("port", 0))
        token = str(endpoint.get("token", ""))
    except (KeyError, TypeError, ValueError):
        return None
    if port <= 0 or not token:
        return None
    if (time.time() - heartbeat_epoch) >= ttl:
        return None
    return {"host": host, "port": port, "token": token}


def _read_data_token_lease(config_dir: pathlib.Path, base_url: str) -> str | None:
    """Best-effort read of the client's cached DATA token for *base_url*.

    Ported verbatim (nexus-gjv9b PART 2 CRITICAL review fix) from
    ``t2_prefix_scan.py``'s identically-named function -- same
    format-version check, same digest rule, same fail-safe stance.
    ``tests/test_routing_hooks.py``'s ``test_parity_read_data_token_
    lease_*`` suite runs both against the same on-disk lease fixture
    (fresh match, wrong digest, expired, missing) and asserts identical
    return values.
    """
    import hashlib  # noqa: PLC0415 — stdlib, only needed on this path
    import urllib.parse  # noqa: PLC0415 — stdlib, only needed on this path

    host = urllib.parse.urlsplit(base_url).netloc or base_url
    now = time.time()
    best_token, best_expiry = "", 0.0
    try:
        candidates = sorted(config_dir.glob(f"{_DATA_TOKEN_LEASE_PREFIX}*"))
    except OSError:
        return None
    for path in candidates:
        try:
            data = json.loads(path.read_text())
            if data.get("format_version") != _DATA_TOKEN_LEASE_FORMAT_VERSION:
                continue
            tenant = str(data["tenant"])
            digest = hashlib.sha256(f"{host}\x00{tenant}".encode("utf-8")).hexdigest()
            if data.get("base_url_digest") != digest:
                continue
            token = str(data["token"])
            expires_at = float(data["expires_at"])
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
        if token and expires_at > now and expires_at > best_expiry:
            best_token, best_expiry = token, expires_at
    return best_token or None


def _read_config_yml_credentials(config_dir: pathlib.Path) -> dict:
    """Bounded, stdlib-only extraction of ``service_url``/``service_token``
    from the persisted ``config.yml``.

    Ported verbatim (nexus-gjv9b PART 2 CRITICAL review fix) from
    ``t2_prefix_scan.py``'s identically-named function -- see that
    docstring for the full "why a line-oriented scan, not a YAML parser"
    rationale. Returns ``{}`` when the file is absent, unreadable, or has
    no ``credentials:`` block. ``tests/test_routing_hooks.py``'s
    ``test_parity_read_config_yml_credentials_*`` suite runs both
    against the same on-disk ``config.yml`` fixture and asserts
    identical return values.
    """
    path = config_dir / "config.yml"
    try:
        text = path.read_text()
    except OSError:
        return {}

    result: dict = {}
    in_credentials = False
    cred_indent = 0
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if not in_credentials:
            if stripped == "credentials:":
                in_credentials = True
                cred_indent = indent
            continue
        if indent <= cred_indent:
            break
        for key in ("service_url", "service_token"):
            prefix = f"{key}:"
            if not stripped.startswith(prefix):
                continue
            value = stripped[len(prefix):].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            if value:
                result[key] = value
    return result


def _engine_endpoint() -> "tuple[str, str] | tuple[None, None]":
    """``(base_url, token)`` for the nexus-service engine, or
    ``(None, None)`` when nothing is resolvable. Never raises.

    FULL RESOLUTION (nexus-gjv9b PART 2 CRITICAL review fix, replacing
    an env-only leg that left the routing hooks write-never on any
    normal interactive install -- nothing exports ``NX_SERVICE_HOST``/
    ``PORT``/``TOKEN`` into a Claude Code process by default). Reuses
    ``t2_prefix_scan.py``'s stdlib-only discovery: a fresh
    ``ServiceRegistry`` lease file, then ``config.yml`` credentials, then
    the ``NX_SERVICE_*`` env vars, with a data-token lease (nexus-znvjd)
    preferred over any static token once a base URL is known. Mirrors
    ``nexus.db.service_endpoint.resolve_service_endpoint``'s precedence
    exactly, minus the raise-on-failure (this caller wants a quiet
    ``(None, None)`` to fall through to the metered drop, never an
    exception to handle).

    Precedence, matching ``t2_prefix_scan._resolve_endpoint``:
      1. ``service_url`` -- ``NX_SERVICE_URL`` env, else ``config.yml``'s
         ``service_url``. Token: ``NX_SERVICE_TOKEN`` env, else
         ``config.yml``'s ``service_token``, else the lease's token.
      2. ``NX_SERVICE_HOST``/``PORT`` env (+ ``NX_SERVICE_TOKEN``, else
         the lease's token).
      3. The bare local-supervisor lease alone.
    On every leg, a fresh data-token lease for the resolved host wins
    over whatever static token was found.
    """
    config_dir = _default_config_dir()
    lease = _read_service_lease(config_dir)
    yaml_creds = _read_config_yml_credentials(config_dir)

    url = os.environ.get("NX_SERVICE_URL", "").strip().rstrip("/")
    if not url:
        url = yaml_creds.get("service_url", "").strip().rstrip("/")
    if url:
        data_token = _read_data_token_lease(config_dir, url)
        if data_token:
            return url, data_token
        token = os.environ.get("NX_SERVICE_TOKEN", "").strip()
        if not token:
            token = yaml_creds.get("service_token", "").strip()
        if not token:
            token = lease["token"] if lease else ""
        if not token:
            return None, None
        return url, token

    port_str = os.environ.get("NX_SERVICE_PORT", "").strip()
    if port_str:
        try:
            port = int(port_str)
        except ValueError:
            return None, None
        host = os.environ.get("NX_SERVICE_HOST", "").strip() or "127.0.0.1"
        url = f"http://{host}:{port}"
        data_token = _read_data_token_lease(config_dir, url)
        if data_token:
            return url, data_token
        token = os.environ.get("NX_SERVICE_TOKEN", "").strip() or (
            lease["token"] if lease else ""
        )
        if not token:
            return None, None
        return url, token

    if lease is not None:
        url = f"http://{lease['host']}:{lease['port']}"
        data_token = _read_data_token_lease(config_dir, url)
        if data_token:
            return url, data_token
        return url, lease["token"]

    return None, None


#: Mirrors ``nexus.db.t2.http_telemetry_store.DEFAULT_TENANT`` verbatim --
#: hardcoded, not imported, because this script has no ``nexus`` import.
_DEFAULT_TENANT = "default"

#: Mirrors ``nexus.dropped_writes.NAMED_DROP_CAUSES`` verbatim (nexus-gjv9b
#: review fold-in round 5, code-review non-blocking item 1) -- the FULL
#: shared cause vocabulary every drop-meter producer and reader (this
#: hook, ``_session_end_census.py``'s capability_census producer,
#: ``health._check_t2_dropped_writes``) recognizes as one closed set,
#: not merely the subset :func:`_post_routing_event_http` itself can
#: literally return -- this function alone never produces
#: ``"guard_refused"`` (that is specific to the production-write-guard
#: refusal path, which only ``_session_end_census.py`` can hit), but the
#: VOCABULARY is shared across both producers, so it belongs here too.
#: Hardcoded, not imported (this script has no ``nexus`` import,
#: RDR-121 § Contract), so ``tests/test_routing_hooks.py::
#: test_parity_cause_vocabulary_matches_dropped_writes`` is what keeps
#: this mirror honest -- edit both sets, or edit one and let the parity
#: test catch the drift, exactly like the discovery-function parity
#: suite below. Excludes ``""`` (the 2xx/success return, and
#: :func:`classify_drop_cause`'s "nothing to classify" case) and the
#: dynamic ``f"http_{code}"`` escape valve for a status this function
#: does not recognize by name -- that string is passed to
#: :func:`nexus.dropped_writes.record_drop` as an EXPLICIT ``cause``,
#: bypassing that module's classifier (and this closed vocabulary)
#: entirely.
_STATIC_CAUSE_NAMES = frozenset({
    "guard_refused", "unresolvable", "401", "403", "route_absent",
    "5xx", "timeout", "connect", "other",
})


def _post_routing_event_http(record: dict, *, timeout: float = 0.25) -> str:
    """Best-effort ``POST /v1/telemetry/routing_events/record`` via
    ``urllib`` (no ``httpx``/``requests`` dependency — this script runs
    under the system interpreter, RDR-121 § Contract). Returns ``""`` on
    a 2xx response, else a short CAUSE string classifying the failure —
    never raises.

    Cause vocabulary (nexus-gjv9b review fold-in rounds 3-4, critique
    CRITICAL 1/2, code-review item 1): ``"unresolvable"`` (no
    endpoint/credential at all — :func:`_engine_endpoint` returned
    ``(None, None)``), ``"401"``/``"403"``/``"route_absent"``
    (HTTP 404/405 — the serving engine predates this route entirely, the
    plugin-cut-ahead-of-the-engine case a doctor check must never read
    as a failing service)/``"5xx"``/``"http_<code>"``
    (a non-2xx response, read straight from the raised
    ``urllib.error.HTTPError.code`` — never guessed from response text),
    ``"timeout"`` (a connect/read timeout), ``"connect"`` (any other
    transport-level failure — DNS, connection refused, TLS), ``"other"``
    for anything unrecognized. Classifying HERE, at the transport layer
    that actually knows the failure mode, is strictly more reliable than
    :func:`nexus.dropped_writes.classify_drop_cause`'s text-matching
    fallback (which exists for producers, like
    ``_session_end_census._post_capability_census``, that only have an
    exception's ``str()`` to work with) — this is why the cause travels
    through to :func:`_record_dropped_routing_event` explicitly rather
    than being re-derived from the error string on the far side.
    """
    base_url, token = _engine_endpoint()
    if base_url is None:
        return "unresolvable"
    try:
        import urllib.error  # noqa: PLC0415 — stdlib, only needed on this path
        import urllib.request  # noqa: PLC0415 — stdlib, only needed on this path

        body = json.dumps(record).encode("utf-8")
        req = urllib.request.Request(
            base_url + "/v1/telemetry/routing_events/record",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Nexus-Tenant": _DEFAULT_TENANT,
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — fixed internal engine URL, not user input
            if 200 <= resp.status < 300:
                return ""
            return f"http_{resp.status}"
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return str(exc.code)
        if exc.code in (404, 405):
            # nexus-gjv9b review fold-in round 4: a plugin cut can ship
            # this hook ahead of the paired engine tag -- the SERVING
            # engine simply predates the routing_events route, on every
            # call, until the engine catches up. Version skew, not a
            # failure of anything; _check_t2_dropped_writes treats a
            # route_absent-only window as informational, never a WARN.
            return "route_absent"
        if 500 <= exc.code < 600:
            return "5xx"
        return f"http_{exc.code}"
    except TimeoutError:
        # Raw socket.timeout (an alias of TimeoutError since Python 3.10),
        # raised directly by urlopen on a connect-phase timeout.
        return "timeout"
    except urllib.error.URLError as exc:
        # A read-phase (post-connect) timeout arrives wrapped here, with
        # .reason carrying the underlying TimeoutError -- everything else
        # (connection refused, DNS failure, TLS) is a genuine "connect".
        if isinstance(exc.reason, TimeoutError):
            return "timeout"
        return "connect"
    except Exception:  # noqa: BLE001 — boundary: anything else recognized as failed but not classifiable further
        return "other"


def _record_dropped_routing_event(
    error: str, *, cause: str = "", event: dict | None = None,
) -> None:
    """Metered-drop fallback (nexus-gjv9b PART 2 design decision): a
    routing event that could not reach the engine is counted, not
    silently discarded and not appended to ``routing_log.jsonl`` either
    (that JSONL machinery stays in place for PART 3's deferred deletion
    only -- see this module's docstring). Hand-replicates
    ``nexus.dropped_writes.record_drop``'s exact on-disk record shape,
    ``cause`` field included (never imported -- no ``nexus`` dependency
    here) so ``nx doctor``'s existing drop-meter aggregation (including
    its dominant-cause tally) picks these up with no changes of its own.

    *event* (nexus-gjv9b review fold-in round 6, found via a full-suite
    red on ``test_routing_subagent_git_write.py::TestAllow::
    test_escape_token_allows_and_logs`): the ORIGINAL ``rule``/
    ``outcome``/``escape_reason`` fields from :func:`log_routing_event`'s
    own record, when given. The canonical ``nexus.dropped_writes.
    record_drop`` shape has no room for them (shared across every
    producer, capability_census included), but THIS hook's own
    independent on-disk record is schemaless JSON -- adding them here
    costs nothing to any reader (``count_drops`` only ever ``.get()``s
    the fields it knows about) and closes a real audit-fidelity gap the
    original PART 2 writer swap introduced silently: an escape-token
    fire (nexus-mzvwa.9's over-use-visibility concern) that hits an
    engine-down window used to still land in ``routing_log.jsonl`` with
    its ``rule``/``outcome``/``escape_reason`` intact; the drop-meter
    record before this fix carried only a generic ``error``/``cause``,
    losing exactly the fields an audit review needs.
    """
    try:
        override = os.environ.get("NX_DROPPED_WRITES_LOG_PATH", "").strip()
        if override:
            path = pathlib.Path(override)
        else:
            cfg_override = os.environ.get("NEXUS_CONFIG_DIR", "").strip()
            base = pathlib.Path(cfg_override) if cfg_override else pathlib.Path.home() / ".config" / "nexus"
            path = base / "dropped_writes.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "hook": "routing_events",
            "collection": "",
            "rows": 1,
            "error": str(error)[:200],
            "cause": str(cause)[:32],
        }
        if event:
            if event.get("rule"):
                record["rule"] = str(event["rule"])[:200]
            if event.get("outcome"):
                record["outcome"] = str(event["outcome"])[:64]
            if event.get("escape_reason"):
                record["escape_reason"] = str(event["escape_reason"])[:300]
        line = json.dumps(record, separators=(",", ":")) + "\n"
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:
        # The meter is itself best-effort; must never raise.
        pass


def log_routing_event(
    rule: str,
    outcome: str,
    *,
    tool_name: str = "",
    command_fragment: str = "",
    escape_reason: str = "",
    session_id: str = "",
) -> None:
    """Record one routing-hook event (nexus-gjv9b PART 2 writer swap: the
    engine's ``routing_events`` table, replacing the former JSONL append).
    Never raises; the hook's own exit code NEVER depends on this call.

    Best-effort, fire-and-forget: POSTs to the engine with a SHORT
    (~250ms) timeout via :func:`_post_routing_event_http`; on ANY
    failure (unresolvable endpoint -- no live supervisor lease, no
    config.yml credentials, no NX_SERVICE_* env, per
    :func:`_engine_endpoint` -- timeout, non-2xx), degrades to
    :func:`_record_dropped_routing_event`
    rather than a JSONL fallback (same design decision as
    ``nexus._session_end_census.write_session_capability_census`` for
    PART 1 — the routing hooks' own timeout budget has no room for a
    filesystem retry story either). The JSONL append and rotation
    machinery was deleted at PART 3 (2026-09-05).
    """
    try:
        record: dict = {
            "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "rule": rule,
            "outcome": outcome,
        }
        if session_id:
            record["session_id"] = session_id
        if tool_name:
            record["tool_name"] = tool_name
        if command_fragment:
            # Cap fragment length so the wire payload stays small.
            record["command_fragment"] = command_fragment[:200]
        if escape_reason:
            # Dedicated field (nexus-mzvwa.9): the reason trails the command,
            # so the fragment cap above routinely truncated it away.
            record["escape_reason"] = escape_reason[:300]
        cause = _post_routing_event_http(record)
        if cause:
            _record_dropped_routing_event(
                f"routing_events POST failed: {cause}", cause=cause, event=record,
            )
    except Exception:
        # Telemetry must never crash a hook. Swallow.
        pass


# ---------------------------------------------------------------------------
# Top-level runner — wraps every hook entry point
# ---------------------------------------------------------------------------


def run_hook(
    body: Callable[[dict[str, Any]], None],
    *,
    fail_closed: bool = False,
    rule_name: str = "",
) -> None:
    """Execute ``body(payload)`` under the fail-open / fail-closed contract.

    ``body`` is responsible for calling ``allow()`` / ``deny()`` /
    ``warn()`` itself; those calls ``sys.exit(0)``. If ``body`` returns
    normally without emitting an envelope, we fall through to a default
    allow. If ``body`` raises ``SystemExit`` (from our own emitters), we
    re-raise — that is the normal path. Any other exception triggers
    the fail-open / fail-closed branch.
    """
    raw = ""
    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""

    payload = parse_stdin(raw)

    try:
        body(payload)
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001
        if fail_closed:
            log_routing_event(
                rule=rule_name or "unknown",
                outcome="deny_fail_closed",
                tool_name=payload.get("tool_name", "") or "",
                session_id=payload.get("session_id", "") or "",
            )
            deny(f"cannot verify, fail-closed: {exc}")
        else:
            log_routing_event(
                rule=rule_name or "unknown",
                outcome="allow_fail_open",
                tool_name=payload.get("tool_name", "") or "",
                session_id=payload.get("session_id", "") or "",
            )
            allow()

    # Body returned without emitting — default allow.
    allow()
