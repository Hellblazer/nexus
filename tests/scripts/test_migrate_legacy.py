# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Legacy uv-tool install migration: extras bridge, shim takeover, DEFERRED
legacy reap.

nexus-utpuw.7 (P3). ``uv tool uninstall conexus`` deletes ``$(uv tool dir)/
conexus`` -- the exact tree every live holder is running from (the
nexus-q3xrx hazard verbatim). A migration that uninstalls is a migration
that breaks live sessions, so this migration NEVER uninstalls. Instead:

  1. read the legacy uv-receipt.toml ONE LAST TIME -> extract extras -> seed
     the new generation's nexus-install.json (the only bridge for [local]);
  2. build the new generation side-by-side (legacy tree untouched);
  3. flip current;
  4. replace the uv-owned bin SYMLINKS with nexus-owned shim FILES;
  5. register the legacy tree as a pseudo-generation in the GC ledger, so a
     LATER, SEPARATE ``nx_gc_generations`` pass reaps it once nothing holds
     it -- never during the migrating run itself. ``migrate_legacy.sh``
     never sources gc.sh and never calls ``nx_gc_generations``, so reap
     cannot fire in this process at all; the separate-pass tests below call
     GC themselves, afterward, exactly as a later install would.

THE LEDGER PROBLEM. gc.sh enumerates ``<tools>/gen-*`` directories carrying
a receipt; the legacy tree lives at ``$(uv tool dir)/conexus``, entirely
outside ``<tools>/``. It is registered as a SYMLINK named
``gen-legacy-uv-tool`` pointing at the real tree. That symlink is never
given a receipt (nothing ever writes ``nexus-install.json`` into a tree this
project does not own), so gc.sh's existing "receipt-less -> reapable, never
counted toward keep-last-N" rule already gives it exactly the semantics
wanted, with zero change to gc.sh's enumeration or protection rules --
see ``test_generation_gc.py``'s symlink-aware reap tests for that half.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_MIGRATE = _REPO / "src" / "nexus" / "_install" / "migrate_legacy.sh"
_GC = _REPO / "src" / "nexus" / "_install" / "gc.sh"


def _make_executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _stub_uv(bin_dir: Path, *, argv_log: Path, scripts: list[str]) -> None:
    """A fake ``uv`` that fabricates a venv (with a python stub that answers
    the entry-point query shims.sh makes) and a pip install that populates
    the scripts the shim writer is expected to find."""
    names = "\n".join(scripts)
    script_loop = " ".join(scripts)
    _make_executable(
        bin_dir / "uv",
        f"""#!/bin/bash
echo "$@" >> "{argv_log}"
if [[ "$1" == "venv" ]]; then
    target=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --python) shift 2 ;;
            -*) shift ;;
            venv) shift ;;
            *) target="$1"; shift ;;
        esac
    done
    mkdir -p "$target/bin"
    cat > "$target/bin/python" <<'PYEOF'
#!/bin/sh
cat <<'NAMES'
{names}
NAMES
PYEOF
    chmod +x "$target/bin/python"
    printf 'home = /opt/uv/pythons/cpython-3.12.8/bin\\nversion = 3.12.8\\n' > "$target/pyvenv.cfg"
    exit 0
fi
if [[ "$1" == "pip" && "$2" == "install" ]]; then
    py=""
    shift 2
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --python) py="$2"; shift 2 ;;
            *) shift ;;
        esac
    done
    if [[ -n "$py" ]]; then
        gen_bin="$(dirname "$py")"
        for name in {script_loop}; do
            printf '#!/bin/sh\\necho ran-%s\\n' "$name" > "$gen_bin/$name"
            chmod +x "$gen_bin/$name"
        done
    fi
    exit 0
fi
exit 0
""",
    )


def _stub_ps(bin_dir: Path, lines: list[str]) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    ps = bin_dir / "ps"
    ps.write_text("#!/bin/sh\ncat <<'PSEOF'\n" + "\n".join(lines) + "\nPSEOF\n")
    ps.chmod(ps.stat().st_mode | stat.S_IXUSR)


