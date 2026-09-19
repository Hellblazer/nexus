# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every hooks.json entry has one of the declared shapes (RDR-215, nexus-q02nx.26).

`tests/hooks/test_hooks_json_handlers_resolve.py` asks whether a declared
handler EXISTS. This file asks whether the entry is SHAPED like something
this epic still permits -- which is the other half, and the half that
catches a regression rather than a typo.

RDR-215 retires the bash hook layer. The regression it has to survive is
somebody re-introducing a shell string, because that is what every entry
in this file used to look like and what every example in a search result
still looks like. A `"command": "bash ..."` entry is perfectly valid to
Claude Code and resolves to a real handler, so the resolution gate above
would pass it without comment.

WHOLE-STRING MATCHING IS THE POINT, NOT A DETAIL. The forbidden tokens are
`bash`, `sh` and `nx`; the permitted commands include `nx-hook` and
`nx-session-end-launcher`. A substring reading of the `nx` clause rejects
both of the permitted ones. A gate residual was raised on exactly that
misreading during planning, so the comparison is equality against a token
set and `test_the_permitted_commands_are_not_rejected_as_substrings` is
the fixture that fails if anyone rewrites it as `in`.

WHY THE python3 ALLOWLIST IS BY NAME. Bead .26 was written on 2026-09-18
and says `python3` is permitted only for `version_lockstep_hook.py`. That
was true when written and is not true now: bead .21's tier resolution
(T2 `nexus_rdr/215-tier-resolution-bead-21`) settled FIVE handlers as
deliberately plugin-resident, and `conexus/PENDING_RELEASE.md` names all
five. The bead body is stale, not the manifest. Pinning the five by name
rather than allowing any `.py` is deliberate: each of the five was argued
for individually, so a sixth appearing is drift the lint should refuse
until someone argues for it too.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus.mcp.hooks import HOOK_TOOLS

pytestmark = pytest.mark.lint

REPO_ROOT = Path(__file__).resolve().parents[1]
CONEXUS_HOOKS = REPO_ROOT / "conexus" / "hooks" / "hooks.json"
SN_HOOKS = REPO_ROOT / "sn" / "hooks" / "hooks.json"

#: Measured on the tree that introduced this gate (2026-09-19, 441eb67d2).
#: A DROP means either entries were removed or the walker stopped
#: recognising how they are declared; rule out the second reading first.
#: 24 -> 25 at 7ad666ebb (bead .22), which gave `behaviour_census.py` its
#: own SessionStart entry.
EXPECTED_CONEXUS_ENTRIES = 25
EXPECTED_SN_ENTRIES = 4

MCP_SERVER = "plugin:conexus:nexus"

#: Equality targets, never substrings. See the module docstring.
FORBIDDEN_TOKENS = frozenset({"bash", "sh", "nx"})

CONEXUS_COMMANDS = frozenset({"nx-hook", "nx-session-end-launcher", "python3"})

#: The five handlers bead .21 resolved as plugin-resident, each for its own
#: reason (stdlib-only siblings the wheel cannot import; the lockstep, which
#: must run when the wheel is behind; and the transcript census, which is
#: plugin-domain). conexus/PENDING_RELEASE.md carries the full reasoning.
PLUGIN_RESIDENT_SCRIPTS = frozenset(
    {
        "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/behaviour_census.py",
        "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/mailbox_drain.py",
        "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/version_lockstep_hook.py",
        "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/routing/phase_review_close_requires_gate.py",
        "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/routing/subagent_git_write_requires_orchestrator.py",
    }
)

SN_SCRIPT_PREFIX = "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/"

REGISTERED_TOOLS = frozenset(f"hook_{spec.name}" for spec in HOOK_TOOLS)


def _walk(path: Path) -> list[tuple[str, dict]]:
    """``(event, entry)`` for every leaf hook entry in a manifest."""
    data = json.loads(path.read_text())
    out: list[tuple[str, dict]] = []
    for event, groups in data["hooks"].items():
        for group in groups:
            for entry in group.get("hooks", []):
                out.append((event, entry))
    return out


def _forbidden_token(entry: dict) -> str | None:
    """The first `bash`/`sh`/`nx` token or `.sh` path in the entry, if any."""
    candidates = [entry.get("command")]
    candidates += [a for a in entry.get("args", []) if isinstance(a, str)]
    for value in candidates:
        if not isinstance(value, str):
            continue
        if value in FORBIDDEN_TOKENS:
            return value
        if value.endswith(".sh"):
            return value
    return None


