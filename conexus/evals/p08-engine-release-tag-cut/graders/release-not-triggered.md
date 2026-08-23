---
type: llm
criteria: >
  Fail if the transcript contains a Skill tool call whose input names the
  "release" skill. This prompt is entirely about the Java engine-service
  binary and its own tag/deploy lifecycle — it names no PyPI publish, no
  conexus version bump, and no marketplace.json ref — so "release" (the
  PyPI lifecycle) is the wrong sibling skill and must not fire. Any other
  skill invocation (including "engine-release" itself) is fine and should
  not affect the verdict.
---

Disambiguation half of the release / engine-release pair (see
p07-release-version-bump for the mirror case).

OPEN QUESTION (enablement day): same caveat as
p07-release-version-bump/graders/engine-release-not-triggered.md — no
documented negation primitive; `llm` criteria used as best-effort,
`criteria` field name unconfirmed.
