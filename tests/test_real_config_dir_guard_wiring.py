# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-pfuns round 2, item 3: WIRING non-vacuity for the real-config-dir
mutation guard.

Every other test in ``tests/test_real_config_dir_mutation_guard.py``
exercises the pure helper functions (``_diff_config_dir_snapshots``,
``_snapshot_real_config_dir``) directly -- never pytest's own
``pytest_sessionstart``/``pytest_sessionfinish`` hook machinery. Without a
test that actually drives that machinery, the wiring itself (are these two
functions really registered as hookimpls that pytest calls at session
boundaries, in the right order, with the right effect on the run's exit
code) is an UNTAKEN BRANCH -- the same class of gap code-review flagged.

This spins up a genuinely separate, isolated pytest run via the ``pytester``
sandbox-subprocess plugin (enabled for the whole ``tests/`` tree via
``pytest_plugins = ["pytester"]`` in ``tests/conftest.py``). The sandbox's
own tiny conftest.py loads the REAL ``tests/conftest.py`` via
``importlib.util.spec_from_file_location`` under a DISTINCT module name
(verified side-effect-free at import time -- it only defines
classes/functions; nothing substrate/T2-dependent runs until a FIXTURE is
requested, and this sandbox requests none of the real conftest's
fixtures) and wires its real ``pytest_sessionstart``/``pytest_sessionfinish``
into its own hookimpls. The inner throwaway test file deliberately writes
a non-allowlisted file into a FAKE "real config dir" -- pointed there via
the ``NX_REAL_CONFIG_DIR_FOR_GUARD_TEST`` env seam
(``tests/conftest.py::_real_config_dir_for_guard``), so this test can
never touch the actual ``~/.config/nexus/``.

A bare ``import conftest as _real_conftest`` was tried first and hit
infinite recursion (``INTERNALERROR``): pytest itself loads a rootdir-less
``conftest.py`` under the plain top-level name ``conftest`` in
``sys.modules`` (there is no parent package), so the SANDBOX's own
generated conftest.py is registered as ``sys.modules["conftest"]`` before
its body finishes executing. A same-named ``import conftest`` from
*inside* that very file hits the ``sys.modules`` cache -- which ALWAYS
wins over a fresh ``sys.path`` search -- and resolves to itself, not to
``tests/conftest.py``. By the time ``pytest_sessionstart`` actually runs,
``_real_conftest`` is bound to this same sandbox module, so calling
``_real_conftest.pytest_sessionstart`` calls itself, forever.
``spec_from_file_location`` under an explicit distinct name (mirrors
``tests/test_routing_hooks.py``'s own ``_load_lib()`` pattern for the
identical reason) sidesteps the ``sys.modules["conftest"]`` collision
entirely -- it never touches that cache key.
"""
from __future__ import annotations

from pathlib import Path


def test_sessionfinish_wiring_fails_the_run_on_a_real_mutation(
    pytester, tmp_path: Path, monkeypatch
) -> None:
    fake_real_home = tmp_path / "fake-real-home"
    fake_real_home.mkdir()

    real_conftest_path = str(Path(__file__).resolve().parent / "conftest.py")
    repo_root = str(Path(__file__).resolve().parent.parent)

    pytester.makeconftest(f"""
        import importlib.util
        import sys

        # tests/conftest.py does `import tests._engine_substrate` (an
        # absolute import of the `tests` PACKAGE -- tests/__init__.py
        # exists) -- resolvable only with the repo ROOT on sys.path. The
        # sandbox subprocess's own cwd is its throwaway tmp dir, not the
        # repo, so this must be inserted explicitly (unlike `nexus` itself,
        # which resolves via the editable install regardless of cwd).
        sys.path.insert(0, {repo_root!r})

        _spec = importlib.util.spec_from_file_location(
            "nx_pfuns_wiring_test_real_conftest", {real_conftest_path!r}
        )
        _real_conftest = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_real_conftest)

        def pytest_sessionstart(session):
            _real_conftest.pytest_sessionstart(session)

        def pytest_sessionfinish(session, exitstatus):
            _real_conftest.pytest_sessionfinish(session, exitstatus)
    """)

    pytester.makepyfile(test_leaky_write="""
        import os
        from pathlib import Path

        def test_writes_a_leak_into_the_fake_real_config_dir():
            fake_home = Path(os.environ["NX_REAL_CONFIG_DIR_FOR_GUARD_TEST"])
            leak_dir = fake_home / ".config" / "nexus"
            leak_dir.mkdir(parents=True, exist_ok=True)
            (leak_dir / "definitely_not_allowlisted.json").write_text("leaked")
    """)

    # `pytester` itself has no `.monkeypatch` attribute (only the private
    # `_monkeypatch`) -- the standard fixture is requested as its own
    # parameter, same as any other test; it still modifies the real
    # `os.environ`, which `runpytest_subprocess`'s spawned child inherits.
    monkeypatch.setenv("NX_REAL_CONFIG_DIR_FOR_GUARD_TEST", str(fake_real_home))
    # Real conftest.py's autouse `_isolate_config_dir`/`_pin_t2_substrate`
    # etc. never apply here (this sandbox has its own, separate conftest
    # that does not re-export them) -- only the two hook functions we
    # explicitly wired above run.
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")

    # The INNER test itself does nothing wrong -- it passes.
    result.assert_outcomes(passed=1)
    # But the guard's pytest_sessionfinish must still fail the OUTER run.
    assert result.ret != 0, (
        f"expected non-zero exit from the guard, got {result.ret}; "
        f"stdout tail: {result.stdout.lines[-15:]}"
    )
    result.stdout.fnmatch_lines(["*definitely_not_allowlisted.json*"])

    # And the seam must be inert by default: nothing here ever touched the
    # real ~/.config/nexus/, only the fake tmp one.
    assert not (Path.home() / ".config" / "nexus" / "definitely_not_allowlisted.json").exists()
