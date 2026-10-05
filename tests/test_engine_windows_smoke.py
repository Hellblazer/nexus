# SPDX-License-Identifier: AGPL-3.0-or-later
"""The windows-x64 engine smoke (RDR-224 P1.2, nexus-f9bgu.9).

``scripts/engine_windows_smoke.py`` boots the packaged engine exe against the
Windows PostgreSQL bundle, the way service/native-smoke.sh does against a Docker
pgvector on the other three legs. The process launcher, the HTTP client, the
command runner and the platform are injected, so both the Windows and the
non-Windows branch run here on every OS and nothing skip-passes on a laptop. The
real run (a real exe, the real bundle, a real bge embedding) happened on
qwentescence and is recorded on the bead.
"""

from __future__ import annotations

import hashlib
import io
import json
import lzma
import re
import sys
import tarfile
from pathlib import Path

import pytest

import build_pg_bundle_windows as bw
import engine_windows_smoke as es
import pg_bundle_windows_smoke as sm
import windows_engine_release as wer

REPO = Path(__file__).resolve().parent.parent
CHANGELOG = REPO / "service" / "src" / "main" / "resources" / "db" / "changelog"


# --------------------------------------------------------------------------- #
# The expected changeset count is read from the changelog
# --------------------------------------------------------------------------- #


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_changeset_count_follows_includes_and_ignores_comments(tmp_path: Path) -> None:
    root = tmp_path / "db" / "changelog"
    _write(root / "db.changelog-master.xml", """<databaseChangeLog>
      <!-- <include file="db/changelog/commented.xml"/> -->
      <include file="db/changelog/a.xml"/>
      <include file="db/changelog/b.xml"/>
      <changeSet id="own" author="x"><sql>select 1</sql></changeSet>
    </databaseChangeLog>""")
    _write(root / "a.xml", """<databaseChangeLog>
      <changeSet id="1" author="x"/><changeSet id="2" author="x" runAlways="true"/>
      <!-- <changeSet id="old" author="x"/> -->
    </databaseChangeLog>""")
    _write(root / "b.xml", "<databaseChangeLog><changeSet id=\"3\" author=\"x\"/></databaseChangeLog>")
    _write(root / "commented.xml", "<databaseChangeLog><changeSet id=\"9\" author=\"x\"/></databaseChangeLog>")
    assert es.changeset_count(root) == 4


def test_changeset_count_on_the_real_changelog_is_the_sum_over_its_includes() -> None:
    n = es.changeset_count(CHANGELOG)
    assert n >= 500  # 508 on 2026-10-05; the count is read, never hardcoded, so only a floor is asserted
    strip = lambda t: re.sub(r"<!--.*?-->", "", t, flags=re.S)  # noqa: E731
    master = strip((CHANGELOG / "db.changelog-master.xml").read_text())
    files = re.findall(r'<include\s+file="db/changelog/([^"]+)"', master)
    assert len(set(files)) == len(files), "the master includes a file twice: Liquibase would run it once"
    independent = len(re.findall(r"<changeSet\b", master)) + sum(
        len(re.findall(r"<changeSet\b", strip((CHANGELOG / f).read_text()))) for f in files
    )
    assert n == independent


def test_changeset_count_refuses_a_missing_master_or_an_empty_changelog(tmp_path: Path) -> None:
    with pytest.raises(es.SmokeError, match="master"):
        es.changeset_count(tmp_path)
    _write(tmp_path / "db.changelog-master.xml", "<databaseChangeLog/>")
    with pytest.raises(es.SmokeError, match="no changeSet"):
        es.changeset_count(tmp_path)


def test_changeset_count_refuses_an_include_that_does_not_exist(tmp_path: Path) -> None:
    _write(tmp_path / "db.changelog-master.xml", '<databaseChangeLog><include file="db/changelog/gone.xml"/></databaseChangeLog>')
    with pytest.raises(es.SmokeError, match="gone.xml"):
        es.changeset_count(tmp_path)


# --------------------------------------------------------------------------- #
# The bge model
# --------------------------------------------------------------------------- #


