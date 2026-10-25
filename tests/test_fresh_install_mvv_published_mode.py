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

import fnmatch
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "tests" / "e2e" / "fresh-install-mvv.sh"


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


_SENTINEL = "operator-file-that-must-survive.txt"


@pytest.fixture(autouse=True)
def _isolated_tmpdir(tmp_path_factory, monkeypatch):
    """nexus-0tqr7: the script's work tree and failure evidence now land under
    ``$TMPDIR``. Every script run in this file inherits a throwaway TMPDIR (all
    the env dicts below derive from ``os.environ``), and a sentinel file in it
    must survive the test: the old cleanup did ``rmtree(evidence.parent)``,
    which after the move IS the TMPDIR, and deleted an operator's scratch dir.
    """
    tmpdir = tmp_path_factory.mktemp("mvv-tmpdir")
    sentinel = tmpdir / _SENTINEL
    sentinel.write_text("keep\n")
    monkeypatch.setenv("TMPDIR", str(tmpdir))
    yield tmpdir
    assert sentinel.read_text() == "keep\n", "a test or the script wiped the TMPDIR"


def _remove_evidence(stderr: str) -> None:
    """Remove the failure-evidence dir the script printed, and ONLY that.

    Refuses unless the path is ``<TMPDIR>/fresh-mvv-*-logs`` with this test's
    own TMPDIR as its parent, so a changed message format can never turn this
    into a delete of the TMPDIR itself or of anything outside it.
    """
    match = re.search(r"FAILURE EVIDENCE PRESERVED: (\S+)", stderr)
    if not match:
        return
    evidence = Path(os.path.realpath(match.group(1)))
    tmpdir = Path(os.path.realpath(os.environ["TMPDIR"]))
    if (
        evidence.parent == tmpdir
        and fnmatch.fnmatch(evidence.name, "fresh-mvv-*-logs")
        and evidence.is_dir()
    ):
        shutil.rmtree(evidence, ignore_errors=True)


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
        # The overrides fetch (nexus-11x9o) runs before `tool install`; answer it.
        + _fetch_stub(tmp_path)
        + 'if [ "$1" = "tool" ] && [ "$2" = "install" ]; then\n'
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
        # Best-effort cleanup of the preserved-on-failure evidence dir (the
        # script deliberately keeps it, see cleanup()); guarded, see
        # _remove_evidence.
        _remove_evidence(result.stderr)


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


def test_the_summary_prints_the_slim_leg_status_just_before_the_sentinel() -> None:
    """A skipped leg 9b must be visible in the summary (nexus-2k66t.4.9); the PASSED sentinel stays last."""
    text = _text()
    summary = text.index('echo "FRESH-INSTALL MVV leg 9b (slim generation): $SLIM_LEG"')
    sentinel = text.index('echo "FRESH-INSTALL MVV PASSED', summary)
    assert 0 < sentinel - summary < 200, "the status line must sit directly before the sentinel"
    assert text.rindex('echo "FRESH-INSTALL MVV PASSED') > summary


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
    _remove_evidence(result.stderr)


def test_evidence_cleanup_removes_only_the_evidence_dir_never_the_tmpdir(
    _isolated_tmpdir: Path,
) -> None:
    """Regression (nexus-0tqr7 review): the printed evidence path is
    ``<TMPDIR>/fresh-mvv-*-logs``; removing its parent removed the TMPDIR."""
    ev = _isolated_tmpdir / "fresh-mvv-AbC123-logs"
    ev.mkdir()
    (ev / "leg.log").write_text("x")
    _remove_evidence(f"FAILURE EVIDENCE PRESERVED: {ev} (kept: legs)\n")
    assert not ev.exists()
    assert (_isolated_tmpdir / _SENTINEL).exists()


@pytest.mark.parametrize("name", ["fresh-mvv-AbC123", "other-logs"])
def test_evidence_cleanup_refuses_a_path_that_is_not_the_evidence_dir(
    _isolated_tmpdir: Path, name: str,
) -> None:
    victim = _isolated_tmpdir / name
    victim.mkdir()
    _remove_evidence(f"FAILURE EVIDENCE PRESERVED: {victim}\n")
    assert victim.exists()
    assert (_isolated_tmpdir / _SENTINEL).exists()


