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


def test_doctor_allowlist_stays_in_grep_level_parity_with_health_py() -> None:
    """nexus-8hpad (filed while validating nexus-796zn): the pinned engine
    floor (v0.1.65) predates both the /chash/conformance route
    (nexus-du2dw) and the begin-many index-run fence route (nexus-vw594
    F1), so `nx doctor` unconditionally emits two WARN-shaped lines on a
    virgin box today, in EITHER install layer. The MVV's ALLOWLIST_REGEX
    carries a scoped entry for both. GREP-LEVEL PARITY (the same idiom
    ``tests/test_health_service_checks.py`` already uses for the earlier
    dangling-manifests entry): reword either side's substring without the
    other and this fails at unit-test speed instead of reddening the MVV
    2-4 minutes into a real run.
    """
    script_text = _text()
    assert "ALLOWLIST_REGEX=" in script_text
    assert "nexus-8hpad" in script_text  # removal-trigger bead reference

    # health.py builds the chash-conformance detail across two adjacent
    # string-literal fragments ("...the chash-conformance " + "route — ...")
    # that only join at runtime, so pin each fragment separately rather than
    # the concatenated phrase.
    health_text = (REPO_ROOT / "src" / "nexus" / "health.py").read_text(encoding="utf-8")
    for substring in ("the chash-conformance", "stale index-run fences"):
        assert substring in health_text, f"{substring!r} missing from health.py — MVV allowlist is now stale"
    for substring in ("chash-conformance route", "stale index-run fences"):
        assert substring in script_text, f"{substring!r} missing from the MVV's ALLOWLIST_REGEX"


def test_allowlist_regex_matches_vw594_signal_but_not_a_real_stuck_run_alarm() -> None:
    """nexus-iocp8 SIGNIFICANT (substantive-critic, empirically verified):
    health.py's ``_check_stale_indexing_runs`` has TWO independent WARN
    branches sharing the label "stale index-run fences" — the vw594
    engine-half-inert signal (transitional, safe to allowlist) and a
    genuinely stuck mid-run alarm (never safe to allowlist — it means a
    real re-index is wedged). Both render as
    "stale index-run fences: N document(s) ..." so a bare
    ``[0-9]+ document`` pattern swallows BOTH. Extract the ACTUAL
    ALLOWLIST_REGEX from the script and prove it matches only the vw594
    branch's rendered line, using literal detail text mirrored from
    health.py (lines 4134-4141 and 4156-4162 as of this writing).
    """
    script_text = _text()
    match = re.search(r"^ALLOWLIST_REGEX='(.*)'$", script_text, re.MULTILINE)
    assert match, "ALLOWLIST_REGEX assignment not found in the script"
    allowlist_regex = match.group(1)

    # Mirrors health.py's vw594-branch detail (_check_stale_indexing_runs,
    # the `reported_null > 0 and newest_reported_null_dt > _FENCE_RELEASE_DT`
    # branch) as rendered by doctor's ⚠ prefix: "{label}: {detail}".
    vw594_line = (
        "  ⚠ stale index-run fences: 1 document(s) report index_state but it "
        "is NULL on every one of them, and at least one was indexed "
        "2026-08-05T19:07:24+00:00 — after the fence's "
        "2026-08-02T22:26:00+00:00 release. The fence engine is live; a "
        "producer wrote this document without ever calling index-run "
        "begin/complete (nexus-vw594 coverage gap), not a pre-fence engine."
    )
    # Mirrors the OTHER branch (the `if stale:` block) — a real stuck-run
    # alarm that must NEVER be silenced by this allowlist.
    stuck_run_line = (
        "  ⚠ stale index-run fences: 3 document(s) stranded in "
        "index_state='indexing' beyond 24h: some/doc.md (30.0h); "
        "other/doc.md (26.2h); third/doc.md (24.5h). This is SAFE "
        "(re-indexing never skips an 'indexing' document, nexus-lcmbp) but "
        "wastes a full re-chunk/re-embed on every intervening run — check "
        "for a stuck run or a rolling deploy that split a begin/complete "
        "pair across engine versions."
    )
    assert re.search(allowlist_regex, vw594_line) is not None, (
        "ALLOWLIST_REGEX no longer matches the vw594 engine-half-inert "
        "signal — the MVV would go red on the currently-pinned engine floor"
    )
    assert re.search(allowlist_regex, stuck_run_line) is None, (
        "ALLOWLIST_REGEX over-matches: it swallows a genuine stuck-mid-run "
        "alarm (nexus-iocp8 regression) — a real wedged re-index would pass "
        "the MVV silently"
    )


def test_non_vacuity_leg_list_is_mode_aware() -> None:
    """The 9/9 non-vacuity sweep must check `install.log` in published mode
    and `build.log` in local-wheel mode — checking for the wrong log name
    would make the leg vacuously skip its own non-vacuity proof."""
    text = _text()
    assert 'LEGS_TO_CHECK="install.log $LEGS_TO_CHECK"' in text
    assert 'LEGS_TO_CHECK="build.log $LEGS_TO_CHECK"' in text
