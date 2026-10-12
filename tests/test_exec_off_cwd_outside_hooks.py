# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx`` commands and the indexer never spawn a binary the current directory can supply.

RDR-224 Phase 4 critique S1, sibling sweep (nexus-f9bgu.37). On Windows
``CreateProcess`` searches the parent's current directory before ``PATH``, and an
``nx`` command runs wherever the user is, normally a cloned repository, so a bare
``git`` / ``bd`` / ``claude`` argv[0] runs a ``git.exe`` planted in that repository.
``tests/hooks/test_hook_verbs_exec_off_cwd.py`` closed the hook verbs; this is the
same class in the rest of ``src/nexus``.

Windows runs here on any host by injection, as in that file: ``nexus.util.nx_argv``
reports ``win32``, its ``os.getcwd`` / ``os.pathsep`` / ``os.environ`` are faked
inside that one module, and its ``shutil.which`` is a stand-in that answers the way
CPython on Windows does (the working directory's hit first, as a relative
``.\\git.exe``). Nothing process-wide is patched (``tests/test_module_seam.py``).

Most call sites reach the spawn through ``nexus.bounded_subprocess.run_bounded``,
which resolves its argv head itself, so those are tested as one wrapper plus one
representative site per module, with ``subprocess.Popen`` replaced on
``nexus.bounded_subprocess`` by a recorder. The sites that call ``subprocess`` or
``asyncio`` directly resolve through ``argv_off_cwd`` themselves; each module has
its own test below.

Every site is checked three ways: a copy that exists only in the working directory
spawns nothing and degrades the way "not found" did, a copy on ``PATH`` is spawned
as an absolute path, and POSIX is untouched.
"""
from __future__ import annotations

import asyncio
import ntpath
import types
from pathlib import Path

import pytest

from nexus import (
    _mineru_spawn,
    aspect_extractor,
    bounded_subprocess,
    frecency,
    indexer,
    indexer_utils,
    repo_identity,
    session,
)
from nexus._git_hooks_meta import git_common_dir
from nexus._install import generation_core
from nexus.commands import rdr, self_cmd
from nexus.daemon import storage_service_daemon
from nexus.doc.resolvers import BeadResolver, ResolutionError
from nexus.operators import dispatch
from nexus.util import nx_argv as nx_argv_mod
from tests._module_seam import setattr_in
from tests._windows_box import CWD, TOOLS, install_windows


def _files(installed: set[str], planted: set[str]) -> set[str]:
    """The ``.exe`` files that exist: *installed* in ``TOOLS``, *planted* in the cwd."""
    return {rf"{TOOLS}\{n}.exe" for n in installed} | {rf"{CWD}\{n}.exe" for n in planted}


@pytest.fixture()
def windows_box(monkeypatch: pytest.MonkeyPatch):
    """Windows semantics for ``nx_argv``; returns ``configure(installed, planted)``."""
    monkeypatch.setattr(nx_argv_mod, "_platform", lambda: "win32")

    def configure(installed: set[str], planted: set[str]) -> None:
        install_windows(monkeypatch, "nexus.util.nx_argv", _files(installed, planted))

    everything = {"git", "bd", "claude", "bash", "java", "ss", "mineru-api"}
    configure(everything, everything)
    return configure


class _FakeProc:
    def __init__(self, args, **_kw):  # noqa: ANN001, ANN003
        self.args = args
        self.returncode = 0
        # Not a real pid: on a native Windows host ``contain()`` assigns a spawned pid
        # to a kill-on-close job object, and a made-up pid could name a live process.
        # ``contain`` ignores a pid that is not positive.
        self.pid = 0

    def communicate(self, input=None, timeout=None):  # noqa: A002, ANN001
        return ("", "")

    def poll(self):  # noqa: ANN201
        return 0

    def wait(self, timeout=None):  # noqa: ANN001, ANN201
        return 0


@pytest.fixture()
def popen_seen(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Every argv ``run_bounded`` hands ``subprocess.Popen``."""
    seen: list[list[str]] = []

    def fake(args, **kw):  # noqa: ANN001, ANN003
        seen.append(list(args))
        return _FakeProc(args, **kw)

    setattr_in(monkeypatch, "nexus.bounded_subprocess", "subprocess.Popen", fake)
    return seen


GIT = rf"{TOOLS}\git.exe"
BD = rf"{TOOLS}\bd.exe"


