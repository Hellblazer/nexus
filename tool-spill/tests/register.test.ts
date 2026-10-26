// SPDX-License-Identifier: AGPL-3.0-or-later
import { expect, mock, test } from 'claude-code/testing'

// `claude plugin test tool-spill`. Nothing sits beneath the plugin: the hooks each test registers stand for the
// engine, so a `tool.call` hook answers what the tool "returned", an `fs.write` hook records what was spilled and a
// `session.surfaces` hook says whether the session is interactive. Defaults throughout unless a test sets options:
// limit 4000, head 1400, tail 800, headless runs left alone.

const lines = (n: number) => Array.from({ length: n }, (_, i) => `line ${i}`).join('\n')
// ASCII text of exactly `n` bytes.
const exact = (n: number) => lines(2_000).slice(0, n)
const size = (s: string) => new TextEncoder().encode(s).length
const BIG_BASH = (stdout: string, extra: object = {}) => ({ result: { stdout, stderr: '', ...extra } })
const NOTE = /\n\[tool-spill:[^\]]*\]\n?/

// `env` is what $TMPDIR, $USER and friends answer (unlisted ones are unset); `surfaces` what the session draws on.
function spy(on: any, env: Record<string, string> = {}, surfaces: string[] = ['terminal']) {
  mock.env(on, env)
  on('session.surfaces', () => ({ value: surfaces }))
  const writes: { path: string; text: string }[] = []
  on('fs.write', (_: unknown, e: { path: string; text: string }) => {
    writes.push({ path: e.path, text: e.text })
    return { value: undefined }
  })
  return writes
}

// A cut Bash text split into what comes before the note, the note and what comes after.
function parts(s: string) {
  const m = NOTE.exec(s)
  if (!m) throw new Error('no tool-spill note in: ' + s.slice(0, 200))
  return { head: s.slice(0, m.index), note: m[0], tail: s.slice(m.index + m[0].length) }
}

const readResult = (content: string, filePath = '/repo/big.txt', startLine = 1) => ({
  result: {
    type: 'text',
    file: { filePath, content, startLine, numLines: content.split('\n').length, totalLines: content.split('\n').length },
  },
})

// ---------------------------------------------------------------- Bash: the boundary, with the options omitted

test('a Bash result of exactly limit bytes passes unchanged (default limit 4000)', async ($, on) => {
  const writes = spy(on)
  const stdout = exact(4_000)
  expect(size(stdout)).toBe(4_000)
  on('tool.call', () => BIG_BASH(stdout))

  const r: any = await $.tool.call({ tool: 'Bash', command: 'ls' })

  expect(r.result).toEqual({ stdout, stderr: '' })
  expect(writes).toHaveLength(0)
})

test('a Bash result of limit + 1 bytes spills (default limit 4000)', async ($, on) => {
  const writes = spy(on)
  const stdout = exact(4_001)
  on('tool.call', () => BIG_BASH(stdout))

  const r: any = await $.tool.call({ tool: 'Bash', command: 'ls' })

  expect(writes).toHaveLength(1)
  expect(writes[0].text).toBe(stdout)
  expect(r.result.stdout).toContain('[tool-spill:')
  expect(size(r.result.stdout)).toBeLessThan(4_001)
})

test('stdout and stderr count together toward the limit', async ($, on) => {
  const writes = spy(on)
  on('tool.call', () => ({ result: { stdout: exact(3_000), stderr: exact(1_500) } }))

  const r: any = await $.tool.call({ tool: 'Bash', command: 'x' })

  // stdout alone (3000) is under the limit; only the 4500-byte sum makes it spill
  expect(writes).toHaveLength(1)
  expect(r.result.stdout).toContain('[tool-spill:')
})

