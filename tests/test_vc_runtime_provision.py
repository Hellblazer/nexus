# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-lqjll, cloud mode: a Windows install that never downloads the engine or
the PG bundle still gets ``msvcp140.dll`` and ``msvcp140_1.dll`` for the client's
extension modules (onnxruntime, pymupdf, torch, fasttext).

``ensure_vc_runtime`` takes the signed PG bundle for the pinned engine tag
through the same sha256 + sigstore gates ``install_pg_bundle`` uses and copies
ONLY those two DLLs into ``<config>/vcrt``. The Windows branches run on every
host through the injected ``platform``; the download is the ``_download`` seam
the PG bundle tests use, with a REAL txz fixture holding fake DLL files.
"""

from __future__ import annotations

import hashlib
import io
import json
import lzma
import os
import tarfile
from pathlib import Path

import pytest
from click.testing import CliRunner

import nexus
from nexus import _vcrt
from nexus.daemon import binary_install as b
from tests._module_seam import setattr_in

_DLLS = _vcrt.VC_RUNTIME_DLLS
_ATOMIC_WAIT = "msvcp140_atomic_wait.dll"
_BODY = {n: f"fake {n}".encode() for n in (*_DLLS, _ATOMIC_WAIT, "vcruntime140.dll", "postgres.exe")}


def _bundle_bytes(*, omit: tuple[str, ...] = (), arcroot: str = "bundle", atomic_wait: bool = False) -> bytes:
    """A txz laid out like scripts/build_pg_bundle_windows.py's: members under
    ``bundle/``, PostgreSQL first so an early break matters."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tf:
        for name in ("postgres.exe", "vcruntime140.dll", *_DLLS, *((_ATOMIC_WAIT,) if atomic_wait else ())):
            if name in omit:
                continue
            data = _BODY[name]
            ti = tarfile.TarInfo(f"{arcroot}/bin/{name}")
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
        ti = tarfile.TarInfo(f"{arcroot}/share/readme.txt")
        ti.size = 2
        tf.addfile(ti, io.BytesIO(b"hi"))
    return lzma.compress(raw.getvalue())


class _OkChecker:
    def __init__(self) -> None:
        self.seen: list[int] = []

    def check(self, *, asset_bytes: bytes, **_kw) -> None:
        self.seen.append(len(asset_bytes))


class _RejectChecker:
    def check(self, **_kw) -> None:
        raise ValueError("signature does not verify")


class _Net:
    """The ``_download`` seam: records the URLs and serves a self-consistent asset."""

    def __init__(self, content: bytes, *, sha: str | None = None) -> None:
        self.content = content
        self.sha = sha
        self.urls: list[str] = []

    def __call__(self, url: str, dest: Path, *, timeout: float = 0) -> None:
        self.urls.append(url)
        if url.endswith(".sha256"):
            digest = self.sha or hashlib.sha256(self.content).hexdigest()
            dest.write_text(f"{digest}  {b.pg_bundle_asset_name('windows-x64')}\n")
        elif url.endswith(".sigstore.json"):
            dest.write_text("{}")
        else:
            dest.write_bytes(self.content)

    @property
    def asset_fetches(self) -> int:
        return sum(1 for u in self.urls if u.endswith(".txz"))


@pytest.fixture()
def cfg(tmp_path: Path) -> Path:
    d = tmp_path / "cfg"
    d.mkdir()
    return d


@pytest.fixture()
def system(tmp_path: Path) -> str:
    """A System32 stand-in WITHOUT the DLLs (a machine without the redistributable)."""
    d = tmp_path / "System32"
    d.mkdir()
    return str(d)


def _put_dlls(d: Path, names: tuple[str, ...] = _DLLS) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    for n in names:
        (d / n).write_bytes(b"MZ")
    return d


def _ensure(cfg: Path, system: str, net: _Net | None, **kw):  # noqa: ANN202
    return b.ensure_vc_runtime(
        cfg, platform=kw.pop("platform", "win32"), system_dir=system,
        checker=kw.pop("checker", _OkChecker()), download_dir=cfg, **kw,
    )


