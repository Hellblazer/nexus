# SPDX-License-Identifier: AGPL-3.0-or-later
"""Durable falsifiability demonstration for rehearse_shakeout.sh's Phase D
concurrent-store-put census (nexus-xm0cp bead's own MANDATORY ACCEPTANCE
CRITERION — "the changed gate has been SHOWN FAILING against a deliberately
broken input, and that demonstration is recorded"). A pasted RED/GREEN
transcript decays; a fixture re-runs on every PR via the ``-m lint`` bucket.

WHAT THIS EXERCISES. rehearse_shakeout.sh has no ``--self-test`` seam and
cannot be driven end-to-end without a container + native binary + live
service, so this module does not invoke the harness script itself. Instead
the harness's Phase D census logic was extracted into
``tests/e2e/migration-rehearsal/lib/store_put_census.sh``
(``census_concurrent_store_puts``), which the real harness sources and
calls (``test_rehearse_shakeout_sources_and_calls_the_library`` pins that
wiring so the extraction cannot silently drift back into a copy-pasted
inline loop). Every test below sources that SAME file and calls that SAME
function with a stub ``nx`` placed first on ``$PATH`` — real bash, real
function, a fake subprocess boundary standing in for the network call.

TWO ASSERTIONS ARE PROVEN AGAINST BOTH A BROKEN AND A REPAIRED INPUT:

  1. ``STORE_FAILS`` / ``STORE_FAIL_IDX`` — the client-rc census
     (nexus-xm0cp's original de-vacuation: a `nx store put` that exits
     non-zero must be counted and its index named).
  2. ``STORE_RETRIES`` / ``STORE_RETRY_IDX`` — the absorbed-gateway-retry
     census (Finding 2's closed gap: a call that exits 0 because
     HttpVectorClient's bounded retry silently absorbed a 502/503/504 must
     still be caught, via a ``vector_gateway_retry`` log scan, since the
     bare exit code cannot see it).

Each has a RED test (deliberately broken stub — a failing call / a
retry-logging call that still exits 0) and a GREEN test (repaired stub —
every call clean) in the same module, so a reader sees both the failure
shape and the healthy shape side by side.

ALSO VERIFIED (nexus-xm0cp's "also verify" instruction): the load-bearing
assumption that ``nx store put`` genuinely exits non-zero on a
non-retryable service error is NOT re-derived here by construction — a
stub can always be made to exit nonzero regardless of what the real CLI
does. It is established by reading the real code path instead (recorded
in the bead's write-back, not re-asserted as a test here): ``nx store
put`` (``src/nexus/commands/store.py``) calls ``T3Database.put`` ->
``HttpVectorClient``, whose ``_request()`` wrapper
(``src/nexus/db/http_vector_client.py``) retries ONLY
``_GATEWAY_RETRY_CODES = {502, 503, 504}`` within a bounded budget and
re-raises ``urllib.error.HTTPError`` immediately for every other non-2xx
code (a plain 500, 4xx, etc.); ``store.py``'s ``except Exception as
put_exc: ... raise`` re-raises that unchanged; Click's ``main()`` only
catches its own ``ClickException``/``Abort`` — a bare Python exception
propagates to the interpreter, which exits the process with a nonzero
code. So a non-retryable 5xx really does surface as a nonzero `nx store
put` exit, which is what the stub-based RED test above simulates.

BUCKET: ``-m lint`` (invocation-independent — subprocess + a throwaway
stub binary, no repo-filesystem-walking dependency, but the same placement
convention as the sibling ``test_release_artifact_log_event_rot.py`` this
module was filed alongside).
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = Path(__file__).parent.parent
LIB = REPO_ROOT / "tests/e2e/migration-rehearsal/lib/store_put_census.sh"
SHAKEOUT = REPO_ROOT / "tests/e2e/migration-rehearsal/rehearse_shakeout.sh"

#: Homebrew bash on macOS (bash 4+ associative-array-capable); falls back to
#: whatever `bash` resolves to on PATH elsewhere (e.g. Linux CI, where the
#: system bash is already 4+). Mirrors the convention already established by
#: tests/e2e/lib/harness_lock_test.sh / commit_scope_audit_test.sh for this
#: same "macOS /bin/bash is 3.2" hazard — this module's stub scripts and the
#: sourced library only use POSIX-shape constructs, but the invoking bash
#: itself is pinned for consistency with the rest of the repo's e2e tooling.
_HOMEBREW_BASH = "/opt/homebrew/bin/bash"
BASH = _HOMEBREW_BASH if Path(_HOMEBREW_BASH).is_file() else (shutil.which("bash") or "bash")


def _write_stub_nx(bin_dir: Path, script_body: str) -> Path:
    """Install a fake `nx` executable first on $PATH standing in for the
    real CLI's subprocess boundary. `census_concurrent_store_puts` only
    ever invokes `nx store put ...` — this stub is the seam."""
    stub = bin_dir / "nx"
    stub.write_text(script_body)
    mode = stub.stat().st_mode
    stub.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return stub


def _run_census(tmp_path: Path, stub_script: str, n: int = 3) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_stub_nx(bin_dir, stub_script)
    doc_dir = tmp_path / "docs"
    doc_dir.mkdir()
    log_prefix = tmp_path / "load-put"

    cmd = (
        f'set -uo pipefail; source "{LIB}"; '
        f'census_concurrent_store_puts {n} "{log_prefix}" "{doc_dir}"; '
        'printf "FAILS=%s\\nIDX=%s\\nRETRIES=%s\\nRETRY_IDX=%s\\n" '
        '"$STORE_FAILS" "$STORE_FAIL_IDX" "$STORE_RETRIES" "$STORE_RETRY_IDX"'
    )
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    result = subprocess.run(
        [BASH, "-c", cmd],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"census subprocess itself failed (rc={result.returncode}); "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    parsed: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            parsed[key] = value
    for required in ("FAILS", "IDX", "RETRIES", "RETRY_IDX"):
        assert required in parsed, f"census output missing {required!r} line: {result.stdout!r}"
    return parsed


# ── Wiring / non-vacuity ─────────────────────────────────────────────────


def test_lib_exists_and_defines_the_function() -> None:
    assert LIB.is_file(), f"census library moved or deleted: {LIB}"
    text = LIB.read_text()
    assert "census_concurrent_store_puts()" in text, (
        "census_concurrent_store_puts() definition missing from the library "
        "-- extraction regressed"
    )


def test_rehearse_shakeout_sources_and_calls_the_library() -> None:
    """The real gate must stay wired to this library, not a copy-pasted
    inline loop that could silently drift out of sync with what this
    module's tests actually exercise."""
    assert SHAKEOUT.is_file(), f"rehearse_shakeout.sh moved: {SHAKEOUT}"
    text = SHAKEOUT.read_text()
    assert "lib/store_put_census.sh" in text, (
        "rehearse_shakeout.sh no longer sources the census library"
    )
    assert "census_concurrent_store_puts" in text, (
        "rehearse_shakeout.sh no longer calls census_concurrent_store_puts"
    )