test('streams over the limit together but each within head + tail are left whole', async ($, on) => {
  const writes = spy(on)
  on('tool.call', () => ({ result: { stdout: exact(2_000), stderr: exact(2_001) } }))

  const r: any = await $.tool.call({ tool: 'Bash', command: 'x' })

  expect(writes).toHaveLength(0)
  expect(r.result.stdout).toBe(exact(2_000))
  expect(r.result.stderr).toBe(exact(2_001))
})

// ---------------------------------------------------------------- Bash: head and tail, and what the note says

test('a long Bash result keeps its head and its tail, cut at line ends, with the whole text in a file', async ($, on) => {
  const writes = spy(on, { TMPDIR: '/var/tmp/x/', USER: 'sam' })
  const stdout = lines(1_000)
  on('tool.call', () => BIG_BASH(stdout))

  const r: any = await $.tool.call({ tool: 'Bash', command: 'seq 1000' })

  expect(writes).toHaveLength(1)
  expect(writes[0].path).toMatch(/^\/var\/tmp\/x\/claude-tool-spill-sam\/[A-Za-z0-9_.-]+\.txt$/)
  expect(writes[0].text).toBe(stdout)
  const { head, note, tail } = parts(r.result.stdout)
  expect(head.startsWith('line 0\nline 1\n')).toBe(true)
  expect(tail.endsWith('line 999')).toBe(true)
  // defaults: head 1400, tail 800, each cut on a line end, each using most of its share
  expect(size(head)).toBeLessThanOrEqual(1_400)
  expect(size(head)).toBeGreaterThan(700)
  expect(size(tail)).toBeLessThanOrEqual(800)
  expect(size(tail)).toBeGreaterThan(400)
  const all = stdout.split('\n')
  expect(all).toContain(head.split('\n').at(-1))
  expect(all.indexOf(tail.split('\n')[0])).toBeGreaterThan(0)
  expect(note).toContain(`Full output: ${writes[0].path}`)
  expect(note).toContain('1000 lines')
  // the note counts the lines it cut
  const cut = Number(/(\d+) middle lines cut/.exec(note)![1])
  expect(head.split('\n').length + cut + tail.split('\n').length).toBe(1_000)
})

test('a failure on the last line of an exit-0 result is still in what the model reads', async ($, on) => {
  spy(on)
  const stdout = lines(1_000) + '\nFAILED: 3 tests, exit code hidden by tee'
  on('tool.call', () => BIG_BASH(stdout))

  const r: any = await $.tool.call({ tool: 'Bash', command: 'make test | tee log' })

  expect(r.result.stdout.endsWith('FAILED: 3 tests, exit code hidden by tee')).toBe(true)
})

test('error lines in the cut middle are counted in the note', async ($, on) => {
  spy(on)
  const all = lines(1_000).split('\n')
  all[500] = 'ERROR: disk full'
  all[501] = 'Traceback (most recent call last):'
  all[502] = 'java.lang.IllegalStateException: boom'
  on('tool.call', () => BIG_BASH(all.join('\n')))

  const r: any = await $.tool.call({ tool: 'Bash', command: 'x' })

  const { head, tail, note } = parts(r.result.stdout)
  expect(head + tail).not.toContain('disk full') // really in the cut part
  expect(note).toContain('3 of them mention an error or failure')
})

test('a clean middle says none of it mentions an error', async ($, on) => {
  spy(on)
  on('tool.call', () => BIG_BASH(lines(1_000)))

  const r: any = await $.tool.call({ tool: 'Bash', command: 'x' })

  expect(parts(r.result.stdout).note).toContain('0 of them mention an error or failure')
})

test('stderr is cut to its head and tail too, and both streams land in the file', async ($, on) => {
  const writes = spy(on)
  const stderr = lines(500) + '\npanic: runtime error at the very end'
  on('tool.call', () => ({ result: { stdout: lines(500), stderr } }))

  const r: any = await $.tool.call({ tool: 'Bash', command: 'noisy' })

  expect(writes[0].text).toContain('--- stderr ---')
  expect(writes[0].text).toContain(stderr)
  expect(r.result.stderr).toContain('stderr ')
  expect(r.result.stderr.endsWith('panic: runtime error at the very end')).toBe(true)
  expect(size(r.result.stderr)).toBeLessThan(2_600)
})

