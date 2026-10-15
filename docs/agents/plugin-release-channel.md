# Independent plugin release channel

Moved from AGENTS.md on 2026-10-14 (nexus-2ziaz); AGENTS.md keeps only the per-turn rules and links here. A `§ <heading>` reference below names a section of AGENTS.md or of a sibling file; [README.md](README.md) maps each heading to its file.

### Independent plugin release channel (RDR-197)

A plugin cut ships plugin-surface content (paths under `conexus/` and
`sn/`, plus `.claude-plugin/marketplace.json`) without a client release:
it moves the changed plugin's `source.ref` to an anchored tag of the
shape `plugin-v{X.Y.Z}-{n}`, where `X.Y.Z` is the CURRENT released
client version and `n` is the cut's sequence number. The number comes
from git's own tag list at cut time (one more than the highest existing
`plugin-v{X.Y.Z}-*` tag); there is NO stored counter file, so there is
nothing to reset and nothing to read from the wrong place. The channel
publishes nothing to PyPI: the `plugin-v*` tag fires a verify-only
workflow (`.github/workflows/plugin-release.yml`), and the wheel-surface
proof asserts the cut's range touches no wheel content. The cut itself
is `scripts/cut_plugin_release.py` (a script with tests, never a
checklist); the post-cut back-merge is `scripts/plugin_cut_back_merge.sh`,
never a bare merge (`conexus/PENDING_RELEASE.md` conflicts by
construction on every cut); invariants R and W live in
`scripts/plugin_channel.py`'s docstring.

**Usage discipline** (was this cut warranted?):
1. Cut when accumulated plugin functionality is worth shipping and no
   client release is imminent. A client release ships the same content
   for free.
2. An open release PR always wins. Never cut while one is in flight.
3. Batch related plugin work into one cut, exactly as the release
   cadence rules above batch client releases.
4. **Rehearse before the real cut, every time:**
   `tests/e2e/plugin-cut-rehearsal/run.sh` (container by default, `--host`
   for a fast loop) walks the whole flow against a FAKE origin — the real
   cut script with its battery, the cut PR's CI on a synthetic merge ref
   with a `pull_request` payload, merge + anchored tag, the tag workflow's
   steps, back-merge — and commits the source's uncommitted changes onto
   its clone's develop, so machinery fixes are rehearsed before they land.
   Must end `PLUGIN-CUT REHEARSAL PASSED`. The first real cut (2026-08-30,
   nexus-a2wmi.12) refused three times on machinery that only runs at cut
   time, each round costing a PR to main; every one was reachable here.

**Sunset trigger:** if two years pass with zero cuts, delete the
workflow, the parity test's anchored-form branch, and the cut script.
RDR-197 (`docs/rdr/rdr-197-plugin-only-release-channel.md`) is the
record that keeps the design recoverable.

`docs/` is NOT in the channel allowlist, deliberately: docs-only commits
outnumber plugin-surface commits about 12 to 1, and admitting them would
make nearly every cut a docs cut.
