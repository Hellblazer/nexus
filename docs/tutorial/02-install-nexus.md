# 2. Install Nexus

> **Time**: 3–4 minutes
> **Goal**: `nx` CLI installed and verified

---

## VOICE

The package is called "conexus" on PyPI. The command is "nx." One command to install.

## SCREEN [8s]

```bash
uv tool install conexus
```

## VOICE [OVER SCREEN]

uv creates an isolated environment and installs everything. No virtualenv to manage.

[PAUSE 2s]

Let's verify.

## SCREEN [3s]

```bash
nx --version
```

## VOICE

Now the health check.

## SCREEN [5s]

```bash
nx doctor
```

## VOICE [OVER SCREEN]

Checkmarks for things that are ready. X marks for things not configured. The cloud credentials show X — that's fine. We're using local mode. Zero API keys needed.

## OVERLAY

> **What just happened?**
> - Isolated Python environment created
> - All dependencies installed
> - `nx` available everywhere
> - No virtualenv to activate

[PAUSE 1s]

## VOICE

Here's what nx can do.

## SCREEN [5s]

```bash
nx --help
```

## VOICE [OVER SCREEN]

The main commands: search, index, memory, and scratch. We'll cover each one next.

## OVERLAY

> **Key commands**
> - `nx search` — find things by meaning
> - `nx index` — add content to search
> - `nx memory` — persistent project notes
> - `nx scratch` — temporary session notes
> - `nx doctor` — health check
