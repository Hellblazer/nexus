# 3. First Use — CLI

> **Time**: 5–7 minutes
> **Goal**: Viewer has used memory, scratch, indexed a repo, and searched it

---

## VOICE

Let's use nexus for real. Everything here runs locally. No accounts, no API keys.

### Memory

## VOICE

Memory is nexus's notepad. Store things by project. They survive across sessions.

One thing to know — notes expire after 30 days by default. Add "ttl permanent" to keep them forever.

## SCREEN [12s]

```bash
nx memory put "Auth uses JWT tokens with 24-hour expiry" --project myapp --title auth-notes --ttl permanent

nx memory list --project myapp

nx memory get --project myapp --title auth-notes

nx memory search "JWT"
```

## VOICE [OVER SCREEN]

Store, list, retrieve, search. That's the whole model.

### Scratch

[PAUSE 1s]

## VOICE

Scratch is like memory, but temporary. It lasts one session. Good for working notes and hypotheses.

## SCREEN [6s]

```bash
nx scratch put "hypothesis: the bug is in the retry logic"
nx scratch list
nx scratch search "retry"
```

## VOICE

When the session ends, scratch is gone.

### Index and Search

[PAUSE 1s]

## VOICE

Now the main event. Semantic search. Regular search matches exact words. Nexus matches by meaning.

First, we index a repo.

## SCREEN [8s]

```bash
cd ~/your-project

nx index repo .
```

## VOICE [OVER SCREEN]

That analyzed every file — code, docs, everything — and made it all searchable. Completely local.

[PAUSE 2s]

Now let's search.

## SCREEN [12s]

```bash
nx search "how does authentication work"

nx search "error handling" --corpus code

nx search "getting started" --corpus docs

nx search "retry logic" -c
```

## VOICE [OVER SCREEN]

The results found relevant code even when the exact words didn't match. That's semantic search. It matches by meaning, not by words.

[PAUSE 2s]

## VOICE

Quick tip — you can re-index automatically after every commit.

## SCREEN [3s]

```bash
nx hooks install
```

## VOICE

Set it up once. Every commit keeps your search index fresh.

## OVERLAY

> **CLI Quick Reference**
> - `nx memory put "text" -p project -t title` — save a note
> - `nx memory search "query" -p project` — find notes
> - `nx scratch put "text"` — temporary session note
> - `nx index repo .` — index current repo
> - `nx search "query"` — search by meaning
> - `nx search "query" -c` — show matching text
