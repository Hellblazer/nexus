# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-796zn: ``tests/e2e/fresh-install-mvv.sh`` must support a
``--published [VERSION]`` mode that installs the PUBLISHED conexus artifact
from PyPI via ``uv tool install``, not only the local wheel built from this
checkout (T2 nexus/shakedown-playbook SS2 S1 GAP).

This is a text-level pin, not a functional test — the script itself is a
self-provisioning bash e2e gate that downloads real binaries and talks to
real PyPI/the engine; that is exercised by actually RUNNING the script (see
the release/shakedown skills), not by unit tests. What CAN regress silently
at review time, and is worth pinning here at unit-test speed, is the surface
contract: the flag exists, the published layer never bypasses the mcp<2
tripwire that nexus-l2ku5 needed, the install step is HOME-redirected before
it touches uv's tool state (never the live install), and the PASSED line
honestly names which layer ran (the honest-recording concern from the
2026-08-04 shakedown).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "tests" / "e2e" / "fresh-install-mvv.sh"


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_script_exists_and_is_executable() -> None:
    assert SCRIPT.is_file(), f"missing {SCRIPT}"
    import os
    import stat

    mode = os.stat(SCRIPT).st_mode
    assert mode & stat.S_IXUSR, f"{SCRIPT} is not executable"


def test_bash_syntax_is_valid() -> None:
    # Cheap falsification control: `bash -n` catches nothing semantic, but a
    # syntax break in either mode branch (e.g. an unbalanced if/fi across
    # the local-wheel/published split) would still fail this.
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)], capture_output=True, text=True,
    )
    assert result.returncode == 0, f"bash -n failed: {result.stderr}"


def test_supports_published_flag_with_optional_version() -> None:
    text = _text()
    assert "--published" in text
    assert "PUBLISHED_VERSION" in text
    assert "PUBLISHED_MODE" in text


def test_published_mode_uses_uv_tool_install_not_local_wheel() -> None:
    text = _text()
    assert "uv tool install" in text
    # The published branch must not secretly fall back to `uv build`/
    # `uv pip install` (that would silently run the local-wheel layer under
    # the --published banner — the exact honest-recording failure this bead
    # exists to close). Sliced between each leg's own unique banner line —
    # robust to reformatting/reindentation, unlike an exact-whitespace
    # ``text.index()`` anchored on the `if [ "$PUBLISHED_MODE" = 1 ]; then`
    # line, which is NOT unique on its own (the arg-parsing banner above
    # also branches on PUBLISHED_MODE with the identical literal text).
    published_marker = 'echo "── 1/9 Install PUBLISHED artifact from PyPI (uv-tool resolution layer) ──"'
    local_marker = 'echo "── 1/9 Build the wheel under test'
    assert text.count(published_marker) == 1, "published-leg banner must be unique to slice on"
    assert text.count(local_marker) == 1, "local-wheel-leg banner must be unique to slice on"
    published_block_start = text.index(published_marker)
    local_block_start = text.index(local_marker)
    assert published_block_start < local_block_start
    published_block = text[published_block_start:local_block_start]
    # Check actual invocations, not prose — the published block's own
    # comment legitimately mentions "uv build"/"uv venv" by name when
    # explaining why PATH/network stay ambient in both layers.
    assert "uv build --wheel" not in published_block
    assert "uv pip install -q --python" not in published_block


def test_published_install_is_isolated_via_env_dash_i_not_bare_env_home() -> None:
    """nexus-enfoh CRITICAL (code-review-expert + substantive-critic,
    empirically verified 2026-08-05): a bare ``env HOME=... uv ...``
    invocation does NOT clear the rest of the ambient environment — only
    HOME is set/overridden, so an operator with UV_TOOL_DIR (or
    UV_TOOL_BIN_DIR / XDG_DATA_HOME / XDG_BIN_HOME) exported would have
    this step install into their LIVE tool venv. The fix reuses `_nx()`'s
    existing ``env -i`` allowlist pattern via a new `_uv_sandboxed()`
    helper. This pins the fixed shape AND regression-guards against the
    original vulnerable shape silently coming back.
    """
    text = _text()
    assert "_uv_sandboxed()" in text
    assert "env -i" in text
    assert "_uv_sandboxed tool install" in text
    assert "_uv_sandboxed tool dir" in text
    # Regression guard: the ORIGINAL vulnerable shape must never come back.
    assert 'env HOME="$HOME_DIR" uv tool install' not in text
    assert 'env HOME="$HOME_DIR" uv tool dir' not in text