def _legacy_tree(root: Path, *, receipt: str | None) -> Path:
    """A fabricated legacy ``uv tool install conexus`` layout."""
    legacy = root / "uv-tool-dir" / "conexus"
    (legacy / "bin").mkdir(parents=True)
    (legacy / "bin" / "nx").write_text("#!/bin/sh\necho legacy-nx\n")
    (legacy / "bin" / "nx").chmod(0o755)
    # Every Python venv carries pyvenv.cfg (PEP 405), and `uv tool install`
    # produces a real venv. GC requires it before it will reap THROUGH the
    # ledger pointer -- that check is what keeps a mis-pointed ledger from
    # deleting an arbitrary directory, so the fixture has to be realistic.
    (legacy / "pyvenv.cfg").write_text("home = /opt/py/bin\n")
    if receipt is not None:
        (legacy / "uv-receipt.toml").write_text(receipt)
    return legacy


def _run(env_dict: dict, *args: str):
    return subprocess.run(
        ["bash", str(_MIGRATE), *args],
        capture_output=True, text=True, env=env_dict,
    )


@pytest.fixture
def scaffold(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    stub_bin = tmp_path / "stubbin"
    argv_log = tmp_path / "uv-argv.log"
    _stub_uv(stub_bin, argv_log=argv_log, scripts=["nx", "nx-mcp"])

    def env(extra: dict | None = None) -> dict:
        base = {
            "PATH": f"{stub_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(tmp_path / "home"),
            "NX_TOOLS_DIR": str(tools),
            "NX_BIN_DIR": str(bin_dir),
        }
        base.update(extra or {})
        return base

    return tmp_path, tools, bin_dir, stub_bin, argv_log, env


def test_migrate_legacy_is_present() -> None:
    assert _MIGRATE.is_file(), f"{_MIGRATE} is missing"
    assert os.access(_MIGRATE, os.X_OK), "migrate_legacy.sh is EXECUTED, like install_generation.sh"


# --------------------------------------------------------------------------
# step 1: extras survive the bridge
# --------------------------------------------------------------------------

def test_extras_survive_the_bridge(scaffold) -> None:
    tmp_path, tools, bin_dir, stub_bin, argv_log, env = scaffold
    legacy = _legacy_tree(tmp_path, receipt="""
[tool]
requirements = [
    { name = "conexus", extras = ["local"] },
]
""")

    result = _run(
        env(),
        "--source", "conexus", "--version", "7.18.0", "--legacy-venv", str(legacy),
    )

    assert result.returncode == 0, result.stderr
    gen = Path(result.stdout.strip().splitlines()[-1])
    payload = json.loads((gen / "nexus-install.json").read_text())
    assert payload["extras"] == ["local"], "the [local] extra did not survive the bridge"
    assert payload["spec"] == "conexus[local]==7.18.0"


def test_mineru_is_dropped_from_the_bridge(scaffold) -> None:
    """mineru is a default dependency now, not an extra (nexus-2fyb). A stale
    legacy receipt still listing it must not re-request it as an extra --
    the same filter scripts/reinstall-tool.sh has always applied."""
    tmp_path, tools, bin_dir, stub_bin, argv_log, env = scaffold
    legacy = _legacy_tree(tmp_path, receipt="""
[tool]
requirements = [
    { name = "conexus", extras = ["local", "mineru"] },
]
""")

    result = _run(
        env(),
        "--source", "conexus", "--version", "7.18.0", "--legacy-venv", str(legacy),
    )

    assert result.returncode == 0, result.stderr
    gen = Path(result.stdout.strip().splitlines()[-1])
    payload = json.loads((gen / "nexus-install.json").read_text())
    assert payload["extras"] == ["local"]


def test_no_receipt_bridges_to_no_extras(scaffold) -> None:
    tmp_path, tools, bin_dir, stub_bin, argv_log, env = scaffold
    legacy = _legacy_tree(tmp_path, receipt=None)

    result = _run(
        env(),
        "--source", "conexus", "--version", "7.18.0", "--legacy-venv", str(legacy),
    )

    assert result.returncode == 0, result.stderr
    gen = Path(result.stdout.strip().splitlines()[-1])
    payload = json.loads((gen / "nexus-install.json").read_text())
    assert payload["extras"] == []
    assert payload["spec"] == "conexus==7.18.0"


# --------------------------------------------------------------------------
# steps 2-3: build + flip
# --------------------------------------------------------------------------

def test_current_flips_to_the_new_generation(scaffold) -> None:
    tmp_path, tools, bin_dir, stub_bin, argv_log, env = scaffold
    legacy = _legacy_tree(tmp_path, receipt=None)

    result = _run(
        env(),
        "--source", "conexus", "--version", "7.18.0", "--legacy-venv", str(legacy),
    )

    assert result.returncode == 0, result.stderr
    gen = Path(result.stdout.strip().splitlines()[-1])
    current = tools / "current"
    assert current.is_symlink()
    assert Path(os.readlink(current)) == gen


def test_legacy_tree_is_byte_identical_after_migration(scaffold) -> None:
    tmp_path, tools, bin_dir, stub_bin, argv_log, env = scaffold
    legacy = _legacy_tree(tmp_path, receipt='[tool]\nrequirements = [{ name = "conexus", extras = ["local"] }]\n')
    before_nx = (legacy / "bin" / "nx").read_bytes()
    before_receipt = (legacy / "uv-receipt.toml").read_bytes()

    result = _run(
        env(),
        "--source", "conexus", "--version", "7.18.0", "--legacy-venv", str(legacy),
    )

    assert result.returncode == 0, result.stderr
    assert (legacy / "bin" / "nx").read_bytes() == before_nx, (
        "the legacy tree was mutated -- a migration that touches the tree "
        "live holders run from is the nexus-q3xrx failure verbatim"
    )
    assert (legacy / "uv-receipt.toml").read_bytes() == before_receipt
    assert not (legacy / "nexus-install.json").exists(), (
        "a receipt was written INTO the legacy tree -- we do not own it"
    )


# --------------------------------------------------------------------------
# step 4: shim takeover
# --------------------------------------------------------------------------

def test_shims_replace_uv_owned_symlinks_with_regular_files(scaffold) -> None:
    tmp_path, tools, bin_dir, stub_bin, argv_log, env = scaffold
    legacy = _legacy_tree(tmp_path, receipt=None)
    # Simulate uv's own bin entries: SYMLINKS into the legacy venv.
    (bin_dir / "nx").symlink_to(legacy / "bin" / "nx")
    assert (bin_dir / "nx").is_symlink()

    result = _run(
        env(),
        "--source", "conexus", "--version", "7.18.0", "--legacy-venv", str(legacy),
    )

    assert result.returncode == 0, result.stderr
    assert not (bin_dir / "nx").is_symlink(), (
        "the uv-owned symlink at ~/.local/bin/nx was not replaced with a "
        "nexus-owned regular file"
    )
    assert (bin_dir / "nx").is_file()
    body = (bin_dir / "nx").read_text()
    assert "readlink" in body, "the shim must resolve current before it execs"


# --------------------------------------------------------------------------
# step 5: register, never reap, in this run
# --------------------------------------------------------------------------

def test_legacy_is_registered_as_a_pseudo_generation(scaffold) -> None:
    tmp_path, tools, bin_dir, stub_bin, argv_log, env = scaffold
    legacy = _legacy_tree(tmp_path, receipt=None)

    result = _run(
        env(),
        "--source", "conexus", "--version", "7.18.0", "--legacy-venv", str(legacy),
    )

    assert result.returncode == 0, result.stderr
    pseudo = tools / "gen-legacy-uv-tool"
    assert pseudo.is_symlink()
    assert Path(os.readlink(pseudo)) == legacy
    assert not (pseudo / "nexus-install.json").exists(), (
        "the pseudo-generation must never carry a receipt -- that would let "
        "keep-last-N shield it, and it must always be reapable once unheld"
    )


def test_the_whole_migration_exits_0_with_a_live_holder_present(scaffold) -> None:
    tmp_path, tools, bin_dir, stub_bin, argv_log, env = scaffold
    legacy = _legacy_tree(tmp_path, receipt=None)
    _stub_ps(stub_bin, [f"  909 {legacy}/bin/python {legacy}/bin/nx-mcp"])

    result = _run(
        env(),
        "--source", "conexus", "--version", "7.18.0", "--legacy-venv", str(legacy),
    )

    assert result.returncode == 0, result.stderr
    assert legacy.exists(), "a live-holder migration must never touch the legacy tree"


def test_migration_never_reaps_the_legacy_tree_in_the_same_run(scaffold) -> None:
    """Even with ZERO holders at migration time, reap must be deferred to a
    later, separate pass -- migrate_legacy.sh never sources gc.sh."""
    tmp_path, tools, bin_dir, stub_bin, argv_log, env = scaffold
    legacy = _legacy_tree(tmp_path, receipt=None)
    _stub_ps(stub_bin, ["  101 /usr/bin/vim x"])  # nothing holds anything

    result = _run(
        env(),
        "--source", "conexus", "--version", "7.18.0", "--legacy-venv", str(legacy),
    )

    assert result.returncode == 0, result.stderr
    assert legacy.exists(), "the legacy tree was reaped inside the migrating run itself"
    assert (tools / "gen-legacy-uv-tool").is_symlink(), (
        "the pseudo-generation must be registered even with zero holders -- "
        "reap is deferred to a separate later pass, not skipped because it "
        "happened to be safe this time"
    )


# --------------------------------------------------------------------------
# the two-pass reap, via a SEPARATE nx_gc_generations call
# --------------------------------------------------------------------------

def _gc(tools: Path, stub_bin: Path, snippet: str, extra_env: dict | None = None):
    env = {
        "PATH": f"{stub_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(tools.parent / "home"),
        "NX_TOOLS_DIR": str(tools),
        "NX_BIN_DIR": str(tools.parent / "bin"),
    }
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", "-c", f'. "{_GC}"; {snippet}'],
        capture_output=True, text=True, env=env,
    )