def test_model_pins_equal_the_prime_action_s() -> None:
    action = (REPO / ".github" / "actions" / "prime-bge-onnx" / "action.yml").read_text()
    assert re.search(rf"MODEL_SHA256: {es.MODEL_PINS['model.onnx']}\b", action)
    assert re.search(rf"TOKENIZER_SHA256: {es.MODEL_PINS['tokenizer.json']}\b", action)
    assert re.search(rf"default: {es.MODEL_ASSET_TAG}\b", action)


def _fetcher(blobs: dict[str, bytes], calls: list[str]):
    def fetch(url: str, dest: Path) -> None:
        calls.append(url)
        dest.write_bytes(blobs[url.rsplit("/", 1)[1]])
    return fetch


def _pins(blobs: dict[str, bytes]) -> dict[str, str]:
    return {n: hashlib.sha256(b).hexdigest() for n, b in blobs.items()}


BLOBS = {"model.onnx": b"onnx-bytes", "tokenizer.json": b"{}"}


def test_ensure_model_downloads_what_is_absent_and_verifies_it(tmp_path: Path) -> None:
    calls: list[str] = []
    model = es.ensure_model(tmp_path, fetch=_fetcher(BLOBS, calls), repo="o/r", pins=_pins(BLOBS), emit=lambda m: None)
    assert model == tmp_path / "bge-base-en-v1.5" / "onnx" / "model.onnx"
    assert model.read_bytes() == b"onnx-bytes"
    assert sorted(c.rsplit("/", 1)[1] for c in calls) == ["model.onnx", "tokenizer.json"]
    assert all(c.startswith(f"https://github.com/o/r/releases/download/{es.MODEL_ASSET_TAG}/") for c in calls)


def test_ensure_model_leaves_a_verified_copy_alone(tmp_path: Path) -> None:
    es.ensure_model(tmp_path, fetch=_fetcher(BLOBS, []), repo="o/r", pins=_pins(BLOBS), emit=lambda m: None)
    calls: list[str] = []
    es.ensure_model(tmp_path, fetch=_fetcher(BLOBS, calls), repo="o/r", pins=_pins(BLOBS), emit=lambda m: None)
    assert calls == []


def test_ensure_model_replaces_a_corrupt_copy(tmp_path: Path) -> None:
    d = tmp_path / "bge-base-en-v1.5" / "onnx"
    d.mkdir(parents=True)
    (d / "model.onnx").write_bytes(b"truncated")
    (d / "tokenizer.json").write_bytes(BLOBS["tokenizer.json"])
    calls: list[str] = []
    es.ensure_model(tmp_path, fetch=_fetcher(BLOBS, calls), repo="o/r", pins=_pins(BLOBS), emit=lambda m: None)
    assert [c.rsplit("/", 1)[1] for c in calls] == ["model.onnx"]
    assert (d / "model.onnx").read_bytes() == b"onnx-bytes"


def test_ensure_model_refuses_and_removes_a_download_that_fails_its_digest(tmp_path: Path) -> None:
    bad = dict(BLOBS, **{"model.onnx": b"someone-elses-bytes"})
    with pytest.raises(es.SmokeError, match="model.onnx"):
        es.ensure_model(tmp_path, fetch=_fetcher(bad, []), repo="o/r", pins=_pins(BLOBS), emit=lambda m: None)
    assert not (tmp_path / "bge-base-en-v1.5" / "onnx" / "model.onnx").exists()


def test_ensure_model_retries_a_transient_download_failure(tmp_path: Path) -> None:
    attempts = {"n": 0}

    def flaky(url: str, dest: Path) -> None:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise OSError("reset")
        dest.write_bytes(BLOBS[url.rsplit("/", 1)[1]])

    es.ensure_model(tmp_path, fetch=flaky, repo="o/r", pins=_pins(BLOBS), emit=lambda m: None, sleep=lambda s: None)
    assert (tmp_path / "bge-base-en-v1.5" / "onnx" / "model.onnx").exists()


# --------------------------------------------------------------------------- #
# Response checks
# --------------------------------------------------------------------------- #


def test_check_version_requires_the_changelog_count_exactly() -> None:
    es.check_version('{"schema_changeset_count":508,"release_version":null}', 508)
    for text in ('{"schema_changeset_count":507}', '{"schema_changeset_count":null}', '{"schema_changeset_count":"508"}',
                 "{}", "not json", ""):
        with pytest.raises(es.SmokeError):
            es.check_version(text, 508)


