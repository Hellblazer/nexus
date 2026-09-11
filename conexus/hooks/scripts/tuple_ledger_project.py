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
import sys
import time
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

#: Bound on the whole POST round trip -- research 5 measured ~10ms for a
#: healthy engine; this is a ceiling for a degraded/rate-limiting one, not
#: a target. The wrapper has already detached, so this bound only keeps a
#: hung engine from leaving an orphaned connection open forever.
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


def _extract_fields(raw_payload: str) -> tuple[str, str, str]:
    try:
        data = json.loads(raw_payload)
    except (json.JSONDecodeError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    session_id = str(data.get("session_id") or "")
    agent_id = str(data.get("agent_id") or "")
    agent_type = str(data.get("agent_type") or "")
    return session_id, agent_id, agent_type


def _post_via_urllib(base_url: str, token: str, body: dict[str, Any]) -> None:
    """POST *body* to ``{base_url}/v1/tuples/out`` via stdlib
    ``urllib.request`` -- never via a subprocess argv (nexus-em75s.12
    review fix: the prior ``curl -H "Authorization: Bearer <token>"``
    invocation put the bearer in that process's argv, readable by any
    co-resident user via ``ps``/``/proc`` for the life of the call).
    Same repo precedent as ``routing/_lib.py``'s routing-event POST and
    ``t2_prefix_scan.py``'s ``_http_get_json``. Raises :class:`_Skip`
    naming the failure; never raises anything else.
    """
    import urllib.error  # noqa: PLC0415 — stdlib, only needed on this path
    import urllib.request  # noqa: PLC0415 — stdlib, only needed on this path

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
    try:
        with urllib.request.urlopen(req, timeout=_POST_TIMEOUT_S) as resp:  # noqa: S310 — fixed internal engine URL, not user input
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise _Skip(f"transport failure posting to {url}: {exc}") from exc
    if not (200 <= status < 300):
        raise _Skip(f"engine returned HTTP {status} posting to {url}")


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in ("start", "report"):
        # Malformed invocation -- nothing to log a session_id against yet.
        return 0
    kind = argv[1]
    raw_payload = sys.stdin.read()
    session_id, agent_id, agent_type = _extract_fields(raw_payload)

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

    config_dir = _ep.default_config_dir()
    try:
        base_url, token, _is_local_supervisor = _resolve_endpoint_and_token(config_dir)
        body = {
            "subspace": f"ledger/{session_id}",
            "keys": {"agent_id": agent_id, "kind": kind},
            "dims": {"agent_type": agent_type},
        }
        _post_via_urllib(base_url, token, body)
    except _Skip as exc:
        _log_skip(session_id, f"SKIP kind={kind} agent_id={agent_id} {exc}")
        return 0
    except Exception as exc:  # noqa: BLE001 — best-effort projection must never propagate a traceback
        _log_skip(session_id, f"SKIP kind={kind} agent_id={agent_id} unexpected: {exc}")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
