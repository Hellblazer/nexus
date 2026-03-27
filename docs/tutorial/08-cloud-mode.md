# 8. Cloud Mode (Optional)

> **Time**: 2–3 minutes
> **Goal**: Viewer knows when cloud mode is worth it and how to set it up

---

## VOICE

Everything so far has been completely local. The search quality is good. If you want great, there's a cloud option.

## OVERLAY

> **Local (default):** good results, zero cost, no setup
> **Cloud (optional):** better results on large codebases, free tier covers individual use

## VOICE

Local mode finds the right neighborhood. Cloud mode finds the exact house. For most projects, local is plenty. Large codebases benefit from cloud.

Both have free tiers that cover typical individual use.

[PAUSE 1s]

## VOICE

Cloud mode is already included. You just need API keys from two services.

## OVERLAY

> 1. **ChromaDB Cloud** — trychroma.com — vector storage
> 2. **Voyage AI** — voyageai.com — embeddings

## VOICE

Once you have keys, the wizard handles the rest.

## SCREEN [8s]

```bash
nx config init
```

*(Wizard walks through credentials)*

## VOICE [OVER SCREEN]

It provisions the cloud database automatically.

## SCREEN [5s]

```bash
nx doctor
```

## VOICE [OVER SCREEN]

Everything green. Now re-index.

## SCREEN [5s]

```bash
nx index repo .
```

## VOICE

Same search commands. Better results. To switch back to local mode, set one environment variable — shown on screen.

## SCREEN [3s]

```bash
NX_LOCAL=1 nx search "query"
```
