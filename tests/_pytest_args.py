"""Read a pytest argument list: does it select the whole suite?

The laptop refusal in ``tests/conftest.py`` (``_refuse_heavy_laptop_run``) names the
``lab-remote`` class to use; ``lab-l`` is for a full suite only. Kept out of the conftest so
that file stays under its size ceiling (``scripts/file-size-exceptions.txt``).
"""

from __future__ import annotations

from collections.abc import Sequence

# Options whose next argument is their value, so that value is not a path or node id.
PYTEST_VALUE_OPTIONS: frozenset[str] = frozenset({
    "-n", "-p", "-c", "-o", "-W", "--numprocesses", "--maxfail", "--rootdir", "--ignore", "--ignore-glob",
    "--deselect", "--junitxml", "--durations", "--durations-min", "--tb", "--basetemp", "--dist",
    "--timeout", "--confcutdir", "--import-mode", "--color", "--capture", "--override-ini",
    "--log-level", "--maxprocesses", "--max-worker-restart", "--reruns",
})


def is_full_suite(args: Sequence[object]) -> bool:
    """True when ``args`` select the whole suite: no ``-k``/``-m``, no node id, no path but ``tests``.

    Picks the ``lab-remote`` class the laptop refusal names: ``lab-l`` is for a full suite only;
    a lint bucket, a targeted file or a single module takes ``lab-m``.
    """
    skip = False
    for arg in (str(a) for a in args):
        if skip:
            skip = False
            continue
        if arg == "--":
            continue
        if arg.startswith("--"):
            skip = "=" not in arg and arg in PYTEST_VALUE_OPTIONS
            continue
        if arg.startswith("-") and arg != "-":
            if arg[:2] in ("-k", "-m"):
                return False  # -k expr, -k=expr, -kexpr, and the -m forms
            skip = arg in PYTEST_VALUE_OPTIONS
            continue
        if "::" in arg or arg.rstrip("/") != "tests":
            return False
    return True
