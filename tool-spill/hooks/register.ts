// SPDX-License-Identifier: AGPL-3.0-or-later
import type { Engine, Register } from 'claude-code'

// Main-session Bash and Read results over `limit` bytes are cut, so a big read costs the main context a few KB
// instead of all of it. Every other tool passes untouched, and so do subagents (heavy reads belong there) and, by
// default, headless runs (`claude -p`: nx_answer operators, headless critics).
//
//   Bash: the full text goes to a file; the model gets the head and the tail of each stream (cut at line ends), a
//         note between them naming the file, how many middle lines were cut and how many of those mention an error
//         or a failure. A failure on the last line, or an error buried in the middle, is therefore never silent.
//         A command that names the spill directory (reading a spill file back) passes whole.
//   Read: a whole-file read only. A read the model sliced itself (offset or limit) passes whole, so following the
//         note costs nothing extra, and instruction files (skills, agents, rules, AGENTS.md, CLAUDE.md) pass whole.
//
// Options (userConfig, all validated): `limit` 4000 bytes, `head` 1400, `tail` 800, `includeHeadless` false. A
// non-positive or non-numeric number falls back to its default; head and tail are scaled down together when they
// would not fit in `limit`. The spill directory is `<temp dir>/claude-tool-spill-<user>`, the temp dir being $TMPDIR,
// else $TEMP, else $TMP, else /tmp, and the user $USER, else $LOGNAME, else $USERNAME, else `user`.

const DEFAULT_LIMIT = 4_000
const DEFAULT_HEAD = 1_400
const DEFAULT_TAIL = 800
// Either separator, so a Windows path matches too.
const INSTRUCTIONS = /(^|[\\/])(SKILL|AGENTS|CLAUDE|CLAUDE\.local|REGISTER)\.md$|[\\/](skills|agents|commands|rules)[\\/]/i
const ERRORISH = /error|fail|traceback|panic|exception|fatal|denied|not found|no such|cannot|refused|timed out|unable/i

function bytes(s: string): number {
  return new TextEncoder().encode(s).length
}

// UTF-8 length of one code point, as TextEncoder counts it (a lone surrogate becomes U+FFFD, 3 bytes).
function width(cp: number): number {
  return cp < 0x80 ? 1 : cp < 0x800 ? 2 : cp < 0x10000 ? 3 : 4
}

// The longest prefix of `s` that is at most `max` UTF-8 bytes; it never splits a surrogate pair.
function prefixBytes(s: string, max: number): string {
  let used = 0
  let end = 0
  for (const ch of s) {
    used += width(ch.codePointAt(0)!)
    if (used > max) break
    end += ch.length
  }
  return s.slice(0, end)
}

// The longest suffix of `s` that is at most `max` UTF-8 bytes; it never splits a surrogate pair.
function suffixBytes(s: string, max: number): string {
  let used = 0
  let start = s.length
  while (start > 0) {
    const low = s.charCodeAt(start - 1)
    const high = start > 1 ? s.charCodeAt(start - 2) : 0
    const pair = low >= 0xdc00 && low <= 0xdfff && high >= 0xd800 && high <= 0xdbff
    used += pair ? 4 : width(low)
    if (used > max) break
    start -= pair ? 2 : 1
  }
  return s.slice(start)
}

function lineCount(s: string): number {
  const t = s.replace(/\n+$/, '')
  return t ? t.split('\n').length : 0
}

// The first `max` bytes of `s`, trimmed back to a line end when that keeps more than half of them.
function head(s: string, max: number): string {
  const cut = prefixBytes(s, max)
  if (cut.length === s.length || s[cut.length] === '\n') return cut
  const nl = cut.lastIndexOf('\n')
  return nl > cut.length / 2 ? cut.slice(0, nl) : cut
}

// The last `max` bytes of `s`, advanced to a line start when that drops less than half of them.
function tail(s: string, max: number): string {
  if (max <= 0) return ''
  const cut = suffixBytes(s, max)
  if (cut.length === s.length || s[s.length - cut.length - 1] === '\n') return cut
  const nl = cut.indexOf('\n')
  return nl >= 0 && nl < cut.length / 2 ? cut.slice(nl + 1) : cut
}

function positive(v: unknown, fallback: number): number {
  return typeof v === 'number' && Number.isFinite(v) && v > 0 ? Math.floor(v) : fallback
}

// `text` as head + note + tail, or `text` itself when head and tail already cover it. `name` labels a stream.
function squeeze(text: string, headMax: number, tailMax: number, where: string, name: string): string {
  if (bytes(text) <= headMax + tailMax) return text
  const h = head(text, headMax)
  const t = tail(text.slice(h.length), tailMax)
  const middle = text.slice(h.length, text.length - t.length).replace(/^\n/, '').replace(/\n$/, '')
  const lines = middle ? middle.split('\n') : []
  const flagged = lines.filter(l => ERRORISH.test(l)).length
  const note = `\n[tool-spill: ${name}${Math.round(bytes(text) / 1000)} KB, ${lineCount(text)} lines; ` +
    `${lines.length} middle lines cut, ${flagged} of them mention an error or failure. ${where}]`
  return t ? `${h}${note}\n${t}` : h + note
}

