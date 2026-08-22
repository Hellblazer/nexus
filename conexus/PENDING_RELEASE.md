# Pending release: plugin changes that are NOT live yet

`.claude-plugin/marketplace.json` pins `plugins[].source.ref` to an immutable
release tag. Claude Code loads this plugin's hooks, commands, skills, and agents
from **that tag**, not from your working tree. So every change below is merged
on `develop` and **inert in every running session** until the next release ships
and users install it.

This file is the acknowledgement ledger for that gap. It exists because the gap
is otherwise invisible: on 2026-07-25 a subagent ran `git stash -u` in a shared
tree and the guard that covers exactly that verb did not fire, because the
coverage had landed hours earlier and the installed plugin was still `v6.18.1`.
Three guards had been merged, closed as "mechanized", and were protecting
nothing.

**Rules, enforced by `tests/test_plugin_release_drift_ledger.py`:**

- Every file under the behavioural surface that differs from the pinned tag MUST
  be listed here. Adding a guard without declaring it fails the suite.
- When a release ships and the pin advances, drift goes to zero and this list
  MUST be emptied. A stale entry also fails the suite, so the ledger cannot
  quietly become fiction.
- Do NOT "fix" a failure by deleting entries. The entry is the honest statement
  that the thing is not yet live.

**Do not use this to justify skipping a release.** If a guard matters enough to
mechanize, it matters enough to ship.

---


## Awaiting the next release (pinned: v7.14.0)

- `conexus/skills/nexus/reference.md` —
  nexus-nyry9.19 (RDR-196 .p3a): the `nx_answer` parameter table's
  `budget_usd` row no longer documents the unmeasured `0.25` default; it
  now reads `None` = "use `budget_default.DERIVED_BUDGET_USD`" (itself
  unset until a sufficient post-flip derivation is recorded), enforcement
  landing in .p3c. Docs-only drift: an installed plugin at v7.14.0 shows
  a default the 7.15.0 tool no longer has.

- `conexus/skills/using-nx-skills/SKILL.md` —
  Consolidation of a duplicated directive (Sam, 2026-08-21). The
  sequential-thinking rule lived in BOTH this skill and the user-level
  CLAUDE.md in near-verbatim form, including the same 2026-08-19 evidence
  date. Per CLAUDE.md's own precedence rule the plugin owns workflow
  routing, so the skill is the single source; the one sentence the skill
  lacked — "the thought is the record: it is what reviewers, siblings and
  the census can see; internal reasoning is not" — moves here, and
  CLAUDE.md reduces to a pointer. Docs-only drift: a session on the
  pinned v7.14.0 plugin still reads the skill without that sentence.
- `conexus/resources/rdr/REGISTER.md`: nexus-3fab5 NEW prose register for
  the RDR lifecycle (named reader per stage, define-jargon-on-first-use,
  simplified-never-simplistic); copied to $RDR_DIR at rdr-create bootstrap.
- `conexus/skills/rdr-create/SKILL.md`: nexus-3fab5 register pointer +
  bootstrap copies REGISTER.md.
- `conexus/skills/rdr-gate/SKILL.md`: nexus-3fab5 Layer 3 critic brief gains
  the register question (jargon-free-reader comprehension, warn-class on
  failure, never blocks) and the critique-register line.
- `conexus/commands/rdr-create.md`: nexus-3fab5 register pointer.
- `conexus/commands/rdr-research.md`: nexus-3fab5 register pointer.
- `conexus/commands/rdr-gate.md`: nexus-3fab5 Layer 3 register question.
- `conexus/commands/rdr-accept.md`: nexus-3fab5 register pointer.
- `conexus/commands/rdr-close.md`: nexus-3fab5 post-mortem register pointer.
- `conexus/resources/rdr/TEMPLATE.md`: nexus-3fab5 register blockquote line.
- `conexus/resources/rdr/post-mortem/TEMPLATE.md`: nexus-3fab5 register
  blockquote line.
