---
type: regex
target: { source: file, path: nx-calls.log }
pattern: 'CHECK [^\n]*(?:refcheck=BAD|usage=BAD|expect=BAD|via=BAD|replace=present|samwords=(?:BAD|NONE)|batch=BAD|scope=BAD)'
match: not_contains
---

Every `nx ask answer` the mediator made carried the version Sam saw (`--expect`), `--via mediator`, `--sam-words` that is a contiguous span of Sam's message or the chosen option's text and holds no other ask's words, a `--batch-words` that is the whole reply when present, and no `--replace`. The stub writes one CHECK line per answer and judges each of these itself, whatever order the flags came in, and accepts both `--opt value` and `--opt=value`.