@pytest.fixture()
def net(monkeypatch: pytest.MonkeyPatch) -> _Net:
    n = _Net(_bundle_bytes())
    monkeypatch.setattr(b, "_download", n)
    return n


def _vcrt_files(cfg: Path) -> list[str]:
    d = cfg / "vcrt"
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


# ── when nothing is downloaded ──────────────────────────────────────────────


def test_off_windows_is_not_applicable_and_touches_nothing(cfg: Path, system: str, net: _Net) -> None:
    r = _ensure(cfg, system, net, platform="darwin")
    assert r.status == "not_applicable" and r.ok
    assert net.urls == [] and not (cfg / "vcrt").exists()


def test_a_system_runtime_means_no_download(cfg: Path, tmp_path: Path, net: _Net) -> None:
    sysdir = _put_dlls(tmp_path / "System32-with", _vcrt.PDF_STACK_DLLS)
    r = _ensure(cfg, str(sysdir), net)
    assert r.status == "present_system" and net.urls == []


@pytest.mark.parametrize("sub", [("service",), ("pg-bundle", "bundle", "bin"), ("vcrt",)])
def test_an_app_local_directory_with_the_whole_set_means_no_download(cfg: Path, system: str, net: _Net, sub) -> None:
    where = _put_dlls(cfg.joinpath(*sub), _vcrt.PDF_STACK_DLLS)
    r = _ensure(cfg, system, net)
    assert r.status == "present_app_local" and r.dir == where and net.urls == []


def test_a_directory_with_one_dll_does_not_count(cfg: Path, system: str, net: _Net) -> None:
    _put_dlls(cfg / "service", ("msvcp140.dll",))
    assert _ensure(cfg, system, net).status == "provisioned"


# ── provisioning ────────────────────────────────────────────────────────────


def test_provisions_the_two_dlls_from_the_pinned_tags_pg_bundle(cfg: Path, system: str, net: _Net) -> None:
    checker = _OkChecker()
    r = _ensure(cfg, system, net, checker=checker)
    assert r.status == "provisioned" and r.dir == cfg / "vcrt", r.detail
    pinned = b.resolve_service_tag()
    assert net.urls[0] == b.release_asset_url(pinned, "nexus-pg-windows-x64.txz")
    assert checker.seen, "the signature gate must run on the downloaded archive"
    for n in _DLLS:
        assert (cfg / "vcrt" / n).read_bytes() == _BODY[n]
    # ONLY the two DLLs (and the provenance sidecar): no PostgreSQL, no vcruntime, no temp files
    assert _vcrt_files(cfg) == sorted([*_DLLS, "vcrt.meta.json"])
    meta = json.loads((cfg / "vcrt" / "vcrt.meta.json").read_text())
    assert meta["tag"] == pinned and set(meta["dlls"]) == set(_DLLS)
    assert meta["sha256"] == hashlib.sha256(net.content).hexdigest()


def test_a_bundle_from_an_engine_without_atomic_wait_still_provisions_the_base_pair_and_says_so(
    cfg: Path, system: str, net: _Net,
) -> None:
    """The default bundle here is the older one (no msvcp140_atomic_wait.dll). It is a valid
    source for the base pair; the detail says the fifth DLL was not in it (nexus-vyy65)."""
    r = _ensure(cfg, system, net)
    assert r.status == "provisioned" and r.ok
    assert _ATOMIC_WAIT not in _vcrt_files(cfg)
    assert _ATOMIC_WAIT in r.detail and "was not in that bundle" in r.detail
    assert _vcrt.runtime_source("win32", str(cfg), system, _vcrt.PDF_STACK_DLLS) == (_vcrt.MISSING, None)


