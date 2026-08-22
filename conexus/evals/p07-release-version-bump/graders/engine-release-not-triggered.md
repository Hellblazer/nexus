---
type: llm
criteria: >
  Fail if the transcript contains a Skill tool call whose input names the
  "engine-release" skill. This prompt is about the PyPI/conexus release
  (version bump, changelog, tag, PyPI publish) — it names no Java
  engine-service artifact, no "engine-service-vX.Y.Z" tag, and no cloud
  engine deploy, so "engine-release" is the wrong sibling skill and must
  not fire. Any other skill invocation (including "release" itself) is
  fine and should not affect the verdict.
---

Disambiguation half of the release / engine-release pair: the two
skills' descriptions differ only in which release lifecycle (PyPI package
vs. Java engine binary) they cover, and both are keyed on the word
"release" in casual phrasing, so this is exactly the confusion class the
pair is meant to catch.

OPEN QUESTION (enablement day): `claude plugin eval`'s documented grader
types are regex | tool_used | tool_order | file_exists | llm | baseline;
there is no documented "assert tool NOT used" primitive. This grader
expresses the negative assertion via an `llm` judge criterion as a
best-effort choice — the `criteria` field name itself is not confirmed
against the real schema. Re-verify both the field name and whether a
first-class negation exists on `tool_used`/`tool_order` once eval access
lands, and simplify this grader if so.
