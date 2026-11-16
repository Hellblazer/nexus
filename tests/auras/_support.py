# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared aura-file text for the aura parser and loader tests (RDR-249 Technical Design 1)."""
from __future__ import annotations

#: Technical Design 1's example, the first consumer.
TD1_EXAMPLE = '''\
aura = "ci_develop_red"
summary = "A red develop sha, posted to this session's threads entry."
procedure = "ci_red_notice"

[trigger]
subspace = "board/ci/nexus-develop--done"
match = { kind = "sha", outcome = "failure" }

[args]
sha = "dims.sha"
outcome = "dims.outcome"

[when]
field = "show"

[clear]
match = { kind = "sha", outcome = "success" }

[display]
channel = true
threads = "develop CI red"
'''

#: The smallest file that parses: only the required keys.
MINIMAL = '''\
aura = "{name}"
summary = "does a thing"
procedure = "some_proc"

[trigger]
subspace = "board/topic"

[display]
threads = "a block"
'''


def minimal(name: str = "mini", extra: str = "") -> str:
    """:data:`MINIMAL` for *name*, with *extra* TOML appended (more tables or keys)."""
    return MINIMAL.format(name=name) + extra
