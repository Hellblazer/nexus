#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run ``nx-hook <verb>`` so an older installed CLI cannot block a session.

hooks.json ships with the plugin; ``nx-hook`` ships with the CLI, and a plugin
can update before its CLI does. ``nx-hook`` from conexus 7.55.0 through 7.57.x
exits 2 on a verb it does not register, and on ``UserPromptSubmit``,
``PreToolUse`` or ``PermissionRequest`` exit 2 BLOCKS: every prompt, Bash call
or tool call stops until the CLI catches up (nexus-t9klx, found by the 7.58.0
release battery). From 7.58.0 ``nx-hook`` fails open on an unknown verb itself,
but the CLIs already installed will not change, so any hooks.json entry naming a
verb one of them lacks goes through this shim instead
(tests/test_hooks_json_verb_release_floor.py enforces which).

The shim changes exactly one outcome. When ``nx-hook`` exits 2 AND its stderr is
the fail-closed releases' unknown-verb line, the shim notes the skip on stderr
and exits 0. Every other result passes through untouched, so a real verb that
denies with exit 2 still denies. When there is no ``nx-hook`` at all (a CLI
older than 7.55.0, or none), the shim also exits 0 with a note: a missing CLI
must not block a session either, and the version-lockstep hook is what repairs
it.

Standard library only, and no ``nexus`` import: it has to run under whatever
CLI is installed, including none. It sets no timeout of its own; the entry's
``timeout`` in hooks.json bounds the whole call. When that timeout, or anything
else, signals the shim with SIGTERM, SIGINT or SIGHUP, the shim terminates the
``nx-hook`` child before it exits, so the handler is not left running orphaned
(review finding, nexus-rcoze). A SIGKILL cannot be forwarded; the verbs bound
their own subprocess work.
"""
from __future__ import annotations

import re
import signal
import subprocess
import sys

#: What ``nx-hook`` prints for an unregistered verb in every release that exits
#: 2 on one (7.55.0 through 7.57.x), from ``main()`` in
#: ``src/nexus/_hook_runtime/entry.py`` at those tags. Pinned against the tag
#: text by tests/hooks/test_nx_hook_shim.py.
UNKNOWN_VERB_LINE = re.compile(
    r"^nx-hook: unknown verb '[^']*' -- no hook is registered under that name$",
    re.MULTILINE,
)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        sys.stderr.write("nx_hook_shim: expected exactly one argument, the nx-hook verb\n")
        return 0
    verb = argv[0]
    payload = sys.stdin.buffer.read()
    try:
        proc = subprocess.Popen(
            ["nx-hook", verb],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        sys.stderr.write(
            f"conexus: `nx-hook` is not installed, so the {verb} hook is skipped. "
            "Upgrade the conexus CLI (nx self install).\n"
        )
        return 0

    def _forward(signum: int, _frame: object) -> None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        sys.exit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, _forward)
    out, err = proc.communicate(payload)
    stderr = err.decode("utf-8", errors="replace")
    if proc.returncode == 2 and UNKNOWN_VERB_LINE.search(stderr):
        sys.stderr.write(
            f"conexus: the installed CLI does not know the {verb} hook yet, so it "
            "is skipped this session. The plugin is newer than the CLI; restart "
            "Claude Code after the upgrade the version-lockstep hook starts.\n"
        )
        return 0
    sys.stdout.buffer.write(out)
    sys.stdout.buffer.flush()
    sys.stderr.write(stderr)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
