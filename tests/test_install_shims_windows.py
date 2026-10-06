# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Windows shim writer (RDR-224, nexus-f9bgu.47).

A Windows shim is a byte copy of the generation's own ``Scripts\\<name>.exe``,
ownership is the ``.nexus-shims.json`` sidecar, and a running launcher is
renamed aside rather than overwritten. The writer runs on macOS here with
``platform="win32"``: the "interpreter" is a ``#!/bin/sh`` stub named
``python.exe`` that prints the entry points, and the "launchers" are plain
byte files. The real running-exe case is ``TestRealWindows`` in
``tests/test_install_generation_windows.py``.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from nexus._install import layout_core as lc
from nexus._install import shims_core as sc

WIN = "win32"


def _generation(root: Path, stamp: str, scripts: dict[str, bytes]) -> Path:
    """A fake Windows venv whose python.exe declares exactly *scripts*."""
    gen = root / f"gen-{stamp}"
    bin_dir = gen / "Scripts"
    bin_dir.mkdir(parents=True)
    listing = "\n".join(scripts)
    python = bin_dir / "python.exe"
    python.write_text(f"#!/bin/sh\nprintf '%s\\n' '{listing}'\n")
    python.chmod(python.stat().st_mode | stat.S_IXUSR)
    for name, body in scripts.items():
        (bin_dir / f"{name}.exe").write_bytes(body)
    return gen


@pytest.fixture
def bed(tmp_path: Path) -> tuple[Path, Path]:
    tools = tmp_path / "tools"
    tools.mkdir()
    bin_dir = tmp_path / "bin"
    return tools, bin_dir


def test_each_owned_launcher_is_copied_byte_for_byte(bed) -> None:
    tools, bin_dir = bed
    gen = _generation(tools, "A", {"nx": b"NX-LAUNCHER-A", "nx-mcp": b"MCP-LAUNCHER-A"})
    diags = sc.write_shims(gen, bin_dir, platform=WIN)
    assert diags == []
    assert (bin_dir / "nx.exe").read_bytes() == b"NX-LAUNCHER-A"
    assert (bin_dir / "nx-mcp.exe").read_bytes() == b"MCP-LAUNCHER-A"
    # No POSIX-style extensionless script is written.
    assert not (bin_dir / "nx").exists()


def test_the_sidecar_records_names_and_hashes_and_the_generation(bed) -> None:
    tools, bin_dir = bed
    gen = _generation(tools, "A", {"nx": b"AAA"})
    sc.write_shims(gen, bin_dir, platform=WIN)
    payload = json.loads((bin_dir / lc.SHIMS_SIDECAR_NAME).read_text())
    assert payload["schema"] == lc.SHIMS_SIDECAR_SCHEMA
    assert payload["generation"] == str(gen)
    assert payload["shims"] == {"nx": lc.file_sha256(bin_dir / "nx.exe")}
    assert lc.read_shim_record(bin_dir) == payload["shims"]


def test_a_second_generation_rewrites_the_copies_and_the_record(bed) -> None:
    tools, bin_dir = bed
    sc.write_shims(_generation(tools, "A", {"nx": b"AAA"}), bin_dir, platform=WIN)
    gen_b = _generation(tools, "B", {"nx": b"BBBB"})
    sc.write_shims(gen_b, bin_dir, platform=WIN)
    assert (bin_dir / "nx.exe").read_bytes() == b"BBBB"
    assert lc.read_shim_record(bin_dir)["nx"] == lc.file_sha256(lc.venv_script(gen_b, "nx", platform=WIN))
    assert lc.windows_shim_mismatches(gen_b, bin_dir, {"nx"}) == []


def test_a_running_launcher_is_renamed_aside_then_replaced(bed, monkeypatch) -> None:
    """A running exe refuses overwrite with PermissionError; the writer renames
    it aside as ``nx.exe.old-<pid>`` and the new launcher takes the name."""
    tools, bin_dir = bed
    sc.write_shims(_generation(tools, "A", {"nx": b"OLD-RUNNING"}), bin_dir, platform=WIN)
    gen_b = _generation(tools, "B", {"nx": b"NEW"})

    real_replace = os.replace
    calls: list[tuple[str, str]] = []

    def replace(src, dst):
        calls.append((Path(src).name, Path(dst).name))
        if Path(dst).name == "nx.exe" and Path(dst).exists() and not any(
            n.startswith("nx.exe.old-") for n in os.listdir(bin_dir)
        ):
            raise PermissionError(5, "Access is denied")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    sc.write_shims(gen_b, bin_dir, platform=WIN)
    assert (bin_dir / "nx.exe").read_bytes() == b"NEW"
    aside = [p for p in bin_dir.iterdir() if p.name.startswith("nx.exe.old-")]
    assert len(aside) == 1 and aside[0].read_bytes() == b"OLD-RUNNING"
    assert aside[0].name == f"nx.exe.old-{os.getpid()}"


def test_displaced_launchers_are_swept_on_a_later_run(bed) -> None:
    tools, bin_dir = bed
    bin_dir.mkdir()
    (bin_dir / "nx.exe.old-111").write_bytes(b"gone-process")
    (bin_dir / "keep.txt").write_text("not ours")
    sc.write_shims(_generation(tools, "A", {"nx": b"AAA"}), bin_dir, platform=WIN)
    assert not (bin_dir / "nx.exe.old-111").exists()
    assert (bin_dir / "keep.txt").exists()


