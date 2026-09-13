#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-205 Phase 2 Step 3 (bead nexus-em75s.11): the async projection body
for the two ``ledger/<session_id>`` tuple writes.

Invoked ONLY from inside ``subagent-start-tuple-async.sh`` /
``subagent-stop-tuple-async.sh`` — never directly from ``hooks.json`` (so
it is not subject to the Python-hook ``_run_python_hook.sh`` routing rule,
which governs only commands ``hooks.json`` invokes itself; see
``subagent-stop-scan.py`` for the identical precedent of a bash hook
shelling to a stdlib sibling ``.py``). Those two wrapper scripts are the
``async: true`` entries beside ``subagent-start.sh``/``subagent-start-
stamp.sh`` and ``subagent-stop.sh`` in ``hooks.json`` — this module does
the actual work after the wrapper has already detached and returned.

NO HOOK MINTS ANYTHING (RDR-205 "Identity and addressing"). This module
reads the client library's own cross-process caches --
``storage_service_addr.<uid>`` (host/port, written by
``nexus.daemon.service_registry.ServiceRegistry``) and
``data_token_lease.<digest>`` (the bearer, written by
``nexus.db.data_token.DataTokenManager._write_lease``) -- and PRESENTS
what it finds. It never calls ``/v1/data-tokens/mint``, and it never puts
a bearer on a spawned process's argv.

