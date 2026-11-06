---
type: regex
target: { source: file, path: nx-calls.log }
pattern: 'CALL \[ask\]\[answer\][^\n]*\[[^\]\n]*/(aur-batch-size|aur-retention)\][\s\S]*CALL \[ask\]\[answer\][^\n]*\[[^\]\n]*/\1\]|CALL \[ask\]\[answer\][^\n]*\[--via(?:\]\[|=)cli\]'
match: not_contains
---

Sam asked for both recommendations, and the stub refuses every `--via mediator` send. The skill says to stop sending the moment `nx ask answer` refuses because the id did not resolve, and to report the asks not yet sent as unanswered. This grader fails a retry: a second `nx ask answer` for an ask already tried, or any try with `--via cli`, which would put Sam's answer under this session's route. It does not fail one send per ask issued together before the refusal came back: the skill does not say to send one ask at a time, and the log cannot tell that apart from a send made after the refusal was seen. The llm grader judges that case from the trace.
