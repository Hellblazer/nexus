# SPDX-License-Identifier: AGPL-3.0-or-later
"""F3 (nexus-f3yqd.6): a failed setup step names its cause and says to read the log before a retry.

Moved out of test_nx_setup.py, which is at its file-size ceiling.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.hooks.test_nx_setup import Box, _load_module


@pytest.fixture
def box(tmp_path: Path) -> Box:
    return Box(tmp_path)


def test_a_failed_build_names_its_cause_and_says_read_the_log_before_retrying(box: Box) -> None:
    """F3 (nexus-f3yqd.6): the failure carries the last error the build printed, and the next step reads the log
    before it retries, so a retry is not advised blind."""
    rule = box.uv_builds_nx(exit=2)
    rule["out"] = "Resolved 214 packages\nerror: Failed to download `onnxruntime==1.24.2`: connection reset\n\n"
    box.behave([rule])
    box.start("cli-only")
    failed = box.wait_terminal()
    assert failed["failed_step"] == "build"
    assert failed["message"].startswith("the build exited 2."), failed["message"]
    assert "error: Failed to download `onnxruntime==1.24.2`: connection reset" in failed["message"], failed["message"]
    assert failed["next"].startswith("Read the log for the cause"), failed["next"]
    assert failed["next"].index("Read the log") < failed["next"].index("call setup again"), failed["next"]
    assert "resumes at the build step" in failed["next"] and failed["log"] in failed["next"]


#: What a real `nx init` prints when a step fails: the cause, then the step reporter's own tail (nexus-mjmjl).
_INIT_FAILED_OUTPUT = (
    "[1/3] Provision and start the local service\n"
    "Postgres provisioning failed: initdb exited 1\n"
    "  failed after 2.0 s\n"
    "  Re-run `nx init`: it keeps what is already in place and goes on from there.\n"
)


def test_a_failed_init_names_the_cause_not_the_reporters_tail(box: Box) -> None:
    box.behave([box.uv_builds_nx(), {"prog": "nx", "match": ["init"], "exit": 1, "out": _INIT_FAILED_OUTPUT}])
    box.start("local")
    failed = box.wait_terminal()
    assert failed["failed_step"] == "init"
    assert failed["message"] == (
        "nx init exited 1. The last error it printed: Postgres provisioning failed: initdb exited 1"
    ), failed["message"]


@pytest.mark.parametrize(
    ("output", "cause"),
    [
        (_INIT_FAILED_OUTPUT, "The last error it printed: Postgres provisioning failed: initdb exited 1"),
        ("Traceback (most recent call last):\n  File \"x.py\", line 3\nValueError: bad value\n",
         "The last error it printed: ValueError: bad value"),
        ("error: Request failed after 3 retries in 12.0s\n  cause: Failed to fetch: http://127.0.0.1:9/simple/x/\n"
         "  cause: client error (Connect)\n  cause: Connection refused (os error 61)\n",
         "The last error it printed: cause: Connection refused (os error 61)"),
        ("Downloading numpy (failed retry 1/3)\nInstalled 40 packages\n", "The last line it printed: Installed 40 packages"),
        ("check failed=0\nFinished with 0 errors\nexit 3\n", "The last line it printed: exit 3"),
        ("Stop the server first\n", "The last line it printed: Stop the server first"),
        ("\x1b[31merror: boom\x1b[0m\r\n", "The last error it printed: error: boom"),
        ("error: GET https://user:s3cret@host/x failed\n", "The last error it printed: error: GET https://***@host/x failed"),
        ("error: refused, bearer abc.def.ghi\n", "The last error it printed: error: refused, bearer ***"),
        ("error: token=abc123 rejected\n", "The last error it printed: error: token=*** rejected"),
        ("error: token expired\n", "The last error it printed: error: token expired"),
        ("  failed after 1.0 s\n  Re-run `nx init`\n", None),
        ("", None),
    ],
    ids=["reporter-tail", "traceback", "uv-cause", "retry-progress", "zero-counts", "remedy-word", "ansi-crlf",
         "url-credentials", "bearer", "token-value", "token-word", "only-the-tail", "empty"],
)
def test_the_failure_cause_line(output: str, cause: str | None) -> None:
    assert _load_module()._failure_cause(output) == cause


def test_after_a_timeout_the_cause_is_where_it_stopped() -> None:
    assert _load_module()._failure_cause("error: old retry\nbuilding wheel\n", timed_out=True) == "It stopped at: building wheel"


def test_a_long_cause_is_cut() -> None:
    cause = _load_module()._failure_cause("error: " + "x" * 5000)
    assert cause is not None and len(cause) == len("The last error it printed: ") + 300 and cause.endswith("...")


def test_a_failure_with_no_output_keeps_the_bare_message(box: Box) -> None:
    box.behave([box.uv_builds_nx(), {"prog": "nx", "match": ["init"], "exit": 1, "out": ""}])
    box.start("local")
    assert box.wait_terminal()["message"] == "nx init exited 1."