def reject_conexus(event: str, entry: dict) -> str | None:
    """``None`` if the entry is a permitted shape, else why it is not.

    The real manifest and the fixture cases both go through this one
    function on purpose. A fixture that exercises a second, parallel
    implementation proves that implementation, not the guard.
    """
    if "async" in entry:
        return (
            "carries an `async` key. Bead .20 resolved the two async tuple "
            "projections into daemon threads inside the server, so no "
            "manifest key is needed and an unlinted key is how the shape "
            "drifts back."
        )

    bad = _forbidden_token(entry)
    if bad is not None:
        return f"names the retired shell token or script {bad!r}"

    if entry.get("type") == "mcp_tool":
        if event == "SessionStart":
            return (
                "is an mcp_tool on SessionStart. The MCP server is not up "
                "when SessionStart fires, so a tool-tier entry there cannot "
                "run; SessionStart must be command tier."
            )
        if entry.get("server") != MCP_SERVER:
            return f"names server {entry.get('server')!r}, not {MCP_SERVER!r}"
        tool = entry.get("tool")
        if not isinstance(tool, str) or not tool.startswith("hook_"):
            return f"names tool {tool!r}, which is not a `hook_` tool"
        if tool not in REGISTERED_TOOLS:
            return (
                f"names tool {tool!r}, which nexus.mcp.hooks.HOOK_TOOLS does "
                f"not register. Registered: {sorted(REGISTERED_TOOLS)}"
            )
        return None

    # EVERY entry declares its tier explicitly. Without this the function
    # fell THROUGH to the command tier for any `type` at all -- a missing
    # key, or a typo like "commnd" -- and such an entry was accepted as
    # long as its command happened to be permitted. The fallthrough was
    # the default branch, so nothing pointed at it and no fixture reached
    # it (nexus-q02nx.29).
    if entry.get("type") != "command":
        return (
            f"declares type {entry.get('type')!r}. Every entry is exactly one "
            f"of `mcp_tool` or `command`; anything else is a typo that would "
            f"otherwise be read as the command tier by default."
        )
    if "args" not in entry:
        return (
            "is command tier with no `args` key. Exec form carries its "
            "arguments in `args`; a bare `command` string is the shell form "
            "this epic retires."
        )
    command = entry.get("command")
    if command not in CONEXUS_COMMANDS:
        return f"runs command {command!r}, which is not one of {sorted(CONEXUS_COMMANDS)}"
    args = entry.get("args", [])
    if command == "nx-hook" and len(args) != 1:
        return (
            f"runs `nx-hook` with {len(args)} args; exactly one verb is "
            f"permitted. An empty `args` spawns the entry point with no verb, "
            f"which is a live hook that does nothing."
        )
    if command == "nx-session-end-launcher" and args:
        return (
            f"runs `nx-session-end-launcher` with {len(args)} args; it takes "
            f"none, and its empty `args` is what makes the entry exec form."
        )
    if command == "python3":
        if len(args) != 1:
            return f"runs python3 with {len(args)} args; exactly one script path is permitted"
        if args[0] not in PLUGIN_RESIDENT_SCRIPTS:
            return (
                f"runs python3 on {args[0]!r}, which is not one of the five "
                f"handlers bead .21 resolved as plugin-resident. A sixth "
                f"needs the same argument the other five got -- see "
                f"conexus/PENDING_RELEASE.md."
            )
    return None


def reject_sn(event: str, entry: dict) -> str | None:
    """``None`` if the sn entry is a permitted shape, else why it is not.

    sn ships no Python package and no server, so it has one shape only:
    exec-form `python3` on a script inside sn itself. That independence is
    an RDR-215 Cross-Cutting Concern, which is why there is no tool tier
    here to fall back to.
    """
    if "async" in entry:
        return "carries an `async` key; sn has no async tier"
    bad = _forbidden_token(entry)
    if bad is not None:
        return f"names the retired shell token or script {bad!r}"
    if entry.get("type") == "mcp_tool":
        return (
            "is an mcp_tool. sn runs no MCP server of its own and must not "
            "depend on conexus's, so every sn entry is command tier."
        )
    if entry.get("type") != "command":
        return (
            f"declares type {entry.get('type')!r}, not `command`. Same default"
            f"-branch gap as the conexus side (nexus-q02nx.29)."
        )
    if "args" not in entry:
        return "is command tier with no `args` key"
    if entry.get("command") != "python3":
        return f"runs command {entry.get('command')!r}; sn permits only `python3`"
    args = entry.get("args", [])
    if len(args) != 1:
        return f"runs python3 with {len(args)} args; exactly one script path is permitted"
    script = args[0]
    if not script.startswith(SN_SCRIPT_PREFIX) or not script.endswith(".py"):
        return f"runs python3 on {script!r}, which is not a .py under {SN_SCRIPT_PREFIX}"
    return None


# --------------------------------------------------------------------------
# The real manifests
# --------------------------------------------------------------------------