// ---------------------------------------------------------------- Bash: what is left alone

test('output Claude Code already persisted is left alone (marker text, rawOutputPath, persistedOutputPath)', async ($, on) => {
  const writes = spy(on)
  const marked = { stdout: '<persisted-output>\n' + exact(5_000) + '\n</persisted-output>', stderr: '' }
  const raw = { stdout: lines(1_000), stderr: '', rawOutputPath: '/somewhere/out.txt' }
  const typed = { stdout: lines(1_000), stderr: '', persistedOutputPath: '/somewhere/tool-results/x.txt', persistedOutputSize: 99_999 }
  on('tool.call', (_: unknown, e: any) => ({ result: e.command === 'a' ? marked : e.command === 'b' ? raw : typed }))

  for (const [command, want] of [['a', marked], ['b', raw], ['c', typed]] as const) {
    expect(((await $.tool.call({ tool: 'Bash', command })) as any).result).toEqual(want)
  }
  expect(writes).toHaveLength(0)
})

test('output that only mentions the marker is still cut (nexus-x967v)', async ($, on) => {
  // Reading the plugin's own README or source puts the marker text mid-output; only Claude Code's own wrapper,
  // which starts the output, means "already persisted".
  const writes = spy(on)
  const stdout = exact(2_500) + '\nthe <persisted-output> marker, quoted in prose\n' + exact(2_500)
  on('tool.call', () => BIG_BASH(stdout))

  const r: any = await $.tool.call({ tool: 'Bash', command: 'cat README.md' })

  expect(writes).toHaveLength(1)
  expect(writes[0].text).toBe(stdout)
  expect(r.result.stdout).toContain('[tool-spill:')
})

test('the marker after leading whitespace still counts as persisted', async ($, on) => {
  const writes = spy(on)
  const result = { stdout: '\n  <persisted-output>\n' + exact(5_000) + '\n</persisted-output>', stderr: '' }
  on('tool.call', () => ({ result }))

  expect(((await $.tool.call({ tool: 'Bash', command: 'big' })) as any).result).toEqual(result)
  expect(writes).toHaveLength(0)
})

test('an image result is left alone', async ($, on) => {
  const writes = spy(on)
  const result = { stdout: 'A'.repeat(20_000), stderr: '', isImage: true }
  on('tool.call', () => ({ result }))

  expect(((await $.tool.call({ tool: 'Bash', command: 'x' })) as any).result).toEqual(result)
  expect(writes).toHaveLength(0)
})

test('a subagent call (agentId set) is untouched', async ($, on) => {
  const writes = spy(on)
  const stdout = lines(1_000)
  on('tool.call', () => BIG_BASH(stdout))

  const r: any = await $.tool.call({ tool: 'Bash', command: 'seq 1000', agentId: 'a1' })

  expect(r.result.stdout).toBe(stdout)
  expect(writes).toHaveLength(0)
})

test('an errored result passes through whole', async ($, on) => {
  const writes = spy(on)
  const stdout = lines(1_000)
  on('tool.call', () => ({ result: { stdout, stderr: 'boom' }, isError: true }))

  const r: any = await $.tool.call({ tool: 'Bash', command: 'false' })

  expect(r.isError).toBe(true)
  expect(r.result.stdout).toBe(stdout)
  expect(writes).toHaveLength(0)
})

test('a denied call stays denied and nothing is written', async ($, on) => {
  const writes = spy(on)
  on('tool.call', () => ({ deny: 'not allowed here' }))

  const r: any = await $.tool.call({ tool: 'Bash', command: 'rm -rf /' })

  expect(r.deny).toBe('not allowed here')
  expect(r.result).toBeUndefined()
  expect(writes).toHaveLength(0)
})

