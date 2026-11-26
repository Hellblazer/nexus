# SPDX-License-Identifier: AGPL-3.0-or-later
"""The audience fixtures (nexus-sby49.3, the D8 fixture convention; reworked in nexus-sby49.4), a pytest plugin registered in
``tests/conftest.py``.

The conexus plugin's hooks and the src hooks read two settings files and nothing about who the user is: a project's tracked
``.nexus/project.json`` (tighten-only) and the OS account's ``~/.config/nexus/conexus.json``. A developer's laptop may have the
second, so a test that did not pin the account passed there and failed in CI. These fixtures pin it. The convention is three
fixtures: **plain** (the default), **project-strict** and **user-strict**.

* ``_src_audience_is_plain_by_default`` (autouse): every test starts on an account with no settings file, so every setting
  reads its default;
* ``src_user_settings(**settings)`` (and ``src_user_strict``): an account whose ``conexus.json`` carries *settings* (the src
  helper);
* ``project_strict_repo``: a directory shaped like a project whose tracked ``.nexus/project.json`` turns the strict subagent
  git guard on, to pass as the payload's ``cwd``; ``project_settings_repo(**settings)`` writes other keys;
* ``plugin_account(**settings)``: the same for the plugin copy of the helper.

A hook run as a CHILD process cannot reach the in-process seam: it goes through ``tests/hooks/audience_harness.py``.
It lives in its own module because ``tests/conftest.py`` is over its size ceiling (``scripts/file_size_ratchet.py``).
"""
from __future__ import annotations

import contextlib
import importlib
import json
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def _plain_account_home(tmp_path_factory):
    """An account home with no settings file."""
    return tmp_path_factory.mktemp("acct-home-plain")


@pytest.fixture(autouse=True)
def _src_audience_is_plain_by_default(monkeypatch, _plain_account_home):
    """The src copy of the audience helper (``nexus.hooks._audience``) reads the OS account's home, and a developer's laptop may
    have a ``conexus.json`` there, so a hook test that did not pin it passed on the laptop and failed in CI (or the other way
    round). Every test starts with an account that has no settings file; a test of a setting opts in with
    ``src_user_settings`` or a repository from ``project_strict_repo``. A test that runs a hook as a subprocess cannot use this
    seam (it is in-process only): it passes the home through the driver prelude in ``tests/hooks/audience_harness.py``. The
    fixture convention: a bare ``git init`` is a plain repo."""
    from nexus.hooks import _audience  # noqa: PLC0415 — the module the src hooks import

    monkeypatch.setattr(_audience, "_ACCOUNT_HOME_OVERRIDE", str(_plain_account_home))
    # The payload's cwd falls back to CLAUDE_PROJECT_DIR, so a test with no cwd, launched from a session in a project that has
    # its own settings file, would read that project's settings.
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)


def _write_user_settings(home: Path, settings: dict) -> Path:
    path = home.joinpath(*(".config", "nexus", "conexus.json"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings))
    return home


@pytest.fixture
def src_user_settings(monkeypatch, tmp_path_factory):
    """``src_user_settings(**settings)``: the src helper sees an account whose ``conexus.json`` carries *settings*."""
    from nexus.hooks import _audience  # noqa: PLC0415

    def pin(**settings):
        home = tmp_path_factory.mktemp("acct-home-settings")
        _write_user_settings(home, settings)
        monkeypatch.setattr(_audience, "_ACCOUNT_HOME_OVERRIDE", str(home))
        return home

    return pin


@pytest.fixture
def src_user_strict(src_user_settings):
    """The src helper sees an account whose user settings turn the strict subagent git guard on (the user-strict fixture)."""
    return src_user_settings(subagent_git="strict")


@pytest.fixture
def project_settings_repo(tmp_path):
    """``project_settings_repo(**settings)``: a directory with a ``.git`` and a tracked ``.nexus/project.json`` carrying
    *settings*. Pass it as the payload's ``cwd``."""
    counter = iter(range(1000))

    def make(**settings):
        repo = tmp_path / f"project-{next(counter)}"
        (repo / ".git").mkdir(parents=True)
        (repo / ".nexus").mkdir()
        (repo / ".nexus" / "project.json").write_text(json.dumps({"schema": 1, **settings}))
        return repo

    return make


@pytest.fixture
def project_strict_repo(project_settings_repo):
    """A project whose tracked settings turn the strict subagent git guard on (the project-strict fixture)."""
    return project_settings_repo(subagent_git="strict")


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
    """The plugin copy of the audience helper (``conexus/hooks/scripts/_audience.py``), pinned to an account with no settings
    file; call ``plugin_account(**settings)`` to give it a ``conexus.json`` with *settings*. In-process only: a hook script run
    as a child process goes through ``tests/hooks/audience_harness.py``."""
    audience = _plugin_audience()

    def pin(**settings):
        home = tmp_path_factory.mktemp("plugin-acct-settings" if settings else "plugin-acct-plain")
        if settings:
            _write_user_settings(home, settings)
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
