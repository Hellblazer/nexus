### Added

- **The first shipped aura, `ci_develop_red`, off by default** (nexus-mdbds.12.1, RDR-249 Phase 3). When a develop CI run goes red, a session that has opted in gets an `aura: develop CI red` block in its threads entry: "develop red at <short sha>: diagnose before the next push (worktrees rule 7)". The next green develop run removes it. To opt in, put `auras: {ci_develop_red: on}` in `~/.config/nexus/config.yml`. `NX_AURAS_OFF=ci_develop_red` turns it off again. The procedure it runs, `ci_red_notice`, reads and writes nothing.