BEARER PRECEDENCE (nexus-g2lln): a fresh data-token lease is always tried
first. On a MANAGED endpoint (``service_url`` resolved from env or
``config.yml`` -- ``_resolve_base_url``'s first two legs) that is the
ONLY accepted credential: a missing/near-expiry data-token lease is a
SKIP, never a fallback (a wrong-scoped static token would 401 silently on
a fire-and-forget write with no reader). On a LOCAL SUPERVISOR endpoint
-- one this script resolved by literally reading the
``storage_service_addr.<uid>`` lease file for host/port, the last leg of
``_resolve_base_url`` -- a missing/near-expiry data-token lease falls
back to that SAME lease record's own ``endpoint.token`` field, exactly
the credential the real local ``nx``/MCP client itself presents on this
box when no ``mint_token`` is configured (``nexus.db.data_token``'s
documented "falls through to its existing static-``service_token``
resolution unchanged" contract; a default local install has no
``mint_token``, so this is the ONLY credential such an install ever
produces -- proven dead without this fallback, T2
``nexus/shakeout-7.41.0-projector-local-install-proof-2026-09-11``).
That fallback token is refused (SKIP, reason named) if the lease file is
not owner-only (group/other read/write/execute bits set) -- this project
never trusts a same-box bearer off a file another local user could read.

Endpoint/credential resolution (the managed-cloud ``service_url`` leg --
env, then the persisted ``config.yml`` credential -- AND the
local-supervisor discovery leg, not local-only; nexus-0zsmg: a cloud-mode
box with no ``NX_SERVICE_URL`` exported and no local supervisor skipped
every projection, since the pre-fix mirror covered only the local leg)
now lives in the shared sibling module ``_endpoint_resolve.py``
(nexus-aginu) -- this script, that module, and every other hook helper
under this directory stay stdlib-only, no ``nexus`` import (bare
``python3``, nexus-vg6d4).

Wire shape: mirrors ``nexus.db.t2.http_tuple_store.HttpTupleStore.out``
POSTing to ``/v1/tuples/out`` -- ``{"subspace": ..., "keys": {...},
"dims": {...}}`` -- against the ``ledger/<session_id>`` template
(``service/src/main/resources/tuples/templates/ledger.yaml``): keys
``agent_id`` (unconstrained) and ``kind`` (pinned to
``{start, report}``), dimension ``agent_type``. ``id_from: keys`` means
the tuple id is derived from ``(agent_id, kind)`` alone, so a retried
``out`` for the same pair lands on the same row -- idempotent by
construction, no de-dup needed here.

CHECKABLE-REPORT DIMS (bead nexus-cnzei.6 item 2, engine half nexus-d9k5h).
A ``kind=="report"`` write additionally parses the stopping agent's own
final hand-back for ``VERIFY:`` lines (the convention in
``conexus/skills/orchestration/SKILL.md`` "VERIFY Line Convention") and
fills three optional dims the template declares: ``commit`` (a short or
full sha, from a ``VERIFY: commit=<sha>`` line), ``t2_ref`` (a
``project/title`` T2 pointer, from ``VERIFY: t2=<project>/<title>``), and
``verify`` (``present`` when at least one ``VERIFY:`` line was found,
``absent`` otherwise -- always set for a report, never omitted, so an
agent that sent no checkable claims at all is itself a visible finding,
not a blank cell). ``kind=="start"`` never carries these: it fires before
the agent has produced any output to parse. See ``_extract_verify_dims``.

HARD REQUIREMENT (nexus-cnzei.6 item 2): the cloud engine can be older
than ``engine-service-v0.1.118`` (the first tag whose ``ledger.yaml``
declares these three dims -- nexus-d9k5h). Such an engine answers an
``out`` naming an undeclared dim with HTTP 400 (``SchemaViolation``) --
whose JSON body is ``{"error": "SchemaViolation", "detail": "field
'<name>': not a declared dimension for this template"}``
(``SchemaViolationException``/``TupleRepository.validateOutShape``).
``main()`` reads that body (fix round 1, CRE finding 2: a bare ``status
== 400`` is not proof of an undeclared-dim refusal specifically -- other
schema violations, e.g. a bad ``ttl_seconds``, are 400 too) and treats it
as the below-floor case ONLY when the body matches that exact shape
(``_is_undeclared_dim_violation``). On a match, it retries once with the
legacy dims-only body (``agent_type`` alone) -- the row is written either
way, never dropped. A 400 on a body that already carries no extra dims
(the ``kind=="start"`` case, or a genuine unrelated schema problem) has
nothing left to strip and is re-raised as a plain skip; a 400 that is NOT
an undeclared-dim refusal is likewise a plain skip, never retried.

Every failure path -- unresolvable endpoint, no fresh data-token lease,
transport failure, a non-2xx response -- appends one line to a
log file beside the session's expectations ledger
(``<state_dir>/<session_id>.tuple-projection.log``) and exits 0. This
script's stdout, stderr and exit code are never read by anyone (the
wrapper backgrounds it with all three fds already redirected away) --
the log file is the only diagnostic surface, and every code path reaches
it or exits silently on a session_id too malformed to build a safe path
from.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

if sys.version_info < (3, 12):
    sys.stderr.write(
        f"ERROR: conexus plugin hook requires Python 3.12+, got {sys.version.split()[0]}\n"
        f"  Resolved: {sys.executable}\n"
        f"  Install: brew install python@3.13 (macOS) | apt install python3.12 (Ubuntu) | uv python install 3.12\n"
    )
    sys.exit(1)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _endpoint_resolve as _ep  # noqa: E402 -- must follow the sys.path insert
import _tuple_size_limits as _sz  # noqa: E402 -- must follow the sys.path insert

#: Bound on the whole POST round trip -- research 5 measured ~10ms for a
#: healthy engine; this is a ceiling for a degraded/rate-limiting one, not
#: a target. The wrapper has already detached, so this bound only keeps a
#: hung engine from leaving an orphaned connection open forever. Enforced
#: as a bound on the WHOLE call (nexus-em75s.42: urlopen's own `timeout`
#: is a PER-SOCKET-OP timeout, reset by every individual connect/recv --
#: a server that trickles bytes could otherwise keep the call alive far
#: past this many seconds), not just passed to urlopen.
_POST_TIMEOUT_S = 5

_ROUTE = "/v1/tuples/out"

#: The tenant this script's writes are scoped to. Matches
#: ``nexus.db.t2.http_tuple_store``'s (and every sibling ``Http*Store``'s)
#: ``DEFAULT_TENANT`` -- ``HttpTupleStore`` is never constructed with a
#: non-default tenant anywhere this hook's writes correspond to, so a
#: data-token lease minted for any OTHER tenant must never be presented
#: here even when it is the freshest lease on disk for the same host
#: (nexus-em75s.12 review fix: the lease-selection loop used to pick
#: purely on host-digest + freshest-expiry, so a second tenant's lease
#: for the same host could win and be sent as this write's bearer).
_RESOLVED_TENANT = _ep.DEFAULT_TENANT

#: Same 20% refresh/"near-expiry" threshold as
#: ``nexus.db.data_token._REFRESH_THRESHOLD`` -- a lease this close to
#: expiring is not worth presenting on a fire-and-forget write with no
#: retry and no reader of the response.
_NEAR_EXPIRY_THRESHOLD = 0.20

_SESSION_ID_RE_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
)


