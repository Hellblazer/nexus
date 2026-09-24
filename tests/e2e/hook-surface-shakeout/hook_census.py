# SPDX-License-Identifier: AGPL-3.0-or-later
"""Census every declared hook entry against what actually fired.

DENOMINATOR: the shipped ``hooks.json``. NUMERATOR: three surfaces, two of
which are complete rosters and one of which is corroboration only.

WHY THE TRANSCRIPT IS NOT ENOUGH. Claude Code records a hook in the session
transcript only when the hook PRODUCES OUTPUT. ``preflight`` returns
``stdout=None`` on a healthy host by explicit design; ``behaviour_census``
has nothing to say on a virgin box. Both vanish, so "silent and fine" and
"never ran" become the same observation -- and "never ran" is precisely the
fail-open this harness exists to catch. The per-event roster that DOES list
silent hooks (``hookInfos``) is emitted only for ``stop_hook_summary``; the
complete set of system subtypes in a measured transcript was
``{stop_hook_summary, turn_duration}``. There is no SessionStart equivalent.

So two complete rosters are built instead, one per tier:

* COMMAND TIER -- ``hook-census.tsv``. run.sh rewrites every command-tier
  entry to a generated shim that appends ``event, declared, pid, epoch`` and
  THEN execs the real handler. Because the row is written BEFORE delegating,
  a handler that crashes, hangs or exits non-zero still leaves evidence,
  separating "never invoked" from "invoked and died" -- a distinction no
  Claude Code channel offers.
* TOOL TIER -- ``mcp-stdin.jsonl``. The ``mcp_tool`` entries have no command
  to shim, but they dispatch over stdio to ``nx-mcp``, which is ours. run.sh
  routes that server through a tee, so every JSON-RPC call naming a ``hook_*``
  tool is captured whether or not the handler spoke. The wheel under test is
  not modified in order to be measured.
* TRANSCRIPT -- corroboration and the error channel (``hookErrors``, non-zero
  ``exitCode``). Never the roster.

The shim-before-delegate pattern and the baked-literal census path came from
a peer session rather than from here.

Usage: hook_census.py HOOKS_JSON CENSUS_TSV MCP_JSONL [TRANSCRIPT ...]
"""
from __future__ import annotations

import json
import pathlib
import sys

#: Declared silent on a healthy virgin box. Absence from the TRANSCRIPT is
#: expected for these and means nothing; absence from the SHIM roster is a
#: real finding, because the shim runs before the handler can decline to speak.
SILENT_BY_DESIGN = {"nx-hook preflight", "behaviour_census.py"}

#: Removed from the staged manifest by run.sh because they would mutate the
#: wheel under test: `upgrade-auto` installs a generation and flips
#: <tools>/current, `self-gc` reaps generations. The denominator is the
#: ORIGINAL manifest, so they must be named as excluded rather than counted
#: as absent -- a harness's own exclusions appearing as findings is how a
#: census loses the reader's trust in the findings that are real.
EXCLUDED_BY_HARNESS = {"nx-hook upgrade-auto", "nx-hook self-gc"}

#: Real, documented events (code.claude.com/docs/en/hooks.md) that this
#: harness cannot provoke. Verified with claude-code-guide rather than
#: guessed, because "the event name is wrong" and "the event did not occur"
#: are the same observation and only one of them is a defect:
#:   PostCompact fires only when compaction ACTUALLY occurs -- `/compact` on
#:     a short session has nothing to compact, so the turn completes and no
#:     hook runs.
#:   StopFailure fires only on an API error, which this run does not induce.
NOT_PROVOKED = {
    "hook_post_compact": "PostCompact fires only when compaction actually occurs",
    "hook_stop_failure": "StopFailure fires only on an API error",
}


def label(entry: dict) -> str:
    """One command-tier entry's census name; run.sh's shims write the same.

    ``nx-hook <verb>``; ``nx_hook_shim.py <verb>`` for a verb wired through the
    stdlib shim (nexus-rcoze), because a bare script name would fold every shim
    entry into one handler and one firing would mark them all fired; otherwise
    the script's file name, or the bare command.
    """
    args = [a for a in (entry.get("args") or []) if isinstance(a, str)]
    cmd = entry.get("command") or "?"
    if cmd == "nx-hook" and args:
        return f"nx-hook {args[0]}"
    if args and pathlib.Path(args[0]).name == "nx_hook_shim.py" and len(args) == 2:
        return f"nx_hook_shim.py {args[1]}"
    return pathlib.Path(args[0]).name if args else cmd


def declared(hooks_json: pathlib.Path) -> dict[str, str]:
    """``handler -> the events it is declared on``.

    Accumulates rather than assigns: ``hook_auto_approve`` is declared on both
    ``PreToolUse`` and ``PermissionRequest``, and a plain assignment kept only
    the last, reporting 24 handlers against a 25-entry manifest.
    """
    data = json.loads(hooks_json.read_text())
    events: dict[str, list[str]] = {}
    for event, groups in data["hooks"].items():
        for group in groups:
            for entry in group.get("hooks", []):
                if entry.get("type") == "mcp_tool":
                    name = entry.get("tool")
                else:
                    name = label(entry)
                if name:
                    events.setdefault(name, []).append(event)
    return {n: ",".join(sorted(set(e))) for n, e in events.items()}


def command_roster(tsv: pathlib.Path) -> tuple[set[str], int]:
    """Every handler the shims recorded, by both full and bare name."""
    if not tsv.is_file():
        return set(), 0
    seen: set[str] = set()
    rows = 0
    for line in tsv.read_text(errors="replace").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[1].strip():
            rows += 1
            name = parts[1].strip()
            seen.add(name)
            seen.add(pathlib.Path(name.split()[-1]).name)
    return seen, rows


