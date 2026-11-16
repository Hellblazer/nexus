### Changed

- `nx_answer` no longer reuses a grown plan for a lookup or listing because the request that grew it was labelled a lookup or listing. That label was the request's guess, not a check of the plan's steps, so a lookup could take a plan built for synthesis. Until a reviewed decision trusts a grown plan's route, a grown plan serves no request, and the request plans inline instead. The library's current grown plans already served no request, so nothing changes in practice today. The refusal is reported as `grown_route_untrusted_label`.