test('a failing spill write leaves the original result', async ($, on) => {
  mock.env(on, {})
  on('session.surfaces', () => ({ value: ['terminal'] }))
  on('fs.write', () => {
    throw new Error('disk full')
  })
  const stdout = lines(1_000)
  let ran = 0
  on('tool.call', () => {
    ran++
    return BIG_BASH(stdout)
  })

  const r: any = await $.tool.call({ tool: 'Bash', command: 'seq 1000' })

  expect(r.result).toEqual({ stdout, stderr: '' })
  expect(ran).toBe(1) // the tool ran once: a failed spill must not make the engine run it again
})

test('a failed spill-directory lookup is not remembered: the next spill tries again', async ($, on) => {
  on('session.surfaces', () => ({ value: ['terminal'] }))
  const writes: { path: string; text: string }[] = []
  on('fs.write', (_: unknown, e: { path: string; text: string }) => {
    writes.push({ path: e.path, text: e.text })
    return { value: undefined }
  })
  let asked = 0
  on('env.get', (_: unknown, e: { name: string }) => {
    if (e.name === 'TMPDIR' && asked++ === 0) throw new Error('env unavailable')
    return { value: e.name === 'TMPDIR' ? '/var/tmp/retry' : undefined }
  })
  const stdout = lines(1_000)
  on('tool.call', () => BIG_BASH(stdout))

  const first: any = await $.tool.call({ tool: 'Bash', command: 'one' })
  expect(first.result.stdout).toBe(stdout)
  expect(writes).toHaveLength(0)

  const second: any = await $.tool.call({ tool: 'Bash', command: 'two' })
  expect(writes).toHaveLength(1)
  expect(writes[0].path).toMatch(/^\/var\/tmp\/retry\/claude-tool-spill-user\//)
  expect(second.result.stdout).toContain('[tool-spill:')
})

// ---------------------------------------------------------------- bytes, not characters

test('the limit counts UTF-8 bytes: 1500 three-byte characters spill', async ($, on) => {
  const writes = spy(on)
  const stdout = '日本語'.repeat(500) // 1500 characters, 4500 bytes
  on('tool.call', () => BIG_BASH(stdout))

  const r: any = await $.tool.call({ tool: 'Bash', command: 'x' })

  expect(writes).toHaveLength(1)
  expect(size(r.result.stdout)).toBeLessThan(4_500)
})

test('the byte boundary holds for multi-byte text: 3999 bytes pass, 4002 spill', async ($, on) => {
  const writes = spy(on)
  // 1333 x 3 bytes = 3999: passes. 1334 x 3 bytes = 4002: spills.
  on('tool.call', (_: unknown, e: any) => BIG_BASH('語'.repeat(e.command === 'under' ? 1_333 : 1_334)))

  const under: any = await $.tool.call({ tool: 'Bash', command: 'under' })
  expect(under.result.stdout).toBe('語'.repeat(1_333))
  expect(writes).toHaveLength(0)

  await $.tool.call({ tool: 'Bash', command: 'over' })
  expect(writes).toHaveLength(1)
})

test('the head and the tail stay within their byte budgets and never split a surrogate pair', async ($, on) => {
  spy(on)
  const stdout = '😀'.repeat(2_000) // 8000 bytes, 4000 UTF-16 units, no newline
  on('tool.call', () => BIG_BASH(stdout))

  const r: any = await $.tool.call({ tool: 'Bash', command: 'x' })

  const { head, tail } = parts(r.result.stdout)
  expect(size(head)).toBeLessThanOrEqual(1_400)
  expect(size(tail)).toBeLessThanOrEqual(800)
  expect(size(head)).toBeGreaterThan(1_390)
  expect(size(tail)).toBeGreaterThan(790)
  const loneSurrogate = /[\ud800-\udbff](?![\udc00-\udfff])|(?<![\ud800-\udbff])[\udc00-\udfff]/
  expect(loneSurrogate.test(r.result.stdout)).toBe(false)
})

// ---------------------------------------------------------------- options

test('a raised limit lets a 6 KB result through', { options: { limit: 10_000 } }, async ($, on) => {
  const writes = spy(on)
  const stdout = exact(6_000)
  on('tool.call', () => BIG_BASH(stdout))

  const r: any = await $.tool.call({ tool: 'Bash', command: 'x' })

  expect(r.result.stdout).toBe(stdout)
  expect(writes).toHaveLength(0)
})

test('head and tail options set the bytes kept', { options: { limit: 1_000, head: 300, tail: 200 } }, async ($, on) => {
  const writes = spy(on)
  on('tool.call', () => BIG_BASH(lines(500)))

  const r: any = await $.tool.call({ tool: 'Bash', command: 'x' })

  const { head, tail } = parts(r.result.stdout)
  expect(size(head)).toBeLessThanOrEqual(300)
  expect(size(head)).toBeGreaterThan(150)
  expect(size(tail)).toBeLessThanOrEqual(200)
  expect(size(tail)).toBeGreaterThan(100)
  expect(writes).toHaveLength(1)
})

test('a head and tail larger than the limit are scaled down together to fit it', { options: { limit: 1_000, head: 50_000, tail: 50_000 } }, async ($, on) => {
  spy(on)
  on('tool.call', () => BIG_BASH(lines(500)))

  const r: any = await $.tool.call({ tool: 'Bash', command: 'x' })

  const { head, tail } = parts(r.result.stdout)
  expect(size(head) + size(tail)).toBeLessThanOrEqual(1_000)
  expect(size(head)).toBeGreaterThan(250)
  expect(size(tail)).toBeGreaterThan(250)
})

test('nonsense options fall back to the defaults: 4000 passes, 4001 spills', { options: { limit: -5, head: 0, tail: -3 } }, async ($, on) => {
  const writes = spy(on)
  on('tool.call', (_: unknown, e: any) => BIG_BASH(exact(e.command === 'at' ? 4_000 : 4_001)))

  await $.tool.call({ tool: 'Bash', command: 'at' })
  expect(writes).toHaveLength(0)

  const r: any = await $.tool.call({ tool: 'Bash', command: 'over' })
  expect(writes).toHaveLength(1)
  const { head, tail } = parts(r.result.stdout)
  expect(size(head)).toBeLessThanOrEqual(1_400)
  expect(size(head)).toBeGreaterThan(700) // the real default head, not a smaller one
  expect(size(tail)).toBeLessThanOrEqual(800)
  expect(size(tail)).toBeGreaterThan(400)
})

// ---------------------------------------------------------------- the spill directory

test('the directory is $TMPDIR, else $TEMP, else $TMP, else /tmp, under claude-tool-spill-<user>', async ($, on) => {
  const writes = spy(on, { TEMP: '/var/tmp/win//', TMP: '/var/tmp/other', LOGNAME: 'lee' })
  on('tool.call', () => BIG_BASH(lines(1_000)))

  await $.tool.call({ tool: 'Bash', command: 'x' })

  expect(writes[0].path).toMatch(/^\/var\/tmp\/win\/claude-tool-spill-lee\/[A-Za-z0-9_.-]+\.txt$/)
})

test('with no temp variable and no user the directory is /tmp/claude-tool-spill-user', async ($, on) => {
  const writes = spy(on)
  on('tool.call', () => BIG_BASH(lines(1_000)))

  await $.tool.call({ tool: 'Bash', command: 'x' })

  expect(writes[0].path).toMatch(/^\/tmp\/claude-tool-spill-user\/[A-Za-z0-9_.-]+\.txt$/)
})

test('$USER wins over $LOGNAME and $USERNAME, and the name is made path-safe', async ($, on) => {
  const writes = spy(on, { USER: 'a b/../c', LOGNAME: 'x', USERNAME: 'y' })
  on('tool.call', () => BIG_BASH(lines(1_000)))

  await $.tool.call({ tool: 'Bash', command: 'x' })

  expect(writes[0].path).toMatch(/^\/tmp\/claude-tool-spill-a_b_.._c\/[A-Za-z0-9_.-]+\.txt$/)
})

// ---------------------------------------------------------------- headless runs

test('a headless run (the session draws on no surface) is untouched by default', async ($, on) => {
  const writes = spy(on, {}, [])
  const stdout = lines(1_000)
  const file = readResult(lines(2_000))
  on('tool.call', (_: unknown, e: any) => (e.tool === 'Bash' ? BIG_BASH(stdout) : file))

  expect(((await $.tool.call({ tool: 'Bash', command: 'x' })) as any).result.stdout).toBe(stdout)
  expect(((await $.tool.call({ tool: 'Read', file_path: '/repo/big.txt' })) as any).result).toEqual(file.result)
  expect(writes).toHaveLength(0)
})

test('includeHeadless cuts a headless run too', { options: { includeHeadless: true } }, async ($, on) => {
  const writes = spy(on, {}, [])
  on('tool.call', () => BIG_BASH(lines(1_000)))

  const r: any = await $.tool.call({ tool: 'Bash', command: 'x' })

  expect(writes).toHaveLength(1)
  expect(r.result.stdout).toContain('[tool-spill:')
})

test('includeHeadless cuts a headless whole-file Read too', { options: { includeHeadless: true } }, async ($, on) => {
  spy(on, {}, [])
  on('tool.call', () => readResult(lines(2_000)))

  const r: any = await $.tool.call({ tool: 'Read', file_path: '/repo/big.txt' })

  expect(r.result.file.content).toContain('[tool-spill:')
})

// ---------------------------------------------------------------- Read

test('a whole-file Read over the limit is cut, with the next offset, a limit to use, and a warning about Edit', async ($, on) => {
  const writes = spy(on)
  on('tool.call', () => readResult(lines(2_000), '/repo/big.txt', 1))

  const r: any = await $.tool.call({ tool: 'Read', file_path: '/repo/big.txt' })

  const f = r.result.file
  expect(r.result.type).toBe('text')
  expect(f.content.startsWith('line 0\n')).toBe(true)
  expect(f.content).toContain('2000 lines in the file')
  // counted from the text, not from the numLines the hook wrote, so an off-by-one in both cannot pass
  const shown = f.content.slice(0, f.content.indexOf('\n[tool-spill:')).split('\n')
  expect(shown.at(-1)).toBe(`line ${shown.length - 1}`)
  expect(f.content).toContain(`offset ${1 + shown.length} and limit `)
  expect(Number(/limit (\d+)/.exec(f.content)![1])).toBeGreaterThanOrEqual(20)
  expect(f.content).toContain('Do not Edit text you have not seen')
  expect(f.numLines).toBeLessThan(2_000)
  expect(size(f.content.slice(0, f.content.indexOf('\n[tool-spill:')))).toBeLessThanOrEqual(2_200)
  expect(writes).toHaveLength(0) // the file is already on disk
})

test('a Read of exactly limit bytes passes, limit + 1 is cut (default limit)', async ($, on) => {
  spy(on)
  on('tool.call', (_: unknown, e: any) => readResult(exact(e.file_path === '/repo/at.txt' ? 4_000 : 4_001), e.file_path))

  const at: any = await $.tool.call({ tool: 'Read', file_path: '/repo/at.txt' })
  expect(at.result.file.content).toBe(exact(4_000))

  const over: any = await $.tool.call({ tool: 'Read', file_path: '/repo/over.txt' })
  expect(over.result.file.content).toContain('[tool-spill:')
})

test('a Read the model sliced (offset or limit) passes whole', async ($, on) => {
  const writes = spy(on)
  const content = lines(2_000)
  on('tool.call', () => readResult(content, '/tmp/claude-tool-spill-sam/toolu_1.txt', 301))

  const byOffset: any = await $.tool.call({ tool: 'Read', file_path: '/tmp/claude-tool-spill-sam/toolu_1.txt', offset: 301 })
  const byLimit: any = await $.tool.call({ tool: 'Read', file_path: '/tmp/claude-tool-spill-sam/toolu_1.txt', limit: 2_000 })
  const both: any = await $.tool.call({ tool: 'Read', file_path: '/tmp/claude-tool-spill-sam/toolu_1.txt', offset: 301, limit: 2_000 })

  for (const r of [byOffset, byLimit, both]) expect(r.result.file.content).toBe(content)
  expect(writes).toHaveLength(0)
})

test('instruction files pass whole however large', async ($, on) => {
  spy(on)
  const content = lines(2_000)
  on('tool.call', (_: unknown, e: any) => readResult(content, e.file_path))

  for (const path of [
    '/repo/conexus/skills/foo/SKILL.md',
    '/repo/conexus/skills/foo/reference.md',
    '/repo/.claude/rules/ci.md',
    '/repo/.claude/agents/x.md',
    '/repo/.claude/commands/y.md',
    '/repo/AGENTS.md',
    'CLAUDE.md',
    '/repo/conexus/REGISTER.md',
    '/repo/CLAUDE.local.md',
    'C:\\repo\\CLAUDE.md',
    'C:\\repo\\.claude\\rules\\a.md',
    'C:\\repo\\conexus\\skills\\foo\\SKILL.md',
  ]) {
    const r: any = await $.tool.call({ tool: 'Read', file_path: path })
    expect(r.result.file.content).toBe(content)
  }
})

test('files that only look like instruction files are still cut', async ($, on) => {
  spy(on)
  on('tool.call', (_: unknown, e: any) => readResult(lines(2_000), e.file_path))

  for (const path of ['/repo/docs/MYAGENTS.md', '/repo/skills.py', '/repo/notes/CLAUDE.md.bak', '/repo/src/rules.py', 'C:\\repo\\MYAGENTS.md']) {
    const r: any = await $.tool.call({ tool: 'Read', file_path: path })
    expect(r.result.file.content).toContain('[tool-spill:')
  }
})

test('a Read cut inside one very long line points the next read at that line', async ($, on) => {
  spy(on)
  on('tool.call', () => readResult('x'.repeat(10_000), '/repo/min.js', 7))

  const r: any = await $.tool.call({ tool: 'Read', file_path: '/repo/min.js' })

  expect(r.result.file.content).toContain('offset 7 and limit ')
})

test('a Read under the limit passes unchanged', async ($, on) => {
  const { result } = readResult(lines(50), '/repo/s.txt')
  on('tool.call', () => ({ result }))

  const r: any = await $.tool.call({ tool: 'Read', file_path: '/repo/s.txt' })

  expect(r.result).toEqual(result)
})

test('a Read that failed passes through whole', async ($, on) => {
  const writes = spy(on)
  const content = lines(2_000)
  on('tool.call', () => ({ ...readResult(content), isError: true }))

  const r: any = await $.tool.call({ tool: 'Read', file_path: '/repo/big.txt' })

  expect(r.result.file.content).toBe(content)
  expect(writes).toHaveLength(0)
})

// ---------------------------------------------------------------- every other tool

test('a large result of any other tool passes whole: no denial, no write', async ($, on) => {
  const writes = spy(on)
  const text = lines(2_000)
  on('tool.call', () => ({ result: { content: text }, text }))

  for (const tool of [
    'Grep', 'Glob', 'ToolSearch', 'Skill', 'TaskOutput', 'SendMessage', 'WebFetch', 'Agent', 'Task', 'TodoWrite',
    'AskUserQuestion', 'Edit', 'Write', 'NotebookEdit', 'mcp__srv__dump', 'mcp__plugin_conexus_nexus__memory_get',
  ]) {
    const r: any = await $.tool.call({ tool } as any)
    expect(r.deny).toBeUndefined()
    expect(r.text).toBe(text)
    expect(r.result.content).toBe(text)
  }
  expect(writes).toHaveLength(0)
})

// Shaped like a Bash and a Read result, so only the tool-name gates keep these whole.
test('another tool returning a Bash-shaped or Read-shaped result passes whole', async ($, on) => {
  const writes = spy(on)
  const bashShaped = BIG_BASH(lines(1_000), { stderr: lines(1_000) })
  const readShaped = readResult(lines(2_000))
  let shape: object = bashShaped
  on('tool.call', () => shape)

  for (const tool of ['TaskOutput', 'BashOutput', 'Monitor', 'Grep', 'mcp__srv__dump']) {
    shape = bashShaped
    expect(((await $.tool.call({ tool } as any)) as any).result).toEqual(bashShaped.result)
    shape = readShaped
    expect(((await $.tool.call({ tool } as any)) as any).result).toEqual(readShaped.result)
  }
  expect(writes).toHaveLength(0)
})

// ---------------------------------------------------------------- Bash: reading a spill file back (nexus-i9a1p)

const SPILL = '/var/tmp/x/claude-tool-spill-sam'

// Each command names the spill directory in a different spelling; each output is 10 KB, over the limit and under 4x.
for (const command of [
  `sed -n 10,900p ${SPILL}/toolu_1.txt`,
  'sed -n 1,400p "$TMPDIR/claude-tool-spill-sam/toolu_1.txt"',
  'cd /var/tmp/x/claude-tool-spill-sam && head -c 10000 toolu_1.txt',
  'type C:\\Temp\\claude-tool-spill-sam\\toolu_1.txt',
  `ls ${SPILL}; head -c 10000 ${SPILL}/toolu_1.txt`,
  `ls ${SPILL}`,
]) {
  test(`a Bash read of a spill file under 4x the limit passes whole: ${command}`, async ($, on) => {
    const writes = spy(on, { TMPDIR: '/var/tmp/x/', USER: 'sam' })
    const stdout = exact(10_000)
    on('tool.call', () => BIG_BASH(stdout))

    const r: any = await $.tool.call({ tool: 'Bash', command })

    expect(r.result).toEqual({ stdout, stderr: '' })
    expect(writes).toHaveLength(0)
  })
}

test('a Bash read of a spill file over 4x the limit is cut and saved like any other', async ($, on) => {
  const writes = spy(on, { TMPDIR: '/var/tmp/x/', USER: 'sam' })
  const stdout = lines(2_000)
  expect(size(stdout)).toBeGreaterThan(16_000)
  on('tool.call', () => BIG_BASH(stdout))

  const r: any = await $.tool.call({ tool: 'Bash', command: `cat ${SPILL}/toolu_1.txt` })

  expect(writes).toHaveLength(1)
  expect(writes[0].text).toBe(stdout)
  expect(r.result.stdout).toContain('[tool-spill:')
})

for (const command of ['cat /var/tmp/x/claude-tool-spill-samuel/a.txt', 'cat /var/tmp/x/claude-tool-spill-sam.bak']) {
  test(`a path that only shares a prefix with the spill directory is still cut: ${command}`, async ($, on) => {
    const writes = spy(on, { TMPDIR: '/var/tmp/x/', USER: 'sam' })
    on('tool.call', () => BIG_BASH(exact(10_000)))

    const r: any = await $.tool.call({ tool: 'Bash', command })

    expect(writes).toHaveLength(1)
    expect(r.result.stdout).toContain('[tool-spill:')
  })
}

test('the Bash note points at a sliced Read of the spill file', async ($, on) => {
  const writes = spy(on)
  on('tool.call', () => BIG_BASH(lines(1_000)))

  const r: any = await $.tool.call({ tool: 'Bash', command: 'seq 1000' })

  const { note } = parts(r.result.stdout)
  expect(note).toContain(`Full output: ${writes[0].path}`)
  expect(note).toContain('with the Read tool (offset and limit; a sliced Read is never cut)')
})
