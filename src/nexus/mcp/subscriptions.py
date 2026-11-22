# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-211 Phase 1 Step 3 (bead nexus-rplay.11): the session MCP server's
per-session subscription set -- what the lifespan waiter
(:mod:`nexus.mcp.channel`) parks a single ``wait`` call on -- and, kept here
for the aura loader, the reader of Claude Code's session registry
(:func:`resolve_session_entry`).

The set holds two kinds of DELIVERED entry (no cursor: since bead
nexus-vsipz a mailbox's position lives on the engine's row stamp, and since
bead nexus-q82tk a board's lives in the engine's per-subscriber delivery
row):

- ``mailbox/<identity id>`` while the session is bound to an identity (RDR-253
  Phase 2: mail goes to the identity, never to a session id), never removable
  (it is the floor's own address). None while the session is unbound.
- up to :data:`MAX_BOARD_TOPICS` ``board/<topic>`` subspaces.

A ``mailbox/<name>`` subscribe call is refused: the ``directory/<name>`` name
lease it used to arm (RDR-208) and the lease thread that renewed it are gone
(RDR-253 Phase 2, bead nexus-hczdo.2.1). Nothing in this module writes to the
engine.

Persisted in T1 scratch keyed by session id (see :func:`load`/:func:`persist`),
so a ``/resume`` (same session id, a fresh process) restores the list and a
``/clear`` (a new session id) starts clean -- T1 itself is already
session-scoped (``T1Database.list_entries`` filters on its own bound
``session_id``), so a fresh load under a different session id simply never
sees the old rows.

A mutation bumps :attr:`SubscriptionSet.version` and calls every
registered listener with ``self`` (:meth:`SubscriptionSet.add_listener`),
so the waiter can cancel its parked ``wait`` and re-issue it with the new
list (Approach item 6, Technical Design "Waiting").
"""
from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from nexus.db.t2.http_tuple_store import SchemaViolationError
from nexus.tuple_templates import resolve_tuple_template

_log = structlog.get_logger(__name__)


# ── The session's own name, from Claude Code's session registry (nexus-u839f) ─


def _claude_sessions_dir() -> Path:
    """Claude Code's per-process session registry, ``<config>/sessions``.
    ``CLAUDE_CONFIG_DIR`` is honoured the way Claude Code honours it. One definition, shared with the identity
    holder check (RDR-253 Step 3, :func:`nexus.identity.holder.claude_sessions_dir`)."""
    from nexus.identity.holder import claude_sessions_dir  # noqa: PLC0415 — deferred; the identity package is a leaf

    return claude_sessions_dir()


def resolve_session_name(
    session_id: str,
    *,
    sessions_dir: Path | None = None,
    pid_alive: Callable[[int], bool] | None = None,
    log_miss: bool = True,
) -> str | None:
    """The name of :func:`resolve_session_entry`'s entry, or None."""
    entry = resolve_session_entry(session_id, sessions_dir=sessions_dir, pid_alive=pid_alive, log_miss=log_miss)
    return entry[0] if entry is not None else None


def resolve_session_entry(
    session_id: str,
    *,
    sessions_dir: Path | None = None,
    pid_alive: Callable[[int], bool] | None = None,
    log_miss: bool = True,
) -> tuple[str, int] | None:
    """The name and pid of the registry entry for *session_id* (RDR-249 reads the pid to tell a ``claude -p``
    child carrying its parent's session id from the session itself, nexus-mdbds.11.1).

    The name Claude Code shows for the live interactive session whose id
    is *session_id*, read from ``<config>/sessions/<pid>.json`` (fields
    ``sessionId``, ``name``, ``kind``, ``pid``, ``updatedAt``). None for any
    miss: no directory, an unreadable or malformed file, no matching entry,
    a non-interactive entry (a ``claude -p`` child writes its own entry under its own session id, so it never
    matches a parent's id; observed 2026-11-15 with kind ``interactive``), a dead pid, or no name.
    Never raises. Several live entries for one id (a resumed session beside
    its old process's file) resolve to the most recently updated, whichever
    file name sorts first.

    Only the five fields above are read; other keys in the files are
    ignored and the ``*.key`` files beside them are never opened.

    This reads Claude Code's undocumented session registry, so a format
    change shows up here as a miss on a directory that exists. With
    *log_miss* that case logs one INFO ``session_name_unresolved`` line
    (session id, directory, count of ``*.json`` files read; never their
    contents). A missing directory logs nothing: the registry is simply
    absent (another harness). A caller that polls passes ``log_miss=False``
    after its first miss so the 60 s re-resolve does not repeat the line."""
    if not session_id:
        return None
    if pid_alive is None:
        from nexus.daemon.service_registry import pid_alive  # noqa: PLC0415 — the one liveness probe (test_pid_alive_single_probe_lint); deferred, stdlib-heavy module
    base = sessions_dir if sessions_dir is not None else _claude_sessions_dir()
    best: tuple[float, str, int] | None = None
    read = 0
    try:
        files = sorted(base.glob("*.json"))
    except OSError:
        files = []
    for path in files:
        read += 1
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("sessionId") != session_id:
                continue
            if data.get("kind") != "interactive":
                continue
            name, pid = data.get("name"), data.get("pid")
            if not isinstance(name, str) or not name.strip() or isinstance(pid, bool) or not isinstance(pid, int):
                continue
            if not pid_alive(pid):
                continue
            updated = data.get("updatedAt")
            rank = float(updated) if isinstance(updated, (int, float)) and not isinstance(updated, bool) else 0.0
        # RecursionError: json.loads on a deeply nested file; OverflowError:
        # pid_alive(10**30) and float(10**400). A resolver that raised would
        # cost the caller its whole arm, so every per-file failure is a skip.
        except (OSError, ValueError, TypeError, OverflowError, RecursionError):
            continue
        if best is None or rank >= best[0]:
            best = (rank, name.strip(), pid)
    if best is None:
        if log_miss and _dir_exists(base):
            _log.info(
                "session_name_unresolved",
                session_id=session_id, sessions_dir=str(base), files_read=read,
            )
        return None
    return best[1], best[2]


def _dir_exists(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


# ── The subscription set (RDR-211 Technical Design "Subscriptions") ────────

#: "spike 3 tested three, and the bound guards the engine's per-call work,
#: not slots" (RDR-211 Technical Design).
MAX_BOARD_TOPICS = 32


def _take_enabled(templates: list[dict[str, Any]], subspace: str) -> bool:
    """Resolve *subspace* against the ``registry()`` wire's ``templates``
    list and return the matching template's ``take.enabled``.

    The matching is :func:`nexus.tuple_templates.resolve_tuple_template`, the
    one client-side resolver shared with ``nexus.health`` and ``nx tuple
    dictionary`` (it lives in a standard-library-only module so an MCP tool
    call never pulls in ``nexus.health``). Defaults to ``True`` (assume
    claimable) when nothing resolves, matching ``nexus.health``'s own default:
    an unmatched subspace must never be silently treated as safe to subscribe.
    """
    template = resolve_tuple_template(templates, subspace)
    if template is None:
        return True
    return bool(template.get("take", {}).get("enabled", True))


@dataclass
class SubscriptionSet:
    """One session's subscription list.

    ``identity_mailbox`` (``mailbox/<identity>``, RDR-253 Phase 2) is implicit and never stored in ``_board`` or
    removable while an identity is bound; it is the only mailbox ever delivered, and there is none while the
    session is unbound (mail goes to an identity, never to a session id). ``_board`` is the subscribed ``board/<topic>``
    subspaces in subscription order, each mapped to the UTC ISO-8601
    time it was subscribed (bead nexus-zxthy) or ``None`` for a record
    written before that. The value is a START POSITION, not a delivery
    cursor: the per-topic cursor this dict once held moved to the engine
    (bead nexus-q82tk), and the engine's per-subscriber stamp still
    decides what was delivered. A fresh subscriber starts at its subscribe
    time minus ``DEFAULT_BOARD_START_SKEW_S`` (30 s, a clock-skew and
    commit-race margin sent to the engine as the ``since`` watermark),
    the way a new Kafka group, Redis Streams ``$`` consumer or NATS
    ``DeliverNew`` consumer does; posts older than that are read with
    ``tuple_rd``, never pushed (T2 nexus/zxthy-board-subscription-design-
    2026-09-27).
    """

    session_id: str
    #: This session's identity, or None. While set, ``mailbox/<identity>`` is the one mailbox delivered (RDR-253
    #: Phase 2); None delivers no mailbox. Not persisted: it is read from the token file at each load (:func:`load`).
    identity: str | None = None
    _board: dict[str, str | None] = field(default_factory=dict)
    #: RDR-249 TD3 (S7, nexus-mdbds.11.1): aura-owned board topics, each
    #: mapped to its start position and the names of the auras holding it.
    #: Delivered (:meth:`entries`) and counted against MAX_BOARD_TOPICS, but
    #: never in :meth:`board_topics`, never removed by :meth:`unsubscribe`
    #: and never persisted: the server makes them again at each load.
    _aura_board: dict[str, tuple[str, set[str]]] = field(default_factory=dict, repr=False, compare=False)
    version: int = 0
    _listeners: list[Callable[["SubscriptionSet"], None]] = field(default_factory=list, repr=False)
    #: The identity listener :func:`_follow_identity` registered for this set, so :func:`reset_cache` can remove it.
    _identity_listener: Callable[[str | None], None] | None = field(default=None, repr=False, compare=False)

    @property
    def identity_mailbox(self) -> str | None:
        return f"mailbox/{self.identity}" if self.identity else None

    def set_identity(self, identity: str | None) -> None:
        """Deliver ``mailbox/<identity>`` (or stop, with None); the waiter re-reads :meth:`entries` on the bump."""
        identity = identity or None
        if identity == self.identity:
            return
        self.identity = identity
        self._bump()

    # ── Observer (Approach item 6, Technical Design "Waiting") ──────────────

    def add_listener(self, callback: Callable[["SubscriptionSet"], None]) -> None:
        """Register *callback* to be called with ``self`` after every
        mutation (subscribe/unsubscribe). The not-yet-built lifespan
        waiter (bead nexus-rplay.10) uses this to cancel its parked
        ``wait`` and re-issue it with the new list."""
        self._listeners.append(callback)

    def remove_listener(self, callback: Callable[["SubscriptionSet"], None]) -> None:
        try:
            self._listeners.remove(callback)
        except ValueError:
            pass

    def _bump(self) -> None:
        self.version += 1
        for callback in list(self._listeners):
            callback(self)

    # ── Mutation ─────────────────────────────────────────────────────────

    def subscribe(
        self,
        subspace: str,
        *,
        templates: list[dict[str, Any]],
        store_factory: Callable[[], Any] | None = None,
        state_dir: Path | None = None,
    ) -> None:
        """Add *subspace* to this set.

        ``board/<topic>``: added, subject to :data:`MAX_BOARD_TOPICS`. This session's own identity mailbox is
        already delivered, so subscribing it is a no-op; any other ``mailbox/<address>`` is refused (RDR-253 Phase 2:
        mail goes to the identity, and the ``mailbox/<name>`` name lease is gone). Anything else that resolves to a
        take-enabled template (a queue or a lock) is refused naming ``in``, since those are never delivered;
        anything else is refused as not a board topic.

        *store_factory* and *state_dir* are accepted and ignored: they are the call shape from when a name lease
        needed an engine handle and a registration file, kept so no caller changes.
        """
        if not subspace:
            raise SchemaViolationError("subspace must not be empty")
        if subspace.startswith("board/"):
            self._subscribe_board(subspace)
            return
        if subspace.startswith("mailbox/"):
            if not subspace[len("mailbox/"):]:
                raise SchemaViolationError("mailbox subspace must include a name")
            if subspace == self.identity_mailbox:
                return  # already delivered
            raise SchemaViolationError(
                f"{subspace!r} is not subscribable: mail goes to this session's identity mailbox"
                + (f" ({self.identity_mailbox})" if self.identity_mailbox else " once the session is bound")
                + ", which is delivered with no subscribe call, and a session id or name is not an address "
                "(RDR-253 Phase 2)"
            )
        if _take_enabled(templates, subspace):
            raise SchemaViolationError(
                f"{subspace!r} is a take-enabled subspace, worked with `in` and never "
                "delivered; tuple_subscribe accepts only board topics"
            )
        raise SchemaViolationError(f"{subspace!r} is not a board topic; refused")

    def board_topics(self) -> list[str]:
        """The subscribed board subspaces, sorted (nexus-1d6pz.11.5: the
        channel status record carries them, so the Stop-hook wake guard can
        see a covering ``board/ci/*--done`` subscription)."""
        return sorted(self._board)

    def aura_topics(self) -> list[str]:
        """The aura-owned board subspaces, sorted (RDR-249 TD3)."""
        return sorted(self._aura_board)

    def session_owns(self, topic: str) -> bool:
        """Whether the session itself subscribes *topic*: its rows are then
        announced to the agent (RDR-249 TD3)."""
        return topic in self._board

    def aura_only(self, topic: str) -> bool:
        """Whether only an aura holds *topic*: its rows are evaluated and not
        announced (RDR-249 TD3)."""
        return topic in self._aura_board and topic not in self._board

    def _delivered_topic_count(self) -> int:
        return len(self._board.keys() | self._aura_board.keys())

    def _subscribe_board(self, topic: str) -> None:
        if topic in self._board:
            return  # idempotent
        if topic not in self._aura_board and self._delivered_topic_count() >= MAX_BOARD_TOPICS:
            held = len(self._aura_board.keys() - self._board.keys())
            raise SchemaViolationError(
                f"at most {MAX_BOARD_TOPICS} board topics may be subscribed at once"
                + (f" ({held} held by auras)" if held else "")
                + f"; refused before adding {topic!r}"
            )
        self._board[topic] = datetime.now(UTC).isoformat()
        self._bump()

    def subscribe_aura(self, aura: str, topic: str) -> None:
        """Hold *topic* for the aura named *aura* (RDR-249 TD3, S7). The
        topic is delivered and counts against :data:`MAX_BOARD_TOPICS`, and
        an aura that would pass the cap is refused naming it. Idempotent per
        (aura, topic). The session's own subscription to the same topic is
        separate: neither removes the other."""
        if not topic.startswith("board/") or topic == "board/":
            raise SchemaViolationError(f"aura {aura!r}: {topic!r} is not a board topic; refused")
        held = self._aura_board.get(topic)
        if held is not None:
            held[1].add(aura)
            return
        if topic not in self._board and self._delivered_topic_count() >= MAX_BOARD_TOPICS:
            raise SchemaViolationError(
                f"aura {aura!r} refused: at most {MAX_BOARD_TOPICS} board topics may be "
                f"subscribed at once, and {topic!r} would pass that"
            )
        self._aura_board[topic] = (datetime.now(UTC).isoformat(), {aura})
        if topic not in self._board:
            self._bump()

    def release_auras(self) -> None:
        """Drop every aura-owned hold (a reload or a session handoff makes
        them again). The session's own subscriptions are untouched."""
        if not self._aura_board:
            return
        dropped_delivered = any(t not in self._board for t in self._aura_board)
        self._aura_board.clear()
        if dropped_delivered:
            self._bump()

    def unsubscribe(self, subspace: str) -> None:
        """Remove *subspace*. The identity mailbox can never be
        unsubscribed -- it is the floor's address. Unsubscribing something not
        currently subscribed is a silent no-op, not a refusal."""
        if self.identity_mailbox is not None and subspace == self.identity_mailbox:
            raise SchemaViolationError(
                "the identity mailbox cannot be unsubscribed; it is the floor's address"
            )
        if subspace in self._board:
            del self._board[subspace]
            self._bump()

    def entries(self) -> list[dict[str, Any]]:
        """This set's DELIVERED subspaces, in the order
        ``tuple_subscriptions`` renders them: the identity mailbox first
        (when one is bound), then board topics. No cursor: delivery position lives in the
        engine for every shape (beads nexus-vsipz, nexus-q82tk). A board
        entry carries ``since``, the topic's subscribe time (bead
        nexus-zxthy): the waiter pushes only posts created after it
        minus ``DEFAULT_BOARD_START_SKEW_S`` (30 s), so a post made in
        that margin before the subscribe is pushed too. This is the one place
        :mod:`nexus.mcp.channel`'s waiter reads to decide what to wait on
        (:meth:`~nexus.mcp.channel.ChannelWaiter._build_specs`)."""
        out: list[dict[str, Any]] = []
        if self.identity_mailbox:
            out.append({"subspace": self.identity_mailbox})
        # Copies: a tool thread reads this while the loop can add an aura topic.
        for topic, since in list(self._board.items()):
            entry: dict[str, Any] = {"subspace": topic}
            held = self._aura_board.get(topic)
            if since and held is not None:
                # One delivered entry for a topic both hold: the earlier start
                # position, so neither owner misses a row (both share the
                # session's per-subscriber stamp, so nothing is delivered twice).
                since = min(since, held[0])
            if since:
                entry["since"] = since
            out.append(entry)
        for topic, (since, _owners) in list(self._aura_board.items()):
            if topic not in self._board:
                out.append({"subspace": topic, "since": since, "held_by": "aura"})
        return out

    # ── Persistence (T1, keyed by session id) ───────────────────────────

    def to_json(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "board": list(self._board),
            # bead nexus-zxthy: the start position per topic, kept apart
            # from "board" so a reader of the older list shape still works.
            "board_since": {t: s for t, s in self._board.items() if s},
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "SubscriptionSet":
        obj = cls(session_id=data["session_id"])
        # A record written before RDR-253 Phase 2 may carry a "leased_name" (and, before RDR-208 Phase 3, the
        # retired "instance_mailbox"/"instance_name" keys): none is read, the name lease is gone.
        # A record written before bead nexus-q82tk carries a dict of
        # topic -> cursor; only the topics survive a `/resume` across that
        # change, which is exactly what the engine-side stamp makes
        # sufficient.
        board = data.get("board") or []
        topics = list(board.keys()) if isinstance(board, dict) else list(board)
        since = data.get("board_since") or {}
        # A record written before bead nexus-zxthy has no start position:
        # None delivers everything, the behaviour that record was written
        # under, rather than inventing a position at load time.
        obj._board = {t: (since.get(t) or None) for t in topics}
        return obj


# ── T1 persistence helpers ───────────────────────────────────────────────

#: Marks the one T1 entry per session that holds the serialized
#: subscription set. T1 has no upsert-by-title (unlike T2's memory_put),
#: so :func:`persist` deletes any prior tagged entry before writing a
#: fresh one -- at most one such row per session at any time.
_TAG = "rdr211-subscriptions"


def load(t1: Any, session_id: str, *, store_factory: Callable[[], Any] | None = None) -> "SubscriptionSet":
    """Build a FRESH :class:`SubscriptionSet` for *session_id* from
    whatever this session's T1 already holds (a ``/resume``), or an empty
    set (a ``/clear`` — a new session id has
    nothing to find, since T1 itself is already session-scoped).

    Board topics only; the identity is read from the token file at each load (:func:`_token_identity`).
    *store_factory* is accepted for the call shape :func:`get_or_load` passes and is unused.
    """
    for entry in t1.list_entries():
        tags = (entry.get("tags") or "").split(",")
        if _TAG not in tags:
            continue
        try:
            data = json.loads(entry["content"])
        except (TypeError, ValueError, KeyError):
            continue
        if data.get("session_id") != session_id:
            continue
        obj = SubscriptionSet.from_json(data)
        obj.identity = _token_identity()
        return obj
    return SubscriptionSet(session_id=session_id, identity=_token_identity())


def _token_identity() -> str | None:
    """This process's identity from its leased token file (RDR-253 Step 5), or None (no binding, an old engine)."""
    try:
        from nexus.identity.token_header import current  # noqa: PLC0415 -- deferred: MCP startup cost

        record = current()
    except Exception:  # noqa: BLE001 -- no identity: no mailbox is delivered
        return None
    return _valid_identity(str(record.identity) if record is not None else "")


def persist(t1: Any, subs: "SubscriptionSet") -> None:
    """Write *subs*'s current state back to T1, replacing any prior entry
    for its session (delete-then-put — T1 has no upsert-by-title)."""
    for entry in t1.list_entries():
        tags = (entry.get("tags") or "").split(",")
        if _TAG in tags:
            t1.delete(entry["id"])
    t1.put(content=json.dumps(subs.to_json()), tags=_TAG)


#: Process-lifetime cache, keyed by session id, so repeated MCP tool calls
#: in the SAME process reuse the SAME SubscriptionSet (and the one identity
#: listener it registered) -- :func:`load` alone, called fresh every time,
#: would leak one listener per call.
_CACHE: dict[str, SubscriptionSet] = {}
_CACHE_LOCK = threading.Lock()


def get_or_load(t1: Any, session_id: str, *, store_factory: Callable[[], Any]) -> SubscriptionSet:
    """The cached :class:`SubscriptionSet` for *session_id* in THIS
    process, loading it on first use."""
    with _CACHE_LOCK:
        obj = _CACHE.get(session_id)
        if obj is None:
            obj = load(t1, session_id, store_factory=store_factory)
            _CACHE[session_id] = obj
            _follow_identity(obj)
        return obj


def _follow_identity(subs: SubscriptionSet) -> None:
    """Keep *subs*'s identity mailbox in step with this server's lease. The set usually loads before
    the token file exists (the MCP server and the bind hook start together), so the lease tells it when it adopts
    an identity, and again on a switch or take-over."""
    try:
        from nexus.mcp.identity_lease import add_identity_listener  # noqa: PLC0415 -- deferred: MCP import cost

        def follow(identity: str | None) -> None:
            subs.set_identity(_valid_identity(identity))

        subs._identity_listener = follow  # noqa: SLF001 -- the set owns its listener, reset_cache removes it
        add_identity_listener(follow)
    except Exception as exc:  # noqa: BLE001 -- no lease module path: no identity mailbox follows
        _log.debug("identity_follow_unavailable", error=str(exc))


def _valid_identity(identity: str | None) -> str | None:
    return identity if identity and re.fullmatch(r"i[0-9]+", identity) else None


def reset_cache(session_id: str | None = None) -> None:
    """Test/teardown hook: drop the process cache and each dropped set's identity listener. *session_id* drops just
    that entry; omitted drops everything."""
    with _CACHE_LOCK:
        if session_id is None:
            targets = list(_CACHE.values())
            _CACHE.clear()
        else:
            obj = _CACHE.pop(session_id, None)
            targets = [obj] if obj is not None else []
    for obj in targets:
        if obj._identity_listener is not None:  # noqa: SLF001
            try:
                from nexus.mcp.identity_lease import remove_identity_listener  # noqa: PLC0415 -- deferred: MCP import cost

                remove_identity_listener(obj._identity_listener)  # noqa: SLF001
            except Exception as exc:  # noqa: BLE001 -- teardown never fails on the lease module
                _log.debug("identity_unfollow_unavailable", error=str(exc))
            obj._identity_listener = None  # noqa: SLF001
