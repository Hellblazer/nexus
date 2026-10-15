# docs/agents: reference material moved out of AGENTS.md

Moved from AGENTS.md on 2026-10-14 (nexus-2ziaz). AGENTS.md is loaded into every main-session and subagent request, so it keeps only the rules to obey on every turn; the history, incident archives, measurements and procedures live here, verbatim. Each file below carries the full text of one AGENTS.md section, and AGENTS.md links it from a short rules-only section under the same heading.

| AGENTS.md heading | Full text |
|---|---|
| Quick start (test-running details) | [test-running.md](test-running.md) |
| Architecture at a glance § T1 sub-agent contract | [t1-sub-agent-contract.md](t1-sub-agent-contract.md) |
| Architecture at a glance § Catalog/T3 split | [catalog-t3-split.md](catalog-t3-split.md) |
| External service limits | [external-service-limits.md](external-service-limits.md) |
| Hot rules | [hot-rules.md](hot-rules.md) |
| CI Cost Discipline | [ci-cost-discipline.md](ci-cost-discipline.md) |
| Self-hosted runners and fork PRs | [self-hosted-runners.md](self-hosted-runners.md) |
| Workflows § Release cadence policy | [release-cadence.md](release-cadence.md) |
| Workflows § Independent plugin release channel | [plugin-release-channel.md](plugin-release-channel.md) |
| Workflows § Engine-service release (authority) | [engine-service-release.md](engine-service-release.md) |
| Workflows § Cutting a release | [cutting-a-release.md](cutting-a-release.md) |
| Worktrees: one session, one worktree | [worktrees.md](worktrees.md) |

The `engine-release` skill and every document that cites the engine-service release rules treat [engine-service-release.md](engine-service-release.md) as the authority on wording conflicts.
