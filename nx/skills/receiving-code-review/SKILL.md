---
name: receiving-code-review
description: Use when receiving code review feedback — before implementing suggestions — especially if feedback seems unclear or technically questionable
---

# Receiving Code Review

## Overview

Code review requires technical evaluation, not emotional performance.

**Core principle:** Verify before implementing. Ask before assuming. Technical correctness over social comfort.

## The Response Pattern

```
WHEN receiving code review feedback:

1. READ: Complete feedback without reacting
2. UNDERSTAND: Restate requirement in own words (or ask)
3. VERIFY: Check against codebase reality
4. EVALUATE: Technically sound for THIS codebase?
5. RESPOND: Technical acknowledgment or reasoned pushback
6. IMPLEMENT: One item at a time, test each
```

## Forbidden Responses

**NEVER:**
- "You're absolutely right!"
- "Great point!" / "Excellent feedback!"
- "Let me implement that now" (before verification)

**INSTEAD:**
- Restate the technical requirement
- Ask clarifying questions if anything is unclear
- Push back with technical reasoning if wrong
- Just start working (actions speak louder than words)

## Handling Unclear Feedback

```
IF any item is unclear:
  STOP — do not implement anything yet
  ASK for clarification on unclear items

WHY: Items may be related. Partial understanding = wrong implementation.
```

## When to Push Back

Push back when:
- Suggestion breaks existing functionality
- Reviewer lacks full context
- Violates YAGNI (unused feature)
- Technically incorrect for this stack
- Conflicts with prior architectural decisions

**How to push back:**
- Use technical reasoning, not defensiveness
- Reference working tests/code
- Ask specific questions

## Implementation Order

```
FOR multi-item feedback:
  1. Clarify anything unclear FIRST
  2. Implement in this order:
     - Blocking issues (breaks, security)
     - Simple fixes (typos, imports)
     - Complex fixes (refactoring, logic)
  3. Test each fix individually
  4. Verify no regressions
```

## Acknowledging Correct Feedback

```
Correct:  "Fixed. [Brief description]"
Correct:  "Good catch — [specific issue]. Fixed in [location]."
Correct:  [Just fix it and show in the code]

Wrong:    "You're absolutely right!"
Wrong:    "Thanks for catching that!"
Wrong:    ANY gratitude expression
```

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Performative agreement | State requirement or just act |
| Blind implementation | Verify against codebase first |
| Batch without testing | One at a time, test each |
| Avoiding pushback | Technical correctness over comfort |

**REQUIRED BACKGROUND:** Understand nx:verification-before-completion — every fix must be verified before claiming it's done.
