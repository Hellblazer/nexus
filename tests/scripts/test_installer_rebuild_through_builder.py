# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The installer rebuild (nexus-au0s3) through the REAL builder, with a fake
``uv`` that records every argv.

A generation whose receipt says no lock was applied (``constraints`` "none", or
absent in a receipt an older installer wrote) is rebuilt by
``rebuild_stale_installer``: one build at the SAME version, with EXACTLY the
extras ``current`` has, with the constraints the package ships, flipped; a second
call finds nothing to do. A deliberately unpinned generation is left alone.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from _generation_harness import SAFE_BASE_PATH
from test_install_generation import _final_install, _installs, _make_executable, _stub_uv

from nexus import install_layout, pdf_stack
from tests._module_seam import setattr_in

VERSION = "9.9.9"


def _gen(tools: Path, stamp: str, *, extras: list[str], constraints: str | None) -> Path:
    """A registry generation at VERSION. *constraints* None omits the key, as an
    installer that predates the field wrote it."""
    gen = tools / f"gen-{stamp}"
    (gen / "bin").mkdir(parents=True)
    for ep in ("nx", "nx-mcp", "nx-mcp-catalog", "nx-session-end-launcher", "nx-hook"):
        (gen / "bin" / ep).write_text("#!/bin/sh\n")
    (gen / "pyvenv.cfg").write_text("home = /usr/bin\nversion = 3.12.8\n")
    spec = f"conexus[{','.join(extras)}]" if extras else "conexus"
    receipt = {
        "schema": 1, "version": VERSION, "spec": spec, "source_kind": "registry", "source": "conexus",
        "extras": extras, "python": "3.12", "base_interpreter": "/usr/bin",
        "created_at": "2026-10-01T00:00:00Z", "installer_schema": 2,
    }
    if constraints is not None:
        receipt["constraints"] = constraints
    (gen / "nexus-install.json").write_text(json.dumps(receipt))
    return gen


@pytest.fixture
def bed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    tools = tmp_path / "tools"
    tools.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub_bin = tmp_path / "stubbin"
    stub_bin.mkdir()
    argv_log = tmp_path / "uv-argv.log"
    _stub_uv(stub_bin, argv_log=argv_log)
    _make_executable(stub_bin / "uname", "#!/bin/bash\necho Linux\n")
    monkeypatch.setenv("NX_TOOLS_DIR", str(tools))
    monkeypatch.setenv("NX_BIN_DIR", str(bin_dir))
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("PATH", f"{stub_bin}:{SAFE_BASE_PATH}")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("NX_TORCH_BACKEND", raising=False)
    monkeypatch.delenv("NX_INSTALL_UNPINNED", raising=False)
    # The running version needs no target-installer fetch (nexus-2k66t.4.3); this
    # pin makes that explicit so the argv log holds only the build's own calls.
    monkeypatch.setenv("NX_INSTALL_RUNNING_INSTALLER", "1")
    monkeypatch.setattr(pdf_stack, "_running_version", lambda: VERSION)
    # The checkout this suite runs from ships a constraints file; say so rather than rely on it.
    monkeypatch.setattr(pdf_stack, "_package_ships_constraints", lambda: True)
    return tools, argv_log, stub_bin


def _run_as_host(monkeypatch, host: Path, **kw):
    setattr_in(monkeypatch, ("nexus.commands.self_cmd", "nexus.upgrade_finish"), "sys.prefix", str(host))
    return pdf_stack.rebuild_stale_installer(**kw)