def test_a_live_holder_on_the_legacy_tree_blocks_its_reap(scaffold) -> None:
    tmp_path, tools, bin_dir, stub_bin, argv_log, env = scaffold
    legacy = _legacy_tree(tmp_path, receipt=None)

    migrate = _run(env(), "--source", "conexus", "--version", "7.18.0",
                    "--legacy-venv", str(legacy))
    assert migrate.returncode == 0, migrate.stderr

    _stub_ps(stub_bin, [f"  909 {legacy}/bin/python {legacy}/bin/nx-mcp"])
    gc = _gc(tools, stub_bin, "nx_gc_generations --keep 2")

    assert gc.returncode == 0, gc.stderr
    assert legacy.exists(), "a held legacy tree was reaped on a later GC pass"


def test_reap_fires_on_a_later_pass_once_holders_are_gone(scaffold) -> None:
    tmp_path, tools, bin_dir, stub_bin, argv_log, env = scaffold
    legacy = _legacy_tree(tmp_path, receipt=None)

    migrate = _run(env(), "--source", "conexus", "--version", "7.18.0",
                    "--legacy-venv", str(legacy))
    assert migrate.returncode == 0, migrate.stderr
    assert legacy.exists(), "sanity: migration must not have reaped it already"

    _stub_ps(stub_bin, ["  101 /usr/bin/vim x"])  # nothing holds the legacy tree now
    gc = _gc(tools, stub_bin, "nx_gc_generations --keep 2")

    assert gc.returncode == 0, gc.stderr
    assert not legacy.exists(), (
        "the legacy tree was not reaped on a later pass once holders reached zero"
    )
    assert not (tools / "gen-legacy-uv-tool").exists(), (
        "the dangling pseudo-generation pointer was left behind after reap"
    )


# --------------------------------------------------------------------------
# no-op path
# --------------------------------------------------------------------------

def test_no_legacy_install_is_a_clean_no_op(scaffold) -> None:
    tmp_path, tools, bin_dir, stub_bin, argv_log, env = scaffold
    nonexistent = tmp_path / "uv-tool-dir" / "conexus"

    result = _run(
        env(),
        "--source", "conexus", "--version", "7.18.0", "--legacy-venv", str(nonexistent),
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", "no migration happened; nothing should print on stdout"
    assert not (tools / "current").exists(), "a no-op migration must not build or flip anything"
