# SPDX-License-Identifier: AGPL-3.0-or-later
"""The generation install waits out PyPI propagation lag (nexus-qfeez).

7.64.1 and 7.65.0 both went red at fresh-install-mvv.sh --published leg 9
("no version of conexus==X" from install_generation.sh) while legs 1-8 had
installed the same version, and a rerun minutes later passed. Leg 1's
propagation wait proves that ONE uv resolution saw the release; leg 9 is a
different uv invocation, and PyPI's simple index is served by a CDN whose
edges do not all catch up at once, so it can land on an edge that has not.

The wait therefore has to wrap the step under test (the real installer), not
a probe standing in for it, and it must never turn a failure into a pass:
success still requires install_generation.sh itself to exit 0.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tests" / "e2e" / "lib"))

from generation_install_probe import (  # noqa: E402
    PropagationSettings,
    build_generation,
    install_with_propagation_wait,
    is_propagation_miss,
    is_registry_pin,
)

# Captured from uv 0.8.0 resolving a version PyPI does not have.
_UV_MISS = (
    "  × No solution found when resolving dependencies:\n"
    "  ╰─▶ Because there is no version of conexus==7.66.0 and you require\n"
    "      conexus==7.66.0, we can conclude that your requirements are\n"
    "      unsatisfiable.\n"
)
_UV_CONFLICT = (
    "  × No solution found when resolving dependencies:\n"
    "  ╰─▶ Because mineru>=3.4 depends on pdftext>=0.7 and conexus==7.66.0\n"
    "      depends on pdftext<0.7, we can conclude that your requirements\n"
    "      are unsatisfiable.\n"
)

_SETTINGS = PropagationSettings(ceiling_s=100.0, initial_backoff_s=10.0, max_backoff_s=30.0)


class _FakeTime:
    """A clock and a sleep sharing one timeline, so no test really sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.now += s


def _proc(rc: int, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["x"], returncode=rc, stdout="", stderr=stderr)


class TestPropagationMissClassifier:
    def test_real_uv_message_for_a_missing_pinned_version(self) -> None:
        assert is_propagation_miss(_UV_MISS, "conexus==7.66.0") is True

    def test_line_wrapped_between_the_words(self) -> None:
        wrapped = "Because there is no\n      version of\n      conexus==7.66.0 and you"
        assert is_propagation_miss(wrapped, "conexus==7.66.0") is True

    def test_package_absent_from_the_registry_view(self) -> None:
        text = "Because conexus was not found in the package registry and you require conexus==7.66.0"
        assert is_propagation_miss(text, "conexus==7.66.0") is True

    def test_dependency_conflict_naming_conexus_is_not_a_miss(self) -> None:
        # Leg 1's looser shell grep would wait its whole ceiling on this.
        assert is_propagation_miss(_UV_CONFLICT, "conexus==7.66.0") is False

    def test_a_miss_for_a_different_version_is_not_this_miss(self) -> None:
        assert is_propagation_miss(_UV_MISS, "conexus==7.65.0") is False

    def test_extras_spec(self) -> None:
        text = "Because there is no version of conexus[mineru]==7.66.0 and you require"
        assert is_propagation_miss(text, "conexus[mineru]==7.66.0") is True

    def test_only_an_exact_registry_pin_can_be_a_propagation_lag(self) -> None:
        assert is_registry_pin("conexus==7.66.0") is True
        assert is_registry_pin("conexus[a,b]==7.66.0") is True
        assert is_registry_pin("conexus") is False  # unpinned: nothing to wait for
        assert is_registry_pin("/tmp/dist/conexus-7.66.0-py3-none-any.whl") is False


