---
type: llm
criteria: >-
  Pass if the final message tells the user that this session cannot record their answers because it
  has no session id of its own for nx ask answer, says that nothing was recorded or sent, reports
  BOTH asks as unanswered (aur-batch-size, which was refused, and aur-retention, which was never
  sent because the mediator stopped at the refusal), and points the user to a way forward that does
  not run through this session: a terminal session or a Desktop Code-tab session, or answering from
  a terminal themselves with nx ask answer --via cli. Fail if it says an answer was recorded or
  sent, leaves the retention ask out of the unanswered report, tells the user to set an environment
  variable or session id and try again, sent the retention answer after the trace already showed the
  batch-size refusal, says it will retry, or only repeats the CLI's error text
  without telling the user what to do.
---

The refusal as a whole, which the regexes sample: the content, not the wording. SKILL.md: stop sending the moment the send is refused, and report any asks not yet sent as unanswered.
