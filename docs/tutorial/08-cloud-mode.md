# 8. Cloud Mode (Optional)

> **Time**: 3–5 minutes
> **Goal**: Viewer knows when cloud mode is worth it and how to set it up

---

## TALK

Everything we've done so far is completely local. No API keys, no accounts. The search quality is good — but if you want great, there's a cloud option.

### When Would You Want Cloud Mode?

## OVERLAY

> **Local mode** (what we've been using):
> - Bundled MiniLM model, 384 dimensions
> - Good for finding relevant files and functions
> - Free, private, no network
>
> **Cloud mode** (optional upgrade):
> - Voyage AI models, 1024 dimensions
> - Specialized models for code vs. prose
> - Cross-chunk context for better document retrieval
> - Reranking across collections
> - Free tiers cover individual use

## TALK

The short version: local mode finds the right neighborhood, cloud mode finds the exact house. For most personal projects, local is plenty. If you're working with a large team or a huge codebase — hundreds of thousands of lines — cloud mode gives noticeably better results.

Both options are free. Local has zero cost forever. Cloud uses free tiers from ChromaDB and Voyage AI that cover typical individual usage.

### Setup

## TALK

If you want to try it, here's the setup. First, install the cloud extra:

## DO

```bash
uv tool install conexus --with "conexus[cloud]" --force
```

## TALK

Then create accounts — both have generous free tiers:

## OVERLAY

> 1. **ChromaDB Cloud** — [trychroma.com](https://trychroma.com) — vector storage
> 2. **Voyage AI** — [voyageai.com](https://voyageai.com) — embeddings

## TALK

Once you have API keys, the interactive wizard handles the rest:

## DO

```bash
nx config init
```

## TALK

It walks you through each credential and provisions the cloud database automatically. When it's done:

## DO

```bash
nx doctor
```

## TALK

Everything should be green now. Re-index your repo to use the cloud models:

## DO

```bash
nx index repo .
```

## TALK

That's it. Search works the same way — just better results. You can switch back to local anytime by setting `NX_LOCAL=1` in your environment.
