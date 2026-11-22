### Fixed

- The client checks the collection label rule's clauses in the engine's order (control characters before the byte limit), so a label that breaks both gets the same refusal from either side. Client and engine tests now read one case table, `service/src/test/resources/label-rule-cases.json`, which pins the clause order and each case's verdict (nexus-pqirj, nexus-mrmqw).
