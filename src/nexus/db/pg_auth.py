# SPDX-License-Identifier: AGPL-3.0-or-later
"""``pg_hba.conf`` helpers for the nx-managed local cluster (nexus-ja4pq).

The bundled cluster listens on 127.0.0.1 only. Until nexus-ja4pq it was created
with ``initdb --auth=trust``, so any OS account on the box could open a
superuser session on the loopback port with no password. Every cluster nx
creates now authenticates with ``scram-sha-256``, and an existing trust cluster
is converted in place by ``pg_provision.harden_cluster_auth``.

This module holds only the pure, subprocess-free part: reading and rewriting
``pg_hba.conf`` text, and classifying a data directory. It imports nothing from
``pg_provision`` so ``nx doctor`` can read a cluster's auth posture without
loading the provisioner.

The rewrite is line-level on purpose. It replaces the ``trust`` method token on
active lines and touches nothing else, so comments, ordering, address columns
and any non-``trust`` line an operator added survive, and the same code is
right on Windows, where ``initdb`` writes no ``local`` lines (a ``local`` line
there is a parse error, so a whole-file template would not be portable).
"""
from __future__ import annotations

import re
from pathlib import Path

#: Name of the host-based-authentication file inside a data directory.
HBA_FILENAME: str = "pg_hba.conf"

#: The authentication method every nexus-managed line must carry.
SCRAM: str = "scram-sha-256"

#: Methods PostgreSQL accepts in the method column. The method is located as the
#: first token from the fourth onward that names one of these, because the
#: address column is one token (``127.0.0.1/32``) or two (``127.0.0.1
#: 255.255.255.255``) and neither can spell a method name.
_METHODS: frozenset[str] = frozenset({
    "trust", "reject", "md5", "password", "scram-sha-256", "gss", "sspi",
    "ident", "peer", "pam", "bsd", "ldap", "radius", "cert", "oauth",
})

_TOKEN = re.compile(r"\S+")


def hba_path(pgdata: Path) -> Path:
    """Path of ``pg_hba.conf`` inside *pgdata*."""
    return pgdata / HBA_FILENAME


def _strip_comment(line: str) -> str:
    """*line* up to an unquoted ``#``."""
    in_quotes = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == "#" and not in_quotes:
            return line[:i]
    return line


def _method_span(line: str) -> tuple[int, int, str] | None:
    """``(start, end, method)`` of *line*'s method token, or None for a blank or
    comment line, or one with no recognisable method."""
    code = _strip_comment(line)
    tokens = list(_TOKEN.finditer(code))
    if len(tokens) < 4:
        return None
    for tok in tokens[3:]:
        if tok.group(0).lower() in _METHODS:
            return tok.start(), tok.end(), tok.group(0).lower()
    return None


def hba_has_trust(text: str) -> bool:
    """True when any active line of *text* authenticates with ``trust``."""
    return any(
        (span := _method_span(line)) is not None and span[2] == "trust"
        for line in text.splitlines()
    )


def harden_hba_text(text: str) -> str:
    """*text* with every active ``trust`` line switched to ``scram-sha-256``.

    Idempotent: text with no ``trust`` line comes back byte for byte.
    """
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        span = _method_span(line)
        if span is not None and span[2] == "trust":
            start, end, _ = span
            line = f"{line[:start]}{SCRAM}{line[end:]}"
        out.append(line)
    return "".join(out)


def cluster_auth_state(pgdata: Path) -> str:
    """How the cluster at *pgdata* authenticates, read from its ``pg_hba.conf``.

    ``"trust"`` when any active line is trust, ``"scram"`` when none is and at
    least one line is ``scram-sha-256``, ``"other"`` for an operator-managed
    file with neither, ``"absent"`` when the file is missing or unreadable.
    """
    try:
        text = hba_path(pgdata).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "absent"
    if hba_has_trust(text):
        return "trust"
    if any(
        (span := _method_span(line)) is not None and span[2] == SCRAM
        for line in text.splitlines()
    ):
        return "scram"
    return "other"
