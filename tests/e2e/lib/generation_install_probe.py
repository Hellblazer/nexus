#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The generation install path, on a virgin HOME. nexus-utpuw.19.

Driven by ``tests/e2e/fresh-install-mvv.sh``. Run with the INSTALLED
artifact's interpreter, so ``import nexus`` resolves to the wheel (or the
PyPI artifact) under test rather than to this checkout — that is the whole
point of running it from the MVV.

WHY THIS LEG EXISTS. fresh-install-mvv.sh installs via ``uv pip install``
into a scrubbed venv and NEVER touches the tool layout, so it is unaffected
by the generation change AND cannot catch a regression in it. That is a gap
in the one gate whose subject is the virgin journey.

IT ALSO PROVES SOMETHING NOTHING ELSE DOES. ``packaged_install_dir()``
resolves ``nexus/_install`` through ``importlib.resources`` — the shipped
copy, "the half that has to keep working after a release" in its own words.
Every existing test of it runs against an EDITABLE checkout, where that path
exists because the repo does. Nothing asserts the WHEEL actually ships the
shell installer. This leg calls the packaged installer out of a real wheel
install, so a packaging regression that silently drops ``_install/*.sh``
fails here instead of on a user's box.

THE RESTORED CANARY. Bead .19 asks to re-point
``tests/e2e/migration-rehearsal/rehearse_chash_window.sh:104-109``, which
hardcoded a uv-tool ``TOOLPY`` and asserted, explicitly, that the install
root satisfies ``running_from_tool_install`` — aborting with "the transition
gate would never fire". That file no longer exists: nexus-lgdel.l2 deleted
it on 2026-08-16 because its subject (the pre-cutover 32-hex window) died at
L1, taking this assertion with it incidentally. Every surviving reference to
``running_from_tool_install`` in the suite MOCKS it. So the assertion is
restored here, against a REAL generation, with its semantics intact rather
than degraded to a path-existence check: that predicate gates the entire
finish-upgrade pass, and a False from it disables restart-stale,
converge_engine, the diag-view heal and both launchagent unloads at once,
silently (nexus-utpuw.10).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

FAILURES: list[str] = []


# ── nexus-qfeez: wait out PyPI propagation lag around the real installer ─────
#
# `fresh-install-mvv.sh --published` proves leg 1's `uv tool install` saw the
# release, but PyPI's simple index is served by a CDN whose edges do not all
# catch up together. Leg 9 is a SEPARATE uv invocation (install_generation.sh's
# own `uv pip install`), so it can land on an edge that still answers "no
# version of conexus==X" minutes after leg 1 succeeded (7.64.1, 7.65.0).
# A probe that first checks the index would only be a second sample of the
# same lottery, so the wait wraps the real installer: retry it, bounded, and
# succeed only when the installer itself exits 0. Any other failure shape, and
# a ceiling overrun, still fail loud.

_PIN = re.compile(r"conexus(?P<extras>\[[^\]]*\])?==(?P<version>[^\s=;]+)")


def is_registry_pin(source: str) -> bool:
    """True only for an exact registry pin (``conexus==X``, extras allowed).

    A wheel path has no index to lag behind, and an unpinned name has no
    specific version whose absence could be lag, so neither may wait.
    """
    return _PIN.fullmatch(source) is not None


def is_propagation_miss(text: str, source: str) -> bool:
    """uv's "that exact version is not on the index (yet)" for THIS pin.

    Narrower than leg 1's shell grep on purpose. That grep accepts any
    "no solution found" that mentions conexus, which a real dependency
    conflict also does; here a conflict must fail at once rather than burn
    the whole ceiling. The message is line-wrapped by uv, so tokens are
    matched across arbitrary whitespace.
    """
    m = _PIN.fullmatch(source)
    if m is None:
        return False
    extras = re.escape(m.group("extras") or "")
    version = re.escape(m.group("version"))
    no_version = rf"no\s+version\s+of\s+conexus{extras}==\s*{version}"
    # Exactly this package: `conexus-foo was not found` or a dependency that
    # was not found must not read as our own release being late.
    not_in_registry = (
        rf"(?<![\w.-])conexus{extras}\s+was\s+not\s+found\s+in\s+the\s+package\s+registry"
    )
    return re.search(no_version, text) is not None or re.search(not_in_registry, text) is not None


@dataclass(frozen=True)
class PropagationSettings:
    ceiling_s: float = 1800.0
    initial_backoff_s: float = 15.0
    max_backoff_s: float = 60.0

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> PropagationSettings:
        """Same knobs, same defaults as leg 1 (fresh-install-mvv.sh).

        ``or`` rather than a ``get`` default: leg 1's ``${VAR:-default}`` treats
        an exported-but-empty value as unset, and so must this.
        """
        return cls(
            ceiling_s=float(env.get("FRESH_MVV_PROPAGATION_CEILING_SECONDS") or 1800),
            initial_backoff_s=float(
                env.get("FRESH_MVV_PROPAGATION_INITIAL_BACKOFF_SECONDS") or 15
            ),
            max_backoff_s=float(env.get("FRESH_MVV_PROPAGATION_MAX_BACKOFF_SECONDS") or 60),
        )


@dataclass(frozen=True)
class InstallOutcome:
    proc: subprocess.CompletedProcess[str]
    attempts: int
    waited_s: float
    exhausted: bool  # True only when the ceiling ran out on propagation misses


def install_with_propagation_wait(
    run: Callable[[dict[str, str]], subprocess.CompletedProcess[str]],
    source: str,
    settings: PropagationSettings,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> InstallOutcome:
    """Run the installer; while it fails ONLY with a propagation miss, retry.

    ``run(extra_env)`` performs one real install attempt. The first attempt is
    unmodified. Retries add ``UV_NO_CACHE=1``: uv honours the index's own
    cache headers, so a stale "no such version" page cached by an earlier
    attempt could otherwise be served back for the rest of its max-age (the
    same reason leg 1's calls carry ``--no-cache``).
    """
    start = clock()
    backoff = settings.initial_backoff_s
    attempts = 0
    while True:
        attempts += 1
        proc = run({} if attempts == 1 else {"UV_NO_CACHE": "1"})
        if proc.returncode == 0:
            return InstallOutcome(proc, attempts, clock() - start, False)
        if not is_propagation_miss(f"{proc.stderr}\n{proc.stdout}", source):
            return InstallOutcome(proc, attempts, clock() - start, False)
        elapsed = clock() - start
        if elapsed >= settings.ceiling_s:
            return InstallOutcome(proc, attempts, elapsed, True)
        pause = min(backoff, settings.ceiling_s - elapsed)
        print(
            f"  uv does not see {source} yet (attempt {attempts}, {elapsed:.0f}s elapsed, "
            f"retrying in {pause:.0f}s) -- PyPI CDN propagation lag (nexus-qfeez)",
            flush=True,
        )
        sleep(pause)
        backoff = min(backoff * 2, settings.max_backoff_s)


def build_generation(
    install_dir: Path,
    source: str,
    env: dict[str, str],
    settings: PropagationSettings,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> InstallOutcome:
    """One or more real ``install_generation.sh`` runs under the propagation wait."""

    def run(extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(install_dir / "install_generation.sh"), "--source", source],
            capture_output=True, text=True, timeout=1800, env={**env, **extra_env},
        )

    return install_with_propagation_wait(run, source, settings, clock=clock, sleep=sleep)


def pdftext_bound_ok(pdftext_version: str, pypdfium2_version: str) -> bool:
    """nexus-kard5 (GH #1533 follow-on, leg 8c's sibling for this layer).

    ``tests/test_install_source_wiring_pins.py`` proves this checkout's own
    ``uv.lock``/wheel metadata carries the ``pdftext<0.7`` bound, and
    ``fresh-install-mvv.sh``'s leg 8c proves a fresh resolve into the
    uv-tool/plain venv (``$PROBE_PYTHON``, BEFORE this probe replaces it
    with a generation) lands below the ceiling too. Neither proves the
    GENERATION install layer -- ``install_generation.sh``'s own ``uv pip
    install`` into the generation's venv, driven by the packaged
    ``overrides.txt`` -- resolves the same bound; that install is a SEPARATE
    ``uv`` invocation with its own resolution, not a copy of what leg 8c
    already resolved. Same bound as leg 8c's inline check
    (``pdftext_v < 0.7 and pypdfium2_v < 5``): mineru's unbounded
    ``pdftext>=0.6.3`` let a fresh resolve land pdftext 0.7.x, whose
    ``PageChars`` dropped ``__iter__`` while mineru's own
    ``span_pre_proc.py`` still iterates it as a list (GH #1533).

    A pure version-comparison function, not a subprocess call, so it is
    unit-testable without a real generation on disk -- mirrors the shape
    of the module's other checks (``check()``), which take an already-
    computed condition rather than doing the computing themselves.
    """
    from packaging.version import Version

    return Version(pdftext_version) < Version("0.7") and Version(pypdfium2_version) < Version("5")


def check(condition: bool, ok: str, bad: str) -> bool:
    if condition:
        print(f"OK   {ok}")
        return True
    print(f"FAIL {bad}")
    FAILURES.append(bad)
    return False


def sandboxed(home: Path) -> dict[str, str]:
    """`env -i` in dict form, mirroring the gate's own allowlist.

    NX_TOOLS_DIR / NX_BIN_DIR are deliberately ABSENT rather than set: the
    subject is where a virgin HOME puts a generation by default, and pinning
    them would test the override instead of the default. Because this is a
    fresh dict rather than a copy of os.environ, an operator's exported
    NX_TOOLS_DIR cannot reach the installer either (nexus-utpuw.18).
    """
    keep = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "TERM": os.environ.get("TERM", "dumb")}
    for proxy in ("HTTPS_PROXY", "HTTP_PROXY"):
        if os.environ.get(proxy):
            keep[proxy] = os.environ[proxy]
    keep["HOME"] = str(home)
    return keep


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: generation_install_probe.py <virgin-home> <source-spec>")
        return 2
    home = Path(sys.argv[1])
    source = sys.argv[2]

    from nexus.commands.self_cmd import packaged_install_dir

    install_dir = packaged_install_dir()
    print(f"packaged installer: {install_dir}")

    # (1) The wheel must actually SHIP the installer. Asserted before use, so
    # a packaging regression reads as a packaging regression rather than as a
    # confusing bash error from a missing file.
    for name in ("install_generation.sh", "flip.sh", "shims.sh", "layout.sh",
                 "layout_core.py", "overrides.txt"):
        if not check(
            (install_dir / name).is_file(),
            f"packaged installer ships {name}",
            f"packaged installer is MISSING {name} — the wheel does not carry "
            f"the shell installer, so `nx self install` cannot work on a user's box",
        ):
            return 1

    env = sandboxed(home)

    # The tools root must exist before the installer runs, and creating it is
    # the CALLER's job: `perform_self_install` does exactly this
    # (`tools.mkdir(parents=True, exist_ok=True)`, self_cmd.py:126) before
    # invoking the same script. Mirrored here so this leg exercises the real
    # caller's contract rather than a shape nothing uses.
    #
    # Getting this wrong is not a quiet failure, it is a MISLEADING one, and
    # this leg reproduced it: install_generation.sh claims its stamp with a
    # bare `mkdir` (correct — that atomicity is what makes the claim
    # race-free), but treats EVERY mkdir failure as a collision, so a missing
    # parent reports "could not claim a generation directory ... (9
    # collisions)" on a virgin box where nothing could possibly have collided.
    # Filed as nexus-14u80; not fixed here, because the fix belongs in the
    # installer's errno handling rather than in a test that works around it.
    tools_root = home / ".local" / "share" / "nexus" / "tools"
    tools_root.mkdir(parents=True, exist_ok=True)

    # (2) Build a generation from the artifact under test.
    #
    # nexus-qfeez: under --published the source is `conexus==X`, and a fresh
    # release can be visible to leg 1's resolve yet not to this one (CDN edge
    # lag). build_generation() retries the REAL installer on exactly that
    # miss, bounded; success still means install_generation.sh exited 0.
    outcome = build_generation(install_dir, source, env, PropagationSettings.from_env(os.environ))
    built = outcome.proc
    if outcome.attempts > 1 and built.returncode == 0:
        print(f"generation install propagation wait: {outcome.waited_s:.0f}s "
              f"over {outcome.attempts} attempts (nexus-qfeez)")
    if built.returncode != 0:
        if outcome.exhausted:
            print(f"FAIL {source} did not become resolvable within "
                  f"{outcome.waited_s:.0f}s ({outcome.attempts} attempts) even though the "
                  f"uv-tool install of the same version succeeded -- PyPI's simple index "
                  f"stayed stale for this resolution; re-run this leg later (nexus-qfeez)")
        else:
            print(f"FAIL install_generation.sh exited {built.returncode}")
        print(built.stderr[-3000:])
        return 1
    generation = Path(built.stdout.strip().splitlines()[-1])
    check(generation.is_dir(), f"generation built: {generation}",
          f"install_generation.sh printed {generation} which is not a directory")

    # It must land under the VIRGIN home, not anywhere ambient.
    check(
        str(generation).startswith(str(home)),
        f"generation is under the virgin HOME ({home})",
        f"generation landed OUTSIDE the virgin HOME: {generation}",
    )
    check((generation / "nexus-install.json").is_file(),
          "receipt written", "generation has no nexus-install.json receipt")

    # (3) Flip, and write the shims.
    tools = generation.parent
    bin_dir = home / ".local" / "bin"
    flipped = subprocess.run(
        ["bash", "-c",
         f'. "{install_dir}/flip.sh"; . "{install_dir}/shims.sh"; '
         f'nx_flip_current "{generation}" "{tools}" && '
         f'nx_write_shims "{generation}" "{bin_dir}"'],
        capture_output=True, text=True, timeout=300, env=env,
    )
    if flipped.returncode != 0:
        print(f"FAIL flip/shims exited {flipped.returncode}")
        print(flipped.stderr[-2000:])
        return 1

    current = tools / "current"
    check(current.is_symlink() and Path(os.readlink(current)) == generation,
          "current resolves to the new generation",
          f"current does not resolve to {generation}")

    # (4) Shims written AND EXECUTABLE — the bead asks for both, and a
    # non-executable shim fails at exec with a permissions error rather than
    # anything self-explanatory.
    shim = bin_dir / "nx"
    check(shim.is_file(), f"shim written: {shim}", f"no nx shim at {shim}")
    check(os.access(shim, os.X_OK), "shim is executable",
          f"shim {shim} is not executable")

    # (5) nx runs THROUGH the shim.
    ran = subprocess.run([str(shim), "--version"], capture_output=True,
                         text=True, timeout=300, env=env)
    check(ran.returncode == 0 and "version" in ran.stdout.lower(),
          f"nx runs through the shim: {ran.stdout.strip()[:60]}",
          f"nx via the shim exited {ran.returncode}: "
          f"{(ran.stdout + ran.stderr).strip()[:300]}")

    # (6) THE RESTORED CANARY (see the module docstring). Evaluated INSIDE the
    # generation, against a real install root — not mocked, and not weakened
    # to "the path exists".
    predicate = subprocess.run(
        [str(generation / "bin" / "python"), "-c",
         "from nexus.upgrade_finish import running_from_tool_install as r;"
         "print('TRUE' if r() else 'FALSE')"],
        capture_output=True, text=True, timeout=300, env=env,
    )
    verdict = predicate.stdout.strip()
    check(
        verdict == "TRUE",
        "running_from_tool_install() is True for the generation install root",
        "running_from_tool_install() returned "
        f"{verdict or predicate.stderr.strip()[:200]!r} for a real generation — "
        "the finish-upgrade pass would return None and silently disable "
        "restart-stale, converge_engine, the diag-view heal and both "
        "launchagent unloads (nexus-utpuw.10). The transition gate would "
        "never fire.",
    )

    # (7) nexus-heykz: `av` must NOT be in the generation. pyproject's
    # [tool.uv] override is read from the invoking project, so only the
    # packaged overrides file (handed to uv by install_generation.sh) keeps
    # av and its ffmpeg-62 dylibs out of a user's install. A checkout-run
    # install inherits the override by accident; this probe runs from the
    # ARTIFACT's own installer, so it sees what a user sees.
    av = subprocess.run(
        [str(generation / "bin" / "python"), "-c",
         "import importlib.util as u;"
         "print('PRESENT' if u.find_spec('av') else 'ABSENT')"],
        capture_output=True, text=True, timeout=300, env=env,
    )
    check(
        av.stdout.strip() == "ABSENT",
        "av is absent from the generation (packaged overrides reached uv)",
        f"av is {av.stdout.strip() or av.stderr.strip()[:200]!r} in the generation — "
        "the packaged overrides file did not reach `uv pip install`; users get "
        "PyAV's ffmpeg-62 dylibs colliding with opencv's ffmpeg-61 (nexus-heykz)",
    )

    # (8) nexus-kard5 (GH #1533 follow-on): the generation's OWN resolve of
    # pdftext/pypdfium2 must land below the same ceiling leg 8c already
    # checks for $PROBE_PYTHON -- see pdftext_bound_ok()'s docstring for why
    # this is a separate resolve, not a copy of that one. No live defect
    # today (the packaged [tool.uv] override already reaches
    # install_generation.sh's overrides.txt); this closes the gap so a
    # regression in that path is caught by the MVV instead of shipping.
    bound = subprocess.run(
        [str(generation / "bin" / "python"), "-c",
         "from importlib.metadata import version;"
         "print(version('pdftext'));"
         "print(version('pypdfium2'))"],
        capture_output=True, text=True, timeout=300, env=env,
    )
    bound_lines = bound.stdout.strip().splitlines()
    bound_ok = (
        bound.returncode == 0
        and len(bound_lines) == 2
        and pdftext_bound_ok(bound_lines[0], bound_lines[1])
    )
    check(
        bound_ok,
        f"pdftext/pypdfium2 in the generation are below the GH #1533 bound: {bound_lines}",
        "pdftext/pypdfium2 in the generation are ABOVE the GH #1533 bound "
        f"({bound_lines or bound.stderr.strip()[:200]!r}) -- the packaged "
        "overrides file did not reach the generation's own `uv pip install`; "
        "mineru's PageChars loses __iter__ on pdftext>=0.7 (GH #1533)",
    )

    if FAILURES:
        print(f"\n{len(FAILURES)} generation-install assertion(s) failed")
        return 1
    print("\ngeneration install path: all assertions passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
