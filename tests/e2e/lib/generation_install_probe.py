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
import subprocess
import sys
from pathlib import Path

FAILURES: list[str] = []


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
    for name in ("install_generation.sh", "flip.sh", "shims.sh", "layout.sh", "overrides.txt"):
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
    built = subprocess.run(
        ["bash", str(install_dir / "install_generation.sh"), "--source", source],
        capture_output=True, text=True, timeout=1800, env=env,
    )
    if built.returncode != 0:
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

    if FAILURES:
        print(f"\n{len(FAILURES)} generation-install assertion(s) failed")
        return 1
    print("\ngeneration install path: all assertions passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