# ── RED: client-rc census against a deliberately broken input ──────────────


def test_census_detects_a_failing_store_put(tmp_path: Path) -> None:
    """Deliberately broken input: the 2nd of 3 calls exits nonzero (as a
    real non-retryable 5xx would — see module docstring's "ALSO VERIFIED").
    The census MUST report exactly 1 failure at index 2. This is the RED
    half of the bead's mandatory falsifiability demonstration for the
    client-rc assertion."""
    stub = (
        "#!/usr/bin/env bash\n"
        'if [[ "$7" == "load-2" ]]; then echo "simulated non-retryable 500" >&2; exit 1; fi\n'
        'echo "Stored: ok"\n'
    )
    out = _run_census(tmp_path, stub)
    assert out["FAILS"] == "1", f"expected exactly 1 failure, got: {out}"
    assert out["IDX"].strip() == "2", f"expected failure at index 2, got: {out}"


def test_census_reports_clean_on_all_success(tmp_path: Path) -> None:
    """Repaired input: every call succeeds cleanly, no retry lines. GREEN
    counterpart to the RED test above -- proves the census is not
    trivially always-red."""
    stub = "#!/usr/bin/env bash\necho \"Stored: ok\"\n"
    out = _run_census(tmp_path, stub)
    assert out["FAILS"] == "0", out
    assert out["IDX"].strip() == "", out


# ── RED/GREEN: absorbed-gateway-retry census (Finding 2's closed gap) ──────


def test_census_detects_absorbed_gateway_retries(tmp_path: Path) -> None:
    """Deliberately broken input for Finding 2's gap: a store put that
    EXITS 0 (the bounded retry succeeded) but logged a vector_gateway_retry
    line. The ORIGINAL vacuous assertion (bare client rc) would have missed
    this entirely -- this is the whole point of Finding 2's fix, so it gets
    its own RED/GREEN pair distinct from the plain-failure case above."""
    stub = (
        "#!/usr/bin/env bash\n"
        'if [[ "$7" == "load-2" ]]; then\n'
        "  echo 'vector_gateway_retry path=/v1/vectors/upsert-chunks code=503 attempt=1 sleep_s=2.0' >&2\n"
        "fi\n"
        'echo "Stored: ok"\n'
    )
    out = _run_census(tmp_path, stub)
    assert out["FAILS"] == "0", f"the call still exits 0 (retry succeeded): {out}"
    assert out["RETRIES"] == "1", f"expected 1 absorbed retry detected, got: {out}"
    assert out["RETRY_IDX"].strip() == "2", f"expected retry at index 2, got: {out}"


def test_census_reports_clean_when_no_retries_logged(tmp_path: Path) -> None:
    """Repaired input: no call ever logs vector_gateway_retry. GREEN
    counterpart -- proves the retry census is not trivially always-red
    either (the log-scan does not, say, match on the literal 'Stored: ok')."""
    stub = "#!/usr/bin/env bash\necho \"Stored: ok\"\n"
    out = _run_census(tmp_path, stub)
    assert out["RETRIES"] == "0", out
    assert out["RETRY_IDX"].strip() == "", out


def test_census_counts_multiple_independent_failures(tmp_path: Path) -> None:
    """Both failure classes can coexist and are counted independently: one
    call hard-fails, a different call absorbs a retry but still succeeds."""
    stub = (
        "#!/usr/bin/env bash\n"
        'case "$7" in\n'
        "  load-1) echo 'vector_gateway_retry path=/v1/vectors code=502 attempt=1 sleep_s=2.0' >&2; echo Stored: ok ;;\n"
        '  load-3) echo "simulated non-retryable 500" >&2; exit 1 ;;\n'
        "  *) echo Stored: ok ;;\n"
        "esac\n"
    )
    out = _run_census(tmp_path, stub)
    assert out["FAILS"] == "1", out
    assert out["IDX"].strip() == "3", out
    assert out["RETRIES"] == "1", out
    assert out["RETRY_IDX"].strip() == "1", out
