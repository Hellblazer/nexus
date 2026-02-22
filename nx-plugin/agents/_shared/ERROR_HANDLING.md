# Shared Error Handling Patterns

This file documents common error handling patterns for agents.

## General Principles

- Try primary method first
- Provide fallback options when available
- Log detailed error information
- Don't fail entire operation for single failure
- Document recovery options

## Common Error Categories

### Tool Execution Errors
- If primary tool fails: try alternative tool
- If all tools fail: report error with details
- Never silently swallow errors

### Knowledge Base Errors
- Connection failures: verify nx CLI is available (`nx doctor`)
- Write failures: check nx store permissions
- Duplicate ID errors: append timestamp suffix

### Context Management Errors
- Missing context: use RECOVER protocol from Context Protocol
- Overflow prevention: chunk work appropriately
- Interruption recovery: resume from checkpoints when possible

## Per-Agent Usage

Agents may reference this guide for general patterns and keep agent-specific
error handling inline in their definition files.
