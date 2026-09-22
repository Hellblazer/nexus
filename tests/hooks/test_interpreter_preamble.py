# SPDX-License-Identifier: AGPL-3.0-or-later
"""The exec-form hooks resolve their own interpreter (RDR-215, nexus-q02nx.21).

Bead .21 re-declared the five plugin-resident Python hooks as
``{"command": "python3", "args": [<script>]}``, which threw away everything
``_run_python_hook.sh`` decided. ``_interpreter.reexec_if_needed()`` puts it
back in Python, and these tests pin both halves of what was lost: the 3.12
floor (four of the five refuse to run below it, and one of those is the
routing framework's only ``fail_closed`` rule, which then fails OPEN) and
the generation python that is the only interpreter guaranteed to import
``nexus`` (the nexus-owna8 misread class, which bites at 3.13 too).
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "conexus" / "hooks" / "scripts"
MODULE = SCRIPTS / "_interpreter.py"
HOOKS_JSON = REPO_ROOT / "conexus" / "hooks" / "hooks.json"

PREAMBLE_SCRIPTS = [
    "mailbox_drain.py",
    "routing/subagent_git_write_requires_orchestrator.py",
    "routing/phase_review_close_requires_gate.py",
]
"""The ones still plugin-resident that refuse to run below 3.12, measured
rather than read off a guard.

Was four, and ``behaviour_census.py`` was a declared fifth exec-form entry
deliberately absent because it is genuinely stdlib-3.9-safe. nexus-t9klx
ported both it and ``version_lockstep_hook.py`` into the wheel, where the
interpreter is the one conexus was installed under and there is no
preamble to assert. This list shrinks to nothing as that bead finishes,
and this module is DELETED with the last entry rather than left asserting
over an empty list — the preamble it pins exists only in plugin-resident
scripts.
"""


def _declared_python3_scripts() -> list[str]:
    """Every ``{"command": "python3"}`` script path hooks.json declares."""
    data = json.loads(HOOKS_JSON.read_text())
    events = data.get("hooks", data)
    out = []
    for entries in events.values():
        for entry in entries:
            for sub in (entry.get("hooks", [entry]) if isinstance(entry, dict) else []):
                if isinstance(sub, dict) and sub.get("command") == "python3":
                    for arg in sub.get("args", []):
                        m = re.search(r"hooks/scripts/(.+\.py)$", str(arg))
                        if m:
                            out.append(m.group(1))
    return out


def _old_python() -> str | None:
    """A real interpreter below 3.12, or None on a box that has none."""
    for cand in ("/usr/bin/python3", shutil.which("python3.9"), shutil.which("python3.11")):
        if not cand or not os.path.exists(cand):
            continue
        probe = subprocess.run(
            [cand, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
            capture_output=True, text=True, timeout=30,
        )
        if probe.returncode != 0:
            continue
        try:
            major, minor = (int(x) for x in probe.stdout.strip().split("."))
        except ValueError:
            continue
        if (major, minor) < (3, 12):
            return cand
    return None


def _fake_generation(tools: Path, marker: str) -> None:
    """A stand-in generation python that announces itself, then really runs.

    The ``-c ""`` branch answers ``_runs()``'s probe without spawning a real
    CPython. That is a cheapness measure only, and it is NOT the cause of the
    intermittent failure below — that was this author's first hypothesis and
    it was wrong: with it in place the test still failed a full ``-n auto``
    run, and ``resolve()`` was measured picking the generation correctly 12
    times out of 12 at load average 69. The branch is kept because a probe
    asks one question, "does this executable start", and answering it without
    a process spawn is a truthful answer; it is narrowed to the EXACT empty
    probe so a genuine ``-c <code>`` re-exec is never swallowed.
    """
    gen = tools / "gen-test"
    (gen / "bin").mkdir(parents=True)
    py = gen / "bin" / "python"
    py.write_text(
        "#!/bin/sh\n"
        '[ "$1" = "-c" ] && [ -z "$2" ] && exit 0\n'
        f"echo {marker} >&2\n"
        f'exec {sys.executable} "$@"\n'
    )
    py.chmod(py.stat().st_mode | stat.S_IXUSR)
    (tools / "current").symlink_to(gen)


def _env(**over: str) -> dict[str, str]:
    """A clean env: the suite's own venv would win over any fixture."""
    env = {k: v for k, v in os.environ.items()
           if k not in {"VIRTUAL_ENV", "NX_HOOK_PYTHON", "NX_TOOLS_DIR",
                        "NX_HOOK_INTERPRETER_REEXEC"}}
    env.update(over)
    return env


