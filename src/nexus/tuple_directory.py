# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-208 Phase 2 Step 2 (bead nexus-galkv.10): session-directory name
resolution -- resolving a `mailbox_send` `to` argument to a concrete
`mailbox/<address>` + `address_kind`, and the default `from` sender
identity.

Kept separate from `nexus.mcp.core` (rather than inlined into the
`mailbox_send` tool) so RDR-208 Phase 2 Step 4's `nx tuple directory`
CLI verb (bead nexus-galkv.11) can reuse the SAME resolution logic
instead of a second, independently-drifting copy.

`to` resolution (RDR-208, Sam's decision 1 --
T2 nexus_rdr/208-decision-gate-2026-09-14):
  - a session-id shape (a UUID, the shape `nexus.session`'s
    `str(uuid4())` session ids take) resolves directly,
    `address_kind="session"`;
  - an agent-id shape ("a" + 16 lowercase hex chars -- the ONLY shape the
    Agent tool's own opaque `agent_id` can take, confirmed against this
    session's own claimant id and the fixture id
    `tests/hooks/test_subagent_stop_hook.py:288` uses) resolves directly,
    `address_kind="agent"` -- NEVER through the directory;
  - anything else is a NAME: every live row of `directory/<name>` is read
    ONCE (`list_directory_entries`, paged with the `since` cursor until a
    short page) and classified by the PURE `classify_directory_holders`
    over that one list -- never a second read -- so a lapse or a re-nonce
    between two reads can never make a caller's printed state and its
    verdict describe different moments. Zero holders is a
    :class:`DirectoryResolutionError` naming the name; one holder
    resolves to that session (several live rows of the SAME session --
    a re-armed watcher's new nonce beside its old row -- are one holder,
    not a conflict); more than one distinct holder is a
    :class:`DirectoryResolutionError` naming every holder, and nothing is
    written by a caller that raises before ever writing.

