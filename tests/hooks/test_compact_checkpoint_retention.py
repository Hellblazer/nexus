# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``compact_checkpoint.py``: what another session's pre prunes (bead nexus-ct9sv).

Split from ``test_compact_checkpoint.py`` (file-size ceiling), whose ``mod`` and ``env`` fixtures and
helpers it imports. The contract pinned here:

- another session's pre prunes a used checkpoint, an archive, a sidecar and a temp file past 48 h, and
  an unused current checkpoint only past 7 days (``_MAX_AGE_S``);
- unused means no summary, not in the sidecar, a usable ``written:`` stamp (the test inject applies);
- every verdict is taken before any delete, young files are not read, and a file rewritten after its
  verdict is not deleted.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import pytest

from tests.hooks.test_compact_checkpoint import (  # noqa: F401 (env and mod are fixtures)
    OTHER_SID,
    env,
    mod,
    pre_payload,
    restamp,
    run,
)


class TestIdleCheckpointRetention:
    """nexus-ct9sv: another session's pre must not prune an idle session's unused checkpoint.

    Session OTHER_SID went idle and its compaction is deferred; SID's pre runs meanwhile. A
    checkpoint no compaction has used is kept until ``_MAX_AGE_S`` (7 days); a used one, and
    every archive, keep the 48 h prune. Ages are set on the file's mtime and its own ``written:``
    stamp relative to now, with margins of a day or more either side of each limit.
    """

    DAY = 86400.0

    @staticmethod
    def _age(path: Path, seconds: float) -> None:
        """Make *path* look *seconds* old to both the prune (mtime) and inject (``written:``)."""
        restamp(path, seconds)
        then = time.time() - seconds
        os.utime(path, (then, then))

    def _idle_checkpoint(self, mod, env, monkeypatch, days: float) -> Path:
        run(mod, monkeypatch, "pre", pre_payload(session_id=OTHER_SID))
        path = env.checkpoints / f"{OTHER_SID}.md"
        self._age(path, days * self.DAY)
        return path

    def test_the_limits_are_two_days_for_the_prune_and_seven_for_an_unused_checkpoint(self, mod) -> None:
        assert mod._PRUNE_AFTER_S == 2 * self.DAY
        assert mod._MAX_AGE_S == 7 * self.DAY

    def test_an_unused_checkpoint_three_days_old_survives_another_sessions_pre_and_is_restored(
        self, mod, env, monkeypatch
    ) -> None:
        path = self._idle_checkpoint(mod, env, monkeypatch, days=3)
        run(mod, monkeypatch, "pre", pre_payload())  # session SID, not OTHER_SID
        assert path.exists(), "another session's pre pruned an idle session's unused checkpoint"
        code, out = run(mod, monkeypatch, "inject", {"session_id": OTHER_SID, "source": "compact"})
        assert code == 0
        context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        assert "P2 Auto-continuation" in context and "wrote this working-state checkpoint 72 h" in context
        assert "no working-state checkpoint" not in context

    @pytest.mark.parametrize("use", ["summary", "injected"])
    def test_a_used_checkpoint_older_than_two_days_is_pruned_by_another_sessions_pre(
        self, mod, env, monkeypatch, use
    ) -> None:
        path = self._idle_checkpoint(mod, env, monkeypatch, days=3)  # within _MAX_AGE_S, so post and inject use it
        sidecar = env.checkpoints / f"{OTHER_SID}.injected"
        if use == "summary":
            run(mod, monkeypatch, "post", {"session_id": OTHER_SID, "compact_summary": "the summary"})
        else:
            run(mod, monkeypatch, "inject", {"session_id": OTHER_SID, "source": "compact"})
            assert sidecar.exists()
        then = time.time() - 3 * self.DAY
        os.utime(path, (then, then))  # post rewrote the file; its stamp (the one the sidecar holds) stays as it was
        if sidecar.exists():
            os.utime(sidecar, (then, then))  # the sidecar goes too; the verdict must not depend on its survival
        run(mod, monkeypatch, "pre", pre_payload())
        assert not path.exists(), f"a checkpoint used ({use}) and three days old survived the prune"
        assert not sidecar.exists()

    def test_an_unused_checkpoint_older_than_seven_days_is_pruned(self, mod, env, monkeypatch) -> None:
        young = self._idle_checkpoint(mod, env, monkeypatch, days=6)
        run(mod, monkeypatch, "pre", pre_payload())
        assert young.exists(), "an unused checkpoint inside the seven days was pruned"
        old = self._idle_checkpoint(mod, env, monkeypatch, days=8)
        run(mod, monkeypatch, "pre", pre_payload())
        assert not old.exists(), "an unused checkpoint past the seven days survived"

    def test_the_verdict_does_not_depend_on_the_order_the_directory_lists_its_files(
        self, mod, env, monkeypatch
    ) -> None:
        """The sidecar is listed BEFORE its checkpoint, so deleting inline would remove it first and
        make the used checkpoint look unused (kept to seven days). Every verdict comes before any delete."""
        path = self._idle_checkpoint(mod, env, monkeypatch, days=3)
        sidecar = env.checkpoints / f"{OTHER_SID}.injected"
        run(mod, monkeypatch, "inject", {"session_id": OTHER_SID, "source": "compact"})
        then = time.time() - 3 * self.DAY
        os.utime(path, (then, then))
        os.utime(sidecar, (then, then))
        real_iterdir = Path.iterdir

        def sidecar_first(self_path):
            entries = list(real_iterdir(self_path))
            if self_path == env.checkpoints:
                entries.sort(key=lambda p: (not p.name.endswith(".injected"), p.name))
                assert [p.name for p in entries][0] == sidecar.name
            return iter(entries)

        with monkeypatch.context() as patched:  # Path.iterdir is process-wide: only for the one pre
            patched.setattr(Path, "iterdir", sidecar_first)
            run(mod, monkeypatch, "pre", pre_payload())
        assert not sidecar.exists() and not path.exists(), "the used checkpoint was judged after its sidecar was gone"

    def test_a_temp_file_with_a_stamp_is_pruned_at_two_days(self, mod, env, monkeypatch) -> None:
        """A ``.tmp-*.md`` left by a killed write can carry a stamp and no summary; it is not a checkpoint."""
        path = self._idle_checkpoint(mod, env, monkeypatch, days=3)
        tmp = env.checkpoints / ".tmp-abcd1234.md"
        tmp.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        then = time.time() - 3 * self.DAY
        os.utime(tmp, (then, then))
        run(mod, monkeypatch, "pre", pre_payload())
        assert not tmp.exists(), "a temp file was kept to seven days as if it were an unused checkpoint"
        assert path.exists()

    def test_a_file_without_a_stamp_is_pruned_at_two_days(self, mod, env, monkeypatch) -> None:
        env.checkpoints.mkdir(parents=True)
        bare = env.checkpoints / f"{OTHER_SID}.md"
        bare.write_text("# Compaction checkpoint\n\n- session: x\n\n## Working state\n\nno stamp\n", encoding="utf-8")
        then = time.time() - 3 * self.DAY
        os.utime(bare, (then, then))
        run(mod, monkeypatch, "pre", pre_payload())
        assert not bare.exists(), "a file with nothing to restore was kept to seven days"

    def test_a_file_with_an_unparseable_stamp_is_pruned_at_two_days(self, mod, env, monkeypatch) -> None:
        """Inject restores only what ``_written_age`` accepts, so the prune keeps only that."""
        path = self._idle_checkpoint(mod, env, monkeypatch, days=3)
        text, n = re.subn(r"(?m)^- written: .*$", "- written: 2026-13-45T99:99:99+00:00", path.read_text(encoding="utf-8"), count=1)
        assert n == 1
        path.write_text(text, encoding="utf-8")
        then = time.time() - 3 * self.DAY
        os.utime(path, (then, then))
        run(mod, monkeypatch, "pre", pre_payload())
        assert not path.exists(), "a checkpoint inject would not restore was kept to seven days"

    def test_a_file_rewritten_after_the_verdict_is_not_deleted(self, mod, env, monkeypatch) -> None:
        """Another session's pre (or a post) rewrites the file between the verdict and the unlink."""
        path = self._idle_checkpoint(mod, env, monkeypatch, days=0)
        run(mod, monkeypatch, "post", {"session_id": OTHER_SID, "compact_summary": "s"})  # used
        then = time.time() - 3 * self.DAY
        os.utime(path, (then, then))
        real = mod._is_unused_current

        def judge_then_rewrite(candidate):
            verdict = real(candidate)
            if candidate == path:
                now = time.time()
                os.utime(candidate, (now, now))  # the rewrite lands after the verdict, before the unlink
            return verdict

        monkeypatch.setattr(mod, "_is_unused_current", judge_then_rewrite)
        run(mod, monkeypatch, "pre", pre_payload())
        assert path.exists(), "a file rewritten since it was judged was deleted on the old verdict"

    def test_a_young_file_is_not_read_by_the_prune(self, mod, env, monkeypatch) -> None:
        self._idle_checkpoint(mod, env, monkeypatch, days=1)
        calls: list[Path] = []
        real = mod._is_unused_current
        monkeypatch.setattr(mod, "_is_unused_current", lambda p: (calls.append(p), real(p))[1])
        run(mod, monkeypatch, "pre", pre_payload())
        assert calls == [], f"the prune read files younger than two days: {calls}"

    def test_an_archive_older_than_two_days_is_pruned_whether_or_not_it_was_used(self, mod, env, monkeypatch) -> None:
        env.checkpoints.mkdir(parents=True)
        archive = env.checkpoints / f"{OTHER_SID}.20260101T000000Z.md"
        archive.write_text("# Compaction checkpoint\n\n- written: 2026-01-01T00:00:00+00:00\n", encoding="utf-8")
        then = time.time() - 3 * self.DAY
        os.utime(archive, (then, then))
        run(mod, monkeypatch, "pre", pre_payload())
        assert not archive.exists(), "an archive survived past two days: it is not a current checkpoint"
