# SPDX-License-Identifier: AGPL-3.0-or-later
"""A stand-in for the script's `nx identity show --session` (RDR-253 Phase 2, nexus-hczdo.2.1).

scripts/claude-restart-sessions mails an ask to the target's identity mailbox, so every test that reaches mail_ask
needs the target to hold an identity. identity_stub gives each session id one; a session id in *unbound* gets
``unknown_session`` instead.
"""

from __future__ import annotations

import json
import subprocess
from typing import Callable


def identity_of(sid: str) -> str:
    """The identity the stand-in gives a session id: its digits after an `i` (sid-9 -> i9)."""
    return "i" + ("".join(c for c in sid if c.isdigit()) or "1")


def identity_stub(unbound: set[str] = frozenset(), calls: list | None = None) -> Callable[..., subprocess.CompletedProcess]:
    """A stand-in `_nx_identity` answering `show --session <sid> --json`; every call is appended to *calls*."""
    def fake(*args: str, timeout: float = 60) -> subprocess.CompletedProcess:
        if calls is not None:
            calls.append(args)
        assert args[:2] == ("show", "--session"), args
        sid = args[2]
        if sid in unbound:
            return subprocess.CompletedProcess(args, 1, "", "Error: unknown_session\n")
        return subprocess.CompletedProcess(args, 0, json.dumps({"id": identity_of(sid), "kind": "ephemeral"}), "")
    return fake