class TestTheMechanism:
    """Always runs -- the non-vacuity floor under the measured tests below."""

    def test_it_reexecs_into_the_generation_python(self, tmp_path: Path) -> None:
        """THE DIAGNOSTIC LINE IS PART OF THE TEST, not debug residue.

        This assertion failed intermittently in full ``-n auto`` runs (bead
        nexus-q02nx.22) reporting ``assert 'GEN-PY-RAN' in ''`` -- an EMPTY
        stderr, with rc 0 and correct stdout. Two different faults produce
        that byte-for-byte and the failure could not tell them apart:

        * ``resolve()`` picked a DIFFERENT interpreter (say ``python3.13`` by
          name, because the generation probe lost its 1.5 s race), re-exec'd
          into it successfully, and that interpreter emits no marker; or
        * ``resolve()`` picked the generation correctly and ``os.execv``
          ITSELF failed -- under process-table pressure it raises EAGAIN, and
          ``reexec_if_needed`` swallows ``OSError`` by design, returning
          silently so the caller's own version guard stays the backstop.
          Reproduced deliberately by raising EAGAIN from ``os.execv``: rc 0,
          ``hook ran ['an-arg']``, stderr ``''``. Byte-identical.

        So the hook now prints what ``resolve()`` returned BEFORE trying to
        act on it. stderr can no longer be empty, and the next failure says
        which of the two happened instead of leaving the reader to guess --
        which is what happened here, at the cost of one wrong fix.
        """
        tools = tmp_path / "tools"
        _fake_generation(tools, "GEN-PY-RAN")
        script = tmp_path / "hook.py"
        script.write_text(
            f"import sys; sys.path.insert(0, {str(SCRIPTS)!r})\n"
            "import _interpreter\n"
            "sys.stderr.write('RESOLVED=%r\\n' % (_interpreter.resolve(),))\n"
            "_interpreter.reexec_if_needed()\n"
            "print('hook ran', sys.argv[1:])\n"
        )
        proc = subprocess.run(
            [sys.executable, str(script), "an-arg"],
            env=_env(NX_TOOLS_DIR=str(tools)),
            capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        # Non-vacuity: the diagnostic must actually be there, or the two
        # assertions below are reading a stream nothing wrote to.
        assert "RESOLVED=" in proc.stderr, proc.stderr
        assert str(tools) in proc.stderr, (
            "resolve() did not pick the generation python -- so this is the "
            f"resolution half, not the exec half.\nstderr: {proc.stderr!r}"
        )
        assert "GEN-PY-RAN" in proc.stderr, (
            "resolve() picked the generation (see RESOLVED= above) but the "
            "marker never ran, so the re-exec itself did not happen -- "
            "os.execv failing is swallowed by design in reexec_if_needed.\n"
            f"stderr: {proc.stderr!r}"
        )
        assert proc.stdout.strip() == "hook ran ['an-arg']", proc.stdout

    def test_the_marker_stops_a_second_pass(self, tmp_path: Path) -> None:
        """Otherwise an interpreter that resolves differently loops forever."""
        tools = tmp_path / "tools"
        _fake_generation(tools, "GEN-PY-RAN")
        script = tmp_path / "hook.py"
        script.write_text(
            f"import sys; sys.path.insert(0, {str(SCRIPTS)!r})\n"
            "import _interpreter; _interpreter.reexec_if_needed()\n"
            "print('once')\n"
        )
        proc = subprocess.run(
            [sys.executable, str(script)],
            env=_env(NX_TOOLS_DIR=str(tools), NX_HOOK_INTERPRETER_REEXEC="1"),
            capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        assert "GEN-PY-RAN" not in proc.stderr, "re-exec'd despite the marker"
        assert proc.stdout.strip() == "once"

    def test_an_unrunnable_generation_falls_through(self, tmp_path: Path) -> None:
        """A partially reaped or wrong-arch generation must not be exec'd into."""
        tools = tmp_path / "tools"
        gen = tools / "gen-broken"
        (gen / "bin").mkdir(parents=True)
        py = gen / "bin" / "python"
        py.write_text("#!/nonexistent/interpreter\n")
        py.chmod(py.stat().st_mode | stat.S_IXUSR)
        (tools / "current").symlink_to(gen)
        script = tmp_path / "hook.py"
        script.write_text(
            f"import sys; sys.path.insert(0, {str(SCRIPTS)!r})\n"
            "import _interpreter; _interpreter.reexec_if_needed()\n"
            "print('fell through')\n"
        )
        proc = subprocess.run(
            [sys.executable, str(script)],
            env=_env(NX_TOOLS_DIR=str(tools)),
            capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "fell through"

    def test_an_explicit_choice_outranks_the_generation(self, tmp_path: Path) -> None:
        """$NX_HOOK_PYTHON is the dev-box and test override, so it wins."""
        tools = tmp_path / "tools"
        _fake_generation(tools, "GEN-PY-RAN")
        chosen = tmp_path / "chosen-python"
        chosen.write_text(f"#!/bin/sh\nexec {sys.executable} \"$@\"\n")
        chosen.chmod(chosen.stat().st_mode | stat.S_IXUSR)
        got = self._resolve(NX_TOOLS_DIR=str(tools), NX_HOOK_PYTHON=str(chosen))
        assert got == str(chosen), f"expected the explicit choice, got {got}"

    def test_the_generation_outranks_a_named_python(self, tmp_path: Path) -> None:
        """The generation python is the only one guaranteed to import nexus;
        a bare python3.13 is what made the rdr hook misread (nexus-owna8)."""
        tools = tmp_path / "tools"
        _fake_generation(tools, "GEN-PY-RAN")
        got = self._resolve(NX_TOOLS_DIR=str(tools))
        assert got == str(tools / "current" / "bin" / "python"), got

    def test_it_falls_through_to_a_named_python(self, tmp_path: Path) -> None:
        tools = tmp_path / "empty"
        tools.mkdir()
        got = self._resolve(NX_TOOLS_DIR=str(tools))
        assert got is None or Path(got).name.startswith("python3.1"), got

    @staticmethod
    def _resolve(**over: str) -> str | None:
        """``resolve()``'s answer, from a subprocess with a controlled env."""
        proc = subprocess.run(
            [sys.executable, "-c",
             f"import sys; sys.path.insert(0, {str(SCRIPTS)!r});"
             " import _interpreter; print(_interpreter.resolve() or '')"],
            env=_env(**over), capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.strip() or None


class TestTheScriptsCarryIt:

    @pytest.mark.parametrize("rel", PREAMBLE_SCRIPTS)
    def test_the_preamble_precedes_the_imports_that_need_it(self, rel: str) -> None:
        text = (SCRIPTS / rel).read_text()
        assert "_interpreter.reexec_if_needed()" in text, f"{rel}: no preamble"
        call = text.index("_interpreter.reexec_if_needed()")
        for later in ("import _lib", "import _endpoint_resolve",
                      "if sys.version_info < (3, 12)"):
            if later in text:
                assert call < text.index(later), (
                    f"{rel}: the preamble must run before `{later}` -- that is "
                    f"what it exists to get ahead of"
                )

    def test_the_list_covers_every_bare_python3_entry(self) -> None:
        """A sixth exec-form hook must not join silently.

        Nothing is exempt any more; anything
        appearing here has never been checked against a 3.9 interpreter.
        """
        declared = set(_declared_python3_scripts())
        assert declared, "hooks.json declares no bare-python3 scripts; extractor is blind"
        unaccounted = declared - set(PREAMBLE_SCRIPTS)
        assert not unaccounted, (
            f"exec-form hooks with no interpreter preamble and no exemption: "
            f"{sorted(unaccounted)}"
        )


class TestMeasuredAgainstAnOldInterpreter:
    """The empirical half. ``TestTheMechanism`` above is the floor when a
    box has no interpreter below 3.12 to drive these with."""

    @pytest.mark.parametrize("rel", PREAMBLE_SCRIPTS)
    def test_it_runs_under_an_interpreter_below_3_12(self, rel: str) -> None:
        old = _old_python()
        if old is None:
            pytest.skip("no interpreter below 3.12 on this box")
        proc = subprocess.run(
            [old, str(SCRIPTS / rel)],
            input="{}", capture_output=True, text=True, timeout=120,
            env=_env(),
        )
        assert proc.returncode == 0, (
            f"{rel} exited {proc.returncode} under {old}. For "
            f"phase_review_close_requires_gate that is not a deny -- Claude Code "
            f"reads a non-zero exit with no envelope as a non-blocking error, so "
            f"the close gate fails OPEN.\n{proc.stderr[-2000:]}"
        )

class TestTheThingsPreviouslyAsserted:
    """Two premises this module stated in prose and nothing enforced.

    Both are the project's own "an assumption stated in prose that
    nothing enforces" class, and both are checkable without an old
    interpreter -- ``ast.parse(feature_version=...)`` needs only the
    grammar, so these run everywhere and floor the measured leg above,
    which skips on a box with no sub-3.12 python.
    """

    @pytest.mark.parametrize(
        "rel", [*PREAMBLE_SCRIPTS, "_interpreter.py"]
    )
    def test_it_parses_under_python_3_9(self, rel: str) -> None:
        """A 3.12-only construct anywhere in the file defeats the preamble.

        The re-exec runs at import time, which is after the whole module
        has been PARSED. A walrus in a comprehension or a `match` block
        is a SyntaxError on 3.9 before the preamble gets a turn, so the
        file never reaches the interpreter that could have served it.
        """
        src = (SCRIPTS / rel).read_text()
        try:
            ast.parse(src, filename=str(rel), feature_version=(3, 9))
        except SyntaxError as exc:  # pragma: no cover - the failure IS the message
            pytest.fail(
                f"{rel} does not parse as Python 3.9 ({exc.msg} at line "
                f"{exc.lineno}). The interpreter preamble cannot help: the "
                f"file is parsed in full before any of it runs."
            )

    # DELETED at nexus-t9klx: test_behaviour_census_is_genuinely_stdlib_only
    # and test_it_runs_under_an_interpreter_below_3_12. Both existed to
    # check that behaviour_census.py's exemption from PREAMBLE_SCRIPTS
    # still held -- that it imported no `nexus` and really did run under a
    # pre-3.12 interpreter. The file is in the wheel now, where it imports
    # `nexus` by design and runs under conexus's own interpreter, so the
    # exemption and both of its checks have no subject. Removed rather than
    # rewritten: there is nothing left for them to be about.