def test_published_install_scrubs_ambient_uv_tool_env(tmp_path) -> None:
    """nexus-enfoh CRITICAL, functional regression test: even with
    UV_TOOL_DIR / UV_TOOL_BIN_DIR / XDG_DATA_HOME / XDG_BIN_HOME /
    UV_INDEX_URL / PIP_INDEX_URL set to decoy values in the CALLING
    environment (simulating an operator's real shell), the `uv` process
    the script actually invokes for `tool install` must see NONE of them.
    A stub `uv` on a prepended PATH records what it saw and fails fast
    (no real network/engine provisioning needed — the isolation failure
    the reviewers proved happens at the very first install step).

    This test fails against the pre-fix `env HOME=... uv tool install`
    shape (that a plain `env VAR=... CMD` leaves the rest of the ambient
    environment untouched is exactly the bug) and passes against the
    `env -i` allowlist fix.
    """
    import os
    import shutil

    marker = tmp_path / "uv-env-seen.txt"
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir()
    stub_uv = stub_dir / "uv"
    stub_uv.write_text(
        "#!/bin/bash\n"
        'if [ "$1" = "tool" ] && [ "$2" = "install" ]; then\n'
        "    {\n"
        '        echo "UV_TOOL_DIR=${UV_TOOL_DIR-<unset>}"\n'
        '        echo "UV_TOOL_BIN_DIR=${UV_TOOL_BIN_DIR-<unset>}"\n'
        '        echo "XDG_DATA_HOME=${XDG_DATA_HOME-<unset>}"\n'
        '        echo "XDG_BIN_HOME=${XDG_BIN_HOME-<unset>}"\n'
        '        echo "UV_INDEX_URL=${UV_INDEX_URL-<unset>}"\n'
        '        echo "PIP_INDEX_URL=${PIP_INDEX_URL-<unset>}"\n'
        f'    }} > "{marker}"\n'
        "    exit 1\n"
        "fi\n"
        "exit 1\n"
    )
    stub_uv.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{stub_dir}:{env.get('PATH', '')}"
    env["UV_TOOL_DIR"] = str(tmp_path / "DECOY-live-tool-dir")
    env["UV_TOOL_BIN_DIR"] = str(tmp_path / "DECOY-live-bin-dir")
    env["XDG_DATA_HOME"] = str(tmp_path / "DECOY-xdg-data")
    env["XDG_BIN_HOME"] = str(tmp_path / "DECOY-xdg-bin")
    env["UV_INDEX_URL"] = "https://decoy.example/simple"
    env["PIP_INDEX_URL"] = "https://decoy.example/simple"

    result = subprocess.run(
        [str(SCRIPT), "--published", "1.2.3"],
        env=env, capture_output=True, text=True, timeout=60,
    )
    try:
        assert result.returncode != 0
        assert marker.is_file(), (
            "stub uv never received 'tool install' — script's install "
            f"logic changed; stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        seen = marker.read_text()
        for line in (
            "UV_TOOL_DIR=<unset>", "UV_TOOL_BIN_DIR=<unset>",
            "XDG_DATA_HOME=<unset>", "XDG_BIN_HOME=<unset>",
            "UV_INDEX_URL=<unset>", "PIP_INDEX_URL=<unset>",
        ):
            assert line in seen, f"decoy env leaked into uv tool install: {seen}"
    finally:
        # Best-effort cleanup of the preserved-on-failure sandbox WORK dir
        # (the script deliberately keeps failure evidence — see cleanup()).
        match = re.search(r"FAILURE EVIDENCE PRESERVED: (\S+)", result.stderr)
        if match:
            shutil.rmtree(Path(match.group(1)).parent, ignore_errors=True)


def test_published_install_failure_is_named_not_skip_passed() -> None:
    text = _text()
    assert "uv tool install $PKG_SPEC failed" in text
    assert "no skip-pass permitted" in text


def test_mcp_lt2_tripwire_is_shared_across_both_layers_not_duplicated() -> None:
    """nexus-l2ku5's mcp<2 dist-info assertion must run once, unconditionally
    — it is the layer test this bead exists to make reachable in published
    mode, not a local-wheel-only leg."""
    text = _text()
    assert text.count('if [ "$MCP_MAJOR" -ge 2 ]; then') == 1
    # It must not be nested inside an `if [ "$PUBLISHED_MODE" ...` guard —
    # i.e. it has to run in BOTH modes. Crude but effective: the guard
    # clause must not appear between the mcp-dist-info lookup and the
    # major-version check.
    dist_info_idx = text.index('MCP_DIST_INFO="$(find')
    major_check_idx = text.index('if [ "$MCP_MAJOR" -ge 2 ]; then')
    between = text[dist_info_idx:major_check_idx]
    assert "PUBLISHED_MODE" not in between


def test_passed_line_names_the_layer_honestly_in_both_modes() -> None:
    text = _text()
    assert "PUBLISHED artifact, uv-tool resolution layer" in text
    assert "LOCAL WHEEL, release-battery layer" in text


# The two allowlist grep-parity tests that lived here (nexus-8hpad era:
# test_doctor_allowlist_stays_in_grep_level_parity_with_health_py and
# test_allowlist_regex_matches_vw594_signal_but_not_a_real_stuck_run_alarm)
# retired 2026-08-07 with the REQUIRED_ENGINE_VERSION (0,1,67) bump: the
# ALLOWLIST_REGEX entries they pinned are gone (see
# tests/test_engine_version.py::Test8hpadAllowlistDoesNotOutliveItsTrigger,
# which enforced exactly that removal). The MVV allowlist is now the
# never-matching sentinel; a future entry must bring back a parity pin.


def test_non_vacuity_leg_list_is_mode_aware() -> None:
    """The 9/9 non-vacuity sweep must check `install.log` in published mode
    and `build.log` in local-wheel mode — checking for the wrong log name
    would make the leg vacuously skip its own non-vacuity proof."""
    text = _text()
    assert 'LEGS_TO_CHECK="install.log $LEGS_TO_CHECK"' in text
    assert 'LEGS_TO_CHECK="build.log $LEGS_TO_CHECK"' in text