def tool_roster(jsonl: pathlib.Path) -> tuple[set[str], int]:
    """``hook_*`` tool names seen on the MCP server's stdin."""
    if not jsonl.is_file():
        return set(), 0
    seen: set[str] = set()
    calls = 0
    for chunk in jsonl.read_text(errors="replace").splitlines():
        if "hook_" not in chunk:
            continue
        name = None
        try:
            msg = json.loads(chunk)
        except ValueError:
            msg = None
        if isinstance(msg, dict):
            params = msg.get("params")
            if isinstance(params, dict) and isinstance(params.get("name"), str):
                name = params["name"]
        if name and name.startswith("hook_"):
            seen.add(name)
            calls += 1
            continue
        # Frames may be batched or split across lines; a scan keeps a parse
        # failure from reading as "the tool never fired".
        for token in chunk.replace('"', " ").replace(",", " ").split():
            token = token.strip()
            if token.startswith("hook_"):
                seen.add(token)
                calls += 1
    return seen, calls


def transcript_trouble(paths: list[pathlib.Path]) -> list[str]:
    out: list[str] = []
    for path in paths:
        if not path.is_file():
            continue
        for line in path.read_text(errors="replace").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            att = entry.get("attachment")
            if isinstance(att, dict) and str(att.get("type", "")).startswith("hook"):
                who = att.get("command") or att.get("hookName")
                rc = att.get("exitCode")
                if isinstance(rc, int) and rc != 0:
                    out.append(f"exitCode={rc} from {who}")
                if att.get("type") == "hook_error":
                    out.append(f"hook_error from {who}")
            if entry.get("type") == "system":
                for err in entry.get("hookErrors") or []:
                    out.append(f"{entry.get('subtype')}: {err}")
    return out


def main(argv: list[str]) -> int:
    decl = declared(pathlib.Path(argv[1]))
    cmd_seen, cmd_rows = command_roster(pathlib.Path(argv[2]))
    tool_seen, tool_calls = tool_roster(pathlib.Path(argv[3]))
    trouble = transcript_trouble([pathlib.Path(p) for p in argv[4:]])
    fired = cmd_seen | tool_seen

    print(f"declared handlers : {len(decl)}")
    print(f"command-tier rows : {cmd_rows}  (shim roster, includes silent)")
    print(f"tool-tier calls   : {tool_calls}  (MCP stdin, includes silent)")
    print()
    for name in sorted(decl):
        mark = "FIRED  " if name in fired else "  --   "
        print(f"  {mark} {decl[name]:<26} {name}")

    # NON-VACUITY IN TWO CLAUSES. The first catches a harness that observed
    # nothing. The second catches a roster that only ever sees LOUD hooks,
    # which would satisfy the first while being blind to exactly the class
    # this census replaced the transcript to see.
    if cmd_rows == 0 and tool_calls == 0:
        print()
        print("CENSUS MISSING: both rosters are empty, so this run examined "
              "NOTHING. Not a pass. Check the shims are executable in the "
              "image and that their census path is a baked literal -- command "
              "hooks run under a stripped env, so a path read from $VAR "
              "appends to nothing and still exits 0.")
        return 2

    silent_present = SILENT_BY_DESIGN & fired
    if cmd_rows and not silent_present:
        print()
        print("CENSUS SUSPECT: no silent-by-design handler "
              f"({', '.join(sorted(SILENT_BY_DESIGN))}) appears in the shim "
              "roster. The shim runs BEFORE the handler can decline to speak, "
              "so a silent handler must still leave a row. Seeing none means "
              "the roster is catching only handlers that produce output, "
              "which is the blindness this census exists to remove.")
        return 2

    rc = 0
    absent = set(decl) - fired
    excluded = sorted(absent & EXCLUDED_BY_HARNESS)
    unprovoked = sorted(absent & set(NOT_PROVOKED))
    never = sorted(absent - set(excluded) - set(unprovoked))

    if excluded:
        print()
        print("EXCLUDED BY THIS HARNESS (cannot fire, by construction):")
        for name in excluded:
            print(f"  {name}  -- dropped from the staged manifest; it would "
                  f"replace the wheel under test")
    if unprovoked:
        print()
        print("NOT PROVOKED BY THIS RUN (real events, verified documented):")
        for name in unprovoked:
            print(f"  {name}  -- {NOT_PROVOKED[name]}")
        print("These are gaps in the PROVOCATION SET, not in the hooks. Saying "
              "so is the point: a run that cannot trigger an event has no "
              "opinion about its handler.")
    if never:
        print()
        print(f"NEVER FIRED ({len(never)}), unexplained, by name:")
        for name in never:
            print(f"  {decl[name]:<26} {name}")
        print("Each is a candidate fail-open. Decide per handler; do not "
              "total them.")
        rc = 1

    if trouble:
        print()
        print(f"TROUBLE ({len(trouble)}):")
        for line in trouble[:20]:
            print(f"  {line[:200]}")
        rc = 1

    if rc == 0:
        covered = len(decl) - len(excluded) - len(unprovoked)
        print()
        print(f"Every provocable handler fired: {covered} of {len(decl)} "
              f"declared, with {len(excluded)} excluded by the harness and "
              f"{len(unprovoked)} not provocable here. Silent ones included "
              f"({', '.join(sorted(silent_present))}), which is what proves "
              f"the roster sees quiet hooks. No error line.")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
