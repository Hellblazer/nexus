---
name: receiving-review
description: Use when receiving any code review feedback, before implementing any suggestion - requires technical rigor and verification, not performative agreement or blind implementation
effort: medium
---

# Receiving Code Review Feedback

Code review requires technical evaluation, not emotional performance.

## Response Pattern

```
1. READ       — complete feedback without reacting
2. UNDERSTAND — restate requirement in own words (or ask)
3. VERIFY     — check claims against codebase reality
4. EVALUATE   — technically sound for THIS codebase?
5. RESPOND    — technical acknowledgment or reasoned pushback
6. IMPLEMENT  — one item at a time, test each
```

## Rules

**Never say:** "You're absolutely right!", "Great point!", "Thanks for catching that!"
**Instead:** State the fix, or push back with technical reasoning. Actions > words.

**If any item is unclear:** STOP. Ask for clarification on ALL unclear items before implementing any.

**If suggestion seems wrong:** Push back with technical reasoning — reference working tests, existing code, or architectural decisions.

## Handling External Review Feedback

Before implementing, verify:
1. Technically correct for THIS codebase?
2. Breaks existing functionality?
3. Reason for current implementation?
4. Does reviewer understand full context?
5. Conflicts with prior architectural decisions?

If can't verify: say so. "I can't verify this without [X]. Should I investigate?"

## YAGNI Check

When reviewer suggests "implement properly": use `/nx:serena-code-nav` to find all callers before accepting. If unused: "This isn't called anywhere. Remove it (YAGNI)?"

## Implementation Order

For multi-item feedback:
1. Clarify anything unclear FIRST
2. Blocking issues (breaks, security)
3. Simple fixes (typos, imports)
4. Complex fixes (refactoring, logic)
5. Test each fix individually
6. Verify no regressions

## When to Push Back

- Suggestion breaks existing functionality
- Reviewer lacks full context
- Violates YAGNI (unused feature)
- Technically incorrect for this stack
- Conflicts with architectural decisions

## Acknowledging Correct Feedback

```
✅ "Fixed. [Brief description of what changed]"
✅ "Good catch — [specific issue]. Fixed in [location]."
✅ [Just fix it and move on]
```

## Correcting a Wrong Pushback

If you pushed back and were wrong:
```
✅ "You were right — I checked [X] and it does [Y]. Implementing now."
✅ "Verified and you're correct. My initial read was wrong because [reason]. Fixing."
```
State the correction factually. No apology, no over-explaining.

## GitHub Thread Replies

Reply in the comment thread (`gh api repos/{owner}/{repo}/pulls/{pr}/comments/{id}/replies`), not as a top-level PR comment.