def test_evidence_cleanup_refuses_the_tmpdir_itself(_isolated_tmpdir: Path) -> None:
    _remove_evidence(f"FAILURE EVIDENCE PRESERVED: {_isolated_tmpdir}\n")
    assert (_isolated_tmpdir / _SENTINEL).exists()


def test_evidence_cleanup_refuses_a_dir_outside_the_tmpdir(tmp_path: Path) -> None:
    elsewhere = tmp_path / "fresh-mvv-ZZZ-logs"
    elsewhere.mkdir()
    _remove_evidence(f"FAILURE EVIDENCE PRESERVED: {elsewhere}\n")
    assert elsewhere.exists()


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


# The harness reads the overrides and constraints from the PUBLISHED package, not
# the checkout (nexus-11x9o): it creates a scratch venv (`uv venv --python 3.12`,
# which downloads a managed Python when the host has none), fetches the conexus
# wheel alone into it (`uv pip install --python VENV/bin/python --no-deps
# --target DIR conexus==X`) and hands DIR/nexus/_install/{overrides,constraints}.txt
# to `uv tool install`. These stub fragments answer the venv and the fetch (the
# marker lines prove the files came from the fetched package) and record what
# `tool install` was given.
#
# The fetch branch REFUSES `--python 3.12` the way a `uv pip` that does not
# download interpreters does on a host with no system python3.12 (whether uv pip
# downloads depends on the uv version: 0.12.24 does, so the harness may not count
# on it): the --python value must be an executable path, i.e. the scratch venv's
# interpreter. That is what lets this suite see the 2026-10-25 review's B1 class.
# The venv branch creates that interpreter. Both sit BEFORE the generic `pip install` / `venv`
# branches of the individual stubs, whose calls the propagation probe counts.
OVERRIDES_MARKER = "# stub-published-package-overrides"
CONSTRAINTS_MARKER = "# stub-published-package-constraints"

_FETCH_STUB_BRANCH = (
    'if [ "$1" = "venv" ] && [[ "${{@: -1}}" == *overrides-fetch-venv ]]; then\n'
    '    echo "$@" >> "{venv_argv_log}"\n'
    "{venv_fail}"
    '    D="${{@: -1}}"; mkdir -p "$D/bin"\n'
    "    printf '#!/bin/sh\\nexit 0\\n' > \"$D/bin/python\"; chmod +x \"$D/bin/python\"\n"
    "    exit 0\n"
    "fi\n"
    'if [ "$1" = "pip" ] && [ "$2" = "install" ] && [[ " $* " == *" --target "* ]]; then\n'
    '    echo "$@" >> "{fetch_argv_log}"\n'
    '    PY=""; PREV=""\n'
    '    for A in "$@"; do [ "$PREV" = "--python" ] && PY="$A"; PREV="$A"; done\n'
    '    if [ ! -x "$PY" ]; then\n'
    "        echo \"error: No interpreter found for Python $PY in managed installations or search path\" >&2\n"
    "        exit 2\n"
    "    fi\n"
    '    N=$(grep -c x "{fetch_counter}" 2>/dev/null || echo 0)\n'
    '    echo x >> "{fetch_counter}"\n'
    '    if [ "$N" -lt {fetch_fail_count} ]; then\n'
    "        echo '  x No solution found when resolving dependencies:' >&2\n"
    "        echo '  ... Because there is no version of conexus==1.2.3 ...' >&2\n"
    "        exit 1\n"
    "    fi\n"
    '    T=""; PREV=""\n'
    '    for A in "$@"; do [ "$PREV" = "--target" ] && T="$A"; PREV="$A"; done\n'
    '    mkdir -p "$T/nexus/_install"\n'
    f'    printf "%s\\nopencv-python; sys_platform == \'never\'\\n" "{OVERRIDES_MARKER}" > "$T/nexus/_install/overrides.txt"\n'
    f'    printf "%s\\nopencv-python-headless==4.13.0.92\\n" "{CONSTRAINTS_MARKER}" > "$T/nexus/_install/constraints.txt"\n'
    "    exit 0\n"
    "fi\n"
)


