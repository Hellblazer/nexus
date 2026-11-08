# SPDX-License-Identifier: AGPL-3.0-or-later
"""The shipped procedures obey the runtime's own rules, and reach a built wheel (RDR-240 Technical Design 1 and 2).

Every ``src/nexus/procedures/*.lua``:

* parses with ``header.parse`` under its own file name;
* grants only verbs in RDR-242's version 1 table (``static_check.VERBS_V1``), so a procedure
  cannot ship a grant ``run_script`` would refuse at run time;
* passes ``static_check.check`` with its own grants (which also refuses a grant it never uses
  and a verb it calls without a grant).

Non-vacuity: the sweep must find the two procedures that ship now, and each rule has a planted
violation. The wheel test builds a wheel and requires the procedures and the vocabulary table
``research_add`` reads (``tables.get``) to be in it: a file missing from the wheel is a
procedure that works in a checkout and fails on every installed ``conexus``.
File-only, so it runs in the ``-m lint`` job.
"""
from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path

import pytest

from nexus.lua_runtime import header, static_check
from nexus.lua_runtime.errors import ScriptError

pytestmark = pytest.mark.lint

REPO_ROOT = Path(__file__).resolve().parent.parent
PROCEDURES = REPO_ROOT / "src" / "nexus" / "procedures"
#: The procedures that ship at this commit; a sweep that finds fewer is looking in the wrong place.
SHIPPED_FLOOR = {"research_add", "gate_record"}


def findings(path: Path, verbs: frozenset[str] = static_check.VERBS_V1) -> list[str]:
    """What is wrong with the procedure file *path*; empty when it is clean."""
    source = path.read_bytes()
    try:
        head = header.parse(source, name=path.stem)
    except ScriptError as exc:
        return [f"{path.name}: header {exc.detail}"]
    out = [f"{path.name}: grants {verb}, which is not in the version 1 verb table" for verb in sorted(head.grants - verbs)]
    try:
        static_check.check(source, head.grants, verbs)
    except static_check.StaticCheckRefusal as exc:
        out.append(f"{path.name}: static check {exc}")
    return out


def test_every_shipped_procedure_is_clean() -> None:
    files = sorted(PROCEDURES.glob("*.lua"))
    assert SHIPPED_FLOOR <= {p.stem for p in files}, [p.name for p in files]
    bad = [f for p in files for f in findings(p)]
    assert not bad, "\n".join(bad)


def test_the_directory_holds_procedures_and_nothing_a_run_would_not_read() -> None:
    odd = sorted(p.name for p in PROCEDURES.iterdir() if p.suffix != ".lua" and p.name != "__pycache__")
    assert not odd, f"files in src/nexus/procedures that are not .lua: {odd}"


# -- planted violations: each rule fires --------------------------------------

GOOD = (
    b"-- procedure: planted\n-- summary: a planted procedure\n-- args: none\n-- grants: memory.get\n"
    b"return memory.get('p', 't')\n"
)


def _plant(tmp_path: Path, source: bytes, name: str = "planted") -> Path:
    path = tmp_path / f"{name}.lua"
    path.write_bytes(source)
    return path


def test_a_clean_planted_procedure_passes(tmp_path: Path) -> None:
    assert findings(_plant(tmp_path, GOOD)) == []


def test_a_grant_outside_the_version_1_table_is_flagged(tmp_path: Path) -> None:
    source = GOOD.replace(b"grants: memory.get", b"grants: memory.put").replace(b"memory.get(", b"memory.put(")
    out = findings(_plant(tmp_path, source))
    assert any("memory.put" in f and "version 1 verb table" in f for f in out), out


def test_a_header_under_the_wrong_name_is_flagged(tmp_path: Path) -> None:
    out = findings(_plant(tmp_path, GOOD, name="other"))
    assert out and "header" in out[0] and "not the file name" in out[0], out


def test_a_missing_header_is_flagged(tmp_path: Path) -> None:
    out = findings(_plant(tmp_path, b"return 1\n"))
    assert out and "header" in out[0], out


def test_a_call_without_its_grant_is_flagged(tmp_path: Path) -> None:
    source = GOOD.replace(b"grants: memory.get", b"grants: none")
    out = findings(_plant(tmp_path, source))
    assert any("verb-ungranted" in f for f in out), out


def test_a_grant_the_script_never_uses_is_flagged(tmp_path: Path) -> None:
    source = GOOD.replace(b"grants: memory.get", b"grants: memory.get, memory.list")
    out = findings(_plant(tmp_path, source))
    assert any("grant-unused" in f for f in out), out


def test_a_forbidden_global_is_flagged(tmp_path: Path) -> None:
    out = findings(_plant(tmp_path, GOOD + b"load('x')\n"))
    assert any("static check" in f for f in out), out


# -- the wheel -----------------------------------------------------------------


def test_procedures_and_the_vocabulary_table_are_present_in_a_built_wheel(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    result = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(dist_dir)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, (
        f"uv build --wheel failed (rc={result.returncode}):\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    wheels = sorted(dist_dir.glob("*.whl"))
    assert wheels, f"uv build --wheel produced no .whl in {dist_dir}"
    with zipfile.ZipFile(wheels[-1]) as zf:
        names = set(zf.namelist())

    shipped = sorted(f"nexus/procedures/{p.name}" for p in PROCEDURES.glob("*.lua"))
    assert len(shipped) >= len(SHIPPED_FLOOR), shipped
    missing = [n for n in [*shipped, "nexus/tables/rdr-research-vocab.toml"] if n not in names]
    assert not missing, (
        f"missing from the built wheel: {missing}. A procedure or table that is not package data "
        "works in a checkout and fails on every installed conexus. Wheel contents "
        f"(nexus/procedures/*, nexus/tables/rdr-research-vocab*): "
        f"{sorted(n for n in names if n.startswith('nexus/procedures/') or 'rdr-research-vocab' in n)}"
    )