def test_a_bundle_that_carries_atomic_wait_provisions_it_too(
    cfg: Path, system: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(b, "_download", _Net(_bundle_bytes(atomic_wait=True)))
    r = _ensure(cfg, system, None)
    assert r.status == "provisioned", r.detail
    assert _vcrt_files(cfg) == sorted([*_DLLS, _ATOMIC_WAIT, "vcrt.meta.json"])
    assert (cfg / "vcrt" / _ATOMIC_WAIT).read_bytes() == _BODY[_ATOMIC_WAIT]
    assert "was not in that bundle" not in r.detail and _ATOMIC_WAIT in r.detail
    assert _ATOMIC_WAIT in json.loads((cfg / "vcrt" / "vcrt.meta.json").read_text())["dlls"]
    # the gate reads the disk: <config>/vcrt now satisfies the PDF stack's set
    assert _vcrt.runtime_source("win32", str(cfg), system, _vcrt.PDF_STACK_DLLS) == (
        _vcrt.APP_LOCAL, str(cfg / "vcrt"))


def test_idempotent_second_call_finds_them_and_downloads_nothing(cfg: Path, system: str, net: _Net) -> None:
    assert _ensure(cfg, system, net).status == "provisioned"
    second = _ensure(cfg, system, net)
    assert second.status == "present_app_local" and second.dir == cfg / "vcrt"
    assert net.asset_fetches == 1


def test_an_explicit_tag_wins_over_the_pin(cfg: Path, system: str, net: _Net) -> None:
    _ensure(cfg, system, net, tag="engine-service-v9.9.9")
    assert "engine-service-v9.9.9" in net.urls[0]


def test_provisioning_puts_the_directory_on_this_processes_dll_path(
    cfg: Path, system: str, net: _Net, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(nexus, "_add_vc_runtime_dirs", lambda **kw: calls.append(kw) or [])
    _ensure(cfg, system, net)
    assert calls and calls[0]["config_dir"] == str(cfg)


# ── atomic write ────────────────────────────────────────────────────────────


def test_a_failed_rename_leaves_no_partial_dll_and_no_temp_file(
    cfg: Path, system: str, net: _Net, monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_replace = os.replace

    def refuse(src, dst, *a, **k):  # noqa: ANN001, ANN202
        if Path(dst).name in _DLLS:
            raise PermissionError("held by a scanner")
        return real_replace(src, dst, *a, **k)

    setattr_in(monkeypatch, "nexus.daemon.binary_install", "os.replace", refuse)
    r = _ensure(cfg, system, net)
    assert r.status == "failed"
    left = _vcrt_files(cfg)
    assert not [n for n in left if n in _DLLS], "a DLL must only appear by rename, never half-written"
    assert not [n for n in left if n.startswith(".nx_")], f"temp files left behind: {left}"


# ── never raises ────────────────────────────────────────────────────────────


def test_a_failed_download_is_a_result_not_an_exception(cfg: Path, system: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(url, dest, *, timeout=0):  # noqa: ANN001, ANN202
        raise b.BinaryDownloadError("network down")

    monkeypatch.setattr(b, "_download", boom)
    r = _ensure(cfg, system, None)
    assert r.status == "failed" and not r.ok and "network down" in r.detail
    assert _vcrt_files(cfg) == []


def test_an_unexpected_error_is_still_a_result(cfg: Path, system: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(url, dest, *, timeout=0):  # noqa: ANN001, ANN202
        raise RuntimeError("something nobody planned for")

    monkeypatch.setattr(b, "_download", boom)
    assert _ensure(cfg, system, None).status == "failed"


def test_a_sha256_mismatch_places_nothing(cfg: Path, system: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(b, "_download", _Net(_bundle_bytes(), sha="0" * 64))
    r = _ensure(cfg, system, None)
    assert r.status == "failed" and "sha256 mismatch" in r.detail
    assert not [n for n in _vcrt_files(cfg) if n in _DLLS]


def test_a_rejected_signature_places_nothing(cfg: Path, system: str, net: _Net) -> None:
    r = _ensure(cfg, system, net, checker=_RejectChecker())
    assert r.status == "failed" and "signature" in r.detail
    assert not [n for n in _vcrt_files(cfg) if n in _DLLS]


def test_an_archive_missing_a_dll_places_nothing(cfg: Path, system: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(b, "_download", _Net(_bundle_bytes(omit=("msvcp140_1.dll",))))
    r = _ensure(cfg, system, None)
    assert r.status == "failed" and "msvcp140_1.dll" in r.detail
    assert not [n for n in _vcrt_files(cfg) if n in _DLLS], "a single DLL is not a usable runtime"


def test_dlls_outside_bundle_bin_are_not_taken(cfg: Path, system: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(b, "_download", _Net(_bundle_bytes(arcroot="elsewhere")))
    assert _ensure(cfg, system, None).status == "failed"
    assert not [n for n in _vcrt_files(cfg) if n in _DLLS]


def test_an_unreadable_archive_is_a_result(cfg: Path, system: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(b, "_download", _Net(b"this is not an xz tarball"))
    assert _ensure(cfg, system, None).status == "failed"


def test_no_pinned_tag_is_a_result(cfg: Path, system: str, net: _Net, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(b, "resolve_service_tag", lambda: None)
    r = _ensure(cfg, system, net)
    assert r.status == "failed" and net.urls == []


# ── failure backoff (the automatic path runs every session) ────────────────


def test_after_a_failure_the_automatic_path_backs_off_and_a_person_does_not(
    cfg: Path, system: str, net: _Net, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(b, "_download", _RaisesOnce(net))
    assert _ensure(cfg, system, None).status == "failed"
    assert (cfg / b._VCRT_FAILED_SENTINEL).exists()
    deferred = _ensure(cfg, system, None, failure_backoff_s=3600)
    assert deferred.status == "deferred" and deferred.ok
    assert net.asset_fetches == 0, "no download inside the backoff"
    assert _ensure(cfg, system, None).status == "provisioned", "no backoff requested: retry now"
    assert not (cfg / b._VCRT_FAILED_SENTINEL).exists(), "success clears the failure stamp"


def test_an_old_failure_stamp_does_not_defer(cfg: Path, system: str, net: _Net) -> None:
    sentinel = cfg / b._VCRT_FAILED_SENTINEL
    sentinel.touch()
    os.utime(sentinel, (1, 1))
    assert _ensure(cfg, system, net, failure_backoff_s=3600).status == "provisioned"


class _RaisesOnce:
    def __init__(self, inner: _Net) -> None:
        self.inner = inner
        self.failed = False

    def __call__(self, url: str, dest: Path, *, timeout: float = 0) -> None:
        if not self.failed:
            self.failed = True
            raise b.BinaryDownloadError("first attempt fails")
        self.inner(url, dest, timeout=timeout)


# ── the third search directory ──────────────────────────────────────────────


def _search(cfg: Path, system: str) -> list[str]:
    added: list[str] = []
    out = nexus._add_vc_runtime_dirs(
        platform="win32", config_dir=str(cfg), system_dir=system, add=lambda d: added.append(d) or object(),
    )
    assert out == added
    return out


def test_the_vcrt_directory_joins_the_dll_search_path(cfg: Path, system: str) -> None:
    where = _put_dlls(cfg / "vcrt")
    assert _search(cfg, system) == [str(where)]


def test_search_order_is_engine_then_bundle_then_vcrt(cfg: Path, system: str) -> None:
    dirs = [_put_dlls(cfg / "service"), _put_dlls(cfg / "pg-bundle" / "bundle" / "bin"), _put_dlls(cfg / "vcrt")]
    assert _search(cfg, system) == [str(d) for d in dirs]


def test_a_vcrt_directory_with_one_dll_is_skipped(cfg: Path, system: str) -> None:
    _put_dlls(cfg / "vcrt", ("msvcp140_1.dll",))
    assert _search(cfg, system) == []


def test_a_system_runtime_still_wins_over_vcrt(cfg: Path, tmp_path: Path) -> None:
    _put_dlls(cfg / "vcrt")
    assert _search(cfg, str(_put_dlls(tmp_path / "System32-with"))) == []


# ── the doctor row ──────────────────────────────────────────────────────────


def _row(cfg: Path, system: str, platform: str = "win32"):  # noqa: ANN202
    from nexus.health import _check_vc_runtime

    return _check_vc_runtime(cfg, platform=platform, system_dir=system)


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_doctor_row_is_not_applicable_off_windows(cfg: Path, system: str, platform: str) -> None:
    assert _row(cfg, system, platform) == []


def test_doctor_row_ok_when_the_system_has_the_runtime(cfg: Path, tmp_path: Path) -> None:
    (row,) = _row(cfg, str(_put_dlls(tmp_path / "System32-with")))
    assert row.ok and "found in" in row.detail


@pytest.mark.parametrize("sub", [("service",), ("pg-bundle", "bundle", "bin"), ("vcrt",)])
def test_doctor_row_ok_when_an_app_local_directory_has_both(cfg: Path, system: str, sub) -> None:
    where = _put_dlls(cfg.joinpath(*sub))
    (row,) = _row(cfg, system)
    assert row.ok and str(where) in row.detail


def test_doctor_row_fails_with_both_remedies_when_neither_has_it(cfg: Path, system: str) -> None:
    (row,) = _row(cfg, system)
    assert not row.ok and not row.warn, "a box whose PDF and embedding imports fail is a hard row"
    assert "client's extension modules" in row.label
    fixes = " ".join(row.fix_suggestions)
    assert "nx init" in fixes and "https://aka.ms/vs/17/release/vc_redist.x64.exe" in fixes


def test_doctor_row_fails_when_only_one_dll_is_app_local(cfg: Path, system: str) -> None:
    _put_dlls(cfg / "vcrt", ("msvcp140.dll",))
    (row,) = _row(cfg, system)
    assert not row.ok


def test_doctor_row_is_registered_in_the_sweep() -> None:
    import inspect

    import nexus.health as h

    assert "_check_vc_runtime()" in inspect.getsource(h.run_health_checks)


def test_doctor_row_is_silent_on_this_host_by_default() -> None:
    # The fresh-install MVV and every POSIX box see no row at all (nexus-7zhag doctrine).
    if os.name != "nt":
        from nexus.health import _check_vc_runtime

        assert _check_vc_runtime() == []


# ── the triggers ────────────────────────────────────────────────────────────


@pytest.fixture()
def _recorded(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    calls: list[dict] = []

    def fake(config_dir, **kw):  # noqa: ANN001, ANN202
        calls.append({"config_dir": config_dir, **kw})
        return b.VcRuntimeResult("provisioned", "placed")

    monkeypatch.setattr(b, "ensure_vc_runtime", fake)
    return calls


@pytest.fixture()
def cfg_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "initcfg"
    d.mkdir()
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(d))
    for var in ("NX_LOCAL", "NX_SERVICE_URL", "NX_SERVICE_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("nexus.commands.init._converge_ladder_best_effort", lambda: None)
    monkeypatch.setattr("nexus.commands.init._seed_builtin_plans_best_effort", lambda: None)
    return d


def _fake_caps():  # noqa: ANN202
    from nexus.db.managed_endpoint import ManagedCapabilities

    return ManagedCapabilities(
        base_url="https://m.example", app_version="1.2.3", release_version="0.1.9",
        embedding_mode="voyage", embedding_models=["voyage-context-3"],
        schema_latest_id=None, schema_changeset_count=None,
    )


def test_init_cloud_arm_provisions_the_runtime(
    cfg_dir: Path, _recorded: list[dict], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus.commands.init import init_cmd

    monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)
    result = CliRunner().invoke(init_cmd, [])
    assert result.exit_code == 0, result.output
    assert "CLOUD mode" in result.output
    assert [c["config_dir"] for c in _recorded] == [cfg_dir]
    assert "VC++ runtime" in result.output


def test_init_managed_arm_provisions_the_runtime(
    cfg_dir: Path, _recorded: list[dict], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus.commands.init import init_cmd

    monkeypatch.setenv("NX_LOCAL", "0")
    monkeypatch.setenv("NX_SERVICE_URL", "https://m.example")
    monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
    monkeypatch.setattr("nexus.db.managed_endpoint.probe_managed_service", lambda **kw: _fake_caps())
    result = CliRunner().invoke(init_cmd, [])
    assert result.exit_code == 0, result.output
    assert len(_recorded) == 1


def test_init_managed_probe_failure_does_not_skip_the_runtime(
    cfg_dir: Path, _recorded: list[dict], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus.commands.init import init_cmd

    def down(**kw):  # noqa: ANN003, ANN202
        raise RuntimeError("probe failed")

    monkeypatch.setenv("NX_LOCAL", "0")
    monkeypatch.setenv("NX_SERVICE_URL", "https://m.example")
    monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
    monkeypatch.setattr("nexus.db.managed_endpoint.probe_managed_service", down)
    CliRunner().invoke(init_cmd, [])
    assert len(_recorded) == 1, "the probe exits non-zero; the DLLs must already be placed"


def test_init_reports_a_failure_with_the_remedy_and_still_exits_zero(
    cfg_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus.commands.init import init_cmd

    monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)
    monkeypatch.setattr(
        b, "ensure_vc_runtime", lambda config_dir, **kw: b.VcRuntimeResult("failed", "network down"),
    )
    result = CliRunner().invoke(init_cmd, [])
    assert result.exit_code == 0, result.output
    assert "network down" in result.output and "vc_redist.x64.exe" in result.output


def test_init_is_silent_when_nothing_was_needed(cfg_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.commands.init import init_cmd

    monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)
    monkeypatch.setattr(b, "ensure_vc_runtime", lambda config_dir, **kw: b.VcRuntimeResult("present_system"))
    result = CliRunner().invoke(init_cmd, [])
    assert "VC++" not in result.output


def _converge(monkeypatch: pytest.MonkeyPatch, *, auto: bool) -> None:
    import nexus.commands.self_cmd as self_cmd
    import nexus.upgrade_ladder.preconditions as pre
    from nexus.commands import upgrade as up

    monkeypatch.setattr(self_cmd, "repair_uv_takeover", lambda: [])
    monkeypatch.setattr(pre, "converge_preconditions", lambda **kw: [])
    up._converge_preconditions(auto_mode=auto)


def test_upgrade_auto_runs_the_runtime_step_with_a_failure_backoff(
    _recorded: list[dict], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    _converge(monkeypatch, auto=True)
    assert len(_recorded) == 1
    assert _recorded[0]["config_dir"] == tmp_path
    assert _recorded[0]["failure_backoff_s"] > 0, "every session start calls this; a failure must back off"


def test_upgrade_by_hand_retries_without_backoff_and_says_what_it_did(
    _recorded: list[dict], monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    _converge(monkeypatch, auto=False)
    assert _recorded[0]["failure_backoff_s"] == 0
    assert "VC++ runtime" in capsys.readouterr().out


def test_upgrade_runtime_step_never_breaks_the_upgrade(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))

    def boom(config_dir, **kw):  # noqa: ANN001, ANN003, ANN202
        raise RuntimeError("must not escape")

    monkeypatch.setattr(b, "ensure_vc_runtime", boom)
    _converge(monkeypatch, auto=True)  # does not raise


def test_upgrade_auto_reaches_the_precondition_stage_that_hosts_the_step() -> None:
    import inspect

    from nexus.commands import upgrade as up

    src = inspect.getsource(up.upgrade.callback)
    assert "_converge_preconditions(auto_mode=auto_mode" in src
    assert "_converge_vc_runtime(auto_mode=auto_mode)" in inspect.getsource(up._converge_preconditions)


# ── the PDF-only DLL is topped up from the bundle (nexus-vyy65) ─────────────
#
# A box whose System32 or <config>/vcrt already holds the base pair never took the
# provisioning path, so an engine release carrying msvcp140_atomic_wait.dll could not end
# its need for the redistributable. ensure_vc_runtime now tops the DLL up, at most once
# per pinned tag.

_PDF_SET = _vcrt.PDF_STACK_DLLS


def _gap(cfg: Path, system: str):  # noqa: ANN202
    from nexus.pdf_stack import _windows_runtime_gap

    return _windows_runtime_gap("win32", str(cfg), system)


def _new_bundle(monkeypatch: pytest.MonkeyPatch) -> _Net:
    n = _Net(_bundle_bytes(atomic_wait=True))
    monkeypatch.setattr(b, "_download", n)
    return n


def test_a_system_base_pair_is_topped_up_with_the_pdf_dll_from_a_bundle_that_carries_it(
    cfg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sysdir = str(_put_dlls(tmp_path / "System32-base"))
    net = _new_bundle(monkeypatch)
    assert _gap(cfg, sysdir) == ("base_only", sysdir)
    r = _ensure(cfg, sysdir, net)
    assert r.status == "provisioned" and r.ok and r.dir == cfg / "vcrt", r.detail
    assert _ATOMIC_WAIT in r.detail
    assert net.asset_fetches == 1
    assert _vcrt_files(cfg) == sorted([*_PDF_SET, "vcrt.meta.json"])
    assert (cfg / "vcrt" / _ATOMIC_WAIT).read_bytes() == _BODY[_ATOMIC_WAIT]
    assert _gap(cfg, sysdir) is None, "the whole set is in one directory now: the gap is closed"
    meta = json.loads((cfg / "vcrt" / "vcrt.meta.json").read_text())
    assert meta["pdf_dll_checked_tag"] == b.resolve_service_tag() and _ATOMIC_WAIT in meta["dlls"]


def test_a_stale_base_only_vcrt_is_topped_up_without_rewriting_its_base_pair(
    cfg: Path, system: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    where = _put_dlls(cfg / "vcrt")  # what an earlier client provisioned: the base pair only
    for n in _DLLS:
        (where / n).write_bytes(b"EARLIER-" + n.encode())
    net = _new_bundle(monkeypatch)
    assert _gap(cfg, system) == ("base_only", str(where))
    r = _ensure(cfg, system, net)
    assert r.status == "provisioned", r.detail
    for n in _DLLS:  # a loaded DLL cannot be replaced on Windows: the existing ones stay
        assert (where / n).read_bytes() == b"EARLIER-" + n.encode()
    assert (where / _ATOMIC_WAIT).read_bytes() == _BODY[_ATOMIC_WAIT]
    assert _gap(cfg, system) is None


def test_an_older_bundle_is_fetched_once_per_pinned_tag_not_on_every_call(
    cfg: Path, tmp_path: Path, net: _Net,
) -> None:
    """`net` serves the older bundle (no msvcp140_atomic_wait.dll)."""
    sysdir = str(_put_dlls(tmp_path / "System32-base"))
    first = _ensure(cfg, sysdir, net, tag="engine-service-v9.9.1")
    assert first.status == "present_system" and first.ok
    assert net.asset_fetches == 1
    assert _vcrt_files(cfg) == ["vcrt.meta.json"], "an older bundle places nothing"
    assert _gap(cfg, sysdir) == ("base_only", sysdir)
    for _ in range(2):
        _ensure(cfg, sysdir, net, tag="engine-service-v9.9.1")
    assert net.asset_fetches == 1, "the tag was recorded as checked: no refetch"
    _ensure(cfg, sysdir, net, tag="engine-service-v9.9.2")
    assert net.asset_fetches == 2, "a new pinned tag rechecks"


def test_a_newer_tag_that_carries_the_dll_picks_it_up_after_an_older_one_was_recorded(
    cfg: Path, tmp_path: Path, net: _Net, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sysdir = str(_put_dlls(tmp_path / "System32-base"))
    _ensure(cfg, sysdir, net, tag="engine-service-v9.9.1")
    new = _new_bundle(monkeypatch)
    r = _ensure(cfg, sysdir, new, tag="engine-service-v9.9.2")
    assert r.status == "provisioned" and new.asset_fetches == 1
    assert _gap(cfg, sysdir) is None
    assert json.loads((cfg / "vcrt" / "vcrt.meta.json").read_text())["pdf_dll_checked_tag"] == "engine-service-v9.9.2"


def test_a_full_provision_from_an_older_bundle_is_not_refetched_for_the_same_tag(
    cfg: Path, system: str, net: _Net,
) -> None:
    assert _ensure(cfg, system, net).status == "provisioned"
    assert _ensure(cfg, system, net).status == "present_app_local"
    assert net.asset_fetches == 1


def test_a_present_pdf_set_never_reaches_the_network(cfg: Path, tmp_path: Path, net: _Net) -> None:
    sysdir = str(_put_dlls(tmp_path / "System32-full", _PDF_SET))
    assert _ensure(cfg, sysdir, net).status == "present_system"
    where = _put_dlls(cfg / "service", _PDF_SET)
    r = _ensure(cfg, str(tmp_path / "empty"), net)
    assert r.status == "present_app_local" and r.dir == where
    assert net.urls == []


def test_the_top_up_is_not_applicable_off_windows(cfg: Path, tmp_path: Path, net: _Net) -> None:
    sysdir = str(_put_dlls(tmp_path / "System32-base"))
    r = _ensure(cfg, sysdir, net, platform="darwin")
    assert r.status == "not_applicable" and net.urls == [] and not (cfg / "vcrt").exists()


def test_a_failed_top_up_is_a_result_stamps_the_failure_and_backs_off(
    cfg: Path, tmp_path: Path, net: _Net, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sysdir = str(_put_dlls(tmp_path / "System32-base"))
    monkeypatch.setattr(b, "_download", _RaisesOnce(net))
    r = _ensure(cfg, sysdir, None)
    assert r.status == "failed" and _ATOMIC_WAIT in r.detail and "first attempt fails" in r.detail
    assert (cfg / b._VCRT_FAILED_SENTINEL).exists()
    deferred = _ensure(cfg, sysdir, None, failure_backoff_s=3600)
    assert deferred.status == "present_system" and net.asset_fetches == 0, "no download inside the backoff"
    assert _ensure(cfg, sysdir, None).status == "present_system"
    assert net.asset_fetches == 1 and not (cfg / b._VCRT_FAILED_SENTINEL).exists()


def test_a_top_up_puts_the_directory_on_this_processes_dll_path(
    cfg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sysdir = str(_put_dlls(tmp_path / "System32-base"))
    net = _new_bundle(monkeypatch)
    calls: list[dict] = []
    monkeypatch.setattr(nexus, "_add_vc_runtime_dirs", lambda **kw: calls.append(kw) or [])
    _ensure(cfg, sysdir, net)
    assert calls and calls[0]["config_dir"] == str(cfg)


def test_the_search_adds_a_vcrt_that_holds_the_whole_set_over_a_base_only_system32(
    cfg: Path, tmp_path: Path,
) -> None:
    """The whole-set rule stays: a topped-up vcrt joins the path; a base-only one never does."""
    sysdir = str(_put_dlls(tmp_path / "System32-base"))
    _put_dlls(cfg / "vcrt")
    assert _search(cfg, sysdir) == []
    _put_dlls(cfg / "vcrt", _PDF_SET)
    assert _search(cfg, sysdir) == [str(cfg / "vcrt")]


# ── the bundle read stops once the required pair is in hand ─────────────────


def test_the_extraction_stops_at_the_end_of_bin_instead_of_reading_postgresql(tmp_path: Path) -> None:
    """The members after ``bundle/bin/`` sit in an xz stream cut short. Reading on to them
    raises; an extraction that stops at the end of ``bin/`` never gets there. Checked by
    deleting the early exit: this test then fails with BinaryVerificationError."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tf:
        for name in ("postgres.exe", *_DLLS):
            ti = tarfile.TarInfo(f"bundle/bin/{name}")
            ti.size = len(_BODY[name])
            tf.addfile(ti, io.BytesIO(_BODY[name]))
        filler = os.urandom(400_000)
        for member in ("bundle/share/a.bin", "bundle/share/b.bin"):
            ti = tarfile.TarInfo(member)
            ti.size = len(filler)
            tf.addfile(ti, io.BytesIO(filler))
    packed = lzma.compress(raw.getvalue(), preset=0)
    archive = tmp_path / "cut.txz"
    archive.write_bytes(packed[: len(packed) // 2])  # cut inside the filler
    dest = tmp_path / "out"
    digests = b._extract_vc_runtime(archive, dest, _DLLS, optional=(_ATOMIC_WAIT,))
    assert set(digests) == set(_DLLS)  # an older bundle: no optional DLL, and no read to the end
    assert sorted(p.name for p in dest.iterdir()) == sorted(_DLLS)