def _fetch_stub(tmp_path, fetch_fail_count: int = 0, venv_fails: bool = False) -> str:
    """The venv + overrides-fetch stub branches, logging under *tmp_path*."""
    venv_fail = (
        "    echo 'error: failed to download Python 3.12: network unreachable' >&2\n    exit 1\n"
        if venv_fails else ""
    )
    return _FETCH_STUB_BRANCH.format(
        venv_argv_log=tmp_path / "venv-argv.log",
        venv_fail=venv_fail,
        fetch_argv_log=tmp_path / "fetch-argv.log",
        fetch_counter=tmp_path / "fetch-calls.txt",
        fetch_fail_count=fetch_fail_count,
    )


_RECORD_OVERRIDES_STUB = (
    '    PREV=""\n'
    '    for A in "$@"; do\n'
    '        if [ "$PREV" = "--overrides" ] && [ -f "$A" ]; then cat "$A" >> "{overrides_seen_log}"; fi\n'
    '        if [ "$PREV" = "--constraints" ] && [ -f "$A" ]; then cat "$A" >> "{constraints_seen_log}"; fi\n'
    '        PREV="$A"\n'
    "    done\n"
)


def _run_propagation_branch(
    tmp_path,
    extra_env: dict[str, str],
    probe_fail_count: int,
    install_fail_count: int,
    tag_verdict: str = "yes",
    fetch_fail_count: int = 0,
    script: Path = SCRIPT,
    venv_fails: bool = False,
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
    overrides_seen_log = tmp_path / "install-overrides-content.log"
    constraints_seen_log = tmp_path / "install-constraints-content.log"
    stub_uv.write_text(
        "#!/bin/bash\n"
        + _fetch_stub(tmp_path, fetch_fail_count, venv_fails)
        + 'if [ "$1" = "tool" ] && [ "$2" = "install" ]; then\n'
        f'    echo "$@" >> "{install_argv_log}"\n'
        + _RECORD_OVERRIDES_STUB.format(
            overrides_seen_log=overrides_seen_log, constraints_seen_log=constraints_seen_log,
        )
        + f'    N=$(grep -c x "{install_counter}" 2>/dev/null || echo 0)\n'
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
        [str(script), "--published", "1.2.3"],
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
        # The overrides fetch runs first (nexus-11x9o); answer it so the failure
        # under test is the `tool install` one.
        + _fetch_stub(tmp_path)
        + 'if [ "$1" = "tool" ] && [ "$2" = "install" ]; then\n'
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
    text = text.replace(needle, "pip install --dry-run --no-deps \\", 1)
    # The copy lives in tmp_path, so its own `dirname/../..` no longer finds the
    # repo; pin REPO_ROOT to the real checkout.
    repo_root_line = 'REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"'
    assert repo_root_line in text, "REPO_ROOT derivation changed; update this control"
    text = text.replace(repo_root_line, f'REPO_ROOT="{REPO_ROOT}"', 1)
    # Same for the interpreter resolver the script sources from beside itself (nexus-u67ow).
    py_line = 'source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/python.sh"'
    assert py_line in text, "python.sh sourcing changed; update this control"
    text = text.replace(py_line, f'source "{REPO_ROOT}/tests/e2e/lib/python.sh"', 1)
    patched.write_text(text)
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
        + _fetch_stub(tmp_path)
        + 'if [ "$1" = "tool" ] && [ "$2" = "install" ]; then\n'
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


# ── nexus-11x9o: the registry install applies the PUBLISHED package's overrides
# (a bare `uv tool install conexus[pdf]` ignores src/nexus/_install/overrides.txt,
# so opencv-python and opencv-python-headless both landed, the non-headless cv2
# won 6/6 on chas, and MinerU's formula leg died on a libGL-less Linux box).


def _patched_script(tmp_path, old: str, new: str) -> Path:
    """A copy of the harness with *old* replaced by *new* (a hand-made regression),
    runnable from tmp_path: REPO_ROOT and the python.sh source are pinned to the checkout."""
    text = SCRIPT.read_text()
    assert old in text, f"harness shape changed; update this control's needle: {old!r}"
    text = text.replace(old, new, 1)
    repo_root_line = 'REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"'
    assert repo_root_line in text
    text = text.replace(repo_root_line, f'REPO_ROOT="{REPO_ROOT}"', 1)
    py_line = 'source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/python.sh"'
    assert py_line in text
    text = text.replace(py_line, f'source "{REPO_ROOT}/tests/e2e/lib/python.sh"', 1)
    patched = tmp_path / "fresh-install-mvv-patched.sh"
    patched.write_text(text)
    patched.chmod(0o755)
    return patched


def _assert_install_carries_published_overrides_and_constraints(tmp_path) -> None:
    """The assertion body shared by the real test and its controls, so a control
    proves THIS check fails on a broken harness rather than re-asserting its own patch."""
    argv = _last_argv_line(tmp_path / "install-argv.log")
    assert "--overrides" in argv, (
        f"`uv tool install` carries no --overrides: a bare registry install "
        f"leaves both opencv wheels in one cv2/ (nexus-11x9o): {argv}"
    )
    assert "--constraints" in argv, (
        f"`uv tool install` carries no --constraints: the install resolves fresh "
        f"instead of the release's uv.lock versions (install_generation.sh does both): {argv}"
    )
    overrides_seen = (tmp_path / "install-overrides-content.log").read_text()
    assert OVERRIDES_MARKER in overrides_seen, (
        f"the --overrides file is not the fetched package's overrides.txt: {overrides_seen!r}"
    )
    constraints_seen = (tmp_path / "install-constraints-content.log").read_text()
    assert CONSTRAINTS_MARKER in constraints_seen, (
        f"the --constraints file is not the fetched package's constraints.txt: {constraints_seen!r}"
    )


def test_published_install_passes_the_published_packages_overrides_and_constraints(tmp_path) -> None:
    """`uv tool install` carries `--overrides FILE` and `--constraints FILE`, and each
    holds exactly the file of the fetched PUBLISHED package (the stub's marker
    lines), not this checkout's copy: the harness tests published bytes."""
    result, _probe_calls, _install_calls = _run_propagation_branch(
        tmp_path, _FAST_PROPAGATION_ENV, probe_fail_count=0, install_fail_count=0,
    )
    try:
        _assert_install_carries_published_overrides_and_constraints(tmp_path)
        fetch = _last_argv_line(tmp_path / "fetch-argv.log")
        for flag in ("--no-deps", "--no-cache", "--target"):
            assert flag in fetch, f"overrides fetch argv missing {flag}: {fetch}"
        assert "conexus==1.2.3" in fetch, f"fetch is not pinned to the version under test: {fetch}"
        assert not any(a.startswith("--index") for a in fetch), fetch
    finally:
        _cleanup_preserved_evidence(result)


@pytest.mark.parametrize(
    "needle",
    [' --overrides "$OVERRIDES_FILE"', ' --constraints "$CONSTRAINTS_FILE"'],
    ids=["overrides", "constraints"],
)
def test_control_a_harness_without_the_flag_fails_the_shared_assertion(tmp_path, needle) -> None:
    """Falsification control (nexus-11x9o F3): strip the flag from a copy of the
    harness, run it, and the SAME assertion the real test uses must fail."""
    patched = _patched_script(tmp_path, needle, "")
    result, _p, install_calls = _run_propagation_branch(
        tmp_path, _FAST_PROPAGATION_ENV, probe_fail_count=0, install_fail_count=0, script=patched,
    )
    try:
        assert install_calls >= 1, "control setup failed: the patched harness never reached `tool install`"
        with pytest.raises(AssertionError):
            _assert_install_carries_published_overrides_and_constraints(tmp_path)
    finally:
        _cleanup_preserved_evidence(result)


def _assert_fetch_runs_in_a_scratch_venv(tmp_path) -> None:
    """On a uv whose `uv pip` does not download interpreters, `--python 3.12` on the fetch
    dies on a host with no system python3.12. The fetch must name the scratch venv's
    interpreter, which `uv venv --python 3.12` created (and downloaded a managed Python for)."""
    fetch = _last_argv_line(tmp_path / "fetch-argv.log")
    py = fetch[fetch.index("--python") + 1]
    assert py != "3.12" and py.endswith("/bin/python"), (
        f"the overrides fetch names {py!r} as its interpreter; a uv whose `uv pip` does not "
        f"download one fails before installing on a host without a system python3.12: {fetch}"
    )
    venv_calls = [ln.split() for ln in (tmp_path / "venv-argv.log").read_text().splitlines() if ln.strip()]
    assert venv_calls, "no scratch venv was created before the fetch"
    assert "--python" in venv_calls[-1] and venv_calls[-1][venv_calls[-1].index("--python") + 1] == "3.12"
    assert py == venv_calls[-1][-1] + "/bin/python", (venv_calls, py)


def test_overrides_fetch_runs_in_a_scratch_venv_not_bare_python_312(tmp_path) -> None:
    result, _p, install_calls = _run_propagation_branch(
        tmp_path, _FAST_PROPAGATION_ENV, probe_fail_count=0, install_fail_count=0,
    )
    try:
        assert install_calls == 1, (
            "the install was never reached: the stub refuses `uv pip install --python 3.12` "
            f"the way a non-downloading uv does on a host with no system python3.12; stderr={result.stderr!r}"
        )
        _assert_fetch_runs_in_a_scratch_venv(tmp_path)
    finally:
        _cleanup_preserved_evidence(result)


def test_control_a_fetch_with_bare_python_312_is_refused_and_fails_the_assertion(tmp_path) -> None:
    patched = _patched_script(tmp_path, '--python "$fetch_venv/bin/python"', "--python 3.12")
    result, _p, install_calls = _run_propagation_branch(
        tmp_path, _FAST_PROPAGATION_ENV, probe_fail_count=0, install_fail_count=0, script=patched,
    )
    try:
        assert install_calls == 0, "control setup failed: the stub did not refuse the bare interpreter"
        assert "FRESH-INSTALL MVV FAILED" in result.stderr
        with pytest.raises(AssertionError):
            _assert_fetch_runs_in_a_scratch_venv(tmp_path)
    finally:
        _cleanup_preserved_evidence(result)


def test_a_scratch_venv_failure_is_a_plain_failure_not_a_propagation_wait(tmp_path) -> None:
    """The venv step must not change the propagation-miss signature: its failure
    (no network to fetch a Python) never mentions conexus, so it fails at once with the
    install-failure message instead of waiting for a PyPI edge that was never at fault."""
    result, probe_calls, install_calls = _run_propagation_branch(
        tmp_path, _FAST_PROPAGATION_ENV, probe_fail_count=0, install_fail_count=0, venv_fails=True,
    )
    try:
        assert result.returncode != 0
        assert "waiting on uv's own resolution" not in result.stdout
        assert probe_calls == 0 and install_calls == 0
        assert "network unreachable, version not published on PyPI" in result.stderr
    finally:
        _cleanup_preserved_evidence(result)


def test_overrides_fetch_miss_enters_the_propagation_wait_not_a_hard_fail(tmp_path) -> None:
    """The overrides fetch is the first resolver call of a published run, so a
    just-published version that the index does not serve yet fails THERE. That
    must enter the same propagation wait as a failed `uv tool install`, and the
    retried install must then carry the overrides."""
    result, probe_calls, install_calls = _run_propagation_branch(
        tmp_path, _FAST_PROPAGATION_ENV, probe_fail_count=1, install_fail_count=0,
        fetch_fail_count=1,
    )
    try:
        assert "waiting on uv's own resolution" in result.stdout, result.stdout + result.stderr
        assert probe_calls >= 2, "the propagation probe never ran after the fetch miss"
        assert install_calls >= 1
        _assert_install_carries_published_overrides_and_constraints(tmp_path)
    finally:
        _cleanup_preserved_evidence(result)


# ── the installed tool env holds one OpenCV (nexus-11x9o S1) ──────────────────


def _run_with_installed_dists(tmp_path, dists: list[str], pdf_check: bool, script: Path = SCRIPT):
    """Run the published leg against a stub whose `tool install` leaves *dists*
    (dist-info directory names) in a tool venv, so the post-install OpenCV check runs."""
    import os

    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir()
    tool_dir = tmp_path / "tooldir"
    site = tool_dir / "conexus" / "lib" / "python3.12" / "site-packages"
    infos = "".join(f'    mkdir -p "{site}/{d}.dist-info"\n' for d in dists)
    (stub_dir / "uv").write_text(
        "#!/bin/bash\n"
        + _fetch_stub(tmp_path)
        + 'if [ "$1" = "tool" ] && [ "$2" = "install" ]; then\n'
        + f'    mkdir -p "{site}"\n'
        + infos
        + "    exit 0\n"
        "fi\n"
        'if [ "$1" = "tool" ] && [ "$2" = "dir" ]; then\n'
        f'    echo "{tool_dir}"\n'
        "    exit 0\n"
        "fi\n"
        "exit 1\n"
    )
    (stub_dir / "uv").chmod(0o755)
    _write_stub_git(stub_dir, "yes")
    env = dict(os.environ)
    env["PATH"] = f"{stub_dir}:{env.get('PATH', '')}"
    if pdf_check:
        env["NX_MVV_FORMULA_PDF_CHECK"] = "1"
    return subprocess.run(
        [str(script), "--published", "1.2.3"], env=env, capture_output=True, text=True, timeout=60,
    )


_TWO_OPENCV = ["opencv_python-4.13.0.92", "opencv_python_headless-4.13.0.92"]
_HEADLESS_ONLY = ["opencv_python_headless-4.13.0.92"]


def test_a_non_headless_opencv_in_the_tool_env_fails_the_leg() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        result = _run_with_installed_dists(Path(d), _TWO_OPENCV, pdf_check=True)
    try:
        assert result.returncode != 0
        assert "opencv_python-4.13.0.92.dist-info" in result.stderr, result.stderr
        assert "libGL" in result.stderr
    finally:
        _cleanup_preserved_evidence(result)


def test_headless_only_passes_the_opencv_check_and_proceeds() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        result = _run_with_installed_dists(Path(d), _HEADLESS_ONLY, pdf_check=True)
    try:
        assert "opencv" not in result.stderr.lower(), result.stderr
        assert "did not expose" in result.stderr, "the leg should proceed to the next assertion"
    finally:
        _cleanup_preserved_evidence(result)


def test_the_formula_run_fails_when_no_opencv_is_installed_at_all() -> None:
    """Non-vacuity: with [pdf] requested the headless build must be there, or the
    absence of the non-headless one proves nothing."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        result = _run_with_installed_dists(Path(d), [], pdf_check=True)
    try:
        assert result.returncode != 0
        assert "opencv_python_headless" in result.stderr and "vacuous" in result.stderr, result.stderr
    finally:
        _cleanup_preserved_evidence(result)


def test_a_slim_run_needs_no_opencv() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        result = _run_with_installed_dists(Path(d), [], pdf_check=False)
    try:
        assert "opencv" not in result.stderr.lower(), result.stderr
        assert "did not expose" in result.stderr
    finally:
        _cleanup_preserved_evidence(result)


def test_control_without_the_opencv_check_the_two_build_env_goes_unnoticed(tmp_path) -> None:
    """Falsification control: delete the check's call from a copy of the harness and
    the two-build env sails past it, which is what the test above rejects."""
    patched = _patched_script(tmp_path, "_assert_one_opencv_build\n", ":\n")
    run = tmp_path / "run"
    run.mkdir()
    result = _run_with_installed_dists(run, _TWO_OPENCV, pdf_check=True, script=patched)
    try:
        assert "opencv_python-4.13.0.92.dist-info" not in result.stderr
        assert "did not expose" in result.stderr
    finally:
        _cleanup_preserved_evidence(result)


def test_local_wheel_install_applies_the_wheels_own_overrides() -> None:
    """The local-wheel leg stays consistent with the published one (nexus-11x9o):
    its `uv pip install` carries `--overrides` read out of the wheel under test."""
    text = _text()
    local_block = text[text.index('Build the wheel under test ──"'):]
    local_block = local_block[: local_block.index("MCP entry-point handshake")]
    assert 'nexus/_install/overrides.txt' in local_block
    assert '--overrides "$OVERRIDES_FILE" "$WHEEL"' in local_block
