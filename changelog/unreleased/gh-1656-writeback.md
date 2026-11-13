### Changed

- **The SubagentStart write-back line defers to the brief** (GitHub #1656). It used to tell every subagent that findings not stored are lost, so a subagent told "Do not use any tools" still wrote to T1. The line now applies only to work that produced findings, and says the brief comes first: a brief that says not to use tools, or a task with nothing to keep, writes nothing.
