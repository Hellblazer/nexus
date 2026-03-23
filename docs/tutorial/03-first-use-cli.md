# 3. First Use — CLI

> **Time**: 5–7 minutes
> **Goal**: Viewer has used memory, scratch, indexed a repo, and searched it — all locally

---

## TALK

Let's use nexus for real. Everything in this section runs locally — no accounts, no API keys, no network. Let's start with the simplest thing: saving a note.

### Memory — Persistent Notes

## TALK

Memory is nexus's notepad. You store things per project, and they survive across sessions. Think of it like sticky notes organized by project name. One thing to know: notes expire after 30 days by default. For things you want to keep permanently, add `--ttl permanent`.

## DO

```bash
# Store a note (permanent — won't expire)
nx memory put "Auth uses JWT tokens with 24-hour expiry" --project myapp --title auth-notes --ttl permanent

# List what we stored
nx memory list --project myapp

# Retrieve it
nx memory get --project myapp --title auth-notes

# Search across everything
nx memory search "JWT"
```

## TALK

That's it. Store things, find them later. The search is keyword-based — fast and local. This is useful for design notes, decisions, anything you want to remember between sessions.

### Scratch — Session Notes

## TALK

Scratch is like memory but temporary. It lives only for the current session. It's meant for working notes — hypotheses you're testing, things you want to share between agents during a session.

## DO

```bash
nx scratch put "hypothesis: the bug is in the retry logic"
nx scratch list
nx scratch search "retry"
```

## TALK

When the session ends, scratch is gone. If you find something worth keeping, you can promote it to memory — but we'll come back to that.

### Index and Search a Repo

## TALK

Now the main event — semantic search. Regular search matches exact words. Nexus search matches by meaning. Let me show you.

First, we need to index a repository. Let's use whatever project you have handy.

## DO

```bash
cd ~/your-project    # any git repo with some code

nx index repo .
```

## TALK

That just analyzed every file in your repo — code, docs, everything — and made it all searchable. Completely local, no API calls.

Now let's search:

## DO

```bash
# Search by meaning, not exact words
nx search "how does authentication work"

# Search just code files
nx search "error handling" --corpus code

# Search just docs
nx search "getting started" --corpus docs

# Show the matching text inline
nx search "retry logic" -c
```

## TALK

Notice how the results found relevant code even when the exact words didn't match. That's semantic search — it matches by meaning, not by exact words. You can find things by what they do, not just what they're called.

Quick tip: if you want your repo re-indexed automatically after every commit:

## DO

```bash
nx hooks install
```

## TALK

Now you never have to think about it. Every commit keeps your search index fresh.

## OVERLAY

> **CLI Quick Reference**
> - `nx memory put "text" -p project -t title` — save a note
> - `nx memory search "query" -p project` — find notes
> - `nx scratch put "text"` — temporary session note
> - `nx index repo .` — index current repo
> - `nx search "query"` — search by meaning
> - `nx search "query" -c` — show matching text
> - `nx search "query" --corpus code` — code only
