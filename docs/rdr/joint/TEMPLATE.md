---
id: JDR-NNN
title: "<one line: the shared decision>"
status: active
owners: [RDR-AAA, RDR-BBB]
created: YYYY-MM-DD
---

# JDR-NNN: <title>

A joint decision record holds ONE rule that two or more RDRs would each
otherwise carry a copy of. Append-only: never merge, split or renumber a
JDR, because records cite it by number and a moved anchor is the
duplicate-contract drift this registry exists to end. Amend in place with
a dated line under Revision History; retire by setting `status: retired`
and saying what replaced it.

## Decision

The rule, stated once, in the form a reader can check against the code.

## Owners

The records that own this seam and would each restate the rule without
this file. An owner is added by a Revision History line, never removed.

## Cited by

Every record and document that relies on the rule. `tests/test_docs_reference_rot.py`
checks that any `JDR-NNN` cited under `docs/` resolves to a file here.

## Revision History

- YYYY-MM-DD: Created from <the seam it was extracted from>.
