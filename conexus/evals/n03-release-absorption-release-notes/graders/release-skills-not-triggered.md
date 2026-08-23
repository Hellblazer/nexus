---
type: llm
criteria: >
  Fail if the transcript contains a Skill tool call whose input names
  "release" or "engine-release". This prompt asks for changelog-style
  prose summarizing recent commits for users — it names no version bump,
  no tag, no PyPI publish, and no engine-service artifact. Drafting
  release-notes text is not "cutting a release, bumping version,
  tagging, or publishing to PyPI" (the `release` skill's own trigger
  condition), so neither release skill should fire.
---

Absorption stress on the word "release" appearing in a request that is
about writing prose, not executing the release checklist.

OPEN QUESTION (enablement day): no documented negation primitive on
`tool_used`; `llm` criteria used as best-effort, field name unconfirmed.