// Resolved on the first spill. A failed resolution is not kept: the next spill tries again.
let spillDir: Promise<string> | undefined

function dirOf($: Engine): Promise<string> {
  spillDir ??= (async () => {
    const base = (await $.env.get('TMPDIR')) || (await $.env.get('TEMP')) || (await $.env.get('TMP')) || '/tmp'
    const user = (await $.env.get('USER')) || (await $.env.get('LOGNAME')) || (await $.env.get('USERNAME')) || 'user'
    return `${base.replace(/[\\/]+$/, '')}/claude-tool-spill-${user.replace(/[^A-Za-z0-9_.-]/g, '_')}`
  })().catch(err => {
    spillDir = undefined
    throw err
  })
  return spillDir
}

// Whether `command` names `dir` itself: followed by a separator, a quote, a space or the end, so a sibling directory
// that merely shares the prefix (`claude-tool-spill-samuel` beside `claude-tool-spill-sam`) does not count.
function namesDir(command: string, dir: string): boolean {
  for (let i = command.indexOf(dir); i >= 0; i = command.indexOf(dir, i + 1)) {
    const after = command[i + dir.length]
    if (after === undefined || /[\\/\s'"]/.test(after)) return true
  }
  return false
}

// A session that draws on no surface is a plain `claude -p` run.
async function isHeadless($: Engine): Promise<boolean> {
  return (await $.session.surfaces()).length === 0
}

export const register: Register = (on, options) => {
  const LIMIT = positive(options.limit, DEFAULT_LIMIT)
  let HEAD = positive(options.head, DEFAULT_HEAD)
  let TAIL = positive(options.tail, DEFAULT_TAIL)
  if (HEAD + TAIL > LIMIT) {
    HEAD = Math.max(1, Math.floor((LIMIT * HEAD) / (HEAD + TAIL)))
    TAIL = LIMIT - HEAD
  }
  const INCLUDE_HEADLESS = options.includeHeadless === true

  on('tool.call', async ($, e, next) => {
    const res: any = await next(e)
    if (e.agentId || res.deny || res.isError) return res

    try {
      if (e.tool === 'Bash' && res.result && typeof res.result.stdout === 'string') {
        const out: string = res.result.stdout
        const err: string = res.result.stderr ?? ''
        const size = bytes(out) + bytes(err)
        // Claude Code already moves output past its own cap (about 30 KB) to a file; leave those to it. Its marker
        // wraps the whole output, so it counts only at the start: output that merely quotes it is cut (nexus-x967v).
        if (size <= LIMIT || res.result.isImage || res.result.rawOutputPath || res.result.persistedOutputPath ||
            out.trimStart().startsWith('<persisted-output>')) return res
        if (!INCLUDE_HEADLESS && await isHeadless($)) return res

        const dir = await dirOf($)
        // A command that reads a spill file back (sed, head, grep on it) is following a note: cutting it again would
        // spill a spill (nexus-i9a1p). Claude Code's own cap still bounds it.
        const input: any = e
        if (typeof input.command === 'string' && namesDir(input.command, dir)) return res
        const path = `${dir}/${e.tool_use_id.replace(/[^A-Za-z0-9_.-]/g, '_')}.txt`
        const where = `Full output: ${path}. Read a slice of it with the Read tool (offset and limit; a sliced Read ` +
          'is never cut) or hand it to a subagent; do not re-run the command.'
        const stdout = squeeze(out, HEAD, TAIL, where, '')
        const stderr = squeeze(err, HEAD, TAIL, where, 'stderr ')
        if (stdout === out && stderr === err) return res
        await $.fs.write(path, err ? `${out}\n--- stderr ---\n${err}` : out)
        return { result: { ...res.result, stdout, stderr }, context: res.context }
      }

      if (e.tool === 'Read' && res.result?.type === 'text') {
        const input: any = e
        if (input.offset !== undefined || input.limit !== undefined) return res
        const f = res.result.file
        if (INSTRUCTIONS.test(f.filePath)) return res
        const size = bytes(f.content)
        if (size <= LIMIT) return res
        if (!INCLUDE_HEADLESS && await isHeadless($)) return res

        const shown = head(f.content, HEAD + TAIL)
        const n = lineCount(shown)
        // A cut inside a line (a very long one) leaves that line partial: the next read starts at it.
        const nextLine = f.startLine + (f.content[shown.length] === '\n' ? n : Math.max(0, n - 1))
        const total = f.totalLines ?? f.numLines
        const perLine = Math.max(1, size / Math.max(1, f.numLines))
        const fits = Math.max(20, Math.floor((LIMIT * 4) / perLine))
        const note = `\n[tool-spill: ${Math.round(size / 1000)} KB, ${total} lines in the file; showing the first ${n}. ` +
          `Read it again with offset ${nextLine} and limit ${fits} (a sliced read is never cut), ` +
          'or hand the file to a subagent. Do not Edit text you have not seen.]'
        return { result: { ...res.result, file: { ...f, content: shown + note, numLines: n } }, context: res.context }
      }
    } catch {
      // Spilling is a courtesy: whatever fails here leaves the tool's own result as it was.
    }
    return res
  }).catch(($, e, next) => next(e))
}
