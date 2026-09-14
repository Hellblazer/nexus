# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wiring for the RDR-208 local-mode MVV (tests/e2e/rdr208-mvv, nexus-galkv.19).

The journey itself runs in a container and is not part of the suite; these
checks keep it runnable: every script parses, the image copies only what
run.sh stages, and the symbol run.sh uses to pick step 6's expectation still
exists in the drain hook (a rename would silently flip the expectation to
"defect reproduces" and the MVV would pass on a regression).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

_ROOT = Path(__file__).resolve().parents[1]
_DIR = _ROOT / "tests" / "e2e" / "rdr208-mvv"


@pytest.mark.parametrize("name", ["mvv_in_container.sh", "run.sh", "session_server.sh"])
def test_the_script_parses(name: str) -> None:
    proc = subprocess.run(["bash", "-n", str(_DIR / name)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_the_image_copies_only_what_run_sh_stages() -> None:
    run_sh = (_DIR / "run.sh").read_text(encoding="utf-8")
    staged = {"wheel/", "hooks/"} | set(re.findall(r'"\$HERE/([\w.]+)"', run_sh))
    copied: set[str] = set()
    for line in (_DIR / "Dockerfile").read_text(encoding="utf-8").splitlines():
        if line.startswith("COPY "):
            parts = [p for p in line.split()[1:] if not p.startswith("--")]
            copied.update(parts[:-1])
    assert copied, "no COPY lines found"
    assert copied <= staged, sorted(copied - staged)


def test_the_step6_expectation_names_a_real_hook_symbol() -> None:
    run_sh = (_DIR / "run.sh").read_text(encoding="utf-8")
    match = re.search(r"grep -q '(\w+)' \"\$STAGE/hooks/mailbox_drain.py\"", run_sh)
    assert match, "run.sh no longer derives step 6's expectation from the hook"
    hook = (_ROOT / "conexus" / "hooks" / "scripts" / "mailbox_drain.py").read_text(encoding="utf-8")
    assert f"def {match.group(1)}(" in hook