# -- the wrapper: run_bounded -------------------------------------------------------


def test_run_bounded_spawns_the_path_hit_not_the_planted_copy(windows_box, popen_seen) -> None:
    bounded_subprocess.run_bounded(["git", "status"], timeout=1)
    assert popen_seen == [[GIT, "status"]]


def test_run_bounded_with_only_a_planted_copy_spawns_nothing(windows_box, popen_seen) -> None:
    windows_box(set(), {"git"})
    with pytest.raises(FileNotFoundError):
        bounded_subprocess.run_bounded(["git", "status"], timeout=1)
    assert popen_seen == []


def test_run_bounded_leaves_an_absolute_head_and_posix_alone(monkeypatch, popen_seen) -> None:
    # The platform is pinned through the module's own seam: the suite also runs on a
    # native Windows host, where the real platform would resolve (or refuse) bare ``git``.
    monkeypatch.setattr(nx_argv_mod, "_platform", lambda: "linux")
    bounded_subprocess.run_bounded(["git", "status"], timeout=1)  # POSIX: bare name, PATH search only
    bounded_subprocess.run_bounded(["/usr/bin/git", "log"], timeout=1)
    assert popen_seen == [["git", "status"], ["/usr/bin/git", "log"]]


def test_run_bounded_leaves_an_absolute_windows_head_alone(windows_box, popen_seen) -> None:
    windows_box(set(), {"git"})
    bounded_subprocess.run_bounded([r"C:\Program Files\Git\cmd\git.exe", "log"], timeout=1)
    assert popen_seen == [[r"C:\Program Files\Git\cmd\git.exe", "log"]]


# -- one representative site per module that reaches the spawn through run_bounded ---


def _site_frecency():
    return frecency._git_commit_timestamps(Path("."), Path("f.py"))


def _site_repo_identity():
    repo_identity._resolve_main_repo_cached.cache_clear()
    return repo_identity._resolve_main_repo_cached("/some/clone")


def _site_indexer_head():
    return indexer._current_head(Path("."))


def _site_indexer_utils_root():
    return indexer_utils.find_repo_root(Path("."))


def _site_indexer_utils_ignore():
    return indexer_utils.is_gitignored(Path("x"), Path("."))


def _site_bead_resolver():
    return BeadResolver()._fetch("nexus-1")


#: (call, tool it spawns, what the site did before when the tool was missing). A
#: missing tool made ``Popen`` raise ``FileNotFoundError`` and each site handled it
#: its own way: ``FileNotFoundError`` is what escapes a site that never caught it.
_RUN_BOUNDED_SITES = [
    pytest.param(_site_frecency, GIT, FileNotFoundError, id="frecency"),
    pytest.param(_site_repo_identity, GIT, "/some/clone", id="repo_identity"),
    pytest.param(_site_indexer_head, GIT, "", id="indexer"),
    pytest.param(_site_indexer_utils_root, GIT, None, id="indexer_utils.find_repo_root"),
    pytest.param(_site_indexer_utils_ignore, GIT, False, id="indexer_utils.is_gitignored"),
    pytest.param(_site_bead_resolver, BD, ResolutionError, id="doc.resolvers"),
]


@pytest.mark.parametrize(("site", "head", "when_missing"), _RUN_BOUNDED_SITES)
def test_a_site_behind_run_bounded_spawns_the_path_hit(windows_box, popen_seen, site, head, when_missing) -> None:
    try:
        site()
    except Exception:  # noqa: BLE001 - only the spawned argv matters here; the fake yields empty output
        pass
    assert [argv[0] for argv in popen_seen] == [head]


@pytest.mark.parametrize(("site", "head", "when_missing"), _RUN_BOUNDED_SITES)
def test_a_site_behind_run_bounded_with_only_a_planted_copy_degrades_as_not_found(
    windows_box, popen_seen, site, head, when_missing,
) -> None:
    windows_box(set(), {"git", "bd"})
    if isinstance(when_missing, type):
        with pytest.raises(when_missing):
            site()
    else:
        assert site() == when_missing
    assert popen_seen == []


def test_git_hooks_meta_with_only_a_planted_git_raises_not_found_without_spawning(windows_box, popen_seen) -> None:
    windows_box(set(), {"git"})
    with pytest.raises(FileNotFoundError):
        git_common_dir(Path("."))
    assert popen_seen == []


