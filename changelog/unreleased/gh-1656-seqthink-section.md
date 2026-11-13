### Changed

- **The SubagentStart hook no longer injects its "Sequential Thinking" section** (GitHub #1656). With the section in the hook output, subagents were stopped by the API's safety classifier (`[reasoning_extraction]`) at a sharply higher rate on 2026-11-13; on the reporter's machine the same prompt was blocked 0 times in 11 with it removed, against 4 in 8 with it present. The sequential-thinking tool itself is unchanged.
