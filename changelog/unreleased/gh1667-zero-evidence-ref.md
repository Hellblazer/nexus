### Fixed

- `plan_run` run unbundled (`bundle_operators=False`, or a dispatcher that does not bundle): a reference into a step that short-circuited on empty evidence (`$step5.ranked` after a rank with nothing to rank) no longer raises `PlanRunStepRefError`. It reads as a zero-evidence reference, and the consuming operator short-circuits to the zero-evidence marker without a model call, whatever argument carries it. `nx_answer` is unaffected: it always runs fused, where that reference never raised (GH #1667 follow-up C).