# -- sites that call subprocess / asyncio directly ----------------------------------


def test_rdr_preamble_resolves_git_off_the_cwd(windows_box, monkeypatch) -> None:
    seen: list[list[str]] = []

    def fake(argv, **_kw):  # noqa: ANN001, ANN003
        seen.append(list(argv))
        return f"{CWD}\n"

    setattr_in(monkeypatch, rdr, "subprocess.check_output", fake)
    rdr._preamble_resolve_repo()
    assert [a[0] for a in seen] == [GIT, GIT]


def test_rdr_preamble_with_only_a_planted_git_falls_back_to_the_cwd(windows_box, monkeypatch) -> None:
    windows_box(set(), {"git"})
    seen: list[list[str]] = []
    setattr_in(monkeypatch, rdr, "subprocess.check_output", lambda argv, **_kw: seen.append(argv))
    root, _name = rdr._preamble_resolve_repo()
    assert seen == []
    assert root == str(Path.cwd())  # the documented best-effort fallback, as when git is absent


def test_self_install_script_runs_bash_off_the_cwd(windows_box, monkeypatch, tmp_path: Path) -> None:
    seen: list[list[str]] = []

    def fake(argv, **_kw):  # noqa: ANN001, ANN003
        seen.append(list(argv))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    setattr_in(monkeypatch, self_cmd, "subprocess.run", fake)
    self_cmd._sh(tmp_path, "true")
    assert seen[0][0] == rf"{TOOLS}\bash.exe"
    windows_box(set(), {"bash"})
    with pytest.raises(FileNotFoundError):
        self_cmd._sh(tmp_path, "true")
    assert len(seen) == 1


def test_claude_for_aspect_extraction_resolves_off_the_cwd(windows_box, monkeypatch) -> None:
    seen: list[list[str]] = []

    def fake(args, **kw):  # noqa: ANN001, ANN003
        seen.append(list(args))
        return _FakeProc(args, **kw)

    setattr_in(monkeypatch, aspect_extractor, "subprocess.Popen", fake)
    aspect_extractor._run_claude_isolated("prompt", timeout=5)
    assert seen[0][0] == rf"{TOOLS}\claude.exe"
    windows_box(set(), {"claude"})
    with pytest.raises(FileNotFoundError):
        aspect_extractor._run_claude_isolated("prompt", timeout=5)
    assert len(seen) == 1


def test_claude_for_operator_dispatch_resolves_off_the_cwd(windows_box, monkeypatch, tmp_path: Path) -> None:
    seen: list[tuple[str, ...]] = []

    async def fake(*argv, **_kw):  # noqa: ANN002, ANN003
        seen.append(argv)
        return object()

    prompt = tmp_path / "prompt.txt"
    prompt.write_text("hi")
    setattr_in(monkeypatch, dispatch, "asyncio.create_subprocess_exec", fake)
    asyncio.run(dispatch._spawn_with_prompt_file(["claude", "-p"], prompt))
    assert seen == [(rf"{TOOLS}\claude.exe", "-p")]
    windows_box(set(), {"claude"})
    with pytest.raises(FileNotFoundError):
        asyncio.run(dispatch._spawn_with_prompt_file(["claude", "-p"], prompt))
    assert len(seen) == 1


# -- lookups whose result is the head of a spawn ------------------------------------


def test_mineru_api_lookup_ignores_a_planted_copy(windows_box, monkeypatch, tmp_path: Path) -> None:
    # Candidates (a) and (b) miss: an interpreter directory and a source tree with no mineru-api.
    setattr_in(monkeypatch, _mineru_spawn, "sys.executable", str(tmp_path / "bin" / "python"))
    monkeypatch.setattr(_mineru_spawn, "__file__", str(tmp_path / "x" / "_mineru_spawn.py"))
    assert _mineru_spawn._resolve_mineru_api_bin() == rf"{TOOLS}\mineru-api.exe"
    windows_box(set(), {"mineru-api"})
    assert _mineru_spawn._resolve_mineru_api_bin() is None


def test_java_for_the_jar_launch_ignores_a_planted_copy(windows_box, monkeypatch) -> None:
    monkeypatch.delenv("JAVA_HOME", raising=False)
    assert storage_service_daemon._resolve_java_executable() == rf"{TOOLS}\java.exe"
    windows_box(set(), {"java"})
    with pytest.raises(storage_service_daemon.StorageServiceStartError):
        storage_service_daemon._resolve_java_executable()