def test_check_embedding_requires_one_finite_nonzero_768_vector() -> None:
    vec = [0.1] * 768
    assert es.check_embedding(json.dumps({"embeddings": [vec]})) == 768
    for payload in ({"embeddings": [[0.1] * 767]}, {"embeddings": []}, {"embeddings": [vec, vec]}, {"embeddings": [[0.0] * 768]},
                    {"embeddings": [["x"] * 768]}, {"nope": 1}):
        with pytest.raises(es.SmokeError):
            es.check_embedding(json.dumps(payload))
    with pytest.raises(es.SmokeError):
        es.check_embedding('{"embeddings": [[NaN, 1.0]]}')
    with pytest.raises(es.SmokeError):
        es.check_embedding("garbage")


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakeProc:
    def __init__(self, *, exits_after_interrupt: bool = True, exit_code: int = 149, dies_at_poll: int | None = None) -> None:
        self.pid = 4242
        self.polls = 0
        self.interrupted = False
        self.killed = False
        self._exits_after_interrupt = exits_after_interrupt
        self._exit_code = exit_code
        self._dies_at_poll = dies_at_poll
        self.returncode: int | None = None

    def poll(self) -> int | None:
        self.polls += 1
        if self._dies_at_poll is not None and self.polls >= self._dies_at_poll:
            self.returncode = 1
        return self.returncode

    def interrupt(self) -> None:
        self.interrupted = True

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float) -> int | None:
        if self.interrupted and self._exits_after_interrupt and self.returncode is None:
            self.returncode = self._exit_code
        return self.returncode


class FakeLauncher:
    def __init__(self, proc: FakeProc | None = None) -> None:
        self.proc = proc or FakeProc()
        self.started: list[tuple[list[str], dict[str, str], Path]] = []
        self.engine_dir_listing: list[str] = []

    def start(self, argv, env, log):  # noqa: ANN001
        self.started.append((list(argv), dict(env), log))
        self.engine_dir_listing = sorted(p.name for p in Path(argv[0]).parent.iterdir())
        log.write_text("event=service_ready\n")
        return self.proc


class FakeHttp:
    def __init__(self, *, health_after: int = 1, version: str, embed: str) -> None:
        self.health_after = health_after
        self.version = version
        self.embed = embed
        self.calls: list[tuple[str, str]] = []
        self._h = 0

    def get(self, url: str, headers: dict[str, str]) -> tuple[int, str]:
        self.calls.append(("GET", url))
        if url.endswith("/health"):
            self._h += 1
            return (200, "ok") if self._h >= self.health_after else (0, "")
        if url.endswith("/version"):
            assert headers.get("Authorization") == "Bearer smoketoken"
            return 200, self.version
        return 404, ""

    def post_json(self, url: str, headers: dict[str, str], payload: dict) -> tuple[int, str]:
        self.calls.append(("POST", url))
        assert headers.get("Authorization") == "Bearer smoketoken"
        assert payload == {"model": "bge-base-en-v15-768", "texts": [es.EMBED_TEXT]}
        return 200, self.embed


class FakeRunner(sm.Runner):
    def __init__(self, *, modules: str = "") -> None:
        self.calls: list[list[str]] = []
        self.modules = modules

    def to_file(self, argv, *, env, out):  # noqa: ANN001
        self.calls.append(list(argv))
        return 0

    def capture(self, argv, *, env):  # noqa: ANN001
        self.calls.append(list(argv))
        if argv and "powershell" in str(argv[0]).lower():
            return 0, self.modules
        return 0, ""


def _tar(path: Path, members: dict[str, bytes]) -> Path:
    with lzma.open(path, "wb") as xz, tarfile.open(fileobj=xz, mode="w") as tf:
        for name, data in members.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    return path


def _engine_archive(tmp_path: Path) -> Path:
    return _tar(tmp_path / "engine.txz", {
        "nexus-service.exe": b"MZ", **{d: b"MZ" for d in bw.VC_RUNTIME_DLLS}, "THIRD-PARTY-NOTICES.txt": b"n",
    })


def _pg_archive(tmp_path: Path) -> Path:
    return _tar(tmp_path / "pg.txz", {"bundle/bin/initdb.exe": b"x", "bundle/.build_prefix": b"/gone"})