def test_the_walk_is_not_vacuous() -> None:
    """A lint that passes over a mis-parsed structure proves nothing."""
    conexus = _walk(CONEXUS_HOOKS)
    sn = _walk(SN_HOOKS)
    assert len(conexus) == EXPECTED_CONEXUS_ENTRIES, (
        f"walked {len(conexus)} conexus entries, expected "
        f"{EXPECTED_CONEXUS_ENTRIES}. If entries were added deliberately, "
        f"move the constant and say why in the commit; if this DROPPED, rule "
        f"out the walker going blind before concluding entries were removed."
    )
    assert len(sn) == EXPECTED_SN_ENTRIES, (
        f"walked {len(sn)} sn entries, expected {EXPECTED_SN_ENTRIES}. Same "
        f"reading order."
    )


@pytest.mark.parametrize(
    "event,entry",
    _walk(CONEXUS_HOOKS),
    ids=[
        f"{e}:{x.get('tool') or x.get('command')}"
        for e, x in _walk(CONEXUS_HOOKS)
    ],
)
def test_every_conexus_entry_has_a_permitted_shape(event: str, entry: dict) -> None:
    reason = reject_conexus(event, entry)
    assert reason is None, f"conexus hooks.json [{event}] {reason}"


@pytest.mark.parametrize(
    "event,entry",
    _walk(SN_HOOKS),
    ids=[f"{e}:{(x.get('args') or [''])[0].rsplit('/', 1)[-1]}" for e, x in _walk(SN_HOOKS)],
)
def test_every_sn_entry_has_a_permitted_shape(event: str, entry: dict) -> None:
    reason = reject_sn(event, entry)
    assert reason is None, f"sn hooks.json [{event}] {reason}"


# --------------------------------------------------------------------------
# Non-vacuity: every clause proved to reject what it exists to reject
# --------------------------------------------------------------------------

CONEXUS_REJECTS = [
    pytest.param(
        "PreToolUse",
        {"type": "command", "command": "bash", "args": ["x.sh"]},
        id="bash-command",
    ),
    pytest.param(
        "PreToolUse",
        {"type": "command", "command": "sh", "args": ["x"]},
        id="sh-command",
    ),
    pytest.param(
        "SessionStart",
        {
            "type": "command",
            "command": "python3",
            "args": ["${CLAUDE_PLUGIN_ROOT}/hooks/scripts/old_hook.sh"],
        },
        id="dot-sh-in-args",
    ),
    pytest.param(
        "SessionStart",
        {"type": "command", "command": "nx", "args": ["hook", "session-start"]},
        id="bare-nx-command",
    ),
    pytest.param(
        "SessionStart",
        {"type": "mcp_tool", "server": MCP_SERVER, "tool": "hook_post_compact"},
        id="mcp-tool-on-session-start",
    ),
    pytest.param(
        "Stop",
        {"type": "mcp_tool", "server": MCP_SERVER, "tool": "hook_not_a_real_tool"},
        id="unregistered-hook-tool",
    ),
    pytest.param(
        "Stop",
        {"type": "mcp_tool", "server": "plugin:other:server", "tool": "hook_post_compact"},
        id="wrong-server",
    ),
    pytest.param(
        "Stop",
        {"type": "mcp_tool", "server": MCP_SERVER, "tool": "post_compact"},
        id="tool-without-hook-prefix",
    ),
    pytest.param(
        "Stop",
        {
            "type": "mcp_tool",
            "server": MCP_SERVER,
            "tool": "hook_post_compact",
            "async": True,
        },
        id="async-key",
    ),
    pytest.param(
        "SessionStart",
        {"type": "command", "command": "nx-hook session-start"},
        id="shell-string-no-args-key",
    ),
    pytest.param(
        "UserPromptSubmit",
        {
            "type": "command",
            "command": "python3",
            "args": ["${CLAUDE_PLUGIN_ROOT}/hooks/scripts/a_sixth_script.py"],
        },
        id="unargued-sixth-python3-script",
    ),
    # The default-branch gap. Each of these was ACCEPTED before
    # nexus-q02nx.29, because anything that was not `mcp_tool` fell
    # through to the command tier and was judged only on its command.
    pytest.param(
        "PreToolUse",
        {"command": "nx-hook", "args": ["preflight"]},
        id="no-type-key-at-all",
    ),
    pytest.param(
        "PreToolUse",
        {"type": "commnd", "command": "nx-hook", "args": ["preflight"]},
        id="misspelt-type",
    ),
    pytest.param(
        "SessionStart",
        {"type": "command", "command": "nx-hook", "args": []},
        id="nx-hook-with-no-verb",
    ),
    pytest.param(
        "SessionStart",
        {"type": "command", "command": "nx-hook", "args": ["rdr", "extra"]},
        id="nx-hook-with-two-verbs",
    ),
    pytest.param(
        "SessionEnd",
        {"type": "command", "command": "nx-session-end-launcher", "args": ["x"]},
        id="launcher-with-an-argument",
    ),
    # A command that is neither permitted nor a forbidden token. Without
    # this the CONEXUS_COMMANDS membership check had no case of its own:
    # every command-tier fixture above was rejected by the token ban or
    # the type check first, so the clause was carried by its neighbours.
    pytest.param(
        "PreToolUse",
        {"type": "command", "command": "perl", "args": ["-e", "1"]},
        id="unpermitted-command-that-is-not-a-forbidden-token",
    ),
    pytest.param(
        "UserPromptSubmit",
        {
            "type": "command",
            "command": "python3",
            "args": [
                "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/mailbox_drain.py",
                "--extra",
            ],
        },
        id="python3-with-two-args",
    ),
]


