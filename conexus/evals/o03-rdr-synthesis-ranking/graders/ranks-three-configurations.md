---
type: llm
criteria: >-
  Pass if the answer states what each of the five RDRs concluded (011 HNSW
  m=16, ef_construction=128, ef_search=100; 014 IVFFlat for tenants under
  200k rows; 017 per-query ef_search; 019 halfvec storage; 022 rebuild
  settings) and then gives a ranked top three of recommended index
  configurations with a reason for the order. Fail if a conclusion is wrong
  or the ranking is missing.
---

The ranking half of the request, which no regex can check.
