# Plugin-only releases

The independent plugin channel of RDR-197 (anchored `plugin-vX.Y.Z-n` tags, a cut script, a verify-only tag workflow) was retired on 2026-10-26 (nexus-rbmja.8). It could not carry forward wheel references (nexus-konsk, nexus-semdv). RDR-197 stays as the record.

Plugin-surface content (`conexus/`, `sn/`, `tool-spill/`, `.claude-plugin/marketplace.json`) now ships ahead of `src/` work as a patch release on the normal client tag form: the plugin-only fast lane. The `release` skill's "Plugin-only fast lane" section is the only description of the steps. The pieces it uses:

- `scripts/check_plugin_only_eligibility.py` refuses a diff that leaves the plugin surface, reusing the allowlist in `scripts/plugin_channel.py`.
- `tests/e2e/release-battery.sh --plugin-only` runs the legs a plugin-only diff can change and reports the rest as skipped.
- `tests/e2e/plugin-delivery-proof.sh` proves, after publish, that an install of the previous release receives the new plugin files through Claude Code's normal plugin update.
- `conexus/PENDING_RELEASE.md` is the cherry-pick list, and `tests/test_plugin_release_drift_ledger.py` keeps it honest.

**A new plugin enters the marketplace on develop first.** Its `marketplace.json` entry carries the current client version and the client tag as `source.ref`, a tag that does not hold its directory yet; the drift ledger keeps that honest, because every file on the plugin's surface differs from the pinned tag and so is declared in `conexus/PENDING_RELEASE.md` until a release moves the ref. A directory that is not a plugin name needs both `ALLOWED_PREFIXES` and `PLUGIN_BY_ALLOWLIST_PREFIX` in `scripts/plugin_channel.py` (conexus-tool-spill installs from `tool-spill/`).

`docs/` is NOT in the allowlist, deliberately: docs-only commits outnumber plugin-surface commits about 12 to 1, and admitting them would make nearly every plugin-only release a docs release.
