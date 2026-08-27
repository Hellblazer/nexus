# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The live-holder flip, on real generations. nexus-utpuw.16 (P7a, FAST tier).

THE PROPERTY, and why it is asserted rather than "nothing crashed". nexus-q3xrx
is a CPython fact, not a nexus one: ``Modules/getpath.py`` looks for
``pyvenv.cfg`` next to the executable AS INVOKED, BEFORE resolving symlinks. So
an interpreter reached through ``<tools>/current`` takes ``current`` into
``sys.prefix`` and ``sys.path`` -- and the next flip silently retargets every
module that process has not imported yet. The concrete symptom was 95 cacert
tracebacks in a live session.

The shims exist to make that unconstructible: they ``readlink`` the pointer and
exec the RESOLVED generation, so a spawn binds to a generation for its whole
life. These tests pin that end to end against real venvs -- a real ``uv``
build, a real console script, a real deferred import -- because the defect
lives in the interaction between the installer's layout and CPython's startup,
and neither half can show it alone.

COST CONTROL (bead .16). The property is package-INDEPENDENT, so this runs
against a three-line fixture distribution rather than two real conexus builds:
seconds instead of 60-90s, which is what lets it sit in the fast tier and guard
the shim form on EVERY run. ``tests/e2e`` (.17) does the same ladder on the
real artifact.

WHY THE FIXTURE PACKAGE IS BUILT, NOT FABRICATED. ``_generation_harness``'s
``fabricate_generation`` writes a plausible-looking tree, which is right for
tests about layout bookkeeping and useless here: a fabricated tree has no
interpreter, so it cannot exhibit a CPython startup property. The whole subject
is what a REAL interpreter does with a path, so the venv has to be real.
"""
from __future__ import annotations

import os
import queue
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from _generation_harness import SAFE_BASE_PATH, fabricate_generation

_INSTALL = Path(__file__).resolve().parents[2] / "src" / "nexus" / "_install"
_DIST = "tinyhold"

_PYPROJECT = """\
[project]
name = "tinyhold"
version = "1.0.0"
requires-python = ">=3.12"

