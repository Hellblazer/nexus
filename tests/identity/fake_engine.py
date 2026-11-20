# SPDX-License-Identifier: AGPL-3.0-or-later
"""An in-memory stand-in for the engine's ``/v1/identity`` routes, for the tenant-free Step 4 tests.

It answers an :class:`~nexus.identity.bind_client.IdentityClient`'s calls with the engine's documented shapes
(``IdentityRows.view``, the refusal codes of ``IdentityRepository``) and records every call, so a test can say what the
command sent and, as much, what it did not. It models only what the Step 4 commands read: the compare-and-set on the
generation, ``takeover``/``start``/``release``/``end``/``refresh``, ``gone``, promote, rename, purge's refusals and the
delegated create. The engine's own behaviour is checked by the engine row functions in ``tests/identity/test_identity_step4.py``.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from nexus.identity.bind_client import IdentityApiError

_ID = re.compile(r"^i[0-9]+$")


def _refuse(status: int, code: str, message: str, **body: Any) -> IdentityApiError:
    return IdentityApiError(status, code, message, {"error": code, "message": message, **body})


class FakeEngine:
    """One tenant. ``now`` is a callable returning epoch seconds, used for ``reserved_until``."""

    def __init__(self, *, now: Any = lambda: 1_000.0) -> None:
        self.now = now
        self.records: dict[str, dict[str, Any]] = {}
        self.aliases: dict[str, dict[str, Any]] = {}
        self.logs: dict[str, list[dict[str, Any]]] = {}
        self.sessions: dict[str, tuple[str, int]] = {}
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.purged: list[str] = []
        #: What an engine from 0.1.198 adds to a promote answer (its own retitle of the board entry); None is an older engine.
        self.promote_board: dict[str, Any] | None = None
        self._n = 0
        self._minted = 0
        self.refuse: dict[str, IdentityApiError] = {}     # route name -> the refusal it answers with (a staged failure)
        self.work: dict[tuple[str, str, str], tuple[str, float]] = {}     # (identity, kind, ref) -> (state, updated_at)
        self.member_of: dict[str, dict[str, str]] = {}                  # identity -> {group: role}, in join order
        self.bind_tokens: list[tuple[str, Any]] = []   # (event, token) of every bind that carried one
        self.caller_token: str | None = None   # the identity the caller's token names; a membership write is fenced on it

    # ── staging (not routes) ────────────────────────────────────────────────

    def _next(self) -> str:
        self._n += 1
        return f"i{self._n}"

    def stage(self, *, kind: str = "seat", name: str | None = None, state: str = "unheld", generation: int = 0,
              holder: Mapping[str, Any] | None = None, parent: str | None = None, lease: str = "leased",
              client_version: str | None = None, reserved_until: float | None = None, gone_at: str | None = None,
              display_name: str | None = None) -> str:
        """An identity as the engine would hold it; returns its id."""
        identity = self._next()
        self.records[identity] = {
            "id": identity, "kind": kind, "parent": parent, "generation": generation, "state": state, "name": name,
            "display_name": display_name, "created_by": "sam", "lease": lease, "client_version": client_version,
            "holder": dict(holder) if holder else None, "reserved_until": reserved_until, "gone_at": gone_at,
        }
        self.logs[identity] = []
        if name:
            self.aliases[name] = {"identity": identity, "state": "live"}
        if holder and holder.get("session_id"):
            self.sessions[holder["session_id"]] = (identity, generation)
        return identity

    def bound(self, identity: str) -> dict[str, Any]:
        return self.records[identity]

    def ids_called(self, route: str) -> list[tuple[Any, ...]]:
        return [args for name, args in self.calls if name == route]

    def _token(self, identity: str, generation: int) -> str:
        """A token per mint: ``tok-<id>-<generation>`` the first time, ``...-m<n>`` after (a refresh mints another)."""
        self._minted += 1
        return f"tok-{identity}-{generation}" + ("" if self._minted == 1 else f"-m{self._minted}")

    def close(self) -> None:
        self.calls.append(("close", ()))

    def _maybe_refuse(self, route: str) -> None:
        if route in self.refuse:
            raise self.refuse[route]

    # ── views ───────────────────────────────────────────────────────────────

    def _view(self, rec: Mapping[str, Any]) -> dict[str, Any]:
        out = {k: rec.get(k) for k in ("id", "kind", "parent", "generation", "state", "name", "display_name",
                                       "created_by", "lease", "client_version", "gone_at")}
        out["liveness"] = rec["state"] if rec["state"] != "live" or rec["lease"] == "leased" else "unknown"
        reserved = rec.get("reserved_until")
        out["reserved_until"] = None if reserved is None else datetime.fromtimestamp(float(reserved), tz=timezone.utc).isoformat().replace("+00:00", "Z")
        out["holder"] = dict(rec["holder"]) if rec.get("holder") else None
        return out

    def _get(self, identity: str) -> dict[str, Any]:
        if identity not in self.records:
            raise _refuse(404, "unknown_identity", f"no identity {identity}")
        return self.records[identity]

    # ── reads ───────────────────────────────────────────────────────────────

    def alias(self, name: str) -> dict[str, Any]:
        self.calls.append(("alias", (name,)))
        row = self.aliases.get(name)
        if row is None:
            raise _refuse(404, "unknown_name", f"no seat has been named {name}")
        rec = self.records[row["identity"]]
        return {"identity": row["identity"], "name": name, "state": row["state"], "current_name": rec["name"],
                "generation": rec["generation"], "identity_state": rec["state"]}

    def show(self, identity: str) -> dict[str, Any]:
        self.calls.append(("show", (identity,)))
        return self._view(self._get(identity))

    def session(self, session_id: str) -> dict[str, Any]:
        self.calls.append(("session", (session_id,)))
        if session_id not in self.sessions:
            raise _refuse(404, "unknown_session", f"no identity has held session {session_id}")
        identity, generation = self.sessions[session_id]
        rec = self.records[identity]
        return {"session_id": session_id, "identity": identity, "generation": generation,
                "current_generation": rec["generation"], "kind": rec["kind"], "state": rec["state"]}

    def bindings_by_host(self, host: str, pid: int | None = None) -> list[dict[str, Any]]:
        self.calls.append(("bindings_by_host", (host, pid)))
        return [self._view(r) for r in self.records.values()
                if r["state"] in ("live", "lapsed") and r["holder"] and r["holder"].get("host") == host
                and (pid is None or r["holder"].get("pid") == pid)]

    def log(self, identity: str) -> list[dict[str, Any]]:
        self.calls.append(("log", (identity,)))
        self._get(identity)
        return list(self.logs[identity])

    # ── writes ──────────────────────────────────────────────────────────────

    def create_seat(self, name: str, *, created_by: str = "sam", display_name: str | None = None,
                    token: Any = None) -> dict[str, Any]:
        self.calls.append(("create_seat", (name, created_by, display_name)))
        if _ID.match(name):
            raise _refuse(400, "reserved_name", "names that look like identity ids (i<digits>) are reserved")
        if name in self.aliases:
            raise _refuse(409, "name_taken", f"{name} is taken")
        return self._view(self.records[self.stage(kind="seat", name=name, display_name=display_name)] | {"created_by": created_by})

    def create_ephemeral(self, *, display_name: str | None, created_by: str, evidence: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append(("create_ephemeral", (display_name, created_by, dict(evidence))))
        holder = {k: evidence.get(k) for k in ("host", "pid", "started_at", "proc_start", "session_id", "pane")}
        identity = self.stage(kind="ephemeral", state="live", generation=1, holder=holder, display_name=display_name)
        out = self._view(self.records[identity])
        out["identity_token"] = self._token(identity, 1)
        out["identity_token_expires_in_seconds"] = 3600
        return out

    def history(self, host: str, pid: int, proc_start: str) -> list[dict[str, Any]]:
        self.calls.append(("history", (host, pid, proc_start)))
        return []

    def create_delegated(self, parent: str, *, display_name: str | None, evidence: Mapping[str, Any],
                         token: Any = None) -> dict[str, Any]:
        self.calls.append(("create_delegated", (parent, display_name, dict(evidence))))
        self._maybe_refuse("create_delegated")
        self._get(parent)
        holder = {k: evidence.get(k) for k in ("host", "pid", "started_at", "proc_start", "session_id")}
        identity = self.stage(kind="delegated", state="live", generation=1, holder=holder, parent=parent)
        self.logs[identity].append({"seq": 1, "event": "start", "generation": 1, **{k: evidence.get(k) for k in holder}})
        out = self._view(self.records[identity])
        out["identity_token"] = self._token(identity, 1)
        out["identity_token_expires_in_seconds"] = 3600
        return out

    def promote(self, identity: str, name: str, *, token: Any = None) -> dict[str, Any]:
        self.calls.append(("promote", (identity, name)))
        rec = self._get(identity)
        if _ID.match(name):
            raise _refuse(400, "reserved_name", "names that look like identity ids (i<digits>) are reserved")
        if rec["kind"] != "ephemeral":
            raise _refuse(409, "not_ephemeral", f"{identity} is a {rec['kind']}, not an ephemeral identity")
        if name in self.aliases:
            raise _refuse(409, "name_taken", f"{name} is taken")
        rec["kind"], rec["name"] = "seat", name
        self.aliases[name] = {"identity": identity, "state": "live"}
        return {**self._view(rec), **(self.promote_board or {})}

    def rename(self, old: str, new: str) -> dict[str, Any]:
        self.calls.append(("rename", (old, new)))
        identity = old if _ID.match(old) else (self.aliases.get(old) or {}).get("identity")
        if identity is None:
            raise _refuse(404, "unknown_name", f"no live alias {old}")
        rec = self._get(identity)
        if new in self.aliases:
            raise _refuse(409, "name_taken", f"{new} is taken")
        self.aliases[rec["name"]]["state"] = "retired"
        rec["name"] = new
        self.aliases[new] = {"identity": identity, "state": "live"}
        self.logs[identity].append({"seq": len(self.logs[identity]) + 1, "event": "rename", "generation": rec["generation"]})
        return {**self._view(rec), "renamed_from": old, "board_entries_retitled": 1}

    def purge(self, identity: str) -> dict[str, Any]:
        self.calls.append(("purge", (identity,)))
        rec = self._get(identity)
        if not (rec["state"] == "ended" or (rec["state"] == "lapsed" and rec["gone_at"])):
            raise _refuse(409, "not_purgeable", f"{identity} is {rec['state']}")
        self.purged.append(identity)
        return {"identity": identity, "purged": True}

    def gone(self, identity: str, expected_generation: int, holder_gone: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append(("gone", (identity, expected_generation, dict(holder_gone))))
        rec = self._get(identity)
        if rec["generation"] != expected_generation:
            raise _refuse(409, "generation_conflict", "stale generation")
        cur = rec["holder"] or {}
        if any(cur.get(k) != holder_gone.get(k) for k in ("host", "pid", "started_at", "proc_start")):
            raise _refuse(409, "holder_mismatch", "the verdict names a process that is not the holder")
        if rec["gone_at"]:
            return {**self._view(rec), "recorded": False}
        rec["state"], rec["gone_at"] = "lapsed", "2026-11-18T00:00:00Z"
        self.logs[identity].append({"seq": len(self.logs[identity]) + 1, "event": "gone", "generation": rec["generation"],
                                    "holder_gone": dict(holder_gone)})
        return {**self._view(rec), "recorded": True}

    def renew(self, identity: str, generation: int, client_version: str | None = None) -> dict[str, Any]:
        self.calls.append(("renew", (identity, generation)))
        self._maybe_refuse("renew")
        rec = self._get(identity)
        if rec["generation"] != generation:
            raise _refuse(409, "generation_superseded", "superseded")
        return self._view(rec)

    def bind(self, identity: str, event: str, expected_generation: int, evidence: Mapping[str, Any] | None = None, *,
             token: Any = None) -> dict[str, Any]:
        ev = dict(evidence or {})
        self.calls.append(("bind", (identity, event, expected_generation, ev)))
        if token is not None:
            self.bind_tokens.append((event, token))      # what a refresh or fork carried (engine 0.1.198); not a call
        rec = self._get(identity)
        if rec["generation"] != expected_generation:
            code = "generation_superseded" if event in ("clear", "fork", "refresh", "end") and expected_generation < rec["generation"] else "generation_conflict"
            raise _refuse(409, code, "stale generation")
        cur = rec["holder"]
        held = rec["state"] in ("live", "lapsed")
        noop = False
        if event in ("start", "resume", "takeover"):
            same = held and cur and all(cur.get(k) == ev.get(k) for k in ("host", "pid", "started_at"))
            if not same:
                if event != "takeover" and held and not ev.get("holder_gone"):
                    raise _refuse(409, "held", "the identity is held by another process", holder=cur)
                rec["generation"] += 1
            rec["state"], rec["gone_at"] = "live", None
            rec["holder"] = {k: ev.get(k) for k in ("host", "pid", "started_at", "proc_start", "session_id", "pane")}
            rec["lease"] = ev.get("lease") or "leased"
            if ev.get("session_id"):
                self.sessions[ev["session_id"]] = (identity, rec["generation"])
        elif event == "end":
            if rec["state"] == "ended":
                noop = True
            elif not held:
                raise _refuse(409, "not_held", f"the identity is {rec['state']}")
            else:
                rec["state"], rec["holder"] = "ended", None
        elif event == "release":
            if rec["kind"] != "seat":
                raise _refuse(400, "not_a_seat", "release frees a seat; use end for an ephemeral identity")
            if not held:
                noop = True
            else:
                self_release = cur and all(cur.get(k) == ev.get(k) for k in ("host", "pid", "started_at")) and ev.get("pid") is not None
                verdict = ev.get("holder_gone")
                other_host_free = (rec["reserved_until"] is not None and ev.get("host") not in (None, cur.get("host"))
                                   and self.now() >= float(rec["reserved_until"]))
                if not (self_release or verdict or rec["gone_at"] or other_host_free):
                    raise _refuse(409, "held", "the identity is held by another process", holder=cur)
                rec["state"], rec["holder"], rec["gone_at"] = "unheld", None, None
        elif event == "refresh":
            pass
        else:
            raise _refuse(400, "bad_event", event)
        self.logs[identity].append({"seq": len(self.logs[identity]) + 1, "event": event, "generation": rec["generation"],
                                    **{k: ev.get(k) for k in ("host", "pid", "started_at", "session_id", "client_version")}})
        out = {**self._view(rec), "event": event, "noop": noop}
        if event in ("start", "resume", "takeover", "refresh"):
            out["identity_token"] = self._token(identity, rec["generation"])
            out["identity_token_expires_in_seconds"] = 3600
        return out

    # ── work and memberships (RDR-253 Step 6) ───────────────────────────────

    def _work_row(self, identity: str, kind: str, ref: str) -> dict[str, Any]:
        state, at = self.work[(identity, kind, ref)]
        return {"identity": identity, "kind": kind, "ref": ref, "state": state, "updated_at": at}

    def put_work(self, identity: str, kind: str, ref: str, state: str = "running") -> dict[str, Any]:
        self.calls.append(("put_work", (identity, kind, ref, state)))
        self._maybe_refuse("put_work")
        self._get(identity)
        if kind not in ("watcher", "wait", "labrun", "worktree", "tmux", "agent"):
            raise _refuse(400, "bad_kind", "kind must be one of ['agent', 'labrun', 'tmux', 'wait', 'watcher', 'worktree']")
        if state not in ("running", "done"):
            raise _refuse(400, "bad_state", "state must be running or done")
        if (identity, kind, ref) not in self.work and sum(1 for k in self.work if k[0] == identity) >= 200:
            raise _refuse(409, "work_limit", f"{identity} already holds 200 work rows")
        self.work[(identity, kind, ref)] = (state, float(self.now()))
        return self._work_row(identity, kind, ref)

    def remove_work(self, identity: str, kind: str, ref: str) -> dict[str, Any]:
        self.calls.append(("remove_work", (identity, kind, ref)))
        removed = int(self.work.pop((identity, kind, ref), None) is not None)
        return {"identity": identity, "kind": kind, "ref": ref, "removed": removed}

    def work_of(self, identity: str) -> list[dict[str, Any]]:
        self.calls.append(("work_of", (identity,)))
        self._get(identity)
        return [self._work_row(*k) for k in sorted(self.work) if k[0] == identity]

    def work_owners(self, kind: str, ref: str) -> list[dict[str, Any]]:
        self.calls.append(("work_owners", (kind, ref)))
        return [self._work_row(*k) for k in sorted(self.work) if k[1:] == (kind, ref)]

    def join_group(self, identity: str, group: str, role: str, *, as_identity: str | None = None) -> dict[str, Any]:
        self.calls.append(("join_group", (identity, group, role, as_identity)))
        self._get(identity)
        self._fence(identity, as_identity)
        if role not in ("leader", "member"):
            raise _refuse(400, "bad_role", "role must be leader or member")
        self.member_of.setdefault(identity, {})[group] = role
        return {"identity": identity, "group": group, "role": role, "joined_at": "2026-11-18T00:00:00Z"}

    def leave_group(self, identity: str, group: str, *, as_identity: str | None = None) -> dict[str, Any]:
        self.calls.append(("leave_group", (identity, group, as_identity)))
        self._get(identity)
        self._fence(identity, as_identity)
        removed = int(self.member_of.get(identity, {}).pop(group, None) is not None)
        return {"identity": identity, "group": group, "removed": removed}

    def memberships(self, identity: str) -> list[dict[str, Any]]:
        self.calls.append(("memberships", (identity,)))
        self._get(identity)
        return [{"identity": identity, "group": g, "role": r, "joined_at": "2026-11-18T00:00:00Z"}
                for g, r in self.member_of.get(identity, {}).items()]

    def roster(self, group: str) -> list[dict[str, Any]]:
        self.calls.append(("roster", (group,)))
        return [{"identity": i, "group": group, "role": g[group], "joined_at": "2026-11-18T00:00:00Z"}
                for i, g in self.member_of.items() if group in g]

    def _fence(self, identity: str, as_identity: str | None) -> None:
        """The engine's ownership fence on memberships: the writer's token names the identity, or the write is refused."""
        as_identity = self.caller_token if as_identity is None else as_identity
        if as_identity is None:
            raise _refuse(403, "identity_token_required", "this write needs the identity's token")
        if as_identity != identity:
            raise _refuse(403, "not_owner", f"{as_identity} does not own {identity}")
