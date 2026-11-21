# SPDX-License-Identifier: AGPL-3.0-or-later
"""The upgrade rehearsal's old-bundle scenario (nexus-f3yqd.2.23, release Step 6e).

An install first set up on conexus 8.2.0 or earlier keeps a PostgreSQL bundle without pg_background. Two starts
exercise the refresh from the user's seat: 8.2.0 then ``nx upgrade`` (the engine changes, the bundle with it), and
8.2.0 then ``install-binary <pin> --no-pg-bundle`` then ``nx upgrade`` (the engine is already the pinned one, so
only the bundle leg can fix it). These are the offline halves: the start list, the names and the judgement of what
the run saw. The run itself needs a network, uv and an engine.
"""
from __future__ import annotations

import importlib.util
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DRIVER = ROOT / "scripts" / "upgrade_rehearsal.py"


def _load():
    spec = importlib.util.spec_from_file_location("_upgrade_rehearsal_old_bundle", DRIVER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_upgrade_rehearsal_old_bundle"] = module
    spec.loader.exec_module(module)
    return module


ur = _load()

PUBLISHED = ["7.76.0", "7.98.0", "7.99.0", "8.0.0", "8.1.0", "8.2.0", "8.2.1", "8.2.2"]
CONTROL = Path("pg-bundle") / "bundle" / "share" / "postgresql" / "extension" / "pg_background.control"


def test_split_start_reads_a_variant_off_the_version():
    assert ur.split_start("8.2.0") == ("8.2.0", "")
    assert ur.split_start("8.2.0+engine-first") == ("8.2.0", "engine-first")


def test_both_adds_the_two_old_bundle_starts_for_a_candidate_above_8_2_0():
    got = ur.resolve_from_versions("both", PUBLISHED, candidate="8.3.0")
    assert got[:3] == ["8.2.2", "7.99.0", "7.76.0"]
    assert got[3:] == ["8.2.0", "8.2.0+engine-first"]


def test_an_old_bundle_start_is_not_listed_twice_when_it_is_the_newest_below():
    got = ur.resolve_from_versions("both", PUBLISHED[:6], candidate="8.2.1")
    assert got.count("8.2.0") == 1
    assert got[-1] == "8.2.0+engine-first"


def test_a_candidate_at_or_below_8_2_0_adds_no_old_bundle_start():
    assert all("8.2.0" not in v for v in ur.resolve_from_versions("both", PUBLISHED, candidate="8.1.0"))


def test_a_published_list_without_8_2_0_has_no_old_bundle_start():
    assert ur.resolve_from_versions("both", ["7.76.0", "7.99.0", "8.2.1"], candidate="8.3.0") == ["8.2.1", "7.99.0", "7.76.0"]


def test_a_single_engine_first_start_is_accepted_only_for_a_bundle_without_pg_background():
    assert ur.resolve_from_versions("8.2.0+engine-first", PUBLISHED, candidate="8.3.0") == ["8.2.0+engine-first"]
    with pytest.raises(ur.RehearsalError):
        ur.resolve_from_versions("8.2.2+engine-first", PUBLISHED, candidate="8.3.0")


def test_a_variant_start_takes_its_versions_ceiling_and_kind():
    assert ur.start_kind("8.2.0+engine-first") == ur.start_kind("8.2.0") == "newest-below"
    assert ur.upgrade_ceiling_for("8.2.0+engine-first") == ur.upgrade_ceiling_for("8.2.0")
    assert ur.pci_check_enabled(None, "8.2.0+engine-first", standin=False) is False


def test_the_engine_first_step_installs_the_pinned_engine_and_leaves_the_bundle():
    assert ur.engine_first_argv("/h/nx", "0.1.200") == [
        "/h/nx", "daemon", "service", "install-binary", "engine-service-v0.1.200", "--no-pg-bundle",
    ]


def test_bundle_has_pg_background_reads_the_install_s_extracted_tree(tmp_path):
    assert ur.bundle_has_pg_background(tmp_path) is False
    (tmp_path / CONTROL).parent.mkdir(parents=True)
    assert ur.bundle_has_pg_background(tmp_path) is False
    (tmp_path / CONTROL).write_text("default_version = '1.0'\n", encoding="utf-8")
    assert ur.bundle_has_pg_background(tmp_path) is True


CLEAN_DOCTOR = "Engine convergence: installed engine v0.1.200\nData moves: 1 open or failed: 1 working\n"


def _judge(**kw):
    base = dict(before=False, after=True, extension=True, doctor_text=CLEAN_DOCTOR, variant="")
    base.update(kw)
    return ur.bundle_refresh_findings(**base)


def test_an_old_bundle_that_was_refreshed_is_clean():
    assert _judge() == []
    assert _judge(variant="engine-first") == []


def test_a_start_whose_bundle_already_had_pg_background_exercised_nothing():
    assert any("already had pg_background" in f for f in _judge(before=True))


def test_an_unreadable_start_state_is_a_finding():
    assert any("could not be read" in f for f in _judge(before=None))


def test_a_bundle_still_without_the_control_file_after_nx_upgrade_is_a_finding():
    assert any("pg_background.control" in f for f in _judge(after=False))


def test_an_extension_that_is_not_in_the_database_is_a_finding_and_so_is_an_unread_one():
    assert any("pg_extension" in f for f in _judge(extension=False))
    assert any("pg_extension" in f for f in _judge(extension=None))


def test_a_blocked_move_in_doctor_is_a_finding():
    text = CLEAN_DOCTOR + "Data move repair-x: blocked: pg_background is not installed\n"
    assert any("blocked" in f for f in _judge(doctor_text=text))


def test_a_pending_bundle_row_in_doctor_is_a_finding():
    text = CLEAN_DOCTOR + "PostgreSQL bundle: the extracted PostgreSQL bundle has no pg_background\n"
    assert any("PostgreSQL bundle" in f for f in _judge(doctor_text=text))


def test_the_idle_wait_needs_consecutive_quiet_reads_and_gives_up_at_its_bound():
    reads = iter(["work", None, "work", None, None, None])
    now = {"t": 0.0}

    def sleep(s):
        now["t"] += s

    assert ur.wait_until_idle(lambda: next(reads), polls=3, poll_s=1.0, bound_s=60.0, clock=lambda: now["t"], sleep=sleep)
    assert not ur.wait_until_idle(lambda: "an index build", polls=3, poll_s=10.0, bound_s=30.0, clock=lambda: now["t"], sleep=sleep)


def test_the_busy_rule_is_the_products_own(tmp_path):
    """busy_per_product runs the candidate's engine_busy_reason with its interpreter: here a stand-in interpreter whose
    one-liner answer is the rule's, to pin the contract (empty is idle, text is busy, a failure is busy)."""
    fake = tmp_path / "python"
    fake.write_text("#!/bin/sh\n" + 'case "$FAKE" in idle) echo "log line"; echo "NX_BUSY=";; busy) echo "NX_BUSY=the engine reports background work (x)";; mute) echo "log line";; *) echo oops >&2; exit 3;; esac\n', encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    assert ur.busy_per_product(fake, tmp_path, {"FAKE": "idle"}) is None
    assert "background work" in ur.busy_per_product(fake, tmp_path, {"FAKE": "busy"})
    assert "exited 3" in ur.busy_per_product(fake, tmp_path, {"FAKE": "boom"})
    assert "no answer" in ur.busy_per_product(fake, tmp_path, {"FAKE": "mute"})  # a log line alone is not an answer
    assert "could not run" in ur.busy_per_product(tmp_path / "missing", tmp_path, {})


def test_the_busy_rule_one_liner_runs_against_the_real_module(tmp_path):
    """F33: the one-liner calls the real engine_busy_reason with moves_can_run (a signature drift would exit 1 here,
    and the leg would read busy for its whole bound). No service in this config dir, so the answer is idle."""
    env = {"HOME": str(tmp_path), "NEXUS_CONFIG_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"}
    assert ur.busy_per_product(Path(sys.executable), tmp_path, env) is None


def test_the_installed_engine_version_is_read_from_the_receipt(tmp_path):
    assert ur.installed_engine_version(tmp_path) is None
    (tmp_path / "service").mkdir()
    (tmp_path / "service" / "nexus-service.meta.json").write_text('{"version": "0.1.199"}', encoding="utf-8")
    assert ur.installed_engine_version(tmp_path) == "0.1.199"


def test_the_engine_first_leg_must_be_untouched_before_nx_upgrade():
    assert ur.engine_first_findings(bundle_present=False, engine="0.1.199", pin="0.1.199") == []
    assert any("already has pg_background" in f for f in ur.engine_first_findings(bundle_present=True, engine="0.1.199", pin="0.1.199"))
    assert any("could not be read" in f for f in ur.engine_first_findings(bundle_present=None, engine="0.1.199", pin="0.1.199"))
    assert any("not the candidate's pin" in f for f in ur.engine_first_findings(bundle_present=False, engine="0.1.189", pin="0.1.199"))
    assert any("not the candidate's pin" in f for f in ur.engine_first_findings(bundle_present=False, engine=None, pin=None))
