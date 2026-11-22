# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Resolving a ``mailbox_send`` ``to`` argument to a concrete ``mailbox/<address>`` + ``address_kind``, and the default
``from`` sender.

Mail goes to an identity, not to a session (RDR-253 Phase 2, bead nexus-hczdo.2.1). ``to`` resolves as:

  - an identity id (``i`` and digits) goes straight to ``mailbox/<id>``, ``address_kind="identity"``;
  - an agent id ("a" + 16 lowercase hex chars -- the ONLY shape the Agent tool's own opaque ``agent_id`` can take)
    goes to ``mailbox/<id>``, ``address_kind="agent"``;
  - a session id (a UUID) is REFUSED, naming the identity the session is bound to when the engine's session index
    knows it: a session id is not an address, because it changes at every ``/clear``;
  - anything else is a NAME, resolved through ``alias/<name>`` to a seat's identity. A retired alias inside its
    7-day grace resolves with the engine's notice; past it the engine answers 410 and the send is refused with
    "renamed to <new> on <date>". A name no seat holds is refused, and so is an ephemeral identity's display name
    (an ephemeral is reached by id only).

The ``directory/<name>`` lease (RDR-208) that used to resolve a name to a session is gone, and so is the session
mailbox it pointed at; :func:`resolve_send` never reads either.

``from`` resolution: the default is the tuple-watch session marker for this MCP server's claude ancestor
(``nexus.session_marker._read_session_marker``, written synchronously by SessionStart on every source), falling back
to ``NX_T1_SESSION_ID``, then ``NX_SESSION_ID`` (the one name a host with no Claude Code session can set in a
self-configured MCP server entry; nexus-zl1jv). ``NX_SESSION_ID`` must be a UUID, checked like ``from_address``. The
marker sits above both variables. A caller resolving none is refused rather than stamping a ``from`` that might
already be stale.
"""
from __future__ import annotations

import os
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: "a" + 16 lowercase hex chars -- the ONLY shape the Agent tool's own
#: opaque `agent_id` can take (it has no `name` parameter to produce
#: anything else). Confirmed against two REAL ids, not just the fixture:
#: this worktree's own claimant id ("a0b56337bc52ed773", from the
#: SubagentStart hook's "Claimant id:" line) and
#: `tests/hooks/test_subagent_stop_hook.py:288`'s fixture id
#: ("a16b397f79df79c42").
_AGENT_ID_RE = re.compile(r"^a[0-9a-f]{16}$")
#: The shape of a name ``mailbox_send`` resolves through ``alias/<name>``. A `to` outside it can never resolve,
#: so ``resolve_send`` refuses it before any engine call.
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
def _session_id_refusal(to: str, session_identity: AliasLookup | None) -> str:
    """The refusal for a ``to`` that is a session id: names the identity the session is bound to when the session
    index says, else says to send to the receiver's identity or seat name."""
    bound = ""
    if session_identity is not None:
        try:
            bound = str(session_identity(to).get("identity") or "")
        except Exception:  # noqa: BLE001 -- a session the index cannot place (or an engine that cannot answer): the plain text
            bound = ""
    head = (
        f"{to[:64]!r} is a session id, and a session id is not a mail address: it changes at every /clear. "
        "Mail goes to the receiver's identity"
    )
    if looks_like_identity_id(bound):
        return f"{head}; that session is bound to {bound}, so send to {bound} (mailbox/{bound})"
    return f"{head}: send to its identity id (an \"i\" and digits) or its seat's name"


def resolve_send(
    to: str, alias: AliasLookup | None = None, session_identity: AliasLookup | None = None,
) -> SendTarget:
    """Resolve ``to`` to the identity (or agent) mailbox it names; see the module docstring for the rules.

    *alias* is :func:`identity_alias_lookup`'s shape (``IdentityClient.alias``), *session_identity*
    :func:`identity_session_lookup`'s (``IdentityClient.session``, read only to name the identity in a session id's
    refusal). A name with no *alias* lookup, an alias lookup that cannot be made, and a name no seat holds are each
    refused: nothing falls back to a session or a directory. Nothing is written by a caller that raises.
    """
    if not to:
        raise DirectoryResolutionError("to must not be empty")
    if looks_like_identity_id(to):
        return SendTarget(to, "identity")
    if looks_like_agent_id(to):
        return SendTarget(to, "agent")
    if looks_like_session_id(to):
        raise DirectoryResolutionError(_session_id_refusal(to, session_identity))
    if not SAFE_NAME_RE.fullmatch(to):
        # nexus-3ceda.7: the engine's answer for such a name was "no template registered for subspace", which sent
        # agents to tools that do not exist.
        raise DirectoryResolutionError(
            f"{to[:64]!r} cannot be a seat's name: names are letters, digits, '.', '_' and '-', starting with a "
            "letter or digit. Send to the identity id (an \"i\" and digits) or a seat's name."
        )
    from nexus.identity.bind_client import IdentityApiError  # noqa: PLC0415 -- deferred: MCP/CLI startup cost

    if alias is None:
        raise DirectoryResolutionError(
            f"cannot resolve the name {to!r}: no identity alias lookup is available here; send to its identity id"
        )
    try:
        found = alias(to)
    except IdentityApiError as exc:
        if exc.status == 410:
            raise DirectoryResolutionError(str(exc.message or f"{to!r} was renamed")) from None
        if exc.status == 404:
            raise DirectoryResolutionError(
                f"no seat is named {to!r}. A session is reached by its seat's name or its identity id; an "
                "ephemeral identity by its id only (it has no alias, and two launches can share a display name)"
            ) from None
        raise DirectoryResolutionError(f"cannot resolve the name {to!r}: the alias lookup failed ({exc})") from None
    except Exception as exc:  # noqa: BLE001 -- no identity routes (an older engine) or unreachable: refused, never guessed
        raise DirectoryResolutionError(f"cannot resolve the name {to!r}: the alias lookup failed ({exc})") from None
    identity = str(found.get("identity") or "")
    if not looks_like_identity_id(identity):
        raise DirectoryResolutionError(f"the alias lookup for {to!r} named no identity; send to its identity id")
    return SendTarget(identity, "identity", str(found.get("notice") or ""))


def identity_alias_lookup() -> AliasLookup:
    """``IdentityClient.alias`` for this host's engine. The client is built, used and closed inside each lookup, so
    a send to an identity or agent id (which :func:`resolve_send` never looks up) builds none, and none is left
    open. A host with no endpoint or credential raises from the lookup, which :func:`resolve_send` refuses."""

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
    id, an identity or an agent id -- a subagent sharing its parent's MCP server would
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

    A peer replies to the sender address, and ``resolve_send``
    reads anything that is not an identity or agent id as a NAME, so a
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
    parameters exist so a caller (tests) can pin both without monkeypatching
    process introspection.
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


def resolve_default_sender(*, state_dir: Path | None = None, claude_pid: int | None = None) -> str:
    """Default ``from`` of a send (``mailbox_send``, a ``tuple_ack`` reply): this sender's own identity when it is bound,
    else :func:`resolve_default_from`'s session id (RDR-253 Phase 2).

    A session id is not a mail address, so a recipient replying to a session-id ``from`` was refused; an identity is one.
    An identity lookup that fails is "no identity" and falls back, never a failed send. :func:`resolve_default_from`
    itself stays the session reader (``nx ask`` and the sender-session comparisons want the session)."""
    from nexus import config as _nx_config  # noqa: PLC0415 — deferred: MCP/CLI startup cost, rare path
    from nexus.session import find_immediate_claude_pid  # noqa: PLC0415 — deferred: MCP/CLI startup cost, rare path

    sd = state_dir if state_dir is not None else _nx_config.nexus_config_dir()
    pid = claude_pid if claude_pid is not None else find_immediate_claude_pid()
    try:
        identity = _own_identity(sd, pid)
    except Exception:  # noqa: BLE001 -- an unreadable token file or an unreachable index is no identity
        identity = ""
    return identity or resolve_default_from(state_dir=sd, claude_pid=pid)