class _Skip(Exception):
    """Any resolution/transport failure -- caught once in main(), logged,
    and the script exits 0. Never propagates as a traceback (nothing
    reads stderr anyway, but a clean exit keeps the intent explicit)."""


class _SchemaViolation(_Skip):
    """The engine rejected an ``out`` with HTTP 400. For this script's
    only variable-shaped payload -- the report dims added by
    nexus-cnzei.6 -- this means an engine older than
    engine-service-v0.1.118 (nexus-d9k5h) does not yet declare
    commit/t2_ref/verify as dimensions of the ledger template. Caught
    exactly once, in main()'s report path, to retry with the legacy
    dims-only body; every other raise site (a start-kind 400, or a 400
    on a retry that already carries no extra dims) stays a plain
    ``_Skip``."""


def _default_state_dir() -> Path:
    """Mirrors ``expectations.sh``'s ``_expectations_dir``:
    ``${XDG_STATE_HOME:-$HOME/.local/state}/nexus/orchestration`` -- the
    log file lives beside the session's ``.expectations`` ledger there."""
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "nexus" / "orchestration"


def _valid_session_id(session_id: str) -> bool:
    """Mirrors ``expectations_file``'s path-safe charset guard: a
    traversal-bearing or otherwise unsafe session_id must never be used to
    build a filesystem path."""
    if not session_id or len(session_id) > 128:
        return False
    if not (session_id[0].isalnum()):
        return False
    return all(c in _SESSION_ID_RE_CHARS for c in session_id)


def _log_path(session_id: str) -> Path:
    return _default_state_dir() / f"{session_id}.tuple-projection.log"


def _log_skip(session_id: str, reason: str) -> None:
    """Best-effort append; a failure to even log is not this script's
    problem to escalate -- there is no reader waiting on it either way."""
    if not _valid_session_id(session_id):
        return
    try:
        path = _log_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{ts}\t{reason}\n")
    except OSError:
        pass


# ── Endpoint + credential resolution (delegates to _endpoint_resolve) ──────


def _resolve_endpoint_and_token(config_dir: Path) -> tuple[str, str, bool]:
    """Thin wrapper: nexus-g2lln's policy, via the shared sibling module
    (nexus-aginu). Translates :class:`_ep.EndpointUnresolvable` to this
    script's own :class:`_Skip` so ``main()``'s single catch site is
    unchanged."""
    try:
        return _ep.resolve_endpoint_and_token(
            config_dir, tenant=_RESOLVED_TENANT, near_expiry_threshold=_NEAR_EXPIRY_THRESHOLD,
        )
    except _ep.EndpointUnresolvable as exc:
        raise _Skip(str(exc)) from exc


# ── Payload + POST ───────────────────────────────────────────────────────


