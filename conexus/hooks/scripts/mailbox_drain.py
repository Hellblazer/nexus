#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""UserPromptSubmit hook: drain this session's RDR-205 mailboxes and inject
what it finds (bead nexus-6konb.7, MM-2.2; design bead nexus-73vnw).

THE CONSUMER OF RECORD. Epic nexus-6konb delivers mailbox push in two
disjoint halves and this is the deterministic one. ``nx tuple watch``
(Phase 1) PINGS: it probes with a zero-timeout ``rd``, emits one line
naming the address, sender and tuple id, carries no body, and NEVER
claims. This hook CLAIMS, ACKS and RENDERS. They are never two renderers
of one row -- the watcher tells a session that mail exists, this hook is
what delivers and consumes it.

That split is what makes a lost ping harmless for live mail: the row sits
in the mailbox for its full retention window and this hook drains it at
the receiver's next prompt whether or not any watcher was ever armed.
Arming a Monitor is a request a model can decline or forget; this hook
fires on every prompt. So the watcher buys LATENCY and this hook is the
FLOOR.

WHERE THE FLOOR DOES NOT REACH, stated here because a reader of the
paragraph above would otherwise assume it is universal:

* A DEAD-LETTERED row is unclaimable by construction, so claim-and-ack
  cannot be its dedup and this hook cannot consume it. It is still
  surfaced ONCE, from a local seen-file, because the alternative is that
  a session with no watcher armed never learns the message existed at
  all. Purging it is a human act; this hook only says it is there.
* An INSTANCE-NAME address (the ``ListAgents`` row, e.g. ``nexus-19``) is
  drained only once something has REGISTERED it, because it exists in no
  environment variable anywhere -- MM-1.3 established that, which is why
  ``nx tuple watch`` takes it as an explicit ``--instance`` literal. The
  registry is ``<config>/tuple-watch/addresses``, one address per line;
  arming writes to it, and so can a human. Until an address is in there,
  mail sent to it has no floor. The session id needs no registration: it
  arrives in this hook's own payload.

CONTRACT WITH THE PROMPT. stdout is injected context, so an empty mailbox
prints NOTHING and costs an idle prompt nothing. Every failure -- an
unresolvable endpoint, a slow engine, a malformed payload -- prints one
``SKIP`` line on stderr and exits 0. This hook must never block a prompt
and must never turn a transport problem into injected noise.

ORDER, and why it is not rd-then-render. A row is rendered only after its
``ack`` has succeeded: ``rd`` to see what is there, then ``in`` to claim,
then ``ack`` to consume, then render. Rendering anything ``rd`` merely
saw would tell the session it received mail that a peer claimed in the
meantime, or that is still sitting claimed-but-unconsumed and will return
to the mailbox when the lease lapses. That is the same read-then-write
hazard RDR-206 Step 1 closed inside the engine, appearing here between
two HTTP calls where no transaction can close it -- so the fix is to
trust only what ``ack`` confirmed.

