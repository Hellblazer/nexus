# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ec37o residual 2: a repository's ``OPENSSL_CONF`` and ``SSLKEYLOGFILE`` are dropped BEFORE OpenSSL starts.

OpenSSL reads ``OPENSSL_CONF`` when it initialises, which any import of ``hashlib``, ``ssl``, ``http.client`` or
``urllib.request`` triggers; a configuration can load an engine or a provider, which is code. Dropping the variable after those
imports is too late. So every script ``hooks.json`` launches begins with the two pops, before any import beyond ``os`` and
``sys`` (and the ``__future__`` line, which loads nothing).

* The lint reads the first statements of each such script (the list comes from ``hooks.json``, so a new hook is covered).
* The behaviour test is the one that matters: a configuration that demands FIPS-approved providers (``default_properties =
  fips=yes``) with none installed makes every digest fetch fail in a process that read it. The control shows the configuration
  bites; after a hook's imports a digest must still be fetchable, so OpenSSL never read the file. (A legacy-provider configuration
  would be the more direct probe, but the uv and distribution builds this suite runs on ship no legacy provider module.)
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "conexus"
NAMES = ("SSLKEYLOGFILE", "OPENSSL_CONF")
#: statements that may stand before the two pops: the docstring, the __future__ line, and the two imports that load no OpenSSL
HARMLESS_MODULES = frozenset({"os", "sys"})

#: demands FIPS properties for every fetch; with no FIPS provider installed, no digest can be fetched by a process that read it
BLOCKING_CONF = """openssl_conf = openssl_init

[openssl_init]
alg_section = evp_sect

[evp_sect]
default_properties = fips=yes
"""

#: what a child prints: whether the OpenSSL digest sha256 can be fetched (``_hashlib`` has no fallback to the built-in one)
PROBE = "import _hashlib\ntry:\n    _hashlib.new('sha256'); print('digest-ok')\nexcept Exception:\n    print('digest-blocked')\n"


def hook_scripts() -> list[Path]:
    """Every ``.py`` ``hooks.json`` names, resolved under the plugin root."""
    text = (PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8")
    found = sorted(set(re.findall(r"\$\{CLAUDE_PLUGIN_ROOT\}/([\w/.\-]+\.py)", text)))
    return [PLUGIN / rel for rel in found]


def pops_before_imports(source: str) -> str | None:
    """None when *source* pops both names before any import beyond ``os`` and ``sys``; else why not."""
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue  # the docstring
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            continue
        if isinstance(node, ast.Import) and all(a.name in HARMLESS_MODULES for a in node.names):
            continue
        if isinstance(node, ast.For):
            seg = ast.get_source_segment(source, node) or ""
            if all(n in seg for n in NAMES) and ".pop(" in seg:
                return None
            return "a loop stands before the pops but does not pop both names"
        return f"line {node.lineno} ({type(node).__name__}) comes before the pops"
    return "the two pops are missing"


@pytest.mark.lint
@pytest.mark.parametrize("script", hook_scripts(), ids=lambda p: p.name)
def test_a_hook_script_drops_both_variables_before_its_first_import(script: Path) -> None:
    why = pops_before_imports(script.read_text(encoding="utf-8"))
    assert why is None, f"{script.relative_to(ROOT)}: {why}"


@pytest.mark.lint
def test_the_hook_list_is_not_vacuous() -> None:
    names = {p.name for p in hook_scripts()}
    assert len(names) >= 11, names
    assert {"mailbox_drain.py", "settings_notice.py", "stop_wake_guard.py", "credential_print_guard.py",
            "destructive_action_advice.py", "subagent_git_write_requires_orchestrator.py", "nx_launch.py"} <= names
    assert all(p.is_file() for p in hook_scripts())


GOOD = '"""d"""\nfrom __future__ import annotations\n\nimport os\nimport sys\n\nfor _n in ("SSLKEYLOGFILE", "OPENSSL_CONF"):\n    os.environ.pop(_n, None)\n\nimport hashlib\n'


@pytest.mark.lint
@pytest.mark.parametrize(
    ("source", "ok"),
    [
        (GOOD, True),
        (GOOD.replace('"""d"""\n', ""), True),
        ("import hashlib\n" + GOOD, False),
        (GOOD.replace("import os\nimport sys\n", "import os\nimport json\nimport sys\n"), False),
        (GOOD.replace("import os\nimport sys\n", "import os\nfrom pathlib import Path\n"), False),
        ('"""d"""\nimport os\nimport sys\nimport http.client\n\nfor _n in ("SSLKEYLOGFILE", "OPENSSL_CONF"):\n    os.environ.pop(_n, None)\n', False),
        (GOOD.replace('"OPENSSL_CONF"', '"OTHER"'), False),
        ('"""d"""\nimport os\nos.environ.pop("SSLKEYLOGFILE", None)\n', False),
        ('"""d"""\nimport os\n', False),
    ],
)
def test_the_lint_sees_a_late_or_partial_pop(source: str, ok: bool) -> None:
    assert (pops_before_imports(source) is None) is ok


def _child(code: str, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code, *args], capture_output=True, text=True, env=env, timeout=120, encoding="utf-8", errors="replace",
    )