class TestInstallWithPropagationWait:
    def test_first_attempt_success_never_sleeps(self) -> None:
        t = _FakeTime()
        calls: list[dict[str, str]] = []

        def run(extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
            calls.append(extra_env)
            return _proc(0)

        r = install_with_propagation_wait(
            run, "conexus==7.66.0", _SETTINGS, clock=t.clock, sleep=t.sleep
        )
        assert r.proc.returncode == 0
        assert (r.attempts, r.waited_s, r.exhausted) == (1, 0.0, False)
        assert t.slept == []
        assert calls == [{}]  # the happy path is the unmodified install

    def test_miss_then_success_waits_with_backoff_and_bypasses_the_uv_cache(self) -> None:
        t = _FakeTime()
        results = [_proc(1, _UV_MISS), _proc(1, _UV_MISS), _proc(0)]
        envs: list[dict[str, str]] = []

        def run(extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
            envs.append(extra_env)
            return results.pop(0)

        r = install_with_propagation_wait(
            run, "conexus==7.66.0", _SETTINGS, clock=t.clock, sleep=t.sleep
        )
        assert r.proc.returncode == 0
        assert r.attempts == 3
        assert t.slept == [10.0, 20.0]
        assert r.waited_s == 30.0
        # A stale index page cached by attempt 1 must not be served to
        # attempt 2 (the reason leg 1's calls carry --no-cache).
        assert envs[0] == {}
        assert envs[1] == {"UV_NO_CACHE": "1"}
        assert envs[2] == {"UV_NO_CACHE": "1"}

    def test_never_appearing_is_bounded_and_reported_exhausted(self) -> None:
        t = _FakeTime()
        n = 0

        def run(extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
            nonlocal n
            n += 1
            return _proc(1, _UV_MISS)

        r = install_with_propagation_wait(
            run, "conexus==7.66.0", _SETTINGS, clock=t.clock, sleep=t.sleep
        )
        assert r.exhausted is True
        assert r.proc.returncode == 1  # never converted into a pass
        assert r.waited_s == _SETTINGS.ceiling_s
        assert max(t.slept) <= _SETTINGS.max_backoff_s
        assert n == r.attempts and n < 20

    def test_any_other_failure_fails_at_once_without_waiting(self) -> None:
        t = _FakeTime()
        n = 0

        def run(extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
            nonlocal n
            n += 1
            return _proc(1, _UV_CONFLICT)

        r = install_with_propagation_wait(
            run, "conexus==7.66.0", _SETTINGS, clock=t.clock, sleep=t.sleep
        )
        assert (n, r.attempts, r.exhausted, t.slept) == (1, 1, False, [])
        assert r.proc.returncode == 1

    def test_a_local_wheel_source_never_waits(self) -> None:
        # The local-wheel layer has no index to lag behind; a miss-looking
        # message there is a real defect and must fail on the first attempt.
        t = _FakeTime()
        n = 0

        def run(extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
            nonlocal n
            n += 1
            return _proc(1, _UV_MISS)

        r = install_with_propagation_wait(
            run, "/tmp/dist/conexus-7.66.0-py3-none-any.whl", _SETTINGS,
            clock=t.clock, sleep=t.sleep,
        )
        assert (n, t.slept, r.exhausted) == (1, [], False)


class TestBuildGenerationDrivesTheRealInstaller:
    """The wiring: a real subprocess per attempt, env reaching the child."""

    @staticmethod
    def _fake_install_dir(tmp_path: Path, misses: int) -> Path:
        d = tmp_path / "install"
        d.mkdir()
        counter = tmp_path / "count"
        (d / "install_generation.sh").write_text(
            "#!/usr/bin/env bash\n"
            f'n=$(cat "{counter}" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "{counter}"\n'
            f'echo "attempt=$n UV_NO_CACHE=${{UV_NO_CACHE:-unset}}" >> "{tmp_path}/seen"\n'
            f'if [ "$n" -le {misses} ]; then\n'
            f"  cat >&2 <<'MSG'\n{_UV_MISS}MSG\n"
            "  exit 1\nfi\n"
            f'echo "{tmp_path}/gen-x"\n'
        )
        return d

    def test_retries_the_installer_until_the_version_appears(self, tmp_path: Path) -> None:
        d = self._fake_install_dir(tmp_path, misses=2)
        t = _FakeTime()
        r = build_generation(
            d, "conexus==7.66.0", {"PATH": "/usr/bin:/bin"},
            PropagationSettings(ceiling_s=100.0, initial_backoff_s=1.0, max_backoff_s=2.0),
            clock=t.clock, sleep=t.sleep,
        )
        assert r.proc.returncode == 0 and r.attempts == 3
        assert r.proc.stdout.strip().endswith("gen-x")
        assert (tmp_path / "seen").read_text().splitlines() == [
            "attempt=1 UV_NO_CACHE=unset",
            "attempt=2 UV_NO_CACHE=1",
            "attempt=3 UV_NO_CACHE=1",
        ]

    def test_exhaustion_is_a_failure_carrying_the_last_installer_output(
        self, tmp_path: Path
    ) -> None:
        d = self._fake_install_dir(tmp_path, misses=10_000)
        t = _FakeTime()
        r = build_generation(
            d, "conexus==7.66.0", {"PATH": "/usr/bin:/bin"},
            PropagationSettings(ceiling_s=5.0, initial_backoff_s=1.0, max_backoff_s=2.0),
            clock=t.clock, sleep=t.sleep,
        )
        assert r.exhausted is True and r.proc.returncode != 0
        assert "no version of conexus==7.66.0" in r.proc.stderr


def test_settings_default_to_leg_ones_bounds_and_read_its_env_knobs() -> None:
    s = PropagationSettings.from_env({})
    assert (s.ceiling_s, s.initial_backoff_s, s.max_backoff_s) == (1800.0, 15.0, 60.0)
    s = PropagationSettings.from_env({
        "FRESH_MVV_PROPAGATION_CEILING_SECONDS": "7",
        "FRESH_MVV_PROPAGATION_INITIAL_BACKOFF_SECONDS": "0.5",
        "FRESH_MVV_PROPAGATION_MAX_BACKOFF_SECONDS": "2",
    })
    assert (s.ceiling_s, s.initial_backoff_s, s.max_backoff_s) == (7.0, 0.5, 2.0)


def test_the_probe_routes_the_installer_through_the_wait() -> None:
    """Wiring pin: main() must build the generation via build_generation, not a
    bare subprocess.run of install_generation.sh (which is what shipped the bug).

    Asserted on the CODE, not on substrings a comment could satisfy: the AST of
    main() must contain a call to build_generation assigned to `outcome`, and no
    subprocess.run call at all that names install_generation.sh.
    """
    import ast

    src = (REPO_ROOT / "tests" / "e2e" / "lib" / "generation_install_probe.py").read_text()
    main = next(
        n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "main"
    )
    assigned = [
        n for n in ast.walk(main)
        if isinstance(n, ast.Assign)
        and isinstance(n.value, ast.Call)
        and isinstance(n.value.func, ast.Name)
        and n.value.func.id == "build_generation"
        and any(isinstance(t, ast.Name) and t.id == "outcome" for t in n.targets)
    ]
    assert len(assigned) == 1, "main() must do `outcome = build_generation(...)`"
    for n in ast.walk(main):
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "run"
            and "install_generation.sh" in ast.unparse(n)
        ):
            raise AssertionError(
                "main() runs install_generation.sh directly, bypassing the propagation wait"
            )


def test_empty_env_knobs_fall_back_to_defaults_like_leg_one() -> None:
    # leg 1: ${VAR:-default} treats exported-but-empty as unset; float("") would crash.
    s = PropagationSettings.from_env({
        "FRESH_MVV_PROPAGATION_CEILING_SECONDS": "",
        "FRESH_MVV_PROPAGATION_INITIAL_BACKOFF_SECONDS": "",
        "FRESH_MVV_PROPAGATION_MAX_BACKOFF_SECONDS": "",
    })
    assert (s.ceiling_s, s.initial_backoff_s, s.max_backoff_s) == (1800.0, 15.0, 60.0)


class TestNotInRegistryClauseIsExact:
    @pytest.mark.parametrize("text", [
        "Because conexus-foo was not found in the package registry and you require",
        "Because xconexus was not found in the package registry and you require",
        "Because pdftext was not found in the package registry and conexus==7.66.0 depends on pdftext",
        "Because conexus.extra was not found in the package registry and you require",
    ])
    def test_other_packages_are_not_our_release_being_late(self, text: str) -> None:
        assert is_propagation_miss(text, "conexus==7.66.0") is False

    def test_wrapped_exact_name_still_matches(self) -> None:
        text = "Because conexus\n      was not found in the package registry and you"
        assert is_propagation_miss(text, "conexus==7.66.0") is True


class TestFailedInstallerLeavesNoGenerationDirectory:
    """The retries rely on install_generation.sh's EXIT trap removing the
    half-built gen-* tree, so N misses must not leave N directories behind."""

    def test_real_installer_script_cleans_up_each_missed_attempt(self, tmp_path: Path) -> None:
        stub_bin = tmp_path / "bin"
        stub_bin.mkdir()
        uv_log = tmp_path / "uv.log"
        uv = stub_bin / "uv"
        uv.write_text(
            "#!/usr/bin/env bash\n"
            f'echo "$1" >> "{uv_log}"\n'
            'if [ "$1" = venv ]; then\n'
            '  for last; do :; done\n'
            '  mkdir -p "$last/bin"; printf "home = /x\\nversion = 3.12.0\\n" > "$last/pyvenv.cfg"\n'
            "  exit 0\nfi\n"
            f"cat >&2 <<'MSG'\n{_UV_MISS}MSG\n"
            "exit 1\n"
        )
        uv.chmod(0o755)
        home = tmp_path / "home"
        tools = home / ".local" / "share" / "nexus" / "tools"
        tools.mkdir(parents=True)
        env = {"PATH": f"{stub_bin}:/usr/bin:/bin", "HOME": str(home), "TERM": "dumb"}
        t = _FakeTime()
        r = build_generation(
            REPO_ROOT / "src" / "nexus" / "_install", "conexus==7.66.0", env,
            PropagationSettings(ceiling_s=3.0, initial_backoff_s=1.0, max_backoff_s=1.0),
            clock=t.clock, sleep=t.sleep,
        )
        assert r.exhausted is True and r.attempts >= 3, r.proc.stderr
        assert uv_log.read_text().splitlines().count("pip") == r.attempts
        # No gen-* directory at all -- not merely no receipt inside one.
        assert sorted(p.name for p in tools.iterdir()) == []
