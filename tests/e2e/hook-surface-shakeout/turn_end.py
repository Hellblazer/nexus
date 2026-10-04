# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stop hook: touch a per-session turn-end sentinel.

The pattern is lifted from ~/git/recording-rig (`lib/sentinels.sh`,
`hooks/hooks.json.tmpl`), which drives Claude Code by hook sentinels rather
than by scraping the TUI, and which this repo's own
`tests/cc-validation/README.md` already named as the robustness upgrade it
had not yet taken. The RDR-208 MVV took it after a billed run was lost to
pane scraping: the prompt's echo carries the token the prompt asks for, and
anchoring on a reply marker made the harness depend on how one Claude Code
version renders a turn.

The rig templates one sentinel name per run because it drives one session.
This journey runs four or five sessions against ONE plugin directory, so the
session id comes from the hook's own stdin payload instead, which makes the
sentinel per-session with no templating at all.

Writes `<RUN>/turn-end.<session_id>`. Never raises and always exits 0: a Stop
hook that fails is a hook that interrupts the session it is only observing.
"""
import json
import os
import pathlib
import sys

RUN = pathlib.Path(os.environ.get("MVV_RUN", "/home/nexus/run"))


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        session_id = str(payload.get("session_id") or "").strip()
        if session_id and all(c.isalnum() or c in "._-" for c in session_id):
            RUN.mkdir(parents=True, exist_ok=True)
            (RUN / f"turn-end.{session_id}").touch()
    except Exception:  # noqa: BLE001 — an observer must never break the turn it observes
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