def _extract_fields(raw_payload: str) -> tuple[str, str, str, str]:
    try:
        data = json.loads(raw_payload)
    except (json.JSONDecodeError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    session_id = str(data.get("session_id") or "")
    agent_id = str(data.get("agent_id") or "")
    agent_type = str(data.get("agent_type") or "")
    # SubagentStop's own field name (subagent-stop.sh reads the identical
    # key as TRANSCRIPT) -- SubagentStart payloads carry no such field, so
    # this is always "" for kind=="start".
    transcript_path = str(data.get("agent_transcript_path") or "")
    return session_id, agent_id, agent_type, transcript_path


# ── VERIFY-line extraction (bead nexus-cnzei.6 item 2) ──────────────────────

#: One VERIFY claim per line, in the convention
#: conexus/skills/orchestration/SKILL.md "VERIFY Line Convention" defines:
#: ``VERIFY: <claim>``. Leading whitespace tolerated (a claim inside a
#: bullet or a fenced block); trailing whitespace stripped.
_VERIFY_LINE_RE = re.compile(r"(?im)^[ \t]*VERIFY:[ \t]*(.+?)[ \t]*$")

#: ``VERIFY: commit=<sha>`` -- a short (7+) or full (40) hex sha. Key
#: case-insensitive (fix round 1, CRE finding 3): ``Commit=``/``COMMIT=``
#: must not silently produce ``verify=present`` with no ``commit`` dim,
#: indistinguishable from a deliberate "nothing to check" report.
_COMMIT_VALUE_RE = re.compile(r"^commit=([0-9a-fA-F]{7,40})$", re.IGNORECASE)

#: ``VERIFY: t2=<project>/<title>`` -- a T2 pointer, kept verbatim.
#: Key case-insensitive, same reasoning as ``_COMMIT_VALUE_RE``.
_T2_REF_VALUE_RE = re.compile(r"^t2=(\S.*)$", re.IGNORECASE)


def _content_blocks(entry: dict[str, Any]) -> list[dict[str, Any]]:
    msg = entry.get("message")
    if not isinstance(msg, dict):
        return []
    content = msg.get("content")
    return content if isinstance(content, list) else []


def _final_assistant_text(path: Path) -> str:
    """Text content of the LAST assistant-role transcript entry, its
    ``"text"`` content blocks concatenated. For a synchronous dispatch
    this IS the agent's hand-back the caller reads; a background
    teammate's real report instead lives inside a SendMessage tool_use
    (see :func:`_last_send_message_text`) -- there is no reliable
    transcript-format signal for which dispatch shape produced a given
    transcript (subagent-stop.sh's own comments document the identical
    ambiguity for its report check), so both are searched.
    """
    last_text = ""
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") != "assistant":
                continue
            parts = [
                block["text"]
                for block in _content_blocks(entry)
                if isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            ]
            if parts:
                last_text = "\n".join(parts)
    return last_text


def _last_send_message_text(path: Path) -> str:
    """Text of the LAST assistant SendMessage tool_use's ``"content"``
    input field anywhere in the transcript -- the field name a real
    SendMessage tool_use call carries (``{"to": ..., "content": ...}``,
    matching every other transcript-scanning hook in this directory,
    e.g. subagent-stop-scan.py's own FOUND detection, which this mirrors
    but returns the payload text from instead of a boolean)."""
    last = ""
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or '"SendMessage"' not in line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") != "assistant":
                continue
            for block in _content_blocks(entry):
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_use"
                    and block.get("name") == "SendMessage"
                ):
                    tool_input = block.get("input")
                    if isinstance(tool_input, dict) and isinstance(tool_input.get("content"), str):
                        last = tool_input["content"]
    return last


def _extract_verify_dims(transcript_path: str) -> dict[str, str]:
    """Parse ``VERIFY:`` lines out of the stopping agent's final report
    and return the ledger dims they carry. Never raises: a missing,
    unreadable, or unparseable transcript, or one with no VERIFY lines
    at all, reads as ``{"verify": "absent"}`` -- consistent with
    subagent-stop.sh's fail-open posture toward a transcript it cannot
    use as evidence. ``verify`` is always present in the return value;
    ``commit``/``t2_ref`` are present only when a matching line was
    found (first match wins for each)."""
    if not transcript_path:
        return {"verify": "absent"}
    path = Path(transcript_path)
    try:
        if not path.is_file():
            return {"verify": "absent"}
        combined = "\n".join(
            t for t in (_final_assistant_text(path), _last_send_message_text(path)) if t
        )
    except OSError:
        return {"verify": "absent"}

    claims = _VERIFY_LINE_RE.findall(combined)
    if not claims:
        return {"verify": "absent"}

    dims: dict[str, str] = {"verify": "present"}
    for claim in claims:
        commit_match = _COMMIT_VALUE_RE.match(claim)
        if commit_match and "commit" not in dims:
            dims["commit"] = commit_match.group(1)
            continue
        t2_match = _T2_REF_VALUE_RE.match(claim)
        if t2_match and "t2_ref" not in dims:
            dims["t2_ref"] = t2_match.group(1)
    return dims


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuses to follow any 3xx (nexus-em75s.42 review finding): the
    engine URL is fixed and internal, so a redirect response is never a
    legitimate "moved" answer -- following one would resend the
    Authorization header to whatever host the redirect names. The 3xx
    itself still reaches the caller as ``exc.code`` via the normal
    ``HTTPError`` path (``_do_post`` below), so it is logged as
    ``engine returned HTTP 3xx``, never silently swallowed.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201, N802
        raise urllib.error.HTTPError(newurl, code, "redirect refused", headers, fp)


