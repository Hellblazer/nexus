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
    # The LEG NUMBER is deliberately excluded from both markers (nexus-utpuw.19).
    # These used to read `── 1/9 ...`, which coupled this test to the gate's
    # total leg COUNT: adding a leg renumbered every banner and turned both
    # counts to zero, so the failure surfaced as "banner must be unique" —
    # a message pointing at uniqueness when the real cause was arithmetic.
    # The leg TITLES are what identify the branches, and they are unique on
    # their own.
    published_marker = 'Install PUBLISHED artifact from PyPI (uv-tool resolution layer) ──"'
    local_marker = 'Build the wheel under test ──"'
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


# ── nexus-r433b: the PyPI-propagation retry branch (review folds, 2026-08-31) ─


def _load_mcpb_bootstrap():
    import importlib.util

    path = REPO_ROOT / "mcpb" / "src" / "bootstrap.py"
    spec = importlib.util.spec_from_file_location("mcpb_bootstrap_parity", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


#: The one propagation-signature set, asserted present in BOTH consumers.
#: Adding a phrase to either file without the other (and this list) fails here
#: — the same drift-parity shape as test_gateway_constants_match_reference.
_PROPAGATION_PHRASES = (
    "no solution found",
    "no version of conexus",
    "not found in the package registry",
)


def test_retry_signature_parity_with_mcpb_bootstrap() -> None:
    """code-review-expert + substantive-critic (2026-08-31): the shell retry
    trigger and mcpb/src/bootstrap.py classify the SAME resolver-failure
    concept; the shell grep initially shipped with one phrase missing and
    no package-name anchor. Pin: every phrase appears in both files, the
    shell trigger carries the conexus anchor bootstrap's classifier has,
    and bootstrap's classifier actually accepts each phrase."""
    text = _text()
    bootstrap = _load_mcpb_bootstrap()
    bootstrap_src = (REPO_ROOT / "mcpb" / "src" / "bootstrap.py").read_text()
    for phrase in _PROPAGATION_PHRASES:
        assert phrase in text, f"fresh-install-mvv.sh retry grep lost: {phrase!r}"
        assert phrase in bootstrap_src, f"mcpb bootstrap classifier lost: {phrase!r}"
        assert bootstrap._is_resolution_unavailable(f"error: conexus {phrase}") is True
    # The anchor: bootstrap requires 'conexus' in the output; the shell
    # trigger must too (an unrelated dependency conflict must not be
    # classified as a propagation wait — critic finding 3).
    assert 'grep -qi "conexus" "$LOGS/install.log"' in text


def _cleanup_preserved_evidence(result) -> None:
    match = re.search(r"FAILURE EVIDENCE PRESERVED: (\S+)", result.stderr)
    if match:
        import shutil

        shutil.rmtree(Path(match.group(1)).parent, ignore_errors=True)


def _write_stub_git(stub_dir, tag_verdict: str) -> None:
    """A stub `git` for the nexus-tt5vm follow-up discriminator
    (`_release_tag_exists_on_origin`), which routes `git ls-remote`
    through python3's subprocess.run(timeout=...) rather than calling it
    directly from bash (nexus-tt5vm review round 2: GIT_HTTP_LOW_SPEED_*
    alone does not bound a DNS/connect hang). ``tag_verdict``:
    - "yes": reachable, tag present (rc 0, non-empty stdout).
    - "no": reachable, tag absent (rc 0, empty stdout) — the real
      `git ls-remote` shape for a genuinely nonexistent ref.
    - "unreachable": remote could not be contacted at all (nonzero rc,
      the real shape for a DNS/network failure — verified by hand against
      a nonexistent host).
    - "timeout": the process hangs past the wall-clock bound (verified by
      hand: subprocess.run(timeout=1.0) against this exact shape raised
      TimeoutExpired at ~1.0s) — must resolve to the SAME "unreachable"
      verdict as a hard connection failure, never a false fast-fail."""
    stub_git = stub_dir / "git"
    if tag_verdict == "yes":
        body = (
            "#!/bin/bash\n"
            'if [ "$1" = "ls-remote" ]; then\n'
            "    echo 'deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\trefs/tags/v1.2.3'\n"
            "    exit 0\n"
            "fi\n"
            "exit 1\n"
        )
    elif tag_verdict == "no":
        body = (
            "#!/bin/bash\n"
            'if [ "$1" = "ls-remote" ]; then\n'
            "    exit 0\n"
            "fi\n"
            "exit 1\n"
        )
    elif tag_verdict == "unreachable":
        body = (
            "#!/bin/bash\n"
            'if [ "$1" = "ls-remote" ]; then\n'
            "    echo 'fatal: unable to access: Could not resolve host' >&2\n"
            "    exit 128\n"
            "fi\n"
            "exit 1\n"
        )
    elif tag_verdict == "timeout":
        body = (
            "#!/bin/bash\n"
            'if [ "$1" = "ls-remote" ]; then\n'
            "    sleep 5\n"
            "    exit 0\n"
            "fi\n"
            "exit 1\n"
        )
    else:
        raise ValueError(f"unknown tag_verdict: {tag_verdict!r}")
    stub_git.write_text(body)
    stub_git.chmod(0o755)


def _run_propagation_branch(
    tmp_path,
    extra_env: dict[str, str],
    probe_fail_count: int,
    install_fail_count: int,
    tag_verdict: str = "yes",
) -> tuple:
    """Drive the rewritten propagation branch (nexus-tt5vm, 2026-09-24
    decision) with no real network and no real sleeping: a stub `uv`
    fails `pip install --dry-run` (the cheap probe) for the first
    ``probe_fail_count`` calls, then succeeds; separately fails
    `tool install` (the real install — always at least once, since the
    FIRST top-level call must fail with the propagation signature to
    enter the branch at all) for the first ``install_fail_count`` calls,
    then succeeds. `tool dir`/`venv` are no-ops that let the script run
    to (and fail at) the NEXT assertion past the retry machinery, proving
    the loop actually exited via success rather than the process just
    happening to end. A stub `git` answers the tag-existence discriminator
    (default "yes" — a real release — so these probe/ceiling-focused tests
    reach the probe loop exactly as before that discriminator existed;
    see test_propagation_tag_discriminator_* below for its own coverage).

    `_uv_sandboxed` runs the stub under `env -i` (deliberate — see the
    script's own nexus-enfoh comment), so ambient env vars set on the
    test's own subprocess.run(env=...) never reach the stub; the call
    counters are baked into the stub's own source text as absolute paths
    instead. Returns (result, probe_call_count, install_call_count).

    Every `pip install` (probe) and `tool install` (real install) call's
    FULL argv is also recorded, one invocation per line, to
    ``tmp_path / "probe-argv.log"`` / ``"install-argv.log"`` respectively
    (nexus-tt5vm review round 2, code-review-expert: the old stub matched
    only argv[1]/argv[2], so dropping a flag like --no-cache — this bead's
    exact defect class — would have passed green; see
    test_propagation_probe_argv_is_pinned and
    test_propagation_install_argv_is_pinned below, which read these files
    directly rather than threading them through this function's return)."""
    import os

    probe_counter = tmp_path / "probe-calls.txt"
    install_counter = tmp_path / "install-calls.txt"
    probe_argv_log = tmp_path / "probe-argv.log"
    install_argv_log = tmp_path / "install-argv.log"
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir()
    stub_uv = stub_dir / "uv"
    stub_uv.write_text(
        "#!/bin/bash\n"
        'if [ "$1" = "tool" ] && [ "$2" = "install" ]; then\n'
        f'    echo "$@" >> "{install_argv_log}"\n'
        f'    N=$(grep -c x "{install_counter}" 2>/dev/null || echo 0)\n'
        f'    echo x >> "{install_counter}"\n'
        f'    if [ "$N" -ge {install_fail_count} ]; then\n'
        "        exit 0\n"
        "    fi\n"
        "    echo '  x No solution found when resolving dependencies:' >&2\n"
        "    echo '  ... Because there is no version of conexus==1.2.3 ...' >&2\n"
        "    exit 1\n"
        "fi\n"
        'if [ "$1" = "tool" ] && [ "$2" = "dir" ]; then\n'
        "    echo /nonexistent-stub-uv-tool-dir\n"
        "    exit 0\n"
        "fi\n"
        'if [ "$1" = "venv" ]; then\n'
        "    exit 0\n"
        "fi\n"
        'if [ "$1" = "pip" ] && [ "$2" = "install" ]; then\n'
        f'    echo "$@" >> "{probe_argv_log}"\n'
        f'    N=$(grep -c x "{probe_counter}" 2>/dev/null || echo 0)\n'
        f'    echo x >> "{probe_counter}"\n'
        f'    if [ "$N" -ge {probe_fail_count} ]; then\n'
        "        exit 0\n"
        "    fi\n"
        "    echo '  x No solution found when resolving dependencies:' >&2\n"
        "    echo '  ... Because there is no version of conexus==1.2.3 ...' >&2\n"
        "    exit 1\n"
        "fi\n"
        "exit 1\n"
    )
    stub_uv.chmod(0o755)
    _write_stub_git(stub_dir, tag_verdict)

    env = dict(os.environ)
    env["PATH"] = f"{stub_dir}:{env.get('PATH', '')}"
    env.update(extra_env)

    result = subprocess.run(
        [str(SCRIPT), "--published", "1.2.3"],
        env=env, capture_output=True, text=True, timeout=60,
    )
    probe_calls = probe_counter.read_text().count("x") if probe_counter.is_file() else 0
    install_calls = (
        install_counter.read_text().count("x") if install_counter.is_file() else 0
    )
    return result, probe_calls, install_calls


# Tiny, test-only overrides of the propagation ceiling/backoff (real
# defaults: 1800s ceiling, 15s initial backoff, 60s cap) so the retry
# loop's own logic runs for real — no stubbed `sleep`, no injected clock —
# in well under a second of actual wall time.
_FAST_PROPAGATION_ENV = {
    "FRESH_MVV_PROPAGATION_CEILING_SECONDS": "3",
    "FRESH_MVV_PROPAGATION_INITIAL_BACKOFF_SECONDS": "0.05",
    "FRESH_MVV_PROPAGATION_MAX_BACKOFF_SECONDS": "0.05",
}


def test_propagation_retries_probe_then_succeeds_and_logs_elapsed_wait(tmp_path) -> None:
    """The core mechanism this bead's decision (b) asks for: the wait
    probes THROUGH uv's own resolution (a cheap `pip install --dry-run`
    against a throwaway venv), not a separate HTTP client. Stub uv's probe
    fails twice then succeeds; the real install then succeeds immediately.
    Pins: exactly 3 probe calls, exactly 1 install call after the initial
    (mandatory) failing one — 2 total — and a PROPAGATION_WAIT_S=<n>
    line (decision (a): elapsed wait logged on success)."""
    result, probe_calls, install_calls = _run_propagation_branch(
        tmp_path, _FAST_PROPAGATION_ENV, probe_fail_count=2, install_fail_count=1,
    )
    try:
        assert probe_calls == 3, (
            f"expected exactly 3 probe calls (2 failing + 1 succeeding), "
            f"saw {probe_calls}; stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert install_calls == 2, (
            f"expected exactly 2 install calls (the mandatory first "
            f"failure that enters the branch, then one success), saw "
            f"{install_calls}; stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert "waiting on uv's own resolution (nexus-tt5vm)" in result.stdout
        wait_match = re.search(r"PROPAGATION_WAIT_S=(\d+)", result.stdout)
        assert wait_match, f"no PROPAGATION_WAIT_S= line in stdout: {result.stdout!r}"
        assert int(wait_match.group(1)) >= 0
        # Proceeded past the retry machinery into the next leg (the stub's
        # fake tool venv doesn't exist) — proves the loop exited via
        # success, not merely that the process ended.
        assert "reported success but" in result.stderr, (
            f"script did not proceed past the retry loop; stderr={result.stderr!r}"
        )
        assert "stale for" not in result.stderr
    finally:
        _cleanup_preserved_evidence(result)


def test_propagation_ceiling_hit_reports_stale_edge_message(tmp_path) -> None:
    """nexus-tt5vm DECIDED (2026-09-24): drop the 300s bound fitted to one
    measurement; retry to a ~30min ceiling. Stub uv's probe never
    succeeds, so the tiny test ceiling is exceeded — the failure message
    must name the stale-edge cause and tell the operator to re-run the
    leg, textually distinct from a genuine (non-propagation) install
    failure's message."""
    result, probe_calls, install_calls = _run_propagation_branch(
        tmp_path, _FAST_PROPAGATION_ENV, probe_fail_count=10_000, install_fail_count=1,
    )
    try:
        assert result.returncode != 0
        assert probe_calls >= 2, (
            f"expected the ceiling to allow multiple probe attempts, saw "
            f"only {probe_calls}; stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        # install_calls stays at 1 (the mandatory first failure): the
        # ceiling fires from inside the PROBE loop, so the real install is
        # never retried again.
        assert install_calls == 1
        assert "stayed stale for" in result.stderr
        assert "re-run this leg" in result.stderr
        assert "not an install failure" in result.stderr
        assert "nexus-tt5vm" in result.stderr
        # Distinguishable from the non-propagation-error message (see
        # test_propagation_non_propagation_error_fails_immediately below):
        # never claims "even though uv's own resolve just confirmed".
        assert "just confirmed" not in result.stderr
    finally:
        _cleanup_preserved_evidence(result)


def test_propagation_non_propagation_error_fails_immediately_no_retry(tmp_path) -> None:
    """A failure shape outside the propagation signature set (unrelated
    dependency conflict, network genuinely down, etc.) must fail loud on
    the FIRST attempt — no probe venv built, no waiting, no retry — since
    `_is_propagation_miss` requires BOTH a `conexus` mention AND one of the
    propagation phrases (parity-pinned above)."""
    import os

    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir()
    stub_uv = stub_dir / "uv"
    stub_uv.write_text(
        "#!/bin/bash\n"
        'if [ "$1" = "tool" ] && [ "$2" = "install" ]; then\n'
        "    echo 'error: some unrelated dependency conflict' >&2\n"
        "    exit 1\n"
        "fi\n"
        "exit 1\n"
    )
    stub_uv.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{stub_dir}:{env.get('PATH', '')}"
    env.update(_FAST_PROPAGATION_ENV)

    result = subprocess.run(
        [str(SCRIPT), "--published", "1.2.3"],
        env=env, capture_output=True, text=True, timeout=30,
    )
    try:
        assert result.returncode != 0
        assert "waiting on uv's own resolution" not in result.stdout
        assert "network unreachable, version not published on PyPI" in result.stderr
    finally:
        _cleanup_preserved_evidence(result)


# ── nexus-tt5vm follow-up: the git-tag discriminator (coordinator directive,
# 2026-09-24) ─────────────────────────────────────────────────────────────


def test_propagation_typo_version_fails_in_seconds_no_probe_loop(tmp_path) -> None:
    """origin has no v1.2.3 tag -> this is an operator typo, not a
    propagation lag: fail loud in seconds, naming the missing tag, and
    never enter the probe loop at all (zero probe calls)."""
    result, probe_calls, install_calls = _run_propagation_branch(
        tmp_path, _FAST_PROPAGATION_ENV,
        # install_fail_count=1: the mandatory FIRST call must fail
        # (propagation-class) to enter the branch at all; the tag-absent
        # fast-exit then fires before any second install attempt.
        probe_fail_count=0, install_fail_count=1, tag_verdict="no",
    )
    try:
        assert result.returncode != 0
        assert probe_calls == 0, (
            f"expected the typo fast-exit to enter no probe loop at all, "
            f"saw {probe_calls} probe calls; stdout={result.stdout!r} "
            f"stderr={result.stderr!r}"
        )
        assert install_calls == 1, "only the mandatory first failing install call"
        assert "has no v1.2.3 tag" in result.stderr
        assert "operator typo" in result.stderr
        assert "waiting on uv's own resolution" not in result.stdout
    finally:
        _cleanup_preserved_evidence(result)


def test_propagation_tag_present_proceeds_to_the_probe_loop(tmp_path) -> None:
    """origin DOES have a v1.2.3 tag -> a real release, not a typo; says so
    and proceeds into the normal probe/retry loop exactly as before the
    discriminator existed."""
    result, probe_calls, install_calls = _run_propagation_branch(
        tmp_path, _FAST_PROPAGATION_ENV,
        probe_fail_count=1, install_fail_count=1, tag_verdict="yes",
    )
    try:
        assert "carries tag v1.2.3" in result.stdout
        assert "a real release, not a typo" in result.stdout
        assert "waiting on uv's own resolution (nexus-tt5vm)" in result.stdout
        assert probe_calls == 2
        assert install_calls == 2
        assert "reported success but" in result.stderr
    finally:
        _cleanup_preserved_evidence(result)


def test_propagation_tag_check_unreachable_falls_through_with_warning(tmp_path) -> None:
    """The remote itself could not be contacted (DNS/network failure, rc
    128 — the real git shape, verified by hand against a nonexistent
    host) -- this must NEVER be treated as evidence of a typo. One-line
    warning, then the full propagation wait proceeds unchanged (never a
    false fast-fail)."""
    result, probe_calls, install_calls = _run_propagation_branch(
        tmp_path, _FAST_PROPAGATION_ENV,
        probe_fail_count=1, install_fail_count=1, tag_verdict="unreachable",
    )
    try:
        assert "could not reach" in result.stdout
        assert "proceeding with the full propagation wait" in result.stdout
        assert "waiting on uv's own resolution (nexus-tt5vm)" in result.stdout
        assert "has no v1.2.3 tag" not in result.stderr
        assert probe_calls == 2
        assert install_calls == 2
        assert "reported success but" in result.stderr
    finally:
        _cleanup_preserved_evidence(result)


def test_propagation_tag_check_timeout_falls_through_not_a_false_fastfail(tmp_path) -> None:
    """nexus-tt5vm review round 2 (code-review-expert + substantive-critic):
    GIT_HTTP_LOW_SPEED_LIMIT/TIME does not bound a hung DNS lookup or a
    connect() that never completes -- only a stalled IN-PROGRESS transfer.
    A stub `git` that just sleeps past FRESH_MVV_TAG_CHECK_TIMEOUT_SECONDS
    (never touching HTTP at all) must still resolve to "unreachable" and
    fall through to the full wait -- never fast-fail as a typo, and the
    wall-clock bound must actually fire (this test's own timeout=60s
    subprocess bound would catch a genuinely-unbounded hang, but the
    assertion on elapsed time below is the real proof)."""
    import time

    env = dict(_FAST_PROPAGATION_ENV)
    env["FRESH_MVV_TAG_CHECK_TIMEOUT_SECONDS"] = "1"
    t0 = time.monotonic()
    result, probe_calls, install_calls = _run_propagation_branch(
        tmp_path, env, probe_fail_count=1, install_fail_count=1, tag_verdict="timeout",
    )
    elapsed = time.monotonic() - t0
    try:
        assert "could not reach" in result.stdout
        assert "proceeding with the full propagation wait" in result.stdout
        assert "waiting on uv's own resolution (nexus-tt5vm)" in result.stdout
        assert "has no v1.2.3 tag" not in result.stderr
        # The stub sleeps 5s; a working 1s wall-clock bound means the
        # WHOLE run (tag check + the fast propagation loop below it)
        # finishes well under 5s. This is the proof that the bound fired
        # rather than the process silently completing on its own.
        assert elapsed < 4.5, (
            f"expected the 1s tag-check timeout to bound the 5s-sleeping "
            f"stub git, but the run took {elapsed:.1f}s -- the wall-clock "
            f"bound did not fire"
        )
    finally:
        _cleanup_preserved_evidence(result)


# ── nexus-tt5vm review round 2: pin the exact flags, not just argv[1]/[2]
# (code-review-expert: a stub matching only the first two args would pass
# green even if --no-cache were dropped from the script -- this bead's own
# defect class) ──────────────────────────────────────────────────────────


def _last_argv_line(log_path) -> list[str]:
    assert log_path.is_file(), f"no argv recorded at all: {log_path}"
    lines = [l for l in log_path.read_text().splitlines() if l.strip()]
    assert lines, f"argv log exists but is empty: {log_path}"
    return lines[-1].split()


def test_propagation_probe_argv_is_pinned(tmp_path) -> None:
    """The cheap propagation probe must carry --dry-run (never actually
    install), --no-deps (cheapest resolve of the target package only),
    and --no-cache (nexus-tt5vm's own defect: a stale negative resolution
    cached locally would silently defeat the whole retry). Also pins the
    exact package spec (the version under test, not a drifted one) and
    the absence of any index-override flag (this is a resolution-layer
    test against the REAL default PyPI, never an operator's configured
    mirror -- same invariant _uv_sandboxed's own nexus-enfoh comment
    states for the install call)."""
    result, probe_calls, install_calls = _run_propagation_branch(
        tmp_path, _FAST_PROPAGATION_ENV, probe_fail_count=2, install_fail_count=1,
    )
    try:
        argv = _last_argv_line(tmp_path / "probe-argv.log")
        for flag in ("--dry-run", "--no-deps", "--no-cache"):
            assert flag in argv, f"propagation probe argv missing {flag}: {argv}"
        assert "conexus==1.2.3" in argv, f"propagation probe argv missing the package spec: {argv}"
        assert not any(a.startswith("--index") for a in argv), (
            f"propagation probe argv carries an index-override flag -- "
            f"this must resolve against the real default PyPI: {argv}"
        )
    finally:
        _cleanup_preserved_evidence(result)


def test_propagation_probe_argv_loses_no_cache_would_fail_this_test(tmp_path) -> None:
    """Falsification control (Sam's fix-round directive: 'prove it: remove
    --no-cache from the script and the test fails'): patches a throwaway
    copy of the script with --no-cache stripped from the probe call, runs
    it standalone (never through _run_propagation_branch -- this control
    needs its own minimal stub, not the shared fixture, to isolate what
    changed), and asserts the recorded argv reflects exactly that removal.
    This is the test proving test_propagation_probe_argv_is_pinned has
    teeth, not a coverage duplicate -- it never runs the real script."""
    import os

    patched = tmp_path / "fresh-install-mvv-patched.sh"
    text = SCRIPT.read_text()
    needle = "pip install --dry-run --no-deps --no-cache \\"
    assert needle in text, "probe invocation shape changed; update this control's needle"
    patched.write_text(text.replace(needle, "pip install --dry-run --no-deps \\", 1))
    patched.chmod(0o755)

    probe_argv_log = tmp_path / "probe-argv.log"
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir()
    stub_uv = stub_dir / "uv"
    # Minimal stub: fail `tool install` once (propagation-class, enters
    # the branch), succeed `venv`, record + fail `pip install` forever
    # (the assertion only needs ONE recorded probe call).
    stub_uv.write_text(
        "#!/bin/bash\n"
        'if [ "$1" = "tool" ] && [ "$2" = "install" ]; then\n'
        "    echo '  x No solution found when resolving dependencies:' >&2\n"
        "    echo '  ... Because there is no version of conexus==1.2.3 ...' >&2\n"
        "    exit 1\n"
        "fi\n"
        'if [ "$1" = "venv" ]; then\n'
        "    exit 0\n"
        "fi\n"
        'if [ "$1" = "pip" ] && [ "$2" = "install" ]; then\n'
        f'    echo "$@" >> "{probe_argv_log}"\n'
        "    exit 1\n"
        "fi\n"
        "exit 1\n"
    )
    stub_uv.chmod(0o755)
    _write_stub_git(stub_dir, "yes")

    env = dict(os.environ)
    env["PATH"] = f"{stub_dir}:{env.get('PATH', '')}"
    # Tiny ceiling: the probe fails forever in this control, so bound the
    # wait to a fraction of a second rather than needing a success path.
    env["FRESH_MVV_PROPAGATION_CEILING_SECONDS"] = "1"
    env["FRESH_MVV_PROPAGATION_INITIAL_BACKOFF_SECONDS"] = "0.05"
    env["FRESH_MVV_PROPAGATION_MAX_BACKOFF_SECONDS"] = "0.05"
    result = subprocess.run(
        [str(patched), "--published", "1.2.3"],
        env=env, capture_output=True, text=True, timeout=30,
    )
    try:
        argv = _last_argv_line(probe_argv_log)
        assert "--no-cache" not in argv, (
            f"control setup failed: the patch did not actually remove "
            f"--no-cache from the probe's recorded argv: {argv}"
        )
    finally:
        _cleanup_preserved_evidence(result)


def test_propagation_install_argv_is_pinned(tmp_path) -> None:
    """nexus-tt5vm review round 2 (code-review-expert): the retried
    INSTALL calls (not only the probe) must also carry --no-cache -- a
    lower-layer instance of the exact same staleness class this bead
    exists to close. Checks the MANDATORY first install attempt (the one
    that enters the propagation branch), which is present on every run
    regardless of install_fail_count."""
    result, probe_calls, install_calls = _run_propagation_branch(
        tmp_path, _FAST_PROPAGATION_ENV, probe_fail_count=1, install_fail_count=1,
    )
    try:
        install_log = tmp_path / "install-argv.log"
        assert install_log.is_file(), "no install argv recorded at all"
        for line in install_log.read_text().splitlines():
            argv = line.split()
            if not argv:
                continue
            assert "--no-cache" in argv, (
                f"a `uv tool install` retry carries no --no-cache -- stale "
                f"local-cache class, one layer below the probe's own fix: {argv}"
            )
            assert "conexus==1.2.3" in argv
    finally:
        _cleanup_preserved_evidence(result)