@pytest.fixture
def blocking_env(tmp_path: Path) -> dict[str, str]:
    conf = tmp_path / "blocking.cnf"
    conf.write_text(BLOCKING_CONF, encoding="ascii")
    base = {k: v for k, v in os.environ.items() if k.upper() not in NAMES}
    return {**base, "OPENSSL_CONF": str(conf), "SSLKEYLOGFILE": str(tmp_path / "keys.log")}


def _control_bites(env: dict[str, str]) -> bool:
    return _child(PROBE, env).stdout.strip() == "digest-blocked"


def test_the_control_a_process_that_reads_the_configuration_cannot_fetch_a_digest(blocking_env) -> None:
    """Non-vacuity: the configuration makes the digest fetch fail in a process that reads it, and does not in one that does not."""
    assert _child(PROBE, blocking_env).stdout.strip() == "digest-blocked"
    clean = {k: v for k, v in blocking_env.items() if k not in NAMES}
    assert _child(PROBE, clean).stdout.strip() == "digest-ok"


_LOAD = (
    "import importlib.util, os, sys\n"
    "script = sys.argv[1]\n"
    "sys.path.insert(0, os.path.dirname(script))\n"
    "spec = importlib.util.spec_from_file_location('hook_under_test', script)\n"
    "mod = importlib.util.module_from_spec(spec)\n"
    "sys.modules['hook_under_test'] = mod\n"
    "spec.loader.exec_module(mod)\n"
    + PROBE +
    "print(os.environ.get('OPENSSL_CONF'), os.environ.get('SSLKEYLOGFILE'))\n"
)


@pytest.mark.parametrize("script", hook_scripts(), ids=lambda p: p.name)
def test_openssl_never_reads_a_repo_set_configuration_in_a_hook(script: Path, blocking_env, tmp_path) -> None:
    assert _control_bites(blocking_env)  # a control that stopped biting fails here, it is never a pass
    proc = subprocess.run(
        [sys.executable, "-c", _LOAD, str(script)], capture_output=True, text=True, env=blocking_env, timeout=120,
        encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == 0, (proc.returncode, proc.stdout[-300:], proc.stderr[-400:])
    assert proc.stdout.split() == ["digest-ok", "None", "None"], proc.stdout
    assert not (tmp_path / "keys.log").exists()


def test_openssl_never_reads_it_in_a_process_that_imports_nexus(blocking_env) -> None:
    assert _control_bites(blocking_env)
    proc = _child("import nexus\n" + PROBE, blocking_env)
    assert proc.returncode == 0 and proc.stdout.strip() == "digest-ok", (proc.stdout, proc.stderr[-300:])