def _build_opener(is_local_supervisor: bool) -> urllib.request.OpenerDirector:
    """A fresh no-redirect opener, no-proxy ONLY for a LOCAL supervisor
    endpoint (fix round on nexus-em75s.42's review finding): an explicit
    empty :class:`~urllib.request.ProxyHandler` overrides
    ``build_opener``'s default of reading ``http_proxy``/``https_proxy``
    from the environment -- correct for ``base_url``'s
    ``127.0.0.1``/lease-host leg, which is a fixed loopback address an
    ambient proxy setting could never legitimately need to route to, but
    WRONG for the managed-cloud ``NX_SERVICE_URL``/``service_url`` leg --
    a genuine internet destination a corporate-proxied box may need
    proxied to reach at all. ``t2_prefix_scan.py`` and ``routing/_lib.py``
    hit that same managed endpoint via a bare ``urlopen`` with no explicit
    opener, so they already honour the ambient proxy env there; omitting
    the empty :class:`~urllib.request.ProxyHandler` override here (letting
    ``build_opener``'s own default ``ProxyHandler`` -- which reads
    ``http_proxy``/``https_proxy`` -- apply) matches that behavior for the
    non-local leg instead of silently and permanently breaking ledger
    writes on a proxied cloud-mode box while the sibling hooks keep
    working."""
    handlers: list[urllib.request.BaseHandler] = [_NoRedirectHandler()]
    if is_local_supervisor:
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


#: The exact ``detail`` shape ``SchemaViolationException``/
#: ``TupleRepository.validateOutShape`` produce for an UNKNOWN dimension
#: name specifically (as opposed to any other schema violation -- a bad
#: ``ttl_seconds``, a missing required key, an out-of-set value -- every
#: one of which is also a 400 but must never trigger the legacy-dims
#: retry, fix round 1, CRE finding 2). Java source:
#: ``throw new SchemaViolationException(unknownDims.first(), "not a
#: declared dimension for this template")`` -- rendered by
#: ``TupleHandler`` as ``{"error": "SchemaViolation", "detail": "field
#: '<name>': <reason>"}``.
_UNDECLARED_DIM_DETAIL_RE = re.compile(r"^field '.+': not a declared dimension for this template$")


def _is_undeclared_dim_violation(body: bytes) -> bool:
    """True iff *body* is the engine's JSON refusal of an undeclared
    dimension name -- never true for any other 400 shape (a non-JSON
    body, a different ``error`` code, or a SchemaViolation for a
    different reason, e.g. an out-of-set dimension VALUE, a missing
    required key, or a bad ``ttl_seconds``)."""
    try:
        data = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return False
    if not isinstance(data, dict) or data.get("error") != "SchemaViolation":
        return False
    detail = data.get("detail")
    return isinstance(detail, str) and _UNDECLARED_DIM_DETAIL_RE.match(detail) is not None