GOOD_VERSION = json.dumps({"schema_changeset_count": 3, "release_version": None})
GOOD_EMBED = json.dumps({"embeddings": [[0.01 * (i % 7 + 1) for i in range(768)]]})


def _changelog(tmp_path: Path, n: int = 3) -> Path:
    root = tmp_path / "changelog"
    sets = "".join(f'<changeSet id="{i}" author="t"/>' for i in range(n))
    _write(root / "db.changelog-master.xml", f"<databaseChangeLog>{sets}</databaseChangeLog>")
    return root


def _model(tmp_path: Path) -> Path:
    root = tmp_path / "models"
    d = root / "bge-base-en-v1.5" / "onnx"
    d.mkdir(parents=True)
    (d / "model.onnx").write_bytes(BLOBS["model.onnx"])
    (d / "tokenizer.json").write_bytes(BLOBS["tokenizer.json"])
    return root


def _modules(engine_dir: Path, *, msvcp: bool = True, foreign: bool = False) -> str:
    base = [f"{engine_dir}\\vcruntime140.dll", f"{engine_dir}\\vcruntime140_1.dll"]
    if msvcp:
        base += [f"{engine_dir}\\msvcp140.dll", f"{engine_dir}\\msvcp140_1.dll"]
    if foreign:
        base[0] = r"C:\Windows\System32\vcruntime140.dll"
    return "\r\n".join(base)


def _run(tmp_path: Path, *, windows: bool, http: FakeHttp | None = None, launcher: FakeLauncher | None = None,
         runner: FakeRunner | None = None, engine: Path | None = None, **kw):
    out: list[str] = []
    launcher = launcher or FakeLauncher()
    http = http or FakeHttp(version=GOOD_VERSION, embed=GOOD_EMBED)
    runner = runner or FakeRunner()
    # the module listing names the real engine dir, which only exists once the run extracts it:
    # FakeRunner is told to answer with a listing built from whatever dir the launcher saw.
    es.run(
        engine_archive=engine or _engine_archive(tmp_path), pg_archive=_pg_archive(tmp_path),
        model_root=_model(tmp_path), changelog_dir=_changelog(tmp_path), workdir=tmp_path / "work",
        platform=sm.Platform(windows=windows), runner=runner, launcher=launcher, http=http,
        base_env={"SystemRoot": r"C:\Windows", "TEMP": str(tmp_path), "PATH": r"C:\Program Files\Microsoft Visual Studio\bin"},
        emit=out.append, sleep=lambda s: None, ports=(55001, 55002), keep=False, **kw,
    )
    return out, launcher, http, runner


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #


class _ListingRunner(FakeRunner):
    """Answers the Windows module listing with DLLs under the directory the engine was started from."""

    def __init__(self, launcher: FakeLauncher, **kw) -> None:  # noqa: ANN003
        super().__init__()
        self._launcher = launcher
        self._kw = kw

    def capture(self, argv, *, env):  # noqa: ANN001
        if argv and "powershell" in str(argv[0]).lower():
            self.calls.append(list(argv))
            return 0, _modules(Path(self._launcher.started[0][0][0]).parent, **self._kw)
        return super().capture(argv, env=env)


@pytest.mark.parametrize("windows", [True, False], ids=["windows", "non-windows"])
def test_the_happy_path_runs_every_stage_in_order(tmp_path: Path, windows: bool) -> None:
    launcher = FakeLauncher()
    runner = _ListingRunner(launcher)
    out, launcher, http, runner = _run(tmp_path, windows=windows, launcher=launcher, runner=runner)
    assert out[-1] == "SMOKE PASSED"
    exe = ".exe" if windows else ""
    pg_calls = [c for c in runner.calls if "powershell" not in str(c[0]).lower()]
    assert [Path(c[0]).name for c in pg_calls] == [f"initdb{exe}", f"pg_ctl{exe}", f"createdb{exe}", f"pg_ctl{exe}"]
    assert pg_calls[1][-1] == "start" and pg_calls[3][-1] == "stop"
    kinds = [(m, u.rsplit("/", 1)[-1]) for m, u in http.calls]
    assert kinds == [("GET", "health"), ("GET", "version"), ("POST", "embed")]
    assert (len([c for c in runner.calls if "powershell" in str(c[0]).lower()]) == 1) is windows
    text = "\n".join(out)
    assert "changesets 3/3" in text and "768" in text


