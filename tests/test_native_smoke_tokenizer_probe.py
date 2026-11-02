# SPDX-License-Identifier: AGPL-3.0-or-later
"""Positive control for native-smoke.sh's tokenizer-freed check (nexus-89lp5).

The SIGTERM leg greps the engine's log for a native tokenizer used after it was freed. That grep
had only ever been seen to pass. This feeds the same probe script a log carrying the real error
strings (DJL's IllegalStateException, the Rust panic and abort a freed handle produces) and
requires it to fail, then feeds it a clean log and an unreadable path.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_PROBE = REPO_ROOT / "service" / "smoke-probes" / "tokenizer_freed_in_log.sh"
_SMOKE = REPO_ROOT / "service" / "native-smoke.sh"

_CLEAN = """\
2026-11-01T10:00:00Z INFO  event=engine_ready port=8080
2026-11-01T10:00:09Z INFO  event=ort_run_cancelled count=4
2026-11-01T10:00:09Z WARN  event=embed_refused reason=shutdown_in_progress status=503
2026-11-01T10:00:10Z INFO  event=shutdown_complete
"""

_DJL_ISE = """\
2026-11-01T10:00:09Z ERROR event=embed_failed
java.lang.IllegalStateException: Native resource has been released already.
\tat ai.djl.util.NativeResource.getHandle(NativeResource.java:61)
\tat ai.djl.huggingface.tokenizers.HuggingFaceTokenizer.encode(HuggingFaceTokenizer.java:168)
"""

_RUST_PANIC = """\
thread '<unnamed>' panicked at library/core/src/panicking.rs:221:5:
panic in a function that cannot unwind
fatal runtime error: failed to initiate panic, error 5
"""


def _run(log: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(_PROBE), str(log)], capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("name,body,needle", [
    ("djl_ise", _CLEAN + _DJL_ISE, "Native resource has been released already"),
    ("rust_panic", _CLEAN + _RUST_PANIC, "panicked at"),
    ("rust_abort_only", _CLEAN + "fatal runtime error: failed to initiate panic, error 5\n",
     "fatal runtime error"),
])
def test_probe_fails_on_the_real_error_strings(tmp_path, name, body, needle):
    log = tmp_path / f"{name}.log"
    log.write_text(body)
    r = _run(log)
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    assert needle in r.stdout


def test_probe_passes_a_clean_log(tmp_path):
    log = tmp_path / "clean.log"
    log.write_text(_CLEAN)
    r = _run(log)
    assert (r.returncode, r.stdout) == (0, "")


def test_probe_fails_loud_on_an_unreadable_log(tmp_path):
    r = _run(tmp_path / "does-not-exist.log")
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)


def test_the_sigterm_leg_runs_this_probe_and_fails_on_any_nonzero_exit():
    text = _SMOKE.read_text()
    assert "smoke-probes/tokenizer_freed_in_log.sh" in text
    # Any non-zero probe exit (1 = freed, 2 = unreadable, 127 = missing) must set fail.
    leg = text[text.index("tokrc=0"):]
    leg = leg[:leg.index('SVCPID=""')]
    assert '[ "$tokrc" = "0" ]' in leg and "fail=1" in leg.split("else", 1)[1]
