### Added

- **Engine: `identity_work` takes a new kind, `agent` (RDR-253 Step 6).** `POST /v1/identity/<id>/work` accepts `kind=agent` (a dispatched agent, ref `agent:<agent id>`), and changeset `identity-005` widens the table's `identity_work_kind_ck` CHECK to admit it (DDL only, no row changed). An engine from before this release answers `400 bad_kind` for it, which the client logs and ignores.
