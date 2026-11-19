# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The SessionStart policy table (RDR-253 "Technical Design", the table after "Binding").

The engine owns the compare-and-set and the policy of each bind event (``IdentityRepository.bind``). This module
owns the client's half: which event to send, for which identity, with what evidence, and what to do when the engine
answers ``held``. Every row of the table is one branch here:

* ``startup`` after an in-place ``/restart`` with no exchange (the process's own pid and ``proc_start`` have a
  binding on this host): the same identity, rebound at a new generation. Checked before every other ``startup`` row.
* ``startup``, a seat named by ``NX_SEAT`` or by a ``-n`` that exactly matches a seat alias: bound when unheld,
  ended or its holder no longer exists; ephemeral (with the holder printed) when it is live in another process,
  lapsed with a running holder on this host, or reserved after a lapse from another host.
* ``startup``, nothing named, or a ``-n`` that matches no seat: a new ephemeral identity (the ``-n`` name is its
  display name, never an alias).
* ``resume``: the resumed id's identity is bound again (a new generation if the process is new); unchanged inside
  one process; a cross-seat switch closes the process's own identity first; a live holder elsewhere gives an
  ephemeral; an id in no index, or of an ended ephemeral or delegated identity, is treated as ``startup``.
* ``clear`` and ``fork``: the same generation, a new session id; a lapsed seat is revived by the engine.
* ``compact``: nothing.

The holder check is the client's (:mod:`nexus.identity.holder`); this module sends its verdict as ``holder_gone``
only for a holder on this host that the check finds gone, never for one it finds running or cannot judge.