[project.scripts]
tinyhold = "tinyhold:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
"""

#: The holder. Answers one command per stdin line so the test can poke it AFTER
#: a flip. Written as a file rather than a bash heredoc deliberately: project
#: memory records Bash 5.3 heredocs over 512B deadlocking when macOS degrades
#: pipes, and this is the process on the far side of exactly such a pipe.
_INIT = '''\
import sys


def main() -> None:
    for line in sys.stdin:
        command = line.strip()
        if command == "prefix":
            print(sys.prefix, flush=True)
        elif command == "lazy":
            # NOT imported at startup. This is the deferred import a flip must
            # not be able to retarget -- the q3xrx shape exactly.
            from tinyhold import lazy

            print(f"{lazy.MARKER} {lazy.__file__}", flush=True)
        elif command == "exit":
            return
'''


#: A holder deliberately bound the WRONG way — through the pointer — so the
#: journey can exhibit the defect and the fix side by side across one flip.
#: Inline and tiny: it is passed as `python -c`, never through a pipe.
_POINTER_HOLDER = (
    "import sys\n"
    "for line in sys.stdin:\n"
    "    command = line.strip()\n"
    "    if command == 'lazy':\n"
    "        from tinyhold import lazy\n"
    "        print(f'{lazy.MARKER} {lazy.__file__}', flush=True)\n"
    "    elif command == 'exit':\n"
    "        break\n"
)


def _lazy_module(marker: str) -> str:
    return f'MARKER = "{marker}"\n'


def _write_package(root: Path, marker: str) -> Path:
    pkg = root / "pkg"
    src = pkg / "src" / "tinyhold"
    src.mkdir(parents=True, exist_ok=True)
    (pkg / "pyproject.toml").write_text(_PYPROJECT)
    (src / "__init__.py").write_text(_INIT)
    (src / "lazy.py").write_text(_lazy_module(marker))
    return pkg


class Bed:
    """A throwaway tools/ + bin/ + HOME, and a PATH that cannot reach the
    developer's real shims.

    ``uv`` is SYMLINKED into a private bin rather than reached by widening
    PATH: it lives in ``~/.local/bin``, which ``SAFE_BASE_PATH`` excludes on
    purpose -- that directory also holds the real ``nx`` shims, and a test that
    finds those is testing this machine rather than the code.
    """

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path
        self.tools = tmp_path / "tools"
        self.bin = tmp_path / "bin"
        self.home = tmp_path / "home"
        uv_bin = tmp_path / "uvbin"
        for d in (self.tools, self.bin, self.home, uv_bin):
            d.mkdir(parents=True, exist_ok=True)
        uv = shutil.which("uv")
        if uv is None:  # pragma: no cover - environment guard
            pytest.skip("uv is required to build a real generation")
        (uv_bin / "uv").symlink_to(uv)
        self.path = f"{uv_bin}:{SAFE_BASE_PATH}"

    @property
    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            PATH=self.path,
            HOME=str(self.home),
            NX_TOOLS_DIR=str(self.tools),
            NX_BIN_DIR=str(self.bin),
        )
        return env

    def build(self, source: Path) -> Path:
        """One real generation. Returns its directory."""
        done = subprocess.run(
            ["bash", str(_INSTALL / "install_generation.sh"), "--source", str(source)],
            env=self.env, capture_output=True, text=True, timeout=300,
        )
        assert done.returncode == 0, (
            f"install_generation.sh failed ({done.returncode}):\n{done.stderr[-2000:]}"
        )
        return Path(done.stdout.strip().splitlines()[-1])

    def flip_and_shim(self, generation: Path) -> None:
        done = subprocess.run(
            ["bash", "-c",
             f'. "{_INSTALL}/flip.sh"; . "{_INSTALL}/shims.sh"; '
             f'nx_flip_current "{generation}" "{self.tools}" && '
             f'nx_write_shims "{generation}" "{self.bin}" {_DIST}'],
            env=self.env, capture_output=True, text=True, timeout=120,
        )
        assert done.returncode == 0, f"flip/shim failed:\n{done.stderr[-2000:]}"

    def flip_only(self, generation: Path) -> None:
        """Move ``current`` without rewriting shims — for the fabricated
        generations that only exist to retire a real one from `current` and
        `previous`."""
        done = subprocess.run(
            ["bash", "-c",
             f'. "{_INSTALL}/flip.sh"; nx_flip_current "{generation}" "{self.tools}"'],
            env=self.env, capture_output=True, text=True, timeout=120,
        )
        assert done.returncode == 0, f"flip failed:\n{done.stderr[-2000:]}"

    def gc(self, keep: int) -> str:
        done = subprocess.run(
            ["bash", "-c",
             f'. "{_INSTALL}/gc.sh"; nx_gc_generations --keep {keep} "{self.tools}"'],
            env=self.env, capture_output=True, text=True, timeout=120,
        )
        return done.stdout + done.stderr

    def current(self) -> Path:
        return Path(os.readlink(self.tools / "current"))

    def spawn_via_shim(self) -> subprocess.Popen:
        return subprocess.Popen(
            [str(self.bin / _DIST)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=self.env,
        )


def _pump(holder: subprocess.Popen) -> "queue.Queue":
    """Drain *holder*'s stdout into a queue, once per process.

    `readline()` HAS NO TIMEOUT, and this module runs in the FAST tier on every
    `pytest -n auto` with no pytest-timeout plugin configured — so a holder that
    stops answering would hang the whole dev-loop suite indefinitely rather than
    failing one test. Found by RG-E (nexus-utpuw.25). A daemon pump plus
    `queue.get(timeout=...)` makes the wait bounded: the test fails in seconds
    with a readable message instead of stalling the run.
    """
    existing = getattr(holder, "_nx_lines", None)
    if existing is not None:
        return existing
    lines: queue.Queue = queue.Queue()
    holder._nx_lines = lines  # type: ignore[attr-defined]

    def drain() -> None:
        try:
            for line in holder.stdout:
                lines.put(line)
        finally:
            lines.put(None)

    threading.Thread(target=drain, daemon=True).start()
    return lines


def _ask(holder: subprocess.Popen, command: str, timeout: float = 60.0) -> str:
    lines = _pump(holder)
    holder.stdin.write(f"{command}\n")
    holder.stdin.flush()
    try:
        line = lines.get(timeout=timeout)
    except queue.Empty:
        raise AssertionError(
            f"holder did not answer {command!r} within {timeout}s — it is hung, "
            "not slow; failing rather than stalling the suite"
        ) from None
    assert line is not None, f"holder closed stdout instead of answering {command!r}"
    line = line.strip()
    assert line, f"holder gave no answer to {command!r} (died?)"
    return line


# ---------------------------------------------------------------------------
# The structural tripwire -- the ONLY one of these that the simplification
# actually trips. Measured, not assumed; see the docstring.
# ---------------------------------------------------------------------------

def test_the_shim_resolves_the_pointer_before_exec_rather_than_through_it() -> None:
    """THE ACTUAL REGRESSION TRIPWIRE, and it has to be structural.

    Bead .16 names A2 (below) as what "fails loudly if someone later
    'simplifies' the shim to exec .../current/bin/nx". MEASURED 2026-08-26:
    it does not. Rendering the shim as ``exec "<tools>/current/bin/<cmd>"``
    and running this whole module leaves all three behavioural tests GREEN.

    The reason is that every name nexus shims today is a CONSOLE SCRIPT, and
    uv writes their shebangs ABSOLUTE, straight at the generation's
    interpreter. The kernel execs that absolute interpreter whichever path
    reached the script, so ``current`` never enters argv[0] and ``sys.prefix``
    is correct under BOTH shim forms. For the current entry-point set the two
    forms are behaviourally indistinguishable.

    They are not equivalent in what they GUARANTEE. The moment a shimmed name
    is an interpreter, or a shebang is not absolute, the pointer form leaks and
    the flip retargets a live process (nexus-q3xrx). ``nx_render_shim``'s own
    comment calls the ordering "load-bearing rather than stylistic"; this test
    is what makes that claim falsifiable, and it is why the assertion is on the
    shim's SHAPE rather than on an observable that cannot currently differ.

    Falsified by construction: rendering the pointer form turns this red while
    leaving the rest of the module green."""
    rendered = subprocess.run(
        ["bash", "-c",
         f'. "{_INSTALL}/layout.sh"; nx_render_shim {_DIST} /nx-tools-probe'],
        capture_output=True, text=True, timeout=60,
    )
    assert rendered.returncode == 0, rendered.stderr
    body = rendered.stdout

    exec_lines = [ln.strip() for ln in body.splitlines() if ln.strip().startswith("exec ")]
    assert len(exec_lines) == 1, f"expected exactly one exec line, got {exec_lines}"
    exec_line = exec_lines[0]

    assert "readlink" in body, (
        "the shim no longer resolves the pointer before exec; a spawn is then "
        "bound to whatever `current` names at exec time rather than to a "
        "generation for its lifetime"
    )
    assert "/nx-tools-probe/current" not in exec_line, (
        f"the shim execs THROUGH the pointer ({exec_line!r}). CPython reads "
        "pyvenv.cfg next to the executable as invoked, before resolving "
        "symlinks, so this form leaks the pointer into sys.prefix for any "
        "target that is an interpreter or lacks an absolute shebang — and the "
        "next flip retargets every not-yet-imported module in the running "
        "process (nexus-q3xrx)"
    )
    assert "$NX_GEN/bin/" in exec_line, (
        f"exec does not go through the resolved generation: {exec_line!r}"
    )


# ---------------------------------------------------------------------------
# A2 -- the regression tripwire, its own named test per bead .16
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def one_generation(tmp_path_factory) -> tuple[Bed, Path]:
    """One built generation, shared by the two cheap property tests.

    Order-independent by construction: neither test flips, reaps, or otherwise
    mutates the tree, and both assertions are about a path this fixture fixed.
    """
    bed = Bed(tmp_path_factory.mktemp("shimprop"))
    generation = bed.build(_write_package(bed.root, "A"))
    bed.flip_and_shim(generation)
    return bed, generation


def test_a_shim_spawned_holder_binds_to_the_generation_not_the_pointer(
    one_generation
) -> None:
    """THE TRIPWIRE. If someone later "simplifies" the shim to exec through
    ``<tools>/current``, this is what should fail, and it fails BEFORE any flip
    so the diagnosis is the shim rather than the flip.

    Read it together with the control below: on its own this assertion is NOT
    falsifiable by the simplification it guards against, because uv writes an
    ABSOLUTE shebang into the console script. The pair is the guard."""
    bed, generation = one_generation

    holder = bed.spawn_via_shim()
    try:
        prefix = _ask(holder, "prefix")
    finally:
        holder.stdin.write("exit\n")
        holder.stdin.flush()
        holder.wait(timeout=30)

    assert prefix == str(generation)
    assert "current" not in prefix, (
        "sys.prefix carries the 'current' component, so every not-yet-imported "
        "module in this process resolves through the pointer and the next flip "
        "retargets them underneath it — nexus-q3xrx exactly"
    )


def test_routing_the_interpreter_through_the_pointer_does_leak(
    one_generation
) -> None:
    """The control that gives the tripwire its meaning, and the reason A2 is
    not self-sufficient (measured 2026-08-26, recorded on bead .16).

    A console script cannot demonstrate the defect: uv writes its shebang
    ABSOLUTE, straight at the generation's interpreter, so reaching one THROUGH
    the pointer still execs the right python and ``current`` never appears in
    argv[0]. The leak needs the INTERPRETER ITSELF routed through the pointer —
    which is the ``python <script>`` shape MCP hosts actually run.

    Asserting the defect still reproduces is what stops the test above becoming
    a green that would hold no matter what the shim did."""
    bed, generation = one_generation
    script = "import sys; print(sys.prefix)"

    through_pointer = subprocess.run(
        [str(bed.tools / "current" / "bin" / "python"), "-c", script],
        capture_output=True, text=True, timeout=60, env=bed.env,
    ).stdout.strip()
    through_generation = subprocess.run(
        [str(generation / "bin" / "python"), "-c", script],
        capture_output=True, text=True, timeout=60, env=bed.env,
    ).stdout.strip()

    assert through_pointer == str(bed.tools / "current"), (
        "the pointer-routed interpreter did NOT leak, so this platform cannot "
        "exhibit the defect and the tripwire above proves nothing here"
    )
    assert through_generation == str(generation)


# ---------------------------------------------------------------------------
# A1, A3-A7 -- one journey, one test, multi-assert (tests/AGENTS.md rule 1)
# ---------------------------------------------------------------------------

def test_a_flip_does_not_retarget_a_live_holder(tmp_path: Path) -> None:
    """The whole point of side-by-side generations, end to end.

    Function-scoped and self-contained: this journey flips ``current`` and runs
    a GC pass, so sharing its tree with the property tests above would make
    them order-dependent for no saving worth having."""
    bed = Bed(tmp_path)

    # A1 — generation A, flipped, holder spawned THROUGH the shim.
    source = _write_package(bed.root, "A")
    gen_a = bed.build(source)
    bed.flip_and_shim(gen_a)
    holder = bed.spawn_via_shim()

    pointer_holder = subprocess.Popen(
        [str(bed.tools / "current" / "bin" / "python"), "-c", _POINTER_HOLDER],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=bed.env,
    )

    try:
        assert _ask(holder, "prefix") == str(gen_a)

        # A3 — generation B, built from the SAME source with different code in
        # the deferred module, then flipped. Differing content is what makes
        # A5 about CODE rather than merely about paths.
        (source / "src" / "tinyhold" / "lazy.py").write_text(_lazy_module("B"))
        gen_b = bed.build(source)
        assert gen_b != gen_a
        bed.flip_and_shim(gen_b)
        assert bed.current() == gen_b, "the flip did not move current"

        # NON-VACUITY FOR A5, and it is not hypothetical: both builds are
        # `tinyhold 1.0.0`, and uv caches wheels by (name, version). If the
        # cache served generation A's wheel to generation B, the holder would
        # "still see A" for entirely the wrong reason and A5 below would pass
        # while proving nothing. Assert the new code actually reached B.
        installed_b = list(gen_b.glob("lib/python*/site-packages/tinyhold/lazy.py"))
        assert len(installed_b) == 1, f"expected one lazy.py in {gen_b}, got {installed_b}"
        assert 'MARKER = "B"' in installed_b[0].read_text(), (
            "generation B carries generation A's code — a cached wheel was "
            "reused, so A5 cannot distinguish a correctly-bound holder from a "
            "retargeted one"
        )

        # A4/A5 — a module the holder had NOT imported before the flip must
        # still come from generation A.
        marker, path = _ask(holder, "lazy").split(" ", 1)
        assert marker == "A", (
            f"the live holder imported generation B's code (marker {marker!r}) "
            "after the flip — the running process was retargeted underneath "
            "itself, which is the nexus-q3xrx failure"
        )
        assert path.startswith(str(gen_a)), (
            f"deferred import resolved to {path}, outside generation A"
        )

        # THE CONTRAST, and it is what makes the assertion above mean
        # something. `pointer_holder` was spawned through <tools>/current, so
        # its sys.path was computed from the POINTER rather than from a
        # generation. It is live across the very same flip — and it gets
        # RETARGETED: the identical deferred import now returns generation B's
        # code. That is nexus-q3xrx reproduced in-process, and it is the fate
        # the shim exists to prevent for the holder above.
        drifted_marker, drifted_path = _ask(pointer_holder, "lazy").split(" ", 1)
        assert drifted_marker == "B", (
            "the pointer-bound holder was NOT retargeted, so this platform "
            "cannot exhibit the defect and generation-binding is unfalsifiable "
            f"here (got marker {drifted_marker!r} from {drifted_path})"
        )

        # A6 — a NEW spawn through the same shim gets the NEW generation.
        fresh = bed.spawn_via_shim()
        try:
            assert _ask(fresh, "prefix") == str(gen_b), (
                "a new spawn did not pick up the flip, so the shim is bound to "
                "a generation rather than resolving the pointer per spawn"
            )
        finally:
            fresh.stdin.write("exit\n")
            fresh.stdin.flush()
            fresh.wait(timeout=30)

        # A7 — GC must not reap a generation with a live holder (never-delete
        # rule (c)). The holder is still running from gen A.
        #
        # THE SETUP IS THE TEST, and both of its steps were measured rather
        # than anticipated:
        #
        #  1. With only A and B, flipping to B records A as `previous`, so A
        #     survives by never-delete rule (b) whatever rule (c) does.
        #     Deleting rule (c) from gc.sh outright left this assertion GREEN.
        #  2. Retiring A from `previous` is still not enough: GC then has
        #     nothing it is ALLOWED to reap, so "A survived" is
        #     indistinguishable from "GC did nothing at all". The reapable-B
        #     assertion below is what caught that.
        #
        # Two fabricated generations therefore take over `current` and
        # `previous`, which retires A from both AND leaves B genuinely
        # reapable. Fabricated rather than built because GC reads directory
        # shape and a receipt, never an interpreter — the real builds above are
        # where the cost belongs. Their stamps sort last on purpose: keep-N
        # depends on lexical order being creation order.
        retired = fabricate_generation(bed.tools, "99999998T000000Z", version="9.9.8")
        bed.flip_only(retired)
        newest = fabricate_generation(bed.tools, "99999999T000000Z", version="9.9.9")
        bed.flip_only(newest)
        assert bed.current() == newest
        assert os.readlink(bed.tools / "previous") == str(retired), (
            "generation A must be neither current nor previous, or this "
            "measures rule (b) again rather than rule (c)"
        )

        output = bed.gc(keep=1)
        assert gen_a.is_dir(), (
            f"GC reaped generation A out from under its live holder — "
            f"never-delete rule (c):\n{output}"
        )
        assert not gen_b.exists(), (
            "GC reaped nothing reapable, so generation A's survival is "
            f"indistinguishable from an inert GC pass:\n{output}"
        )
    finally:
        for proc in (holder, pointer_holder):
            proc.stdin.write("exit\n")
            proc.stdin.flush()
            proc.wait(timeout=30)
