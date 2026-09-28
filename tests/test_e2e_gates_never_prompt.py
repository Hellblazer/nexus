# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-wvyvn: a gate script never lets ``nx init`` prompt.

``nx init`` asks whether to register the storage service as a login unit
whenever stdin is a TTY (``commands/init.py`` ``_decide_autostart``, its only
prompt). ``tests/e2e/local-service-gate.sh`` ran it bare, so under tmux the
gate sat on a question nobody could see (hellmini, 2026-09-28). Answering yes
would be worse: a gate would write a real launchd/systemd unit on the host.
Every ``nx init`` in a gate or harness script therefore carries
``--no-autostart``, which wins over every other input. The one exception is
a script that runs only inside its own container (the migration rehearsal,
``*_in_container.sh``): it answers ``--yes`` on purpose, because the unit it
registers is the container's and the script walks the path a user takes.
"""
from __future__ import annotations

import pathlib
import re
import sys

import pytest

pytestmark = pytest.mark.lint

REPO = pathlib.Path(__file__).resolve().parent.parent
ROOTS = (REPO / "tests" / "e2e", REPO / "tests" / "cc-validation", REPO / "scripts")

#: Scripts that only ever run inside their own container, where --yes is
#: allowed (it never prompts, and the unit it registers is the container's).
def _container_only(path: str) -> bool:
    return path.startswith("tests/e2e/migration-rehearsal/") or path.endswith("_in_container.sh")

#: A double-quoted string holding a command substitution is code, not text:
#: ``OUT="$(nx init --yes 2>&1)"`` runs nx init (review of 5ded8b067).
_QUOTED = re.compile(r""""(?:[^"$]|\$(?!\())*"|'[^']*'""")

#: An invocation, not prose: `nx init` / `_nx init` at a command position.
_INIT = re.compile(r"(?:^|[\s;&|(])_?nx init\b")


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """Physical lines joined across trailing-backslash continuations, with
    the first line's number, and comment-only lines dropped."""
    out: list[tuple[int, str]] = []
    buf, start = "", 0
    for n, line in enumerate(text.splitlines(), 1):
        if not buf:
            start = n
        if line.rstrip().endswith("\\"):
            buf += line.rstrip()[:-1] + " "
            continue
        buf += line
        if not buf.lstrip().startswith("#"):
            out.append((start, buf))
        buf = ""
    return out


def _invocations() -> list[tuple[str, int, str]]:
    found = []
    for root in ROOTS:
        for path in sorted(root.rglob("*.sh")):
            for n, line in _logical_lines(path.read_text()):
                # Quoted text is a message about nx init, never a call to it.
                code = _QUOTED.sub('""', line).split(" #", 1)[0]
                if _INIT.search(code):
                    found.append((str(path.relative_to(REPO)), n, code.strip()))
    return found


def test_every_nx_init_in_a_script_passes_no_autostart() -> None:
    found = _invocations()
    assert len(found) >= 15, f"only {len(found)} nx init invocations found; the scan is broken"
    bad = [
        f"{p}:{n}: {c}" for p, n, c in found
        if "--no-autostart" not in c
        and not (_container_only(p) and re.search(r"\s(--yes|-y)\b", c))
    ]
    assert not bad, "nx init without --no-autostart (prompts on a TTY):\n" + "\n".join(bad)


def test_a_call_inside_a_quoted_command_substitution_is_still_seen(tmp_path, monkeypatch) -> None:
    script = tmp_path / "tests" / "e2e" / "probe.sh"
    script.parent.mkdir(parents=True)
    script.write_text('OUT="$(nx init --service 2>&1)"\nsay "nx init is next"\n')
    mod = sys.modules[__name__]
    monkeypatch.setattr(mod, "ROOTS", (tmp_path / "tests" / "e2e",))
    monkeypatch.setattr(mod, "REPO", tmp_path)
    assert [(p, n) for p, n, _ in _invocations()] == [("tests/e2e/probe.sh", 1)]
