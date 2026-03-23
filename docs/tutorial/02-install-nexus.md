# 2. Install Nexus

> **Time**: 3–4 minutes
> **Goal**: `nx` CLI installed and verified

---

## TALK

Nexus is published on PyPI as "conexus" — that's the package name. The CLI command is `nx`. One command to install:

## DO

```bash
uv tool install conexus
```

## TALK

uv just installed nx into its own isolated environment. It picked the right Python version automatically — you didn't have to create a virtualenv or worry about conflicts with other tools.

Let's verify it worked:

## DO

```bash
nx --version
```

## TALK

Now let's run the health check. This tells us what's working and what's not:

## DO

```bash
nx doctor
```

## TALK

You'll see checkmarks for things that are ready and X marks for things that aren't configured. Right now, the cloud credentials show X — that's fine. We're going to use local mode, which needs zero API keys. Everything works out of the box.

## OVERLAY

> **What just happened?**
> - `uv tool install` created an isolated Python environment
> - Installed nexus and all its dependencies
> - Made `nx` available as a command everywhere
> - No virtualenv to activate, no PATH to manage

## TALK

Let me show you what nx can do. Here's the help:

## DO

```bash
nx --help
```

## TALK

The main commands you'll use are `search`, `index`, `memory`, and `scratch`. We'll cover each one in the next section.

## OVERLAY

> **Key commands**
> - `nx search` — find things by meaning
> - `nx index` — add content to search
> - `nx memory` — persistent project notes
> - `nx scratch` — temporary session notes
> - `nx doctor` — health check
> - `nx config` — credentials and settings