def test_a_displaced_launcher_that_is_still_running_survives_the_sweep(bed, monkeypatch) -> None:
    tools, bin_dir = bed
    bin_dir.mkdir()
    busy = bin_dir / "nx.exe.old-222"
    busy.write_bytes(b"still running")
    real_unlink = Path.unlink

    def unlink(self, *a, **kw):
        if self.name == "nx.exe.old-222":
            raise PermissionError(32, "in use")
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", unlink)
    sc.write_shims(_generation(tools, "A", {"nx": b"AAA"}), bin_dir, platform=WIN)
    assert busy.exists()


def test_a_shim_the_new_generation_no_longer_ships_is_pruned(bed) -> None:
    tools, bin_dir = bed
    sc.write_shims(_generation(tools, "A", {"nx": b"AAA", "nx-hook": b"HOOK"}), bin_dir, platform=WIN)
    diags = sc.write_shims(_generation(tools, "B", {"nx": b"BBB"}), bin_dir, platform=WIN)
    assert not (bin_dir / "nx-hook.exe").exists()
    assert any("removed stale shim 'nx-hook.exe'" in d for d in diags), diags
    assert "nx-hook" not in lc.read_shim_record(bin_dir)


def test_a_file_nexus_did_not_write_is_never_pruned(bed) -> None:
    tools, bin_dir = bed
    sc.write_shims(_generation(tools, "A", {"nx": b"AAA", "nx-hook": b"HOOK"}), bin_dir, platform=WIN)
    (bin_dir / "nx-hook.exe").write_bytes(b"someone else's tool at our old name")
    sc.write_shims(_generation(tools, "B", {"nx": b"BBB"}), bin_dir, platform=WIN)
    assert (bin_dir / "nx-hook.exe").read_bytes() == b"someone else's tool at our old name"
    assert "nx-hook" not in lc.read_shim_record(bin_dir)


def test_unrelated_exes_in_the_shared_bin_dir_are_untouched(bed) -> None:
    tools, bin_dir = bed
    bin_dir.mkdir()
    (bin_dir / "uv.exe").write_bytes(b"uv")
    (bin_dir / "python3.12.exe").write_bytes(b"py")
    sc.write_shims(_generation(tools, "A", {"nx": b"AAA"}), bin_dir, platform=WIN)
    assert (bin_dir / "uv.exe").read_bytes() == b"uv"
    assert (bin_dir / "python3.12.exe").read_bytes() == b"py"


def test_a_uv_copy_over_our_name_reads_as_reclaimed_after_a_write(bed) -> None:
    tools, bin_dir = bed
    gen = _generation(tools, "A", {"nx": b"AAA"})
    sc.write_shims(gen, bin_dir, platform=WIN)
    assert lc.reclaimed_from_owned({"nx"}, bin_dir, platform=WIN) == []
    (bin_dir / "nx.exe").write_bytes(b"uv tool install --force wrote this")
    assert lc.reclaimed_from_owned({"nx"}, bin_dir, platform=WIN) == ["nx"]
    assert lc.reclaimed_shims(gen, bin_dir, platform=WIN) == ["nx"]


def test_a_generation_that_declares_nothing_writes_no_partial_set(bed) -> None:
    tools, bin_dir = bed
    gen = _generation(tools, "A", {})
    with pytest.raises(sc.ShimsError):
        sc.write_shims(gen, bin_dir, platform=WIN)
    assert not (bin_dir / lc.SHIMS_SIDECAR_NAME).exists()


def test_a_declared_script_with_no_launcher_gets_no_shim(bed) -> None:
    tools, bin_dir = bed
    gen = _generation(tools, "A", {"nx": b"AAA", "nx-extra": b"X"})
    lc.venv_script(gen, "nx-extra", platform=WIN).unlink()
    sc.write_shims(gen, bin_dir, platform=WIN)
    assert (bin_dir / "nx.exe").exists()
    assert not (bin_dir / "nx-extra.exe").exists()


def test_a_replace_failure_that_is_not_a_lock_refuses_and_leaves_no_temp(bed, monkeypatch) -> None:
    tools, bin_dir = bed
    bin_dir.mkdir()

    def replace(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", replace)
    with pytest.raises(sc.ShimsError):
        sc.write_shims(_generation(tools, "A", {"nx": b"AAA"}), bin_dir, platform=WIN)
    assert [p.name for p in bin_dir.iterdir() if ".tmp." in p.name] == []


def test_the_posix_writer_is_unchanged_by_the_windows_branch(tmp_path: Path) -> None:
    """With platform=None on a POSIX host the shell-script shim is written."""
    if os.name == "nt":
        pytest.skip("POSIX writer")
    tools = tmp_path / "tools"
    gen = tools / "gen-A"
    (gen / "bin").mkdir(parents=True)
    python = gen / "bin" / "python"
    python.write_text("#!/bin/sh\nprintf '%s\\n' nx\n")
    python.chmod(0o755)
    (gen / "bin" / "nx").write_text("#!/bin/sh\n")
    bin_dir = tmp_path / "bin"
    sc.write_shims(gen, bin_dir)
    assert (bin_dir / "nx").read_text().startswith("#!/bin/sh")
    assert not (bin_dir / lc.SHIMS_SIDECAR_NAME).exists()
