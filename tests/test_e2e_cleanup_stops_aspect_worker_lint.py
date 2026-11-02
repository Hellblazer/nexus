# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""A gate's teardown stops the aspect worker before it stops the service.

``nx daemon service stop`` leaves the aspect-worker daemon running by design
(RDR-224: it belongs to the store path). A gate whose ``nx`` calls ran against
its scratch service therefore spawned a worker (spawn-if-absent) that outlived
the gate and its deleted scratch directory: 24 ppid-1 orphans were found on one
laptop, the oldest from Oct 31 (nexus-wl9pk).

Static pin over three families of script. Whole-line comments are ignored, so a
comment that names the verb satisfies nothing:

* ``tests/e2e/**/*.sh`` and ``scripts/spikes/**/run.sh``: each teardown, meaning
  a ``cleanup()`` or ``_stop_service()`` function or an EXIT trap line, that
  contains ``daemon service stop`` also contains ``daemon aspect-worker stop``
  (or a call to a function of the same file that does) EARLIER;
* ``tests/e2e/lib/*.py``: each ``stop_service``/``cleanup`` function that
  stops the service stops the worker earlier (an argv list is read as the
  command it spells);
* ``tests/e2e/*.ps1`` (the Windows gate): it defines a function that runs the
  verb, calls it from a ``finally`` block (every exit path), and calls it
  before its LAST ``daemon service stop``. It stops the worker after A4's
  first stop, not before it, because A4 observes that the stop left the worker
  running.

The worker goes first so its final reclaim sweep still has an engine to ask.
Not covered: a teardown with another name, and a script that never stops the
service. Non-vacuity: every script that leaked must have a scanned teardown.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = Path(__file__).resolve().parent.parent
_E2E = REPO_ROOT / "tests" / "e2e"
_SPIKES = REPO_ROOT / "scripts" / "spikes"

_SHELL_FUNCTION = re.compile(r"^(\w+)\(\)\s*\{\n(.*?)^\}", re.M | re.S)
_PS_FUNCTION = re.compile(r"^function ([\w-]+)[^\n]*\{\n(.*?)^\}", re.M | re.S)
_TEARDOWN_NAMES = frozenset({"cleanup", "_stop_service"})
_TRAP = re.compile(r"^[^#\n]*\btrap\s+'([^'\n]*daemon service stop[^'\n]*)'\s+EXIT", re.M)
_PY_DEF = re.compile(r"^([ \t]*)def (?:stop_service|cleanup)\w*\(.*?\)[^\n]*:\n", re.M)
_SERVICE_STOP = "daemon service stop"
_WORKER_STOP = "daemon aspect-worker stop"

#: Scripts that leaked a worker; each must have a scanned teardown.
_MUST_BE_SCANNED = (
    "local-service-gate.sh",
    "dual-embedder-gate.sh",
    "fresh-install-mvv.sh",
    "benchmark-corpus.sh",
    "pdf-on-demand-rehearsal.sh",
    "setup_journey.py",
    "windows-phase5-gate.ps1",
    "run.sh",  # scripts/spikes/rdr210-p43
)


@dataclass(frozen=True)
class Teardown:
    """A comment-free body that stops the service, plus the names of functions of
    its own file that stop the worker (a call to one counts as the stop)."""

    where: str
    body: str
    helpers: tuple[str, ...] = ()

    def worker_stops_first(self) -> bool:
        hits = [i for i in (self.body.find(t) for t in (_WORKER_STOP, *self.helpers)) if i >= 0]
        return bool(hits) and min(hits) < self.body.find(_SERVICE_STOP)


def strip_comments(text: str) -> str:
    """Drop whole-line comments (``#``) from shell, Python and PowerShell text."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#")) + "\n"


def spell(text: str) -> str:
    """Read an argv list as the command it spells: ``"daemon", "service", "stop"``
    (Python) and ``'daemon', 'service', 'stop'`` (PowerShell) become
    ``daemon service stop``."""
    return re.sub(r"[\"']\s*,\s*[\"']", " ", text)


def shell_teardowns(text: str, where: str = "") -> list[Teardown]:
    code = strip_comments(text)
    functions = {m.group(1): m.group(2) for m in _SHELL_FUNCTION.finditer(code)}
    helpers = tuple(n for n, body in functions.items() if _WORKER_STOP in body and n not in _TEARDOWN_NAMES)
    bodies = [body for name, body in functions.items() if name in _TEARDOWN_NAMES]
    bodies += [m.group(1) for m in _TRAP.finditer(code)]
    return [Teardown(where, b, helpers) for b in bodies if _SERVICE_STOP in b]


def python_teardowns(text: str, where: str = "") -> list[Teardown]:
    code = strip_comments(text)
    found = []
    for m in _PY_DEF.finditer(code):
        indent = len(m.group(1))
        lines = []
        for line in code[m.end():].splitlines():
            if line.strip() and len(line) - len(line.lstrip()) <= indent:
                break
            lines.append(line)
        body = spell("\n".join(lines))
        if _SERVICE_STOP in body:
            found.append(Teardown(where, body))
    return found


def powershell_problem(text: str) -> str | None:
    """Why the Windows gate's worker stop is not what the lint demands, or None."""
    code = spell(strip_comments(text))
    named = [m for m in _PS_FUNCTION.finditer(code) if _WORKER_STOP in m.group(2)]
    if not named:
        return f"no function runs `{_WORKER_STOP}`"
    name = named[0].group(1)
    outside = code.replace(named[0].group(0), "")
    calls = [m.start() for m in re.finditer(rf"\b{re.escape(name)}\b", outside)]
    if not calls:
        return f"{name} is never called"
    if not re.search(rf"\bfinally\s*\{{[^}}]*\b{re.escape(name)}\b", outside):
        return f"{name} is not called from a finally block"
    last_service_stop = outside.rfind(_SERVICE_STOP)
    if last_service_stop >= 0 and not any(c < last_service_stop for c in calls):
        return f"{name} is not called before the last `{_SERVICE_STOP}`"
    return None


def _scripts() -> dict[str, list[Path]]:
    return {
        "shell": sorted({*_E2E.rglob("*.sh"), *_SPIKES.rglob("run.sh")}),
        "python": sorted((_E2E / "lib").glob("*.py")),
        "powershell": sorted(_E2E.glob("*.ps1")),
    }


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _scanned() -> tuple[dict[str, list[Teardown]], dict[str, str | None]]:
    """(shell and Python teardowns by file, PowerShell problem by file) for every
    script that stops the service."""
    teardowns: dict[str, list[Teardown]] = {}
    scripts = _scripts()
    for path in scripts["shell"]:
        found = shell_teardowns(_read(path), str(path.relative_to(REPO_ROOT)))
        if found:
            teardowns[path.name] = found
    for path in scripts["python"]:
        found = python_teardowns(_read(path), str(path.relative_to(REPO_ROOT)))
        if found:
            teardowns[path.name] = found
    ps: dict[str, str | None] = {}
    for path in scripts["powershell"]:
        text = _read(path)
        if _SERVICE_STOP in spell(strip_comments(text)):
            ps[path.name] = powershell_problem(text)
    return teardowns, ps


def test_the_scan_covers_every_script_that_leaked() -> None:
    teardowns, ps = _scanned()
    seen = {*teardowns, *ps}
    missing = [name for name in _MUST_BE_SCANNED if name not in seen]
    assert not missing, f"no service-stopping teardown found in {missing}; scanned {sorted(seen)}"


def test_every_teardown_stops_the_worker_before_the_service() -> None:
    teardowns, ps = _scanned()
    bad = [
        f"{t.where}: {t.body.strip().splitlines()[0][:100]}"
        for found in teardowns.values() for t in found if not t.worker_stops_first()
    ]
    bad += [f"{name}: {problem}" for name, problem in ps.items() if problem]
    assert not bad, (
        "teardowns that stop the service without first stopping the aspect worker "
        "(add `nx daemon aspect-worker stop`; nexus-wl9pk):\n" + "\n".join(bad)
    )


# -- the lint's own reading, on synthetic text (each can fail) ---------------------------------


def test_a_comment_that_names_the_verb_is_not_a_stop() -> None:
    text = (
        "cleanup() {\n"
        "  # nx daemon aspect-worker stop would go here\n"
        "  uv run nx daemon service stop --with-pg\n"
        "}\n"
    )
    (item,) = shell_teardowns(text)
    assert not item.worker_stops_first()


def test_a_stop_after_the_service_stop_does_not_count() -> None:
    text = "cleanup() {\n  uv run nx daemon service stop\n  uv run nx daemon aspect-worker stop\n}\n"
    (item,) = shell_teardowns(text)
    assert not item.worker_stops_first()


def test_a_helper_function_that_stops_the_worker_counts_at_its_call_site() -> None:
    text = (
        "_stop_worker() {\n  uv run nx daemon aspect-worker stop\n}\n"
        "cleanup() {\n  _stop_worker\n  uv run nx daemon service stop\n}\n"
        "trap '_stop_worker; nx daemon service stop' EXIT\n"
    )
    items = shell_teardowns(text)
    assert len(items) == 2 and all(i.worker_stops_first() for i in items)
    without = text.replace("  _stop_worker\n  uv run nx daemon service stop", "  uv run nx daemon service stop")
    assert not all(i.worker_stops_first() for i in shell_teardowns(without))


def test_python_argv_lists_are_read_as_the_command_they_spell() -> None:
    good = (
        "class S:\n"
        "    def stop_service(self):\n"
        "        # nx daemon aspect-worker stop\n"
        '        self.run(["nx", "daemon", "aspect-worker", "stop"])\n'
        '        self.run(["nx", "daemon", "service", "stop"])\n'
    )
    bad = good.replace('"aspect-worker", "stop"', '"nothing", "here"')
    (g,) = python_teardowns(good)
    (b,) = python_teardowns(bad)
    assert g.worker_stops_first()
    assert not b.worker_stops_first()


def test_the_powershell_rules() -> None:
    fn = "function Stop-AspectWorker {\n  Invoke-Nx 'x' @('daemon', 'aspect-worker', 'stop')\n}\n"
    good = fn + "try {\n  Stop-AspectWorker\n  Invoke-Nx 'y' @('daemon', 'service', 'stop')\n} finally {\n  Stop-AspectWorker\n}\n"
    assert powershell_problem(good) is None
    assert "finally" in (powershell_problem(good.replace("} finally {\n  Stop-AspectWorker\n}", "}")) or "")
    after = fn + "try {\n  Invoke-Nx 'y' @('daemon', 'service', 'stop')\n} finally {\n  Stop-AspectWorker\n}\n"
    assert "before the last" in (powershell_problem(after) or "")
    commented = good.replace(fn, "# @('daemon', 'aspect-worker', 'stop')\n")
    assert powershell_problem(commented) is not None