Stdlib only, no ``nexus`` import, endpoint through the shared
``_endpoint_resolve`` sibling (nexus-aginu): the same constraints the
``tuple_ledger_project.py`` hook runs under, for the same reason -- a
hook runs on boxes where the client package may be mid-upgrade.
"""
from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _endpoint_resolve as _ep  # noqa: E402

#: Whole-call wall-clock bound per HTTP call. A prompt is waiting on this
#: hook, so the ceiling is tight: a healthy engine answers a zero-timeout
#: probe in milliseconds, and anything slower is not worth a prompt's
#: latency. Enforced on the WHOLE call rather than passed to urlopen,
#: whose own timeout is per-socket-operation and is reset by every recv
#: -- an engine trickling bytes would otherwise hold the prompt open far
#: past this (the nexus-em75s.42 finding, same fix as the sibling hook).
_CALL_TIMEOUT_S = 2.0

#: Bound on the whole drain, across every address and every row. It must
#: stay well under the harness's own hook timeout (``hooks.json``: 10 s),
#: because that timeout is a KILL: anything this process has consumed but
#: not yet written is lost with it. Checked before every HTTP call, and
#: each call's own deadline is clamped to whatever remains, so a merely
#: slow engine cannot walk past the budget one under-cap call at a time.
_TOTAL_BUDGET_S = 6.0

#: Rows fetched per probe. The engine caps a read at 300; a prompt-time
#: drain wants far less, since anything beyond a handful is a backlog the
#: reader cannot absorb in one turn anyway and the rest keeps for the
#: next prompt.
_PROBE_N = 20

#: Live rows consumed per prompt, for the same reason.
_MAX_DELIVER = 10

_TENANT = _ep.DEFAULT_TENANT
_SAFE_ADDRESS_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-."
)


class _Skip(Exception):
    """Any resolution or transport failure. Caught once in main(), printed as
    one SKIP line on stderr, exit 0. A hook that cannot reach the engine is
    not an error the prompt should ever see."""


def _log_skip(reason: str) -> None:
    print(f"[mailbox-drain] SKIP: {reason}", file=sys.stderr)  # noqa: T201 — stderr is this hook's only diagnostic surface


def _valid_address(address: str) -> bool:
    """Path-safe and subspace-safe. An address becomes both a subspace name on
    the wire and a filename in the seen-store, so a traversal-bearing or
    otherwise odd one is dropped rather than sanitised -- silently repairing a
    bad address would drain the wrong mailbox."""
    return bool(address) and len(address) <= 128 and all(
        c in _SAFE_ADDRESS_CHARS for c in address
    )


def _config_dir() -> Path:
    return _ep.default_config_dir()


def _registry_path(config_dir: Path) -> Path:
    return config_dir / "tuple-watch" / "addresses"


def _seen_path(config_dir: Path, address: str) -> Path:
    return config_dir / "tuple-watch" / f"{address}.drained.json"


def _read_registry(config_dir: Path) -> list[str]:
    """Addresses registered by arming or by hand, one per line. Blank lines and
    ``#`` comments are ignored; anything unsafe is dropped. A missing or
    unreadable file is simply an empty registry -- never a failure, since the
    session-id address does not depend on it."""
    try:
        raw = _registry_path(config_dir).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return []
    out: list[str] = []
    for line in raw.splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        if _valid_address(entry):
            out.append(entry)
    return out


def _read_seen(config_dir: Path, address: str) -> set[str]:
    try:
        data = json.loads(_seen_path(config_dir, address).read_text(encoding="utf-8"))
        return {str(x) for x in data.get("dead_surfaced", [])}
    except (OSError, ValueError, AttributeError):
        return set()


def _write_seen(config_dir: Path, address: str, dead_surfaced: set[str]) -> None:
    """Best-effort. Losing this file re-surfaces a dead row once more, which is
    noise; failing the drain over it would lose live mail, which is worse."""
    path = _seen_path(config_dir, address)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"dead_surfaced": sorted(dead_surfaced)}), encoding="utf-8",
        )
        tmp.replace(path)
    except OSError:
        pass


def _pending_path(config_dir: Path, address: str) -> Path:
    return config_dir / "tuple-watch" / f"{address}.pending.json"


def _read_pending(config_dir: Path, address: str) -> list[dict[str, str]]:
    try:
        raw = _pending_path(config_dir, address).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return []
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return []
    out = []
    for e in entries:
        if isinstance(e, dict) and e.get("id") and e.get("rendered"):
            out.append({"id": str(e["id"]), "rendered": str(e["rendered"])})
    return out


def _save_pending(config_dir: Path, address: str, entries: list[dict[str, str]]) -> None:
    """Best-effort: failing to write this must never stop a drain."""
    path = _pending_path(config_dir, address)
    try:
        if not entries:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"entries": entries}), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def _write_pending(config_dir: Path, address: str, tuple_id: str, rendered: str) -> None:
    """Record a claimed-but-not-yet-acked row, so its delivery survives a lost
    ack RESPONSE.

    A LIST keyed by tuple id, not a single record: a drain consumes several rows
    per prompt, so a single slot would let row B's record overwrite row A's while
    A was still unresolved, losing exactly the trace this file exists to keep.
    """
    entries = [e for e in _read_pending(config_dir, address) if e["id"] != tuple_id]
    entries.append({"id": tuple_id, "rendered": rendered})
    _save_pending(config_dir, address, entries)


def _clear_pending(config_dir: Path, address: str, tuple_id: str) -> None:
    entries = [e for e in _read_pending(config_dir, address) if e["id"] != tuple_id]
    _save_pending(config_dir, address, entries)


def _recover_pending(config_dir: Path, address: str, present_ids: set[str],
                     out: _Out) -> None:
    """Deliver rows this hook consumed on an earlier prompt but never showed.

    The window is narrow and the consequence is total: ``ack`` reaches the
    engine, the engine consumes the row, and the response is lost. The client
    never learns the ack succeeded, so without this the row is gone from the
    mailbox and was never shown to anyone -- silent, permanent loss of a
    delivered message, the failure class this whole epic exists to prevent.

    Presence in the mailbox distinguishes the two cases, and the probe has
    already fetched it, so this costs no extra call:

    * the id is ABSENT -- the ack landed, the row is consumed, its delivery was
      lost. Deliver it now, and drop the record.
    * the id is PRESENT -- the ack never landed, so the row is still there and
      the normal path can deliver it. The record is KEPT, not cleared. Clearing
      here was a defect: it assumed this same drain would reach the live-claim
      loop for that row, and a drain whose budget runs out first would leave
      nothing anywhere to notice if the original ambiguous ack later landed at
      the engine on its own schedule. Keeping the record costs a redundant entry
      that the normal path clears on delivery; clearing it early costs the
      message.
    """
    entries = _read_pending(config_dir, address)
    if not entries:
        return
    keep: list[dict[str, str]] = []
    for entry in entries:
        if entry["id"] in present_ids:
            keep.append(entry)   # still in the mailbox: the normal path owns it
        else:
            out.block(entry["rendered"])
    _save_pending(config_dir, address, keep)


def _post(base_url: str, token: str, route: str, body: dict[str, Any],
          *, is_local: bool, budget_s: float | None = None) -> dict[str, Any] | None:
    """POST and return the decoded body, or None on a 404. Raises :class:`_Skip`
    on a transport failure OR on any other non-2xx. Bounds the whole call, not
    each socket operation.

    ONLY 404 IS A CONFIRMED NEGATIVE, and the distinction is the difference
    between a duplicate and a lost message. Callers read ``None`` as "the engine
    answered and said no": on ``ack`` that clears the pending-ack safety record,
    and on ``rd`` it means the mailbox is empty. A 404 earns that reading -- it
    is ClaimNotFound, the engine's considered answer. A 500, 502, 401, 403 or 429
    does not. The engine may have COMMITTED the mutation and failed on the way
    out (``TupleHandler`` lets an unexpected exception fall through to a bare
    500), so treating it as a clean negative drops the only trace of a row that
    is already consumed, and the message is gone with nobody having seen it.
    Raising ``_Skip`` instead keeps the record and lets the next prompt recover.

    This also restores the pattern the sibling hook ``tuple_ledger_project.py``
    already follows -- it captures ``exc.code`` and refuses anything outside
    2xx -- which this hook's own docstring claims to share.
    """
    payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
    url = f"{base_url}{route}"
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    # Never spend longer than the drain has left: a sequence of calls each
    # just under the per-call cap would otherwise blow the whole budget while
    # every individual call looked fine.
    call_timeout = _CALL_TIMEOUT_S
    if budget_s is not None:
        call_timeout = max(0.1, min(_CALL_TIMEOUT_S, budget_s))
    outcome: dict[str, Any] = {}

    def _do() -> None:
        try:
            handlers: list[urllib.request.BaseHandler] = []
            if is_local:
                handlers.append(urllib.request.ProxyHandler({}))
            opener = urllib.request.build_opener(*handlers)
            with opener.open(req, timeout=call_timeout) as resp:  # noqa: S310 — fixed engine URL
                outcome["body"] = json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                outcome["body"] = None      # ClaimNotFound: a confirmed negative
            else:
                outcome["status"] = exc.code
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=_do, daemon=True)
    thread.start()
    thread.join(timeout=call_timeout)
    if thread.is_alive():
        raise _Skip(
            f"{route} exceeded its {call_timeout:.1f}s deadline; the prompt is not waiting for it",
        )
    if "error" in outcome:
        raise _Skip(f"transport failure on {route}: {outcome['error']}")
    if "status" in outcome:
        raise _Skip(
            f"engine returned HTTP {outcome['status']} on {route}; the outcome is "
            f"UNKNOWN, so nothing is treated as confirmed and any pending record is kept",
        )
    return outcome.get("body")


class _Out:
    """Writes each delivered block to stdout IMMEDIATELY, flushing every time.

    Buffering blocks in memory and printing once at the end is what made an
    already-consumed row losable: a transport failure on a LATER row discarded
    the whole list, and the harness's own hook timeout (hooks.json: 10 s) is a
    KILL, so anything consumed-but-unprinted died with the process. Streaming
    makes "acked implies delivered" hold even under a mid-drain SIGKILL, for
    every row already acked at the moment of the kill.
    """

    def __init__(self) -> None:
        self._opened = False

    def block(self, text: str) -> None:
        if not self._opened:
            # stdout IS the product here: a UserPromptSubmit hook's stdout
            # becomes the injected context. These are not diagnostics.
            print("## Mailbox (RDR-205): delivered at this prompt\n", flush=True)  # noqa: T201
            self._opened = True
        print(text, flush=True)  # noqa: T201


def _render_live(address: str, row: dict[str, Any]) -> str:
    dims = row.get("dims") or {}
    sender = dims.get("from", "?")
    kind = dims.get("kind") or "note"
    corr = dims.get("correlation_id") or "-"
    body = row.get("body")
    body_text = body if body else "(no body)"
    return (
        f"- from={sender} kind={kind} correlation_id={corr} "
        f"address=mailbox/{address} tuple_id={row.get('id', '?')}\n"
        f"  {body_text}"
    )


def _render_dead(address: str, row: dict[str, Any]) -> str:
    dims = row.get("dims") or {}
    return (
        f"- UNDELIVERABLE at mailbox/{address} tuple_id={row.get('id', '?')} "
        f"from={dims.get('from', '?')} attempts={row.get('attempts', '?')}: this row is "
        f"dead-lettered and can never be claimed, so nothing will deliver it. "
        f"Reported once. Purge it with `nx tuple stats mailbox/{address}` and the "
        f"engine's sweep, or ask the sender to resend."
    )


def _drain_address(base_url: str, token: str, address: str, *, is_local: bool,
                   config_dir: Path, deadline: float, out: _Out) -> None:
    """Probe one address and deliver what it can, writing each row as it goes.

    Returns nothing: every delivered row has already been written by the time
    this returns, so a failure part-way through cannot retract an earlier one.
    A :class:`_Skip` still propagates -- the caller stops the drain -- but what
    was already delivered stays delivered.
    """
    import time  # noqa: PLC0415 — deferred: only this path needs a clock

    probe = _post(base_url, token, "/v1/tuples/rd", {
        "subspace": f"mailbox/{address}",
        "keys_pattern": {"to": address},
        "n": _PROBE_N,
    }, is_local=is_local, budget_s=deadline - time.monotonic())
    rows = (probe or {}).get("tuples") or []
    present_ids = {str(r.get("id")) for r in rows}

    # A row this hook consumed on an earlier prompt but never managed to
    # deliver: the ack reached the engine and its RESPONSE did not, so the row
    # is gone from the mailbox and nothing else will ever show it. Recover it
    # here, before anything else, since it is already lost from the engine's
    # point of view.
    _recover_pending(config_dir, address, present_ids, out)

    if not rows:
        return

    dead_rows = [r for r in rows if r.get("claim_state") == "dead"]
    if dead_rows:
        seen = _read_seen(config_dir, address)
        fresh = [r for r in dead_rows if str(r.get("id")) not in seen]
        for row in fresh:
            out.block(_render_dead(address, row))
            seen.add(str(row.get("id")))
        if fresh:
            # Keep only ids still present, so the file cannot grow forever.
            present = {str(r.get("id")) for r in dead_rows}
            _write_seen(config_dir, address, seen & present)

    live_count = sum(1 for r in rows if r.get("claim_state") != "dead")
    for _ in range(min(live_count, _MAX_DELIVER)):
        if time.monotonic() >= deadline:
            break
        claim = _post(base_url, token, "/v1/tuples/in", {
            "subspace": f"mailbox/{address}",
            "keys_pattern": {"to": address},
            "claimant": f"mailbox-drain-{address}",
            "lease_s": 30,
        }, is_local=is_local, budget_s=deadline - time.monotonic())
        if not claim or not claim.get("claim_id"):
            break  # a peer took it between rd and in, or the queue emptied
        row = claim.get("tuple") or {}
        row_id = str(row.get("id"))
        rendered = _render_live(address, row)
        # Recorded BEFORE the ack, so that an ack whose response is lost --
        # the engine consumed the row, the client never learned it -- leaves a
        # trace the next prompt can recover from. Without this the row is gone
        # from the engine and was never shown to anyone.
        _write_pending(config_dir, address, row_id, rendered)
        acked = _post(base_url, token, "/v1/tuples/ack", {
            "claim_id": claim["claim_id"],
            "claimant": f"mailbox-drain-{address}",
        }, is_local=is_local, budget_s=deadline - time.monotonic())
        if acked is None:
            # A clean refusal: the engine answered and said no. The lease lapses
            # and the row returns to the mailbox, so the normal path will deliver
            # it and this record would be a duplicate. Dropped by id, so no other
            # row's record is disturbed.
            _clear_pending(config_dir, address, row_id)
            break
        out.block(rendered)
        _clear_pending(config_dir, address, row_id)


def _resolve_endpoint(config_dir: Path) -> tuple[str, str, bool]:
    """Resolve ``(base_url, token, is_local_supervisor)``.

    CREDENTIAL POLICY, and why it is not the sibling ledger hook's. That hook
    (``tuple_ledger_project.py``) is a fire-and-forget write with no reader and
    no retry, so it deliberately refuses anything but a fresh tenant-scoped
    data-token lease. This hook is a SYNCHRONOUS call with a prompt waiting on
    it, the same shape as ``t2_prefix_scan.py`` and ``routing/_lib.py``, so it
    takes the same looser last resort those two take: a static ``service_token``
    from env or the persisted ``config.yml``. Refusing that would make the drain
    silently inert on a managed box onboarded with ``nx config set
    service_token`` and nothing else, which is a real, documented path -- the
    floor would not exist exactly where a user had done everything right.

    A fresh data-token lease for the resolved host still WINS over the static
    token wherever one exists, mirroring the real client's
    ``DataTokenManager.bearer_for``. Base-URL precedence comes from the shared
    module (nexus-aginu) rather than being re-derived here, since that
    precedence is what drifted between three hand-maintained copies before it
    was factored out.
    """
    try:
        base_url, is_local = _ep.resolve_base_url(config_dir)
    except _ep.EndpointUnresolvable as exc:
        raise _Skip(str(exc)) from exc

    data_token = _ep.read_data_token_lease(config_dir, base_url)
    if data_token:
        return base_url, data_token, is_local

    import os  # noqa: PLC0415 — deferred: only this path reads the environment

    token = os.environ.get("NX_SERVICE_TOKEN", "").strip()
    if not token:
        token = (_ep.read_config_yml_credentials(config_dir) or {}).get(
            "service_token", "",
        ).strip()
    if not token:
        # Through the module's own accessor, never off the raw lease dict: it
        # refuses a lease file that is not owner-only, because the token it
        # carries authorizes real engine writes and a group- or world-readable
        # lease means another local account could have read it too. Reading the
        # dict directly would silently skip that audit.
        try:
            token = _ep.read_local_supervisor_token(config_dir).strip()
        except _ep.EndpointUnresolvable:
            token = ""
    if not token:
        raise _Skip(
            f"resolved {base_url} but found no credential: no data-token lease, no "
            "NX_SERVICE_TOKEN, no persisted service_token, no local supervisor lease",
        )
    return base_url, token, is_local


def main() -> int:
    """The hook entry point. NEVER raises, and never exits non-zero.

    The guarantee at the top of this file -- one SKIP line on stderr, exit 0 --
    is the whole contract with a waiting prompt, so it is enforced here rather
    than assumed from the per-address handlers below. Those cover the drain
    itself; this covers everything before and around it (reading the payload,
    resolving the config directory, reading the registry). A hook that runs on
    EVERY UserPromptSubmit has no business putting a traceback in front of
    someone who typed something unrelated.
    """
    try:
        return _drain_all()
    except Exception as exc:  # noqa: BLE001 — the contract above is the reason
        _log_skip(f"unexpected {type(exc).__name__} before any mailbox was drained: {exc}")
        return 0


def _drain_all() -> int:
    import time  # noqa: PLC0415 — deferred: only main needs a clock

    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        raw = ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    session_id = str(payload.get("session_id") or "").strip()

    config_dir = _config_dir()
    addresses: list[str] = []
    if _valid_address(session_id):
        addresses.append(session_id)
    addresses.extend(_read_registry(config_dir))
    # First-occurrence dedup: a registry naming this session's own id must not
    # make the hook drain it twice and render the same row in two blocks.
    addresses = list(dict.fromkeys(addresses))
    if not addresses:
        _log_skip("no address to drain: the payload carried no usable session id "
                  "and the address registry is empty")
        return 0

    try:
        base_url, token, is_local = _resolve_endpoint(config_dir)
    except _Skip as exc:
        _log_skip(f"no reachable tuple space: {exc}")
        return 0

    deadline = time.monotonic() + _TOTAL_BUDGET_S
    out = _Out()
    for address in addresses:
        if time.monotonic() >= deadline:
            _log_skip(f"drain budget of {_TOTAL_BUDGET_S}s spent before reaching "
                      f"mailbox/{address}; it keeps until the next prompt")
            break
        try:
            _drain_address(
                base_url, token, address, is_local=is_local,
                config_dir=config_dir, deadline=deadline, out=out,
            )
        except _Skip as exc:
            # Per ADDRESS, not around the whole loop: a transport failure on one
            # mailbox must not stop the others, and everything already delivered
            # has already been written and flushed, so nothing can be retracted.
            _log_skip(f"mailbox/{address}: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 — see below
            # UNEXPECTED, and still not the prompt's problem. _Skip covers what
            # this hook anticipated; a malformed engine response (a non-dict body,
            # a row whose dims is not a dict) raises AttributeError or TypeError
            # instead, and before this landed that escaped as an exit-1 traceback
            # on whatever unrelated prompt the user had just typed -- flatly
            # contradicting the contract at the top of this file. The type is
            # named in the line so an unexpected failure stays diagnosable rather
            # than being quietly indistinguishable from a planned skip.
            _log_skip(f"mailbox/{address}: unexpected {type(exc).__name__}: {exc}")
            continue
    return 0


if __name__ == "__main__":
    sys.exit(main())
