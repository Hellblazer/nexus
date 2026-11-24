### Changed

- **The two shipped CI auras write a threads block per base.** A session serving `nexus-develop` now has `## aura: develop CI red (nexus-develop)` and `## aura: develop CI nudge (nexus-develop)` where it had `## aura: develop CI red` and `## aura: develop CI nudge`; the aura names in the state file and the channel notice are `ci_develop_red@nexus-develop` and `ci_develop_nudge@nexus-develop`. The config key is unchanged: `auras.ci_develop_red` and `auras.ci_develop_nudge` still turn them on.
- **The first expansion removes the old blocks.** A session that already had `## aura: develop CI red` or `## aura: develop CI nudge` in its threads entry has them removed from its own entry when the template first expands, so only the per-base heading remains.