def _post_via_urllib(
    base_url: str, token: str, body: dict[str, Any], *, is_local_supervisor: bool
) -> None:
    """POST *body* to ``{base_url}/v1/tuples/out`` via stdlib
    ``urllib.request`` -- never via a subprocess argv (nexus-em75s.12
    review fix: the prior ``curl -H "Authorization: Bearer <token>"``
    invocation put the bearer in that process's argv, readable by any
    co-resident user via ``ps``/``/proc`` for the life of the call).
    Same repo precedent as ``routing/_lib.py``'s routing-event POST and
    ``t2_prefix_scan.py``'s ``_http_get_json``. Raises :class:`_Skip`
    naming the failure; never raises anything else.

    Never follows a redirect; honours an ambient proxy env var except on
    a LOCAL supervisor endpoint (:func:`_build_opener`, nexus-em75s.42
    fix round). Bounds the WHOLE call to
    ``_POST_TIMEOUT_S`` wall-clock time, not just each individual socket
    operation (nexus-em75s.42: ``urlopen``'s own ``timeout`` resets on
    every connect/recv, so a server that trickles bytes could otherwise
    keep the call alive indefinitely) -- the request runs on a daemon
    thread and the caller joins it with a deadline; a thread still alive
    past the deadline is treated as a transport failure and abandoned
    (daemon=True means it can never block process exit).
    """
    payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
    url = f"{base_url}{_ROUTE}"
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    outcome: dict[str, Any] = {}

    def _do_post() -> None:
        try:
            opener = _build_opener(is_local_supervisor)
            with opener.open(req, timeout=_POST_TIMEOUT_S) as resp:  # noqa: S310 — fixed internal engine URL, not user input
                outcome["status"] = resp.status
        except urllib.error.HTTPError as exc:
            outcome["status"] = exc.code
            try:
                outcome["body"] = exc.read()
            except OSError:
                outcome["body"] = b""
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=_do_post, daemon=True)
    thread.start()
    thread.join(timeout=_POST_TIMEOUT_S)
    if thread.is_alive():
        raise _Skip(
            f"transport failure posting to {url}: exceeded the {_POST_TIMEOUT_S}s "
            "whole-call deadline"
        )
    if "error" in outcome:
        raise _Skip(f"transport failure posting to {url}: {outcome['error']}") from outcome["error"]
    status = outcome.get("status")
    if status is None:
        raise _Skip(f"transport failure posting to {url}: no response received")
    if status == 400:
        # nexus-cnzei6-fix1: NOT named `body` -- that name is already this
        # function's own `body: dict[str, Any]` REQUEST payload parameter,
        # and shadowing it with the response's raw bytes here is exactly
        # the kind of same-name/different-type reuse that reads fine at a
        # glance and fails a type checker (and a future reader) outright.
        error_body: bytes = outcome.get("body") or b""
        if _is_undeclared_dim_violation(error_body):
            raise _SchemaViolation(f"engine returned HTTP 400 (undeclared dim) posting to {url}")
        raise _Skip(f"engine returned HTTP 400 posting to {url}: {error_body[:200]!r}")
    if not (200 <= status < 300):
        raise _Skip(f"engine returned HTTP {status} posting to {url}")


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in ("start", "report"):
        # Malformed invocation -- nothing to log a session_id against yet.
        return 0
    kind = argv[1]
    raw_payload = sys.stdin.read()
    session_id, agent_id, agent_type, transcript_path = _extract_fields(raw_payload)

    # kind=="report" WITH NO agent_id AT ALL (nexus-aginu): the harness
    # fires SubagentStop for stops this ledger has no tracked agent for
    # -- measured live on this box at ~250 occurrences per session, every
    # one with a present, valid session_id (confirmed: _log_skip can only
    # write when _valid_session_id() passes, and every one of these DID
    # write, so session_id was never the missing field). Nothing was ever
    # lost by this: the real agent's own report, when one exists, is
    # keyed on ITS OWN agent_id and lands as its own separate invocation.
    # Logging one identical, non-actionable line per untracked stop is
    # pure noise at that volume, so this case projects NOTHING, silently
    # -- no _log_skip call, unlike every other incomplete-payload case
    # below, which keeps its diagnostic line.
    if kind == "report" and not agent_id:
        return 0

    # kind=="report" otherwise tolerates a missing agent_type (nexus-0zsmg):
    # the harness's SubagentStop payload does not reliably carry it the
    # way SubagentStart's does (SubagentStart's agent_type is the
    # dispatch's own subagent_type, injected verbatim -- see
    # agent-dispatch-expect.sh's header), and the ledger.yaml template's
    # agent_type dimension is declared WITHOUT `required: true`
    # (service/src/main/resources/tuples/templates/ledger.yaml), so the
    # engine accepts a blank dimension value (TupleRepository.out only
    # rejects a blank REQUIRED dimension). session_id + agent_id stay
    # mandatory for both kinds -- they key the tuple id and the log path.
    required_fields = (
        (session_id, agent_id) if kind == "report" else (session_id, agent_id, agent_type)
    )
    if not all(required_fields):
        _log_skip(session_id, f"SKIP kind={kind} incomplete payload fields")
        return 0

    # Size pre-check (bead nexus-r7xao): mirrors the engine's own per-field
    # caps. This projection sends no "body" field at all (see the module
    # docstring's "Wire shape" note), so only subspace/keys/dims can ever be
    # oversized; a harness-supplied agent_id or subagent_type this long
    # would be pathological, but the check costs nothing and keeps this
    # writer honest with the engine's own limits rather than finding out via
    # a 413 in the log file.
    subspace = f"ledger/{session_id}"
    size_reason = (
        _sz.check_field_size("subspace", subspace, _sz.MAX_SUBSPACE_BYTES)
        or _sz.check_field_size("keys.agent_id", agent_id, _sz.MAX_FIELD_VALUE_BYTES)
        or _sz.check_field_size("keys.kind", kind, _sz.MAX_FIELD_VALUE_BYTES)
        or _sz.check_field_size("dims.agent_type", agent_type, _sz.MAX_FIELD_VALUE_BYTES)
    )
    if size_reason is not None:
        _log_skip(session_id, f"SKIP kind={kind} agent_id={agent_id} oversized: {size_reason}")
        return 0

    # Checkable-report dims (bead nexus-cnzei.6 item 2). Only kind=="report"
    # has a VERIFY-bearing hand-back to parse -- kind=="start" fires before
    # the agent has produced any output at all.
    dims: dict[str, str] = {"agent_type": agent_type}
    if kind == "report":
        for name, value in _extract_verify_dims(transcript_path).items():
            oversize = _sz.check_field_size(f"dims.{name}", value, _sz.MAX_FIELD_VALUE_BYTES)
            if oversize is not None:
                _log_skip(session_id, f"SKIP dims.{name} oversized, dropping: {oversize}")
                continue
            dims[name] = value

    config_dir = _ep.default_config_dir()
    try:
        base_url, token, is_local_supervisor = _resolve_endpoint_and_token(config_dir)
        body = {
            "subspace": subspace,
            "keys": {"agent_id": agent_id, "kind": kind},
            "dims": dims,
        }
        try:
            _post_via_urllib(base_url, token, body, is_local_supervisor=is_local_supervisor)
        except _SchemaViolation:
            # Below-floor engine (older than engine-service-v0.1.118): it
            # does not declare commit/t2_ref/verify yet. Retry once with
            # the legacy dims-only body so the row is written -- never
            # dropped (nexus-cnzei.6 item 2 HARD REQUIREMENT). Nothing left
            # to strip when dims is already just agent_type (kind=="start",
            # or a report with no extra dims) -- re-raise unchanged there.
            if len(dims) <= 1:
                raise
            _log_skip(
                session_id,
                f"SCHEMA_FALLBACK kind={kind} agent_id={agent_id} "
                "engine refused new dims, retrying with legacy dims only",
            )
            fallback_body = {
                "subspace": subspace,
                "keys": {"agent_id": agent_id, "kind": kind},
                "dims": {"agent_type": agent_type},
            }
            _post_via_urllib(base_url, token, fallback_body, is_local_supervisor=is_local_supervisor)
    except _Skip as exc:
        _log_skip(session_id, f"SKIP kind={kind} agent_id={agent_id} {exc}")
        return 0
    except Exception as exc:  # noqa: BLE001 — best-effort projection must never propagate a traceback
        _log_skip(session_id, f"SKIP kind={kind} agent_id={agent_id} unexpected: {exc}")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