`from` resolution mirrors the bead's rejected-alternatives reasoning: the
default is the tuple-watch session marker for this MCP server's claude
ancestor (`nexus.session_marker._read_session_marker`, written synchronously
by SessionStart on every source -- see that module's own docstrings),
falling back to `NX_T1_SESSION_ID`, then `NX_SESSION_ID` (the one name a
host with no Claude Code session can set in a self-configured MCP server
entry; nexus-zl1jv). `NX_SESSION_ID` must be a UUID, checked like
`from_address`. `resolve_active_session_id()` and a bare `NX_T1_SESSION_ID`
read are deliberately NOT used here: the former returns the spawn-time
`CLAUDE_CODE_SESSION_ID`, stale after an in-process `/clear`; the latter
lags the T2 re-lease mint. The marker sits above both variables. A caller
resolving none is refused rather than stamping a `from` that might already
be stale.
"""
from __future__ import annotations

import os
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nexus.db.limits import MAX_QUERY_RESULTS

#: "a" + 16 lowercase hex chars -- the ONLY shape the Agent tool's own
#: opaque `agent_id` can take (it has no `name` parameter to produce
#: anything else). Confirmed against two REAL ids, not just the fixture:
#: this worktree's own claimant id ("a0b56337bc52ed773", from the
#: SubagentStart hook's "Claimant id:" line) and
#: `tests/hooks/test_subagent_stop_hook.py:288`'s fixture id
#: ("a16b397f79df79c42").
_AGENT_ID_RE = re.compile(r"^a[0-9a-f]{16}$")
#: The names a session can lease under `directory/<name>` (the lease side,
#: ``nexus.mcp.subscriptions``, uses this same pattern). A `to` outside it can
#: never resolve, so ``resolve_send_address`` refuses it before any read.
SAFE_NAME_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class DirectoryResolutionError(RuntimeError):
    """`to` or `from_address` could not be resolved to exactly one
    address. Raised before any tuple is written, so a caller that lets
    this propagate has written nothing."""


def looks_like_session_id(value: str) -> bool:
    """True when *value* parses as a UUID."""
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def looks_like_agent_id(value: str) -> bool:
    """True when *value* is "a" + 16 lowercase hex chars."""
    return bool(_AGENT_ID_RE.fullmatch(value))


_IDENTITY_ID_RE = re.compile(r"^i[0-9]+$")


def looks_like_identity_id(value: str) -> bool:
    """True when *value* is an identity id, ``i`` and digits (RDR-253)."""
    return bool(_IDENTITY_ID_RE.fullmatch(value or ""))


@dataclass(frozen=True)
class SendTarget:
    """Where a ``mailbox_send`` goes: ``mailbox/<address>`` with ``address_kind``, and the engine's notice when the
    name was a retired alias still inside its 7-day grace (RDR-253 Step 5)."""

    address: str
    address_kind: str
    notice: str = ""


#: ``alias(name)`` is ``IdentityClient.alias``: GET /v1/identity/alias/<name>.
AliasLookup = Callable[[str], Mapping[str, Any]]
#: ``holder(session id)`` is the ``(identity id, kind)`` the session is bound to, or None when it is bound to none
#: (an old client) or the answer cannot be had; ``kind`` is ``seat``, ``ephemeral`` or ``delegated``.
HolderLookup = Callable[[str], "tuple[str, str] | None"]


def list_directory_entries(name: str, tuples: Any) -> list[Any]:
    """Every live row of `directory/<name>`, paged with the `since` cursor
    until a short page. Shared ground truth for :func:`resolve_send_address`
    and the `nx tuple directory NAME` CLI verb (bead nexus-galkv.11), so the
    two can never disagree about what counts as a live holder -- one page
    alone cannot rule out a second holder past the page boundary.

    *tuples* is an `HttpTupleStore` (or anything exposing the same `rd`
    signature) -- passed in rather than resolved here so the caller
    controls which T2 client/transaction this read runs against.
    """
    subspace = f"directory/{name}"
    since: tuple[str, str] | None = None
    rows: list[Any] = []
    while True:
        page = tuples.rd(subspace, {"name": name}, n=MAX_QUERY_RESULTS, since=since)
        rows.extend(page)
        if len(page) < MAX_QUERY_RESULTS:
            break
        last = page[-1]
        since = (last.created_at, last.id)
    return rows


def classify_directory_holders(name: str, entries: list[Any]) -> tuple[str, str]:
    """Pure classifier over an ALREADY-FETCHED `directory/<name>` entries
    list (gate audit round B item 1, code review
    T2 nexus/rdr-208-phase2b-cre-2026-09-14): takes no store and does no I/O,
    so both :func:`resolve_send_address` and the `nx tuple directory NAME`
    CLI verb read `list_directory_entries` exactly ONCE and classify that
    SAME list, instead of each reading independently and risking a lapse or
    a re-nonce between two reads making the two callers describe different
    moments.

    Zero distinct `session_id` dims among *entries* is a
    :class:`DirectoryResolutionError` naming *name*; more than one distinct
    holder is a :class:`DirectoryResolutionError` naming every holder.
    Several entries for the SAME session (a re-armed watcher's new nonce
    beside its old row) are one holder, not a conflict.
    """
    holders: set[str] = set()
    for row in entries:
        session_id = (row.dims or {}).get("session_id")
        if session_id:
            holders.add(session_id)

    if not holders:
        raise DirectoryResolutionError(f"no live holder for name {name!r}")
    if len(holders) > 1:
        ids = ", ".join(sorted(holders))
        raise DirectoryResolutionError(
            f"name {name!r} is held by more than one session ({ids}); resend to one of these session ids directly"
        )
    return next(iter(holders)), "session"


def resolve_send_address(to: str, tuples: Any) -> tuple[str, str]:
    """Resolve `mailbox_send`'s `to` into `(address, address_kind)`.

    A directory read that raises propagates unchanged: nothing about `to`
    was resolved, so the caller never reaches a write. Reads
    `list_directory_entries` exactly once and classifies that one list
    through :func:`classify_directory_holders` -- the same classifier the
    `nx tuple directory NAME` CLI verb calls over its own single read, so
    the two can never disagree.
    """
    if not to:
        raise DirectoryResolutionError("to must not be empty")
    if looks_like_session_id(to):
        return to, "session"
    if looks_like_agent_id(to):
        return to, "agent"
    if not SAFE_NAME_RE.fullmatch(to):
        # nexus-3ceda.7: the engine's answer for such a name was "no template registered for
        # subspace 'directory/<to>'", which sent agents to tools that do not exist.
        raise DirectoryResolutionError(
            f"{to[:64]!r} cannot be a session name: names are letters, digits, '.', '_' and '-', "
            "starting with a letter or digit. tuple_list(prefix=\"directory/\") lists the names "
            "sessions hold; or send to the session id (a UUID)."
        )

    return classify_directory_holders(to, list_directory_entries(to, tuples))


def resolve_send(
    to: str, tuples: Any, alias: AliasLookup | None = None, holder: HolderLookup | None = None,
) -> SendTarget:
    """RDR-253 Step 5: resolve ``to`` for a mixed fleet of old and new clients.

    An identity id goes straight to ``mailbox/<id>``. A name resolves through ``alias/<name>`` first, to learn the
    seat and any retired-alias notice, but a live ``identity_state`` does not say the receiver drains
    ``mailbox/<id>``: a Steps 3/4 client and an unleased Desktop binding are live too and read only their session
    mailbox. So the mail goes to the one live holder of ``directory/<name>``, in that holder's session mailbox
    (every new client drains ``mailbox/<session>`` as well, by dual subscription), as :func:`resolve_send_address`
    resolves it; two holders are refused. Only when nobody holds the name does the mail wait in ``mailbox/<id>``
    for the seat to bind. A name that is no seat's alias falls to the directory, but never reaches an ephemeral
    identity by its display name: the RDR reaches an ephemeral "by id only", and two launches with one display name
    would collide. When *holder* names the one holder as an ephemeral, the send is refused with its id; a holder it
    cannot place (an old client, an engine that does not answer) is reached as before. A retired alias inside its grace resolves with the engine's notice naming the new
    name; past it the engine answers 410 and the send is refused with "renamed to <new> on <date>". An unknown name,
    or an alias lookup that cannot be made (an engine without identity routes), is the directory's alone.
    """
    if looks_like_identity_id(to):
        return SendTarget(to, "identity")
    if alias is None or looks_like_session_id(to) or looks_like_agent_id(to) or not SAFE_NAME_RE.fullmatch(to or ""):
        return SendTarget(*resolve_send_address(to, tuples))
    from nexus.identity.bind_client import IdentityApiError  # noqa: PLC0415 -- deferred: MCP/CLI startup cost

    try:
        found = alias(to)
    except IdentityApiError as exc:
        if exc.status == 410:
            raise DirectoryResolutionError(str(exc.message or f"{to!r} was renamed")) from None
        if exc.status == 404:  # no seat has the name: the directory's, unless its holder is an ephemeral's display name
            return _directory_target(to, tuples, holder)
        return SendTarget(*resolve_send_address(to, tuples))
    except Exception:  # noqa: BLE001 -- no identity routes (an older engine) or unreachable: the directory decides
        return SendTarget(*resolve_send_address(to, tuples))
    identity = str(found.get("identity") or "")
    notice = str(found.get("notice") or "")
    if not looks_like_identity_id(identity):
        return SendTarget(*resolve_send_address(to, tuples))
    entries = list_directory_entries(to, tuples)
    if not any((row.dims or {}).get("session_id") for row in entries):
        return SendTarget(identity, "identity", notice)  # nobody holds the name: the seat reads it when it binds
    address, kind = classify_directory_holders(to, entries)  # two holders is still refused, naming both
    return SendTarget(address, kind, notice)


def _directory_target(to: str, tuples: Any, holder: HolderLookup | None) -> SendTarget:
    """``directory/<to>``'s one holder, refused when it is an ephemeral identity reached by its display name."""
    address, kind = resolve_send_address(to, tuples)
    if kind == "session" and holder is not None:
        try:
            bound = holder(address)
        except Exception:  # noqa: BLE001 -- a holder the engine cannot place is reached as before, never a failed send
            bound = None
        if bound is not None and bound[1] == "ephemeral":
            raise DirectoryResolutionError(
                f"{to!r} is the display name of the ephemeral identity {bound[0]}, which is reached by id only "
                f"(an ephemeral has no alias, and two launches can share a display name): send to {bound[0]}"
            )
    return SendTarget(address, kind)


def identity_holder_lookup() -> HolderLookup:
    """The identity a session is bound to, from the session index and the identity's record. The client is built,
    used and closed inside each lookup (as :func:`identity_alias_lookup`'s is); a session in no index, an engine
    without identity routes and a failed call all answer None."""

    def lookup(session_id: str) -> tuple[str, str] | None:
        from nexus.identity.bind_client import IdentityApiError, IdentityClient  # noqa: PLC0415 -- deferred: startup cost

        client = IdentityClient.from_config()
        try:
            identity = str(client.session(session_id).get("identity") or "")
            return (identity, str(client.show(identity).get("kind") or "")) if identity else None
        except IdentityApiError:
            return None
        finally:
            client.close()

    return lookup


def identity_alias_lookup() -> AliasLookup:
    """``IdentityClient.alias`` for this host's engine. The client is built, used and closed inside each lookup, so
    a send to a session or agent id (which :func:`resolve_send` never looks up) builds none, and none is left
    open. A host with no endpoint or credential raises from the lookup, which :func:`resolve_send` reads as "no
    identity routes here": the send then resolves through the directory alone, as an old client does."""

    def lookup(name: str) -> Mapping[str, Any]:
        from nexus.identity.bind_client import IdentityClient  # noqa: PLC0415 -- deferred: MCP/CLI startup cost

        client = IdentityClient.from_config()
        try:
            return client.alias(name)
        finally:
            client.close()

    return lookup


def identity_session_lookup() -> AliasLookup:
    """``IdentityClient.session`` for this host's engine: the session index, a session id in, ``{"identity": ...}`` out;
    the same index the engine reads to map a row's sender-session stamp. Built, used and closed inside each lookup, like
    :func:`identity_alias_lookup`. A session the index does not know raises ``IdentityApiError`` 404."""

    def lookup(session_id: str) -> Mapping[str, Any]:
        from nexus.identity.bind_client import IdentityClient  # noqa: PLC0415 -- deferred: MCP/CLI startup cost

        client = IdentityClient.from_config()
        try:
            return client.session(session_id)
        finally:
            client.close()

    return lookup


def identity_record_lookup() -> AliasLookup:
    """``IdentityClient.show`` for this host's engine: an identity id in, its record out (``kind``, and ``parent`` for a
    delegated identity). Built, used and closed inside each lookup, like :func:`identity_alias_lookup`."""

    def lookup(identity: str) -> Mapping[str, Any]:
        from nexus.identity.bind_client import IdentityClient  # noqa: PLC0415 -- deferred: MCP/CLI startup cost

        client = IdentityClient.from_config()
        try:
            return client.show(identity)
        finally:
            client.close()

    return lookup


def identity_from_not_own_message(value: str, own: str) -> str:
    """Refusal for an identity-shaped ``from_address`` that is not the sender's own identity (RDR-253 Step 5, the
    identity counterpart of :func:`from_address_not_own_message`)."""
    mine = own[:64] if own else "none resolved for this server"
    return (
        f"from_address {value[:64]!r} is an identity but not this server's own ({mine}): `from` names the sender, "
        "so an identity there must be the sending seat's own. Put the recipient in `to`; omit from_address to send "
        "as this session"
    )


def validate_from_address(value: str) -> str:
    """A caller-supplied `from_address` override must itself be a session
    id or an agent id -- a subagent sharing its parent's MCP server would
    otherwise stamp the parent's session id, so it passes its OWN agent
    id here instead."""
    if looks_like_session_id(value) or looks_like_agent_id(value) or looks_like_identity_id(value):
        return value
    raise DirectoryResolutionError(
        f"from_address {value!r} is neither a session id, an identity nor an agent id"
    )


def from_address_not_own_message(value: str, own_ids: list[str]) -> str:
    """Refusal for a session-shaped ``from_address`` that is not one of the
    calling server's own session ids (F3 step 1, nexus-2mv7y, JDR-002).
    Pinned by an equality test."""
    own = ", ".join(i[:64] for i in own_ids)
    return (
        f"from_address {value[:64]!r} is a session id but not this server's own "
        f"({own}): `from` names the sender, so a session id there must be the "
        "sending session's own. The usual slip is putting the recipient's id here; "
        "it belongs in `to`. Omit from_address to send as this session; a subagent "
        "passes its own agent id"
    )


def _own_session_ids(state_dir: Path | None, claude_pid: int | None) -> list[str]:
    """The ids this server may send as: the tuple-watch session marker, then
    the environment tier :func:`resolve_env_session_id` answers with
    (``NX_T1_SESSION_ID``, else ``NX_SESSION_ID``), without duplicates. The
    marker and ``NX_T1_SESSION_ID`` can differ across a ``/clear`` handoff
    (JDR-001), and both are this session's. ``NX_SESSION_ID`` counts only
    when ``NX_T1_SESSION_ID`` is unset, as everywhere else: a ``claude -p``
    child carries its parent's id there and must not send as the parent. A
    non-UUID environment value raises even beside a valid marker, which is
    stricter than the default path (that one returns the marker first):
    fail closed when a ``from_address`` is being checked."""
    from nexus import config as _nx_config  # noqa: PLC0415 — deferred: MCP/CLI startup cost, rare path
    from nexus.session import find_immediate_claude_pid  # noqa: PLC0415 — deferred: MCP/CLI startup cost, rare path
    from nexus.session_marker import _read_session_marker  # noqa: PLC0415 — deferred: MCP/CLI startup cost, rare path

    sd = state_dir if state_dir is not None else _nx_config.nexus_config_dir()
    pid = claude_pid if claude_pid is not None else find_immediate_claude_pid()
    own: list[str] = []
    for c in ((_read_session_marker(sd, pid) if pid else None) or "", resolve_env_session_id()):
        if c and looks_like_session_id(c) and c not in own:
            own.append(c)
    return own


def _own_identity(state_dir: Path | None, claude_pid: int | None) -> str:
    """This server's identity: the leased token file, else the token file binding one of its session ids."""
    from nexus.identity.board import identity_for_session  # noqa: PLC0415 -- deferred: MCP/CLI startup cost
    from nexus.identity.token_header import current  # noqa: PLC0415

    try:
        record = current()
    except Exception:  # noqa: BLE001 -- an unreadable token file is no identity
        record = None
    if record is not None and looks_like_identity_id(str(record.identity)):
        return str(record.identity)
    try:
        own = _own_session_ids(state_dir, claude_pid)
    except DirectoryResolutionError:
        own = []
    for sid in own:
        if found := identity_for_session(sid, state_dir):
            return found
    return ""


def check_from_address_is_own(
    value: str, *, state_dir: Path | None = None, claude_pid: int | None = None,
) -> str:
    """``mailbox_send``'s ``from_address`` check (F3 step 1, nexus-2mv7y).

    The shape check of :func:`validate_from_address` runs first. An agent
    id passes: a subagent shares its parent's server and names itself that
    way. A session id must equal one of this server's own ids, compared as
    UUIDs so case and hyphenation do not matter, and the own id's spelling
    is returned. With no own id resolvable there is nothing to compare and
    it passes, in canonical spelling so a reply's `to` matches: the account-plugin server Claude Desktop and Cowork start,
    and a Claude Code server whose T1 has not published its id yet. The
    tenant is still the only authenticated identity (JDR-002): this stops a
    confused model, not a hostile caller.
    """
    validate_from_address(value)
    if looks_like_agent_id(value):
        return value
    if looks_like_identity_id(value):
        # Fail closed: an identity names a seat, so with no own identity resolvable there is nothing it may claim.
        mine = _own_identity(state_dir, claude_pid)
        if value == mine:
            return value
        raise DirectoryResolutionError(identity_from_not_own_message(value, mine))
    own = _own_session_ids(state_dir, claude_pid)
    if not own:
        return str(uuid.UUID(value))
    wanted = uuid.UUID(value)
    for o in own:
        if uuid.UUID(o) == wanted:
            return o
    raise DirectoryResolutionError(from_address_not_own_message(value, own))

#: The refusal text for an unresolvable session id INSIDE Claude Code
#: (nexus-zl1jv). There the session id exists but T1 has not published it
#: yet: the startup mint was deferred (the storage service was down), or
#: the tuple-watch marker is missing. Any T1 call retries the mint and
#: writes ``NX_T1_SESSION_ID``. Pinned by an equality test.
NO_SESSION_ID_IN_CLAUDE_CODE_MESSAGE: str = (
    "no resolvable session id: T1 is not up yet in this Claude Code "
    "session, so it has published no session id; make any scratch call or "
    "run `nx doctor --check-t1`, then retry"
)

#: The refusal text for an unresolvable session id OUTSIDE Claude Code
#: (a host that sets neither variable). Pinned by an equality test.
NO_SESSION_ID_OUTSIDE_CLAUDE_CODE_MESSAGE: str = (
    "no resolvable session id: NX_T1_SESSION_ID and NX_SESSION_ID are "
    "unset; set NX_SESSION_ID to a UUID, unique per server, in a "
    "self-configured MCP server entry. The account-plugin server (Claude "
    "Desktop, Cowork) cannot set it; use memory_put to carry notes there"
)


def no_session_id_message() -> str:
    """The ONE refusal for an unresolvable session id (nexus-zl1jv).

    ``mailbox_send`` and ``tuple_subscribe``/``tuple_unsubscribe``/
    ``tuple_subscriptions`` all raise it, so no site spells its own text.
    The wording depends on the host: inside Claude Code the remedy is to
    bring T1 up, outside it is to set ``NX_SESSION_ID``. T1 keeps its own
    message, which names the variables T1 reads.
    """
    from nexus.session import started_by_claude_code  # noqa: PLC0415 — deferred: MCP/CLI startup cost, rare path

    if started_by_claude_code():
        return NO_SESSION_ID_IN_CLAUDE_CODE_MESSAGE
    return NO_SESSION_ID_OUTSIDE_CLAUDE_CODE_MESSAGE


def invalid_env_session_id_message(value: str) -> str:
    """Refusal for an ``NX_SESSION_ID`` that is not a UUID.

    A peer replies to the sender address, and ``resolve_send_address``
    reads anything that is not a session id or agent id as a NAME, so a
    non-UUID sender is unreachable.
    """
    return (
        f"NX_SESSION_ID {value[:64]!r} is not a UUID: mail and subscriptions "
        "need a session id shaped like 123e4567-e89b-12d3-a456-426614174000, "
        "because a peer replies to the sender and reads any other shape as a "
        "name. Set NX_SESSION_ID to a UUID, unique per server"
    )


def resolve_env_session_id() -> str:
    """The environment tiers of the tuple tools' session-id chain.

    ``NX_T1_SESSION_ID`` (the MCP lifespan's T1 mint writes it), then
    ``NX_SESSION_ID`` (the one a host with no Claude Code session names;
    T1 resolves it too). Empty string when neither is set. The marker tier
    sits above both and is read by :func:`resolve_default_from`.

    Whichever tier answers gets the same shape check as ``from_address`` (a
    UUID), and a value that fails raises :class:`DirectoryResolutionError`.
    ``NX_T1_SESSION_ID`` is checked too: the T1 mint copies the resolved id
    into it verbatim, so a non-UUID ``NX_SESSION_ID`` reaches this function
    through that tier on any healthy service, not only when the mint was
    deferred. Every other writer of ``NX_T1_SESSION_ID`` (Claude Code's
    session id, ``claude -p`` dispatch's ``uuid4``) writes a UUID.
    """
    value = os.environ.get("NX_T1_SESSION_ID", "").strip() or os.environ.get(
        "NX_SESSION_ID", ""
    ).strip()
    if not value:
        return ""
    if not looks_like_session_id(value):
        raise DirectoryResolutionError(invalid_env_session_id_message(value))
    return value


def resolve_default_from(*, state_dir: Path | None = None, claude_pid: int | None = None) -> str:
    """Default `from` for `mailbox_send` when no `from_address` is given.

    *state_dir*/*claude_pid* default to `nexus.config.nexus_config_dir()`
    and `nexus.session.find_immediate_claude_pid()` respectively -- the
    parameters exist so a caller (tests; the eventual `nx tuple directory`
    CLI verb) can pin both without monkeypatching process introspection.
    """
    from nexus import config as _nx_config  # noqa: PLC0415 — deferred: MCP/CLI startup cost, rare path
    from nexus.session import find_immediate_claude_pid  # noqa: PLC0415 — deferred: MCP/CLI startup cost, rare path
    from nexus.session_marker import _read_session_marker  # noqa: PLC0415 — deferred: MCP/CLI startup cost, rare path

    sd = state_dir if state_dir is not None else _nx_config.nexus_config_dir()
    pid = claude_pid if claude_pid is not None else find_immediate_claude_pid()
    marker = _read_session_marker(sd, pid) if pid else None
    if marker:
        return marker

    env_id = resolve_env_session_id()
    if env_id:
        return env_id

    raise DirectoryResolutionError(no_session_id_message())
