# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-og52j: ``stage_native`` must FAIL when a ``.so`` sibling sits next to
the native candidate binary, instead of silently copying it into the
rehearsal image.

BACKGROUND. Prior to nexus-223oj, a LOCAL ``-Pnative -Ob`` quick build also
emitted native-image ``.so`` siblings (``libawt*``, ``libjavajpeg``,
``liblcms``, plus ``libjava``/``libjvm``). ``stage_native`` in
``tests/e2e/migration-rehearsal/run.sh`` copied any it found alongside the
binary, so every rehearsal ran the candidate WITH libraries the published
release never ships -- ``engine-service-release.yml``'s "Stage artifact +
sha256" step uploads ONLY the bare executable (``dist/$ASSET``), no ``.so``.
No gate could observe a release binary depending on a ``.so`` it doesn't
upload, by construction. nexus-223oj cut the AWT reachability roots that
produced those siblings (measured: 7 emitted in the control build, 0 in the
fixed build), so a correct build today ships the executable alone.

``stage_native``/``stage_wheel`` were extracted from ``run.sh`` into
``tests/e2e/migration-rehearsal/lib/stage_artifacts.sh`` so this test can
drive ``stage_native`` directly against a fixture ``service/target``-shaped
directory (via the ``ARTIFACTS``/`` src`` override it already reads), without
needing Docker or a leg selection to reach it.

WHAT THIS PROVES. A rehearsal that would previously succeed with a stray
``.so`` staged now FAILS instead -- the acceptance nexus-og52j asked for: "a
rehearsal that would FAIL if the release began depending on a .so it does
not upload." RED and GREEN are both proven: staging with no ``.so`` present
still succeeds (the common, expected case since nexus-223oj), so this is not
a blanket refusal that would also break a legitimately self-contained build.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB = REPO_ROOT / "tests/e2e/migration-rehearsal/lib/stage_artifacts.sh"
RUN_SH = REPO_ROOT / "tests/e2e/migration-rehearsal/run.sh"


def _stage_native(src: Path, dest: Path) -> subprocess.CompletedProcess[str]:
    """Drive the real ``stage_native`` function against a fixture *src*
    directory (standing in for ``service/target`` via the ``ARTIFACTS``
    override the function already reads) and destination *dest*."""
    script = f'source "{LIB}"; stage_native "$1"'
    env = {
        "ARTIFACTS": str(src.parent),
        "PATH": "/usr/bin:/bin",
    }
    return subprocess.run(
        ["bash", "-c", script, "_", str(dest)],
        cwd=src.parent,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        check=False,
    )


def _make_native_dir(tmp_path: Path) -> Path:
    """A fixture directory shaped like ``$ARTIFACTS/native`` (what
    ``stage_native`` reads when ``$ARTIFACTS`` is set): holds
    ``nexus-service`` and nothing else, by default."""
    native = tmp_path / "native"
    native.mkdir()
    (native / "nexus-service").write_bytes(b"#!/bin/sh\necho fixture\n")
    (native / "nexus-service").chmod(0o755)
    return native


def test_the_lib_exists() -> None:
    assert LIB.is_file(), f"extraction moved or was never made: {LIB}"


def test_stage_native_succeeds_with_no_so_siblings(tmp_path: Path) -> None:
    """GREEN: the expected shape since nexus-223oj -- the executable alone."""
    native = _make_native_dir(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()

    proc = _stage_native(native, dest)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (dest / "nexus-service").is_file()


def test_stage_native_fails_loud_on_a_so_sibling(tmp_path: Path) -> None:
    """RED: a .so sibling reappearing (a reachability regression) must break
    staging here, not silently ship a library the release never uploads."""
    native = _make_native_dir(tmp_path)
    (native / "libawt.so").write_bytes(b"not a real library")
    dest = tmp_path / "dest"
    dest.mkdir()

    proc = _stage_native(native, dest)

    assert proc.returncode != 0, (
        "stage_native must FAIL when a .so sibling is present -- the "
        "release ships the executable alone.\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert "libawt.so" in proc.stdout + proc.stderr, proc.stdout + proc.stderr


def test_stage_native_names_every_offending_so(tmp_path: Path) -> None:
    native = _make_native_dir(tmp_path)
    (native / "libawt.so").write_bytes(b"x")
    (native / "liblcms.so").write_bytes(b"x")
    dest = tmp_path / "dest"
    dest.mkdir()

    proc = _stage_native(native, dest)

    assert proc.returncode != 0
    combined = proc.stdout + proc.stderr
    assert "libawt.so" in combined
    assert "liblcms.so" in combined


def test_run_sh_sources_and_calls_stage_native_from_the_library() -> None:
    """The wiring pin: extraction must not drift back to an inline copy that
    stops enforcing the nexus-og52j assertion."""
    text = RUN_SH.read_text(encoding="utf-8")
    assert "lib/stage_artifacts.sh" in text, "run.sh no longer sources the library"
    # stage_native is CALLED (not merely mentioned in the source line) at
    # every staging call site.
    call_sites = text.count('stage_native "$STAGE/native"')
    assert call_sites >= 3, (
        f"expected stage_native to be called at least 3 times (default/"
        f"--shakeout, --fullstack/--shakeout-e2e, --candidate-migration); "
        f"found {call_sites}"
    )


def test_run_sh_no_longer_defines_stage_native_inline() -> None:
    """A regression guard against the extraction being silently reverted:
    run.sh must not carry its own compgen/.so-copy logic outside the lib."""
    text = RUN_SH.read_text(encoding="utf-8")
    assert "cp \"$src\"/*.so" not in text, (
        "run.sh has its own inline .so-copying logic again -- the "
        "nexus-og52j assertion lives only in lib/stage_artifacts.sh and an "
        "inline copy here would silently bypass it"
    )