@pytest.mark.parametrize("event,entry", CONEXUS_REJECTS)
def test_the_conexus_lint_rejects_what_it_exists_to_reject(
    event: str, entry: dict
) -> None:
    assert reject_conexus(event, entry) is not None, (
        f"the lint ACCEPTED {entry!r} on {event}. It is the same function the "
        f"real manifest is checked with, so a clause that cannot reject this "
        f"cannot protect the manifest either."
    )


SN_REJECTS = [
    pytest.param(
        "SessionStart",
        {"type": "command", "command": "bash", "args": ["session-start.sh"]},
        id="bash-wrapper",
    ),
    pytest.param(
        "SessionStart",
        {
            "type": "command",
            "command": "python3",
            "args": ["${CLAUDE_PLUGIN_ROOT}/hooks/scripts/session-start.sh"],
        },
        id="dot-sh-in-args",
    ),
    pytest.param(
        "SubagentStart",
        {"type": "mcp_tool", "server": MCP_SERVER, "tool": "hook_subagent_start"},
        id="sn-must-not-use-the-conexus-server",
    ),
    pytest.param(
        "PreToolUse",
        {"type": "command", "command": "python3", "args": ["/etc/elsewhere.py"]},
        id="script-outside-sn",
    ),
    pytest.param(
        "PreToolUse",
        {"type": "command", "command": "python3"},
        id="no-args-key",
    ),
    pytest.param(
        "SessionStart",
        {"command": "python3", "args": ["${CLAUDE_PLUGIN_ROOT}/hooks/scripts/x.py"]},
        id="no-type-key-at-all",
    ),
    pytest.param(
        "SessionStart",
        {
            "type": "weird",
            "command": "python3",
            "args": ["${CLAUDE_PLUGIN_ROOT}/hooks/scripts/x.py"],
        },
        id="bogus-type",
    ),
    # The conexus side had an `async-key` fixture from the start and sn's
    # identical clause had none: the clause was MIRRORED across the two
    # validators and its PROOF was not. Caught in the Phase 4 critique
    # (nexus-q02nx.30), on the plugin whose independence the RDR's
    # Cross-Cutting Concerns single out.
    pytest.param(
        "SubagentStart",
        {
            "type": "command",
            "command": "python3",
            "args": ["${CLAUDE_PLUGIN_ROOT}/hooks/scripts/subagent_start.py"],
            "async": True,
        },
        id="async-key",
    ),
    pytest.param(
        "SessionStart",
        {"type": "command", "command": "node", "args": ["x.js"]},
        id="unpermitted-command-that-is-not-a-forbidden-token",
    ),
    pytest.param(
        "SessionStart",
        {
            "type": "command",
            "command": "python3",
            "args": [
                "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/session_start.py",
                "--extra",
            ],
        },
        id="python3-with-two-args",
    ),
]


@pytest.mark.parametrize("event,entry", SN_REJECTS)
def test_the_sn_lint_rejects_what_it_exists_to_reject(event: str, entry: dict) -> None:
    assert reject_sn(event, entry) is not None, (
        f"the sn lint ACCEPTED {entry!r} on {event}."
    )


def test_the_permitted_commands_are_not_rejected_as_substrings() -> None:
    """`nx-hook` is not `nx`, and `nx-session-end-launcher` is not `nx` either.

    This is the fixture for the gate residual raised during planning: read
    the `nx` clause as a substring and both permitted commands disappear,
    taking seven live entries with them. It fails the moment someone
    rewrites the token comparison as `in`.
    """
    assert (
        reject_conexus(
            "SessionStart",
            {"type": "command", "command": "nx-hook", "args": ["session-start"]},
        )
        is None
    )
    assert (
        reject_conexus(
            "SessionEnd",
            {"type": "command", "command": "nx-session-end-launcher", "args": []},
        )
        is None
    )