@pytest.mark.parametrize("recorded", [
    pytest.param("none", id="the-package-shipped-no-constraints"),
    pytest.param(None, id="an-older-installer-wrote-no-field"),
])
def test_a_generation_built_without_the_lock_is_rebuilt_with_it_once(bed, monkeypatch, recorded) -> None:
    tools, argv_log, _stub = bed
    host = _gen(tools, "20260101T000000Z", extras=["local"], constraints=recorded)
    (tools / "current").symlink_to(host)

    line = _run_as_host(monkeypatch, host)

    assert line is not None and "rebuilt" in line, line
    installs = _installs(argv_log)
    assert "conexus[local]==9.9.9" in installs[0], installs[0]  # same version, same extras, no pdf added
    final = _final_install(argv_log)
    assert "conexus[local]==9.9.9" in final and "pdf" not in final
    shipped = "site-packages/nexus/_install/constraints.txt"
    assert "--constraints" in final and shipped in final, "the release's pins now apply"
    current = (tools / "current").resolve()
    assert current != host.resolve(), "flipped to a new generation"
    receipt = install_layout.read_receipt(current)
    assert receipt.constraints == "applied", "the new receipt records what the build did"
    assert receipt.extras == ["local"] and receipt.spec == "conexus[local]==9.9.9"
    assert host.is_dir(), "the old generation is kept for rollback (keep-3)"

    # Non-vacuity: the second call sees a rebuilt current for the right reason
    # (the lock is recorded), not because the version stopped matching.
    assert pdf_stack._stale_current_extras() is None  # noqa: SLF001
    argv_log.write_text("")
    assert _run_as_host(monkeypatch, current) is None
    assert _installs(argv_log) == [], "a current generation is never rebuilt"


@pytest.mark.parametrize("host_extras, current_extras", [
    pytest.param([], ["local"], id="host-slim-current-local"),
    pytest.param(["pdf"], ["local"], id="host-pdf-current-local"),
    pytest.param(["local", "pdf"], [], id="host-has-more-than-current"),
])
def test_the_rebuild_carries_exactly_currents_extras_not_the_hosts(
    bed, monkeypatch, host_extras, current_extras,
) -> None:
    """Host and current are one version with different extras (a peer flipped current).
    The perform_self_install default unions the host's extras into the build; the rebuild
    must not: it rebuilds CURRENT."""
    tools, _argv_log, _stub = bed
    host = _gen(tools, "20260101T000000Z", extras=host_extras, constraints="applied")
    current_gen = _gen(tools, "20260102T000000Z", extras=current_extras, constraints="none")
    (tools / "current").symlink_to(current_gen)

    line = _run_as_host(monkeypatch, host)

    assert line is not None and "rebuilt" in line, line
    new = (tools / "current").resolve()
    assert new not in (host.resolve(), current_gen.resolve())
    receipt = install_layout.read_receipt(new)
    assert receipt.extras == sorted(current_extras), receipt.extras
    spec = f"conexus[{','.join(sorted(current_extras))}]==9.9.9" if current_extras else "conexus==9.9.9"
    assert receipt.spec == spec


def test_a_deliberately_unpinned_generation_is_never_re_pinned(bed, monkeypatch) -> None:
    tools, argv_log, _stub = bed
    host = _gen(tools, "20260101T000000Z", extras=["local"], constraints="unpinned")
    (tools / "current").symlink_to(host)
    assert _run_as_host(monkeypatch, host) is None
    assert not argv_log.exists() or _installs(argv_log) == []
    assert (tools / "current").resolve() == host.resolve()


def test_a_build_that_comes_out_unpinned_is_a_failure_that_backs_off(bed, monkeypatch) -> None:
    """The package the builder fetched has no constraints file (a stub package without it): the
    build succeeds and flips, but the receipt says `none`. That is a failed rebuild, held back by
    the marker, so the next session start does not build again."""
    tools, argv_log, stub_bin = bed
    (stub_bin / "no-packaged-constraints").write_text("")
    host = _gen(tools, "20260101T000000Z", extras=["local"], constraints="none")
    (tools / "current").symlink_to(host)

    line = _run_as_host(monkeypatch, host, auto_mode=True)

    assert line is not None and "could not be rebuilt" in line and "lock" in line, line
    sentinel = Path(os.environ["NEXUS_CONFIG_DIR"]) / "installer-rebuild-failed"
    assert sentinel.exists()
    built = len(_installs(argv_log))
    now = (tools / "current").resolve()
    assert install_layout.read_receipt(now).constraints == "none"
    again = _run_as_host(monkeypatch, now, auto_mode=True)
    assert again is not None and "deferred" in again
    assert len(_installs(argv_log)) == built, "no second build inside the backoff"