def test_the_engine_starts_from_the_extracted_archive_with_the_four_dlls_beside_it(tmp_path: Path) -> None:
    launcher = FakeLauncher()
    _, launcher, _, _ = _run(tmp_path, windows=True, launcher=launcher, runner=_ListingRunner(launcher))
    argv, env, log = launcher.started[0]
    assert Path(argv[0]).name == "nexus-service.exe"
    assert "-Duser.timezone=UTC" in argv
    assert launcher.engine_dir_listing == sorted(["nexus-service.exe", *bw.VC_RUNTIME_DLLS, "THIRD-PARTY-NOTICES.txt"])
    # the build host's toolchain is not on the engine's PATH: the DLLs must come from beside the exe
    assert "Visual Studio" not in env["PATH"]
    assert Path(argv[0]).parent.as_posix() in env["PATH"].replace("\\", "/")
    assert env["NX_SERVICE_TOKEN"] == "smoketoken" and env["NX_EMBED_MODE"] == "onnx"
    assert env["NX_DB_URL"] == "jdbc:postgresql://127.0.0.1:55001/nexus"
    assert env["NX_SERVICE_PORT"] == "55002"
    assert env["NX_ONNX_MODEL_DIR"] == str(tmp_path / "models")
    assert env["NX_OWNERLESS_WRITE_MODE"] == "enforce"
    assert env["NX_DB_USER"] and env["NX_DB_PASS"]


def test_a_bad_engine_archive_fails_before_anything_starts(tmp_path: Path) -> None:
    bad = _tar(tmp_path / "bad.txz", {"nexus-service.exe": b"MZ"})
    launcher = FakeLauncher()
    with pytest.raises(es.SmokeError, match="msvcp140|missing"):
        _run(tmp_path, windows=True, launcher=launcher, engine=bad)
    assert launcher.started == []


def test_a_wrong_changeset_count_fails_and_still_stops_everything(tmp_path: Path) -> None:
    launcher = FakeLauncher()
    runner = _ListingRunner(launcher)
    http = FakeHttp(version=json.dumps({"schema_changeset_count": 2}), embed=GOOD_EMBED)
    with pytest.raises(es.SmokeError, match="changeset"):
        _run(tmp_path, windows=True, http=http, launcher=launcher, runner=runner)
    assert launcher.proc.interrupted or launcher.proc.killed, "the engine must not be left running"
    assert any("stop" in c for c in runner.calls), "postgres must be stopped"
    assert not any(u.endswith("/v1/vectors/embed") for _, u in http.calls), "the embed must not run past a failed check"


def test_a_bad_embedding_fails(tmp_path: Path) -> None:
    launcher = FakeLauncher()
    http = FakeHttp(version=GOOD_VERSION, embed=json.dumps({"embeddings": [[0.1] * 100]}))
    with pytest.raises(es.SmokeError, match="768"):
        _run(tmp_path, windows=True, http=http, launcher=launcher, runner=_ListingRunner(launcher))


def test_the_engine_dying_during_startup_fails_with_its_log_tail(tmp_path: Path) -> None:
    launcher = FakeLauncher(FakeProc(dies_at_poll=2))
    http = FakeHttp(health_after=99, version=GOOD_VERSION, embed=GOOD_EMBED)
    with pytest.raises(es.SmokeError, match="exited"):
        _run(tmp_path, windows=True, http=http, launcher=launcher, runner=_ListingRunner(launcher))


def test_an_engine_that_never_becomes_healthy_times_out(tmp_path: Path) -> None:
    launcher = FakeLauncher()
    http = FakeHttp(health_after=10**9, version=GOOD_VERSION, embed=GOOD_EMBED)
    with pytest.raises(es.SmokeError, match="healthy"):
        _run(tmp_path, windows=True, http=http, launcher=launcher, runner=_ListingRunner(launcher), health_timeout_s=3)
    assert launcher.proc.interrupted or launcher.proc.killed


