# docs/agents: reference material moved out of AGENTS.md

Moved from AGENTS.md on 2026-10-14 (nexus-2ziaz). AGENTS.md is loaded into every main-session and subagent request, so it keeps only the rules to obey on every turn; the history, incident archives, measurements and procedures live here. Each file below holds the full text of one former AGENTS.md section, edited since the move. The short rules-only form of each section lives in AGENTS.md or, for the situational ones, in a path-scoped `.claude/rules/` file (2026-10-15), and links here.

| Former AGENTS.md heading | Full text | Short form lives in |
|---|---|---|
| Quick start (test-running details) | [test-running.md](test-running.md) | `AGENTS.md` |
| Architecture at a glance § T1 sub-agent contract | [t1-sub-agent-contract.md](t1-sub-agent-contract.md) | `AGENTS.md` |
| Architecture at a glance § Catalog/T3 split | [catalog-t3-split.md](catalog-t3-split.md) | `AGENTS.md` |
| External service limits | [external-service-limits.md](external-service-limits.md) | [`.claude/rules/external-service-limits.md`](../../.claude/rules/external-service-limits.md) |
| Hot rules | [hot-rules.md](hot-rules.md) | `AGENTS.md` |
| CI Cost Discipline | [ci-cost-discipline.md](ci-cost-discipline.md) | [`.claude/rules/ci-and-runners.md`](../../.claude/rules/ci-and-runners.md) |
| Self-hosted runners and fork PRs | [self-hosted-runners.md](self-hosted-runners.md) | [`.claude/rules/ci-and-runners.md`](../../.claude/rules/ci-and-runners.md) |
| Workflows § Release cadence policy | [release-cadence.md](release-cadence.md) | [`.claude/rules/releases-and-engine.md`](../../.claude/rules/releases-and-engine.md) |
| Workflows § Independent plugin release channel | [plugin-release-channel.md](plugin-release-channel.md) | [`.claude/rules/releases-and-engine.md`](../../.claude/rules/releases-and-engine.md) |
| Workflows § Engine-service release (authority) | [engine-service-release.md](engine-service-release.md) | [`.claude/rules/releases-and-engine.md`](../../.claude/rules/releases-and-engine.md) |
| Workflows § Cutting a release | [cutting-a-release.md](cutting-a-release.md) | [`.claude/rules/releases-and-engine.md`](../../.claude/rules/releases-and-engine.md) |
| Worktrees: one session, one worktree | [worktrees.md](worktrees.md) | `AGENTS.md` |

The `engine-release` skill and every document that cites the engine-service release rules treat [engine-service-release.md](engine-service-release.md) as the authority on wording conflicts.
