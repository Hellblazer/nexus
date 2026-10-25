// SPDX-License-Identifier: AGPL-3.0-or-later
import type { Engine, Register } from 'claude-code'

// Main-session tool results over `limit` bytes are written to a file; the model gets the first `head` bytes (cut at
// a line end) and a note naming the file, so a big read costs the main context a few KB instead of all of it.
// Subagents are left alone: they are where heavy reads belong.
//
// `limit` and `head` are the plugin's userConfig options (defaults 4000 and 2000). The spill directory is
// `<temp dir>/claude-tool-spill`, the temp dir being $TMPDIR, else $TEMP, else $TMP, else /tmp.

const DEFAULT_LIMIT = 4_000
const DEFAULT_HEAD = 2_000
const EXEMPT = new Set(['Agent', 'Task', 'TodoWrite', 'AskUserQuestion', 'Edit', 'Write', 'NotebookEdit'])

function bytes(s: string): number {
  return new TextEncoder().encode(s).length
}

function lineCount(s: string): number {
  const t = s.replace(/\n+$/, '')
  return t ? t.split('\n').length : 0
}

function positive(v: unknown, fallback: number): number {
  return typeof v === 'number' && Number.isFinite(v) && v > 0 ? Math.floor(v) : fallback
}

function note(path: string | undefined, size: number, shownLines: number, totalLines: number, hint: string): string {
  return `\n[tool-spill: ${Math.round(size / 1000)} KB, ${totalLines} lines; showing the first ${shownLines}. ` +
    (path ? `Full output: ${path}. ` : '') + `${hint}]`
}

const SLICE = 'Read a slice of that file (offset/limit) or hand the file to a subagent; do not re-run the command to see more.'

// Resolved once, on the first spill: the first of TMPDIR, TEMP, TMP that is set, else /tmp.
let spillDir: Promise<string> | undefined

async function spillPath($: Engine, id: string): Promise<string> {
  spillDir ??= (async () => {
    const base = (await $.env.get('TMPDIR')) || (await $.env.get('TEMP')) || (await $.env.get('TMP')) || '/tmp'
    return `${base.replace(/[\\/]+$/, '')}/claude-tool-spill`
  })()
  return `${await spillDir}/${id.replace(/[^A-Za-z0-9_.-]/g, '_')}.txt`
}

function head(s: string, max: number): string {
  const cut = s.slice(0, max)
  const nl = cut.lastIndexOf('\n')
  return nl > max / 2 ? cut.slice(0, nl) : cut
}

export const register: Register = (on, options) => {
  const LIMIT = positive(options.limit, DEFAULT_LIMIT)
  const HEAD = Math.min(positive(options.head, DEFAULT_HEAD), LIMIT)

  on('tool.call', async ($, e, next) => {
    const res: any = await next(e)
    if (e.agentId || EXEMPT.has(e.tool) || res.deny || res.isError) return res

    if (e.tool === 'Bash' && res.result && typeof res.result.stdout === 'string') {
      const out: string = res.result.stdout
      const err: string = res.result.stderr ?? ''
      const size = bytes(out) + bytes(err)
      // Claude Code already moves output past its own cap (about 30 KB) to a file; leave those to it.
      if (size <= LIMIT || res.result.isImage || res.result.rawOutputPath || out.includes('<persisted-output>')) return res
      const path = await spillPath($, e.tool_use_id)
      await $.fs.write(path, err ? `${out}\n--- stderr ---\n${err}` : out)
      const shown = head(out, HEAD)
      const total = lineCount(out)
      return {
        result: {
          ...res.result,
          stdout: shown + note(path, size, lineCount(shown), total, SLICE),
          stderr: err.length > HEAD ? head(err, HEAD) + '\n[tool-spill: stderr cut; full text in the same file]' : err,
        },
        context: res.context,
      }
    }

    if (e.tool === 'Read' && res.result?.type === 'text') {
      const f = res.result.file
      const size = bytes(f.content)
      if (size <= LIMIT) return res
      const shown = head(f.content, HEAD)
      const n = lineCount(shown)
      const hint = `The file itself is ${f.filePath}: read it with offset ${f.startLine + n} and a limit, ` +
        'or hand it to a subagent.'
      return {
        result: { ...res.result, file: { ...f, content: shown + note(undefined, size, n, f.totalLines ?? f.numLines, hint), numLines: n } },
        context: res.context,
      }
    }

    // Any other tool (Grep, MCP tools): the shape varies, so withhold the result and say where it went.
    const text: string | undefined = res.text
    if (typeof text !== 'string' || bytes(text) <= LIMIT) return res
    const path = await spillPath($, e.tool_use_id)
    await $.fs.write(path, text)
    const shown = head(text, HEAD)
    return { deny: `output saved to a file instead.\n${shown}` +
      note(path, bytes(text), lineCount(shown), lineCount(text), SLICE) }
  }).catch(($, e, next) => next(e))
}