Credentials. The hook runs this with the tenant credential (every event). The MCP server's pending bind runs it
with the data credential, which may send ``start``, ``resume``, ``fork``, ``refresh`` and ``end`` and create an
ephemeral identity but not ``clear``, ``release``, ``takeover`` or ``gone``: ``allow_privileged=False`` keeps this
module off those events, so a pending bind never needs them (a process with no binding has no identity to
``clear`` or ``release``).
"""
from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nexus.identity import token_contract
from nexus.identity.bind_client import IdentityApiError, IdentityUnavailableError, evidence_body
from nexus.identity.holder import (
    GONE, Processes, RegistryEntry, check_holder, holder_process, read_registry_entry,
)
from nexus.identity.token_file import TokenFile, token_path, update_token_file

REGISTRY_WAIT_S: float = 5.0
REGISTRY_POLL_S: float = 0.1

#: Sources whose bind changes what the MCP server must be told (the T1 handoff marker): the transcript's session id
#: changes under a live server. ``startup`` spawns a new server, which reads the token file instead.
HANDOFF_SOURCES = frozenset({"clear", "resume", "fork"})

NX_SEAT_ENV = "NX_SEAT"
NX_IDENTITY_TOKEN_ENV = "NX_IDENTITY_TOKEN"


@dataclass
class LaunchFacts:
    """What the process knows about itself at SessionStart. ``started_at`` and the registry-derived fields are None
    until the registry file exists."""

    source: str
    session_id: str
    host: str
    claude_pid: int
    proc_start: str | None
    started_at: int | None = None
    name: str | None = None
    entrypoint: str | None = None
    pane: str | None = None
    client_version: str | None = None
    seat: str | None = None  # NX_SEAT

    @property
    def interactive(self) -> bool:
        return self.entrypoint in ("cli", "claude-desktop")

    @property
    def desktop(self) -> bool:
        return self.entrypoint == "claude-desktop"

    def with_registry(self, entry: RegistryEntry) -> None:
        self.started_at = entry.started_at
        self.name = self.name or entry.name
        self.entrypoint = self.entrypoint or entry.entrypoint
        self.pane = self.pane or entry.tmux


@dataclass
class BindOutcome:
    """What the hook did. ``action`` is ``bound`` (a seat or a rebind), ``ephemeral``, ``unchanged``, ``pending``
    (no registry file in time: the pending file is written), ``delegated`` (a child that was handed a token),
    ``skipped`` (compact) or ``unbound`` (the engine could not be used). ``row`` names the policy-table row, for
    logs and tests."""

    action: str
    row: str
    identity: str | None = None
    generation: int | None = None
    event: str | None = None
    kind: str | None = None
    alias: str | None = None
    token_file: Path | None = None
    switched_from: str | None = None
    notices: list[str] = field(default_factory=list)

    @property
    def handoff(self) -> bool:
        return self.identity is not None and self.token_file is not None and self.action in ("bound", "ephemeral", "unchanged")


def _token_gone(exc: IdentityApiError) -> bool:
    """Whether the engine refused a ``fork`` for want of a live identity token (engine 0.1.198: a data credential's fork
    needs one, and a process whose token expired while the laptop slept has none). A ``start`` needs none, so the caller
    binds the process as a new start. The code is matched, never the message."""
    return exc.status == 403 and exc.code == "identity_token_required"


class SessionBinder:
    """The policy table, against an engine client and a view of the machine."""

    def __init__(self, *, client: Any, procs: Processes | Any, identity_dir: Path, allow_privileged: bool = True,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep) -> None:
        self._client = client
        self._procs = procs
        self._idir = Path(identity_dir)
        self._privileged = allow_privileged
        self._clock = clock
        self._sleep = sleep

    # ── entry ───────────────────────────────────────────────────────────────

    def _is_this_process(self, facts: LaunchFacts, entry: RegistryEntry) -> bool:
        """Whether a registry file is this process's own, not a stale one a crashed earlier owner of the pid left (Claude
        deletes its file only on a clean exit): its ``sessionId`` is the payload's and its ``procStart`` is the
        process's OS start time. ``procStart`` is compared off Windows only: Windows' spelling of it is not known."""
        if entry.session_id and facts.session_id and entry.session_id != facts.session_id:
            return False
        if getattr(self._procs, "platform", "") != "win32" and entry.proc_start and facts.proc_start:
            return entry.proc_start == facts.proc_start
        return True

    def await_registry(self, facts: LaunchFacts) -> bool:
        """Fill *facts* from the process's registry file, waiting up to 5 seconds for the hook's own file to be
        written (the hook may run first, ``253-research-10`` (5)). A file that is not this process's (a stale one from
        a crashed earlier owner of the pid) is waited past, as if absent. True when the file was found."""
        deadline = self._clock() + REGISTRY_WAIT_S
        while True:
            entry = read_registry_entry(facts.claude_pid, self._procs.sessions_dir)
            if entry is not None and entry.started_at is not None and self._is_this_process(facts, entry):
                facts.with_registry(entry)
                return True
            if self._clock() >= deadline:
                return False
            self._sleep(REGISTRY_POLL_S)

    def bind(self, facts: LaunchFacts, *, env: Mapping[str, str] | None = None) -> BindOutcome:
        """Apply the policy table for *facts*. Raises nothing for an engine that cannot be used: the outcome says
        ``unbound`` and why, because a SessionStart hook must never fail the session over identity."""
        if env is not None and (env.get(NX_IDENTITY_TOKEN_ENV) or "").strip():
            return BindOutcome("delegated", "delegated_child")
        if facts.source == "compact":
            return BindOutcome("skipped", "compact")
        if facts.started_at is None:
            return BindOutcome("pending", "no_registry_file")
        try:
            return self._apply(facts)
        except IdentityUnavailableError as exc:
            return BindOutcome("unbound", "engine_unavailable", notices=[f"identity: not bound, the engine is not answering ({exc})"])
        except IdentityApiError as exc:
            return BindOutcome("unbound", "engine_refused", notices=[f"identity: not bound, the engine refused ({exc})"])

    def _apply(self, facts: LaunchFacts) -> BindOutcome:
        own = self._own_identity(facts)
        if facts.source in ("clear", "fork") and own is not None:
            kept = self._keep(facts, own)
            if kept is not None:
                return kept
        if facts.source == "resume":
            resumed = self._resume(facts, own)
            if resumed is not None:
                return resumed
            if own is not None:
                return self._unindexed_with_own(facts, own)
        return self._startup(facts, rebind=facts.source == "startup")

    # ── evidence ────────────────────────────────────────────────────────────

    def _evidence(self, facts: LaunchFacts, *, holder_gone: Mapping[str, Any] | None = None,
                  prior_session_id: str | None = None) -> dict[str, Any]:
        return evidence_body(
            session_id=facts.session_id, host=facts.host, pid=facts.claude_pid, started_at=facts.started_at,
            proc_start=facts.proc_start, pane=facts.pane, client_version=facts.client_version,
            lease="unleased" if facts.desktop else "leased", prior_session_id=prior_session_id,
            holder_gone=holder_gone,
        )

    def _holder_gone(self, view: Mapping[str, Any], facts: LaunchFacts) -> dict[str, Any] | None:
        """The ``holder_gone`` evidence for *view*'s holder when it is on this host and the check finds it gone."""
        holder = view.get("holder")
        if not isinstance(holder, Mapping) or view.get("state") not in ("live", "lapsed"):
            return None
        verdict = check_holder(holder, procs=self._procs, host=facts.host, identity_dir=self._idir)
        return holder_process(holder) if verdict.state == GONE else None

    # ── own identity ────────────────────────────────────────────────────────

    def _own_identity(self, facts: LaunchFacts) -> dict[str, Any] | None:
        """The identity this very process holds now (its pid, ``startedAt`` and start time are the holder's)."""
        for view in self._client.bindings_by_host(facts.host, facts.claude_pid):
            holder = view.get("holder") or {}
            if view.get("kind") == "delegated":
                continue
            if holder.get("pid") == facts.claude_pid and holder.get("started_at") == facts.started_at:
                return dict(view)
        return None

    # ── rows ────────────────────────────────────────────────────────────────

    def _keep(self, facts: LaunchFacts, own: Mapping[str, Any]) -> BindOutcome | None:
        """``clear`` and ``fork``: same generation, new session id. None when the engine will not (the seat was
        taken or ended meanwhile): the caller then binds the process as a new start. A credential that may not send
        ``clear`` (the data credential of a mint-only install) sends the same change as ``fork``, which it may."""
        if not self._privileged and facts.source == "clear":
            return None
        prior = (own.get("holder") or {}).get("session_id")
        event = facts.source
        try:
            try:
                view = self._client.bind(own["id"], event, int(own["generation"]), self._evidence(facts, prior_session_id=prior))
            except IdentityApiError as exc:
                if exc.status != 403 or event != "clear":
                    raise
                event = "fork"
                view = self._client.bind(own["id"], event, int(own["generation"]), self._evidence(facts, prior_session_id=prior))
        except IdentityApiError as exc:
            if exc.status == 409 or _token_gone(exc):
                return None
            raise
        return self._finish(facts, view, event, row=f"{facts.source}_in_process", action="bound")

    def _resume(self, facts: LaunchFacts, own: Mapping[str, Any] | None) -> BindOutcome | None:
        try:
            indexed = self._client.session(facts.session_id)
        except IdentityApiError as exc:
            if exc.status == 404:
                return None  # in no index: a session from before this RDR, treated as startup
            raise
        target = indexed["identity"]
        view = self._client.show(target)
        if view.get("kind") in ("ephemeral", "delegated") and view.get("state") == "ended":
            return None  # an ended ephemeral or delegated identity: treated as startup
        if view.get("kind") == "delegated":
            return None
        if own is not None and own["id"] == target:
            bound = self._client.bind(target, "resume", int(own["generation"]), self._evidence(facts))
            return self._finish(facts, bound, "resume", row="resume_same_process", action="unchanged")
        if own is not None:
            return self._switch(facts, own, view)
        return self._take(facts, view, "resume", row_prefix="resume")

    @staticmethod
    def _reservation_over(view: Mapping[str, Any], facts: LaunchFacts) -> bool:
        """A seat lapsed on another host whose 10-minute reservation has ended: free to take, as the engine reads it."""
        holder = view.get("holder") or {}
        until = view.get("reserved_until")
        if view.get("state") != "lapsed" or not holder or holder.get("host") == facts.host or not isinstance(until, str):
            return False
        try:
            return datetime.now(timezone.utc) >= datetime.fromisoformat(until.replace("Z", "+00:00"))
        except ValueError:
            return False

    def _close_own(self, facts: LaunchFacts, own: Mapping[str, Any]) -> str | None:
        """End an ephemeral identity this process holds, or release a seat it holds. None on success; the notice to
        print when the credential may not do it (the data credential of a mint-only install: release is the tenant's)."""
        closing = "release" if own.get("kind") == "seat" else "end"
        try:
            self._client.bind(own["id"], closing, int(own["generation"]), self._evidence(facts))
        except IdentityApiError as exc:
            if exc.status == 403:
                return (f"identity: this credential may not {closing} {own.get('name') or own['id']}, so the session keeps it "
                        f"(a data credential cannot release a seat)")
            raise
        return None

    def _switch(self, facts: LaunchFacts, own: Mapping[str, Any], target: Mapping[str, Any]) -> BindOutcome:
        """A ``/resume`` in a process that already holds a different identity: the process switches when the
        resumed seat is free (its holder gone, or its other-host reservation over), and keeps what it has (printing
        the holder) when it is live elsewhere."""
        if not self._privileged:
            return BindOutcome("unchanged", "resume_switch_refused", identity=own["id"], generation=int(own["generation"]))
        if (target.get("state") in ("live", "lapsed") and self._holder_gone(target, facts) is None
                and not self._reservation_over(target, facts)):
            reason = self._held_notice(target, facts)
            return BindOutcome("unchanged", "resume_other_held", identity=own["id"], generation=int(own["generation"]),
                               kind=own.get("kind"), alias=own.get("name"), notices=[reason])
        refused = self._close_own(facts, own)
        if refused is not None:
            return BindOutcome("unchanged", "resume_switch_forbidden", identity=own["id"], generation=int(own["generation"]),
                               kind=own.get("kind"), alias=own.get("name"), notices=[refused])
        outcome = self._take(facts, target, "resume", row_prefix="resume_switch")
        outcome.switched_from = own["id"]
        return outcome

    def _unindexed_with_own(self, facts: LaunchFacts, own: Mapping[str, Any]) -> BindOutcome:
        """A ``resume`` of an id no index has (or of an ended ephemeral) in a process that already holds an identity:
        the process does not start a second identity beside the one it holds. When the launch names another seat that
        seat is taken and the old identity closed; otherwise the process keeps its identity and the new session id is
        added to it (a ``fork`` row)."""
        named = facts.seat or facts.name
        seat_id = self._seat_named(named, from_env=bool(facts.seat))[0] if named else None
        if seat_id is not None and seat_id != own["id"] and self._privileged and self._close_own(facts, own) is None:
            outcome = self._startup(facts, rebind=False)
            outcome.switched_from = own["id"]
            return outcome
        prior = (own.get("holder") or {}).get("session_id")
        try:
            view = self._client.bind(own["id"], "fork", int(own["generation"]), self._evidence(facts, prior_session_id=prior))
        except IdentityApiError as exc:
            if not _token_gone(exc):
                raise
            return self._startup(facts, rebind=False)   # as _keep does: a start needs no token
        return self._finish(facts, view, "fork", row="resume_unindexed_reuse", action="unchanged")

    def _startup(self, facts: LaunchFacts, *, rebind: bool) -> BindOutcome:
        if rebind and facts.proc_start:
            rebound = self._rebind(facts)
            if rebound is not None:
                return rebound
        named = facts.seat or facts.name
        if named:
            seat_id, notice = self._seat_named(named, from_env=bool(facts.seat))
            if seat_id is not None:
                return self._take(facts, self._client.show(seat_id), "start", row_prefix="startup_seat")
            eph = self._ephemeral(facts, display_name=None if facts.seat else named, row="startup_unnamed_seat")
            if notice:
                eph.notices.append(notice)
            return eph
        return self._ephemeral(facts, display_name=None, row="startup_no_seat")

    def _rebind(self, facts: LaunchFacts) -> BindOutcome | None:
        """Row 1: the most recent binding on this host with this process's pid and start time, whatever the
        identity's state, ``ended`` included, when no other process holds that identity now. Only the most recent
        identity is considered."""
        assert facts.proc_start is not None
        for row in self._client.history(facts.host, facts.claude_pid, facts.proc_start):
            if row.get("kind") == "delegated":
                continue
            # only the most recent identity of the process: an older one belongs to an earlier life of the pid
            state = row.get("state")
            holder = row.get("holder") or {}
            if state in ("live", "lapsed"):
                ours = (holder.get("host") == facts.host and holder.get("pid") == facts.claude_pid
                        and holder.get("proc_start") == facts.proc_start)
                if not ours:
                    return None  # held by another process now
                if holder.get("started_at") == facts.started_at:
                    return BindOutcome("unchanged", "startup_already_bound", identity=row["id"],
                                       generation=int(row["generation"]), kind=row.get("kind"), alias=row.get("name"))
            elif state not in ("unheld", "ended"):
                return None
            try:
                view = self._client.bind(row["id"], "start", int(row["generation"]),
                                         self._evidence(facts, holder_gone=self._holder_gone(row, facts)))
            except IdentityApiError as exc:
                if exc.status == 409:
                    return None
                raise
            return self._finish(facts, view, "start", row="startup_rebind", action="bound")
        return None

    def _seat_named(self, name: str, *, from_env: bool) -> tuple[str | None, str | None]:
        """``(identity id, None)`` for a live seat alias *name*; else ``(None, notice)``, the notice only for a
        name ``NX_SEAT`` gave (a ``-n`` that matches no seat is just a display name)."""
        try:
            found = self._client.alias(name)
        except IdentityApiError as exc:
            renamed = exc.body.get("renamed_to") if exc.status == 410 else None
            if renamed:                 # past the alias's grace the engine answers 410, still naming the new name
                return None, (f"identity: NX_SEAT={name} was renamed to {renamed}; this session runs as an ephemeral identity"
                              if from_env else None)
            if exc.status in (404, 410):
                return None, (f"identity: NX_SEAT={name} names no seat; this session runs as an ephemeral identity"
                              if from_env else None)
            raise
        if found.get("state") != "live":
            current = found.get("current_name")
            return None, (f"identity: NX_SEAT={name} was renamed to {current}; this session runs as an ephemeral identity"
                          if from_env else None)
        return str(found["identity"]), None

    def _take(self, facts: LaunchFacts, view: Mapping[str, Any], event: str, *, row_prefix: str) -> BindOutcome:
        """Bind *view*'s identity for this process; when its holder is live elsewhere, or the engine will not
        (a bind raced and lost twice), run as an ephemeral identity and say who holds the seat."""
        for attempt in (1, 2):
            gone = self._holder_gone(view, facts)
            try:
                bound = self._client.bind(view["id"], event, int(view["generation"]), self._evidence(facts, holder_gone=gone))
            except IdentityApiError as exc:
                if exc.status == 409 and exc.code == "generation_conflict" and attempt == 1:
                    view = self._client.show(view["id"])
                    continue
                if exc.status == 409 and exc.code in ("held", "holder_mismatch", "generation_conflict", "not_held"):
                    holder_view = exc.body if exc.code in ("held", "holder_mismatch") else {}
                    eph = self._ephemeral(facts, display_name=None, row=f"{row_prefix}_held")
                    eph.notices.append(self._held_notice({**view, **dict(holder_view)}, facts))
                    return eph
                raise
            return self._finish(facts, bound, event, row=f"{row_prefix}_bound", action="bound")
        raise AssertionError("unreachable")  # the loop returns or raises

    def _held_notice(self, view: Mapping[str, Any], facts: LaunchFacts) -> str:
        name = view.get("name") or view.get("id")
        holder = view.get("holder") or {}
        who = (f"pid {holder.get('pid')} on {holder.get('host')}" if holder else "another process")
        text = f"identity: seat {name} is held by {who} (generation {view.get('generation')}); this session runs as an ephemeral identity."
        if facts.interactive and view.get("name"):
            text += f" To take it over, run: nx identity claim {view['name']} --take-over"
        return text

    def _ephemeral(self, facts: LaunchFacts, *, display_name: str | None, row: str) -> BindOutcome:
        view = self._client.create_ephemeral(display_name=display_name, created_by="session", evidence=self._evidence(facts))
        out = self._finish(facts, view, "start", row=row, action="ephemeral")
        return out

    # ── result ──────────────────────────────────────────────────────────────

    def _finish(self, facts: LaunchFacts, view: Mapping[str, Any], event: str, *, row: str, action: str) -> BindOutcome:
        """Write the process's token file from the bind's answer and describe the result."""
        token, lifetime = token_contract.token_from_response(view)
        path = token_path(self._idir, facts.claude_pid, facts.started_at or 0)

        def record(current: TokenFile | None) -> TokenFile:
            # session ids accumulate under one identity (a /clear keeps it); another identity starts the list again
            earlier = current.session_ids if current is not None and current.identity == view["id"] else ()
            ids = earlier + ((facts.session_id,) if facts.session_id and facts.session_id not in earlier else ())
            return TokenFile(
                identity=view["id"], generation=int(view["generation"]), token=token,
                expires_at=token_contract.expires_at(lifetime, now=self._clock()), session_id=facts.session_id,
                pid=facts.claude_pid, started_at=facts.started_at or 0, proc_start=facts.proc_start, host=facts.host,
                registry_dir=str(self._procs.sessions_dir), session_ids=ids)

        update_token_file(path, record)
        outcome = BindOutcome(action, row, identity=view["id"], generation=int(view["generation"]), event=event,
                              kind=view.get("kind"), alias=view.get("name"), token_file=path)
        if view.get("name") and facts.name and facts.name != view["name"]:
            outcome.notices.append(
                f"identity: Claude Code's display name is '{facts.name}' but the seat is '{view['name']}'; "
                f"/rename changes only the display name")
        return outcome
