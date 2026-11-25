# SPDX-License-Identifier: AGPL-3.0-or-later
"""The audience fixtures (nexus-sby49.3, the D8 fixture convention), a pytest plugin registered in ``tests/conftest.py``.

The conexus plugin's hooks and the src hooks answer from who the session is for: a checkout that carries the tracked nexus
marker, or an OS account with the maintainer flag, is the maintainer's; anything else is a user's. A maintainer's laptop has
the flag, so a test that did not pin the account passed there and failed in CI. These fixtures pin it:

* ``_src_audience_is_a_user_by_default`` (autouse): every test starts as a USER on an account with no flag;
* ``src_maintainer_flag`` and ``plugin_account(flagged=True)``: an account that carries the flag (src helper, plugin helper);
* ``nexus_marked_repo``: a directory shaped like a nexus checkout, to pass as the payload's ``cwd``.

A hook run as a CHILD process cannot reach the in-process seam: it goes through ``tests/hooks/audience_harness.py``.
It lives in its own module because ``tests/conftest.py`` is over its size ceiling (``scripts/file_size_ratchet.py``).
"""
from __future__ import annotations

import contextlib
import importlib
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def _unflagged_account_home(tmp_path_factory):
    """An account home with no maintainer flag (nexus-sby49.3)."""
    return tmp_path_factory.mktemp("acct-home-unflagged")


@pytest.fixture(autouse=True)
def _src_audience_is_a_user_by_default(monkeypatch, _unflagged_account_home):
    """The src copy of the audience helper (``nexus.hooks._audience``) answers from the OS account's home, and a
    maintainer's laptop has the flag there, so a hook test that did not pin it passed on the laptop and failed in CI
    (or the other way round). Every test starts as a USER on an account with no flag; a test of nexus's own
    behaviour opts in with ``src_maintainer_flag`` or a checkout from ``nexus_marked_repo``. A test that runs a hook
    as a subprocess cannot use this seam (it is in-process only): it passes the home through the driver prelude in
    ``tests/hooks/audience_harness.py``. The D8 fixture convention: a bare ``git init`` is a user's repo."""
    from nexus.hooks import _audience  # noqa: PLC0415 — the module the src hooks import

    monkeypatch.setattr(_audience, "_ACCOUNT_HOME_OVERRIDE", str(_unflagged_account_home))
    # The payload's cwd falls back to CLAUDE_PROJECT_DIR, so a test with no cwd, launched from a nexus session, would see a nexus repo.
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)


@pytest.fixture
def src_maintainer_flag(monkeypatch, tmp_path_factory):
    """The src helper sees an account that carries the maintainer flag."""
    from nexus.hooks import _audience  # noqa: PLC0415

    home = tmp_path_factory.mktemp("acct-home-flagged")
    flag = home.joinpath(*_audience.FLAG_PARTS)
    flag.parent.mkdir(parents=True)
    flag.write_text("")
    monkeypatch.setattr(_audience, "_ACCOUNT_HOME_OVERRIDE", str(home))
    return home


@pytest.fixture
def nexus_marked_repo(tmp_path):
    """A directory shaped like a nexus checkout: a ``.git`` and the tracked Phase 2 marker. Pass it as the payload's ``cwd``."""
    repo = tmp_path / "nexus-checkout"
    (repo / ".git").mkdir(parents=True)
    marker = repo / ".nexus" / "dev-process.json"
    marker.parent.mkdir()
    marker.write_text("{}")
    return repo


def _plugin_audience():
    """The PLUGIN copy of the audience helper, imported the way the hook scripts import it (``import _audience``)."""
    import sys  # noqa: PLC0415

    scripts = str(Path(__file__).resolve().parents[1] / "conexus" / "hooks" / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import _audience  # noqa: PLC0415 — the module name the plugin scripts use

    return _audience


@pytest.fixture
def plugin_account(monkeypatch, tmp_path_factory):
    """The plugin copy of the audience helper (``conexus/hooks/scripts/_audience.py``), pinned to an account with no
    flag; call ``plugin_account(flagged=True)`` to give it the flag. In-process only: a hook script run as a child
    process goes through ``tests/hooks/audience_harness.py``."""
    audience = _plugin_audience()

    def pin(*, flagged: bool = False):
        home = tmp_path_factory.mktemp("plugin-acct-flagged" if flagged else "plugin-acct-unflagged")
        if flagged:
            flag = home.joinpath(*audience.FLAG_PARTS)
            flag.parent.mkdir(parents=True)
            flag.write_text("")
        monkeypatch.setattr(audience, "_ACCOUNT_HOME_OVERRIDE", str(home))
        return home

    pin()
    return pin


@contextlib.contextmanager
def broken_audience_import(module):
    """Reload *module* with ``nexus.hooks._audience`` unimportable (its module-level fallbacks bind) and yield it; on exit put
    the real helper back and reload *module* again. The ``sys.modules`` entry is restored by the monkeypatch itself, never
    popped and re-set by hand: a second module object for the helper would split the seam between the modules that bound the
    first and the tests that pin the second."""
    mp = pytest.MonkeyPatch()
    try:
        mp.setitem(sys.modules, "nexus.hooks._audience", None)  # an import of it now raises ImportError
        yield importlib.reload(module)
    finally:
        mp.undo()
        importlib.reload(module)