- `conexus/skills/rdr-close/SKILL.md`: nexus-3fab5 post-mortem register
  pointer + closing check (review round: close stage had no check).
- `conexus/skills/rdr-research/SKILL.md`: nexus-3fab5 register pointer at
  the findings-append step.
- `conexus/skills/rdr-accept/SKILL.md`: nexus-3fab5 register line in the
  strategic-planner dispatch brief.
- `conexus/skills/plan-author/SKILL.md`, `conexus/skills/plan-inspect/SKILL.md`, `conexus/skills/plan-promote/SKILL.md`, `conexus/plans/builtin/plan-author-default.yml`, `conexus/plans/builtin/plan-inspect-default.yml`, `conexus/plans/builtin/plan-inspect-dimensions.yml`, `conexus/plans/builtin/plan-promote-propose.yml`, `conexus/registry.yaml`, `conexus/README.md`, `conexus/plans/dimensions.yml`:
  nexus-77cct RETIRED
  the three plan-meta skills and their four templates. They dispatched a
  `plan_match` MCP tool that has never existed (the server registers
  plan_save / plan_search / plan_delete only), so nothing ever invoked
  them successfully and there are no users to break; what they described
  is `nx plan list` / `nx plan show` / `nx plan hygiene`, which work. They
  were not inert: their templates absorb any question containing the word
  "plan" and outranked the plan a caller actually wanted. Existing
  installs lose the rows on the next `nx plan reseed` / `nx upgrade` via
  `RETIRED_TEMPLATE_DIMENSIONS`, scoped to builtin-template rows only.
  `dimensions.yml`'s verb enumeration was stale independently (it omitted
  `query` and `lookup`, which shipped templates use) and is corrected.
- `conexus/plans/builtin/{debug-default,review-default}.yml`: nexus-7y4v0
  dropped the `subtree:` scoping. Both required a catalog tumbler prefix
  nx_answer can never supply, so it aliased raw question prose into the
  filter and the plans returned no evidence while reading as real
  answers. review-default was reachable on the plain cosine path (0.512,
  above the floor), so this was live.
  `conexus/plans/builtin/traverse-then-generate.yml`: RETIRED. It
  required caller-supplied catalog tumblers, which no question carries
  and no live caller can produce, so it was unreachable by construction;
  hybrid-factual-lookup already serves that shape in the same matcher
  space. Per Sam's directive there are no unofferable plans, and
  tests/test_builtin_plans.py now gates it: a template requiring a typed
  binding that is neither defaulted nor derivable from a question fails
  CI.
- `src/nexus/plans/binding_infer.py` (NEW), `src/nexus/mcp/core.py`:
  nx_answer derives TYPED bindings from the question — content_type from
  "which RDRs / papers / code", author from "by Grossberg" — the same way
  it derives a verb. Without it, find-by-author and type-scoped-search
  were permanently unofferable to the only live caller, despite being
  written for question shapes that carry the value they need. Derivation
  is conservative by design: an ambiguous question ("papers and RDRs")
  derives nothing and the plan stays unofferable, which is the old
  behaviour, while a WRONG typed filter yields a confident empty answer
  (schema.py records plan 14 returning zero results on a bad
  content_type). An explicit caller binding always wins.
- `conexus/hooks/scripts/routing/git_add_all_redirects_to_explicit_paths.py`
  (DELETED), `conexus/hooks/hooks.json`, `conexus/hooks/scripts/routing/registry.yaml`:
  Sam's decision, 2026-08-22 — the push-time review-coverage gate
  (nexus-4av2n) is removed outright, not modified. Measured one true
  positive in its life (denying correct, already-reviewed pushes), it is
  self-attested (the same agent writes the markers it is checked
  against), and it guards `develop`, which is already PR-gated to `main`
  with required checks — unreviewed code there ships to nobody. An
  installed plugin at the pinned v7.14.0 tag still enforces this gate
  until the next release ships; the bead-close review gate
  (`pre_close_verification_hook.sh`) is untouched and stays live.

