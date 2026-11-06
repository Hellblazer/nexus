# rdr243_plan

Live measurement for RDR-243 Phase 2 (nexus-rfvyy.7.16): latency of the search functions with
`p_exclude_sensitive` true and false at 0, 10, 50 and 90 percent sensitive documents, k 10 and 100, plus the
cost of the per-search sensitive-flag lookup (a second tenant transaction) on the default path.

`run.sh` runs `VectorSensitivityExcludePlanMeasure` (service test tree) on a lab host through `lab-remote`.
Results and predictions are recorded in T2 `nexus_rdr/243-plan-evidence-2026-11-06`.
The plan assertions are `VectorSensitivityExcludePlanShapeTest`.