def test_ss_for_the_port_holder_ignores_a_planted_copy(windows_box, monkeypatch) -> None:
    seen: list[list[str]] = []

    def fake(argv, **_kw):  # noqa: ANN001, ANN003
        seen.append(list(argv))
        return types.SimpleNamespace(stdout='LISTEN 0 4096 *:8080 users:(("java",pid=7,fd=9))\n')

    monkeypatch.setattr(bounded_subprocess, "run_bounded", fake)
    assert storage_service_daemon._port_holder(8080) == '"java",pid=7,fd=9'
    assert seen == [[rf"{TOOLS}\ss.exe", "-ltnp", "sport = :8080"]]
    windows_box(set(), {"ss"})
    assert storage_service_daemon._port_holder(8080) is None  # nothing named, nothing spawned
    assert len(seen) == 1


# -- the ps probes in session.py: behind a win32 early return, so defensive only -----


def test_ps_for_the_parent_pid_walk_resolves_off_the_cwd(windows_box, monkeypatch) -> None:
    """``_ppid_of`` and the orphan sweep return before ``ps`` on a Windows host, so
    ``platform="linux"`` here reaches the ``ps`` branch to prove the argv it builds is
    resolved (the conversion is defence in depth, should the early return ever go)."""
    seen: list[list[str]] = []

    def fake(argv, **_kw):  # noqa: ANN001, ANN003
        seen.append(list(argv))
        return "4321\n"

    setattr_in(monkeypatch, session, "subprocess.check_output", fake)
    windows_box({"ps"}, {"ps"})
    no_proc = 4_000_000_000  # no /proc/<pid>/status on any host: the ps fallback runs
    assert session._ppid_of(no_proc, platform="linux") == 4321
    assert seen == [[rf"{TOOLS}\ps.exe", "-o", "ppid=", "-p", str(no_proc)]]
    windows_box(set(), {"ps"})
    assert session._ppid_of(no_proc, platform="linux") is None  # "ps unavailable", as before
    assert len(seen) == 1


# -- the bootstrap-safe installer core: its own stdlib-only copy of the helper -------


def _core_box(monkeypatch: pytest.MonkeyPatch, files: set[str]) -> None:
    """Windows as ``generation_core`` sees it: *files* are the paths that exist."""
    setattr_in(monkeypatch, generation_core, "os.getcwd", lambda: CWD)
    setattr_in(monkeypatch, generation_core, "os.pathsep", ";")
    setattr_in(monkeypatch, generation_core, "os.environ", {"PATH": f"{CWD};{TOOLS}"})
    setattr_in(monkeypatch, generation_core, "os.path.isfile", lambda p: ntpath.normcase(p) in {ntpath.normcase(f) for f in files})


def test_generation_core_resolves_uv_off_the_cwd(monkeypatch) -> None:
    _core_box(monkeypatch, {rf"{CWD}\uv.exe", rf"{TOOLS}\uv.exe"})
    head, *rest = generation_core._off_cwd(["uv", "venv"], platform="win32")
    assert (ntpath.normcase(head), rest) == (ntpath.normcase(rf"{TOOLS}\uv.exe"), ["venv"])


def test_generation_core_with_only_a_planted_uv_raises_not_found(monkeypatch) -> None:
    _core_box(monkeypatch, {rf"{CWD}\uv.exe"})
    with pytest.raises(FileNotFoundError):
        generation_core._off_cwd(["uv", "venv"], platform="win32")


def test_generation_core_leaves_posix_and_a_path_head_alone(monkeypatch) -> None:
    assert generation_core._off_cwd(["uv", "venv"], platform="linux") == ["uv", "venv"]
    assert generation_core._off_cwd([r"C:\uv\uv.exe", "venv"], platform="win32") == [r"C:\uv\uv.exe", "venv"]


def test_generation_core_run_reports_a_planted_only_uv_as_not_found(monkeypatch) -> None:
    _core_box(monkeypatch, {rf"{CWD}\uv.exe"})
    setattr_in(monkeypatch, generation_core, "sys.platform", "win32")
    spawned: list[list[str]] = []
    with pytest.raises(generation_core.GenerationError, match="was not found on PATH"):
        generation_core._run(["uv", "tool", "dir"], lambda argv, **_kw: spawned.append(argv), "uv tool dir")
    assert spawned == []
