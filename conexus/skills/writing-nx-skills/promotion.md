Loaded on demand from the writing-nx-skills skill (SKILL.md § Promoting a Memory File).

## Promoting a Memory File

A memory file is read when someone remembers to read it, and its index line has to summarise the whole procedure. Moving a procedure into a skill, a script or a hook fixes that. A move that shrinks the source can also drop a rule, so a promotion follows five parts, in this order (RDR-240).

### The criterion

A memory file is a candidate when its "How to apply" is any of:

- a procedure, written as ordered steps;
- longer than about 8 KB;
- a bright line, a rule that must hold every time.

### The target

What the memory holds decides where it goes:

| The memory holds | Target | Why | Example |
| --- | --- | --- | --- |
| A procedure that needs judgment | Skill | Loads on demand; the model applies it | release checklist, review gate |
| A fixed sequence with one right answer | Lua script in a procedure library | Runs the same way every time; nothing to misremember | what to check before a push |
| A bright line that must always hold | Hook | Memory is context, a hook is enforcement | no `git stash`, no full-disk `find` |

A procedure library is a directory of Lua scripts, one `<name>.lua` per procedure, each with a four-line header that names its arguments and the verbs it may call. One runner executes them in a sandbox, through the `procedure_run(name, args)` tool. A procedure only one repo needs goes in that repo's library (`.nexus/procedures/`). A practice every conexus user should follow goes in the library that ships with `nx`. Personal rules stay as memory.

A script target counts only once `procedure_run` ships in a released package and, for a repo library, the repo root enabled. Until then the source stays whole, or the target is a skill.

A file that holds both kinds, a fixed part and a judgment part, is split by part. Each part gets its own target, and the source keeps what no target holds. Example: `feedback_test_runs_before_push.md` holds a fixed sequence, what to check before a push, and a judgment part. The fixed sequence becomes a script (`preflight`) and the judgment part becomes a skill. Until the script can run, the fixed part stays in the source or goes into the skill with the judgment part. The source shrinks once every target that holds a part is live (see the empty-list rule).

For a hook target, read the `hook-development` skill (plugin-dev) and write the test first: a test that trips on the bright line, before the hook exists.

### Citing the sources

The target cites its source files, each by file path or T2 title. The source shrinks to the rule and its why, plus a pointer. The pointer names the target by what runs it: a skill by its `/conexus:` name, a script by its path and the call that runs it (`.nexus/procedures/preflight.lua`, `procedure_run("preflight", ...)`), a hook by its file. The source is not deleted. In Bidirectional Memory-Skill Learning, the one paper that tests promotion, memory and skills together beat skills alone.

### The loss check

Before the source shrinks, dispatch `substantive-critic` with the target and the whole source file. For a split file, give it the union of the targets and the whole source file, once. The critic first enumerates the source's rules and maps each one to a line in a target, then lists every rule the target lacks. Brief it with "Ship when the list is empty". Empty means the critic reports 0 ship-blockers and lists nothing as lacking. Record the list, empty or not, on the bead. Promote one source file at a time, each with its own check. Unchecked compaction can score below having no context at all (Agentic Context Management), so a short target is not evidence that nothing was lost.

### The empty-list rule

A promotion lands only with an empty list. A rule on the list goes into the target, and the check runs again; the source does not shrink in between. The same check applies when the target is a script or a hook.

The source shrinks only once the target is live, and only after the empty list is recorded on the bead. Until then the source stays whole. Live means different things per target:

- A script target is live once `procedure_run` is released and, for a repo library, the repo root is enabled.
- A skill or hook target is live once the plugin release that carries it is installed. The plugin loads from the pinned release tag, so a merged skill or hook does nothing until then.

Shrinking at merge would leave a pointer to a target that cannot run. The shrink is often a later change than the one that adds the target.
