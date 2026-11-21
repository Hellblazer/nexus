# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``resolve_identity()``: which identity is this process (RDR-253 "The resolver").

One function replaces the four-tier session resolver for identity. Its inputs, in order:

1. an explicit identity token (a delegated child): ``NX_IDENTITY_TOKEN`` names the token file it was given; when that
   file cannot be read the answer is None, never the inputs below (they would name the parent);
2. the hook payload's session id;
3. ``CLAUDE_CODE_SESSION_ID``;
4. the token file of the nearest Claude ancestor process.

A session id is mapped through the engine's session index to ``(identity, generation)``; an id the index does not
know, from a live registry entry, is mapped through the entry's pid and ``startedAt`` to the identity that process
holds (the ``/branch`` case: it runs no hook, so its new id is in no index). If two inputs map to different
identities the function refuses with both and never picks. It only reads: nothing here writes the engine, the
token file or the registry.

The fourth input is what makes a cross-seat ``/resume`` refuse a Bash child started before the switch (RDR policy
table, line "resume in a process that already holds a different identity"): the child still carries the old
session id, which the index maps to the old identity, while the process's token file now names the new one.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nexus.identity.bind_client import IdentityApiError
from nexus.identity.token_file import ancestor_token_path, read_token_file

SOURCE_EXPLICIT = "explicit_token"
SOURCE_PAYLOAD = "payload"
SOURCE_SESSION_ID = "session_id"
SOURCE_TOKEN_FILE = "token_file"
SOURCE_REGISTRY = "registry"

NX_IDENTITY_TOKEN_ENV = "NX_IDENTITY_TOKEN"
CLAUDE_SESSION_ENV = "CLAUDE_CODE_SESSION_ID"


@dataclass(frozen=True)
class Answer:
    """What one input says: it names *identity* at *generation*. *detail* is the session id or the path."""

    source: str
    identity: str
    generation: int | None
    detail: str


@dataclass(frozen=True)
class Resolved:
    """The identity of this process: the first input's answer, with every agreeing answer kept."""

    identity: str
    generation: int | None
    source: str
    session_id: str | None
    answers: tuple[Answer, ...]


class IdentityConflictError(Exception):
    """Two inputs name different identities. ``answers`` holds one per input that answered."""

    def __init__(self, answers: list[Answer]) -> None:
        self.answers = tuple(answers)
        parts = "; ".join(f"{a.source} ({a.detail}) names {a.identity}" for a in answers)
        super().__init__(f"two sources name different identities: {parts}. Resolve it by restarting the process "
                         f"that carries the stale one.")


def _generation(row: Mapping[str, Any]) -> int | None:
    for key in ("current_generation", "generation"):
        value = row.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _by_session(client: Any, source: str, session_id: str) -> Answer | None:
    try:
        row = client.session(session_id)
    except IdentityApiError as exc:
        if exc.status == 404:
            return None
        raise
    identity = row.get("identity")
    return Answer(source, identity, _generation(row), session_id) if isinstance(identity, str) else None


def _by_registry(client: Any, session_id: str, claude_pid: int, sessions_dir: Path | None, host: str | None,
                 identity_dir: Path | None = None) -> Answer | None:
    """An id the index does not know, from the live registry entry that names it: the identity that process holds."""
    from nexus.identity.holder import read_registry_entry, this_host  # noqa: PLC0415 — deferred: hook import cost
    from nexus.identity.hostname import bindings_here  # noqa: PLC0415

    entry = read_registry_entry(claude_pid, sessions_dir)
    if entry is None or entry.started_at is None or entry.session_id != session_id:
        return None
    here = host if host is not None else this_host()
    for view in bindings_here(client, here, claude_pid, identity_dir):
        holder = view.get("holder") or {}
        if holder.get("pid") == claude_pid and holder.get("started_at") == entry.started_at:
            identity = view.get("id")
            if isinstance(identity, str):
                gen = view.get("generation")
                return Answer(SOURCE_REGISTRY, identity, gen if isinstance(gen, int) else None, session_id)
    return None


def resolve_identity(
    *, client: Any, payload_session_id: str | None = None, env: Mapping[str, str] | None = None,
    claude_pid: int | None = None, sessions_dir: Path | None = None, identity_dir: Path | None = None,
    host: str | None = None,
) -> Resolved | None:
    """The identity of this process, or None when no input names one. Raises :class:`IdentityConflictError` when two
    inputs name different identities. *claude_pid* is the nearest Claude ancestor (the fourth input and the
    registry fallback need it; None skips both). *client* needs ``session`` and ``bindings_by_host``."""
    import os  # noqa: PLC0415 — stdlib, kept local with the env default

    environ = os.environ if env is None else env
    answers: list[Answer] = []
    unknown_ids: list[str] = []

    explicit = (environ.get(NX_IDENTITY_TOKEN_ENV) or "").strip()
    if explicit:
        record = read_token_file(Path(explicit))
        if record is None:
            # a process given a token file is a delegated child: once its file is gone (or it was given none, the
            # never-existing sentinel), it has no identity. Falling through would answer with its parent's.
            return None
        answers.append(Answer(SOURCE_EXPLICIT, record.identity, record.generation, explicit))

    seen: set[str] = set()
    for source, sid in ((SOURCE_PAYLOAD, payload_session_id), (SOURCE_SESSION_ID, (environ.get(CLAUDE_SESSION_ENV) or "").strip())):
        if not sid or sid in seen:
            continue
        seen.add(sid)
        answer = _by_session(client, source, sid)
        if answer is not None:
            answers.append(answer)
        else:
            unknown_ids.append(sid)

    if claude_pid is not None and identity_dir is not None:
        path = ancestor_token_path(identity_dir, claude_pid, sessions_dir)
        record = read_token_file(path) if path is not None else None
        if record is not None:
            answers.append(Answer(SOURCE_TOKEN_FILE, record.identity, record.generation, str(path)))

    if claude_pid is not None and not any(a.source in (SOURCE_PAYLOAD, SOURCE_SESSION_ID) for a in answers):
        for sid in unknown_ids:
            via_registry = _by_registry(client, sid, claude_pid, sessions_dir, host, identity_dir)
            if via_registry is not None:
                answers.append(via_registry)
                break

    if not answers:
        return None
    if len({a.identity for a in answers}) > 1:
        raise IdentityConflictError(answers)
    first = answers[0]
    sid = next((a.detail for a in answers if a.source in (SOURCE_PAYLOAD, SOURCE_SESSION_ID, SOURCE_REGISTRY)), None)
    return Resolved(identity=first.identity, generation=first.generation, source=first.source, session_id=sid,
                    answers=tuple(answers))