def test_a_vc_runtime_loaded_from_outside_the_engine_directory_fails(tmp_path: Path) -> None:
    launcher = FakeLauncher()
    runner = _ListingRunner(launcher, foreign=True)
    with pytest.raises(es.SmokeError, match="vcruntime140.dll"):
        _run(tmp_path, windows=True, launcher=launcher, runner=runner)


def test_the_embed_must_have_loaded_msvcp140_from_the_engine_directory(tmp_path: Path) -> None:
    """msvcp140.dll is imported only by onnxruntime.dll; if the listing lacks it the check saw nothing
    of the very library the shipped copy exists for."""
    launcher = FakeLauncher()
    runner = _ListingRunner(launcher, msvcp=False)
    with pytest.raises(es.SmokeError, match="msvcp140.dll"):
        _run(tmp_path, windows=True, launcher=launcher, runner=runner)


def test_a_graceful_stop_reports_the_exit_code_and_a_hard_stop_is_reported_as_one(tmp_path: Path) -> None:
    launcher = FakeLauncher(FakeProc(exits_after_interrupt=True, exit_code=149))
    out, *_ = _run(tmp_path, windows=True, launcher=launcher, runner=_ListingRunner(launcher))
    assert any("CTRL_BREAK" in ln and "149" in ln for ln in out)
    launcher = FakeLauncher(FakeProc(exits_after_interrupt=False))
    (tmp_path / "again").mkdir()
    out, *_ = _run(tmp_path / "again", windows=True, launcher=launcher, runner=_ListingRunner(launcher))
    assert launcher.proc.killed
    assert any("hard" in ln.lower() for ln in out)
    assert out[-1] == "SMOKE PASSED"


# --------------------------------------------------------------------------- #
# Wiring and environment
# --------------------------------------------------------------------------- #


def test_engine_env_keeps_only_the_os_basics_and_the_nx_settings() -> None:
    env = es.engine_env(
        engine_dir=Path(r"C:\run\engine"), platform=sm.Platform(windows=True),
        base={"SystemRoot": r"C:\Windows", "TEMP": r"C:\t", "USERPROFILE": r"C:\u", "SECRET_TOKEN": "x",
              "GH_TOKEN": "y", "PATH": r"C:\Program Files\Python313", "JAVA_HOME": r"D:\jdk"},
        db_url="jdbc:postgresql://127.0.0.1:1/nexus", db_user="u", port=2, model_root=Path(r"C:\m"),
    )
    assert "SECRET_TOKEN" not in env and "GH_TOKEN" not in env and "JAVA_HOME" not in env
    assert "Python313" not in env["PATH"]
    assert env["TEMP"] == r"C:\t" and env["USERPROFILE"] == r"C:\u"


def test_the_default_launcher_is_windows_aware_and_never_used_in_tests() -> None:
    assert es.ProcessLauncher.WINDOWS_FLAGS_ATTR == "CREATE_NEW_PROCESS_GROUP"


def test_the_script_is_pure_stdlib_and_imports_only_sibling_scripts() -> None:
    src = (REPO / "scripts" / "engine_windows_smoke.py").read_text()
    tops = {m.split(".")[0] for m in re.findall(r"^(?:from|import) ([a-zA-Z_][\w.]*)", src, re.M)}
    siblings = {"pg_bundle_windows_smoke", "windows_engine_release"}
    assert siblings <= tops, "the smoke reuses the PG smoke's helpers and the packaging script's layout check"
    assert not (tops - siblings - set(sys.stdlib_module_names))


def test_the_engine_archive_checked_here_is_the_one_package_builds(tmp_path: Path) -> None:
    """End to end across the two scripts: what windows_engine_release.package writes, the smoke accepts."""
    redist_dir = tmp_path / "r"
    redist_dir.mkdir()
    for d in bw.VC_RUNTIME_DLLS:
        (redist_dir / d).write_bytes(b"MZ")
    exe = tmp_path / "nexus-service.exe"
    exe.write_bytes(b"MZ" * 50)
    archive = wer.package(exe, bw.Redist(redist_dir, "14.44.1"), tmp_path / "dist", min_exe_bytes=1)
    launcher = FakeLauncher()
    out, *_ = _run(tmp_path, windows=True, launcher=launcher, runner=_ListingRunner(launcher), engine=archive)
    assert out[-1] == "SMOKE PASSED"
