// SPDX-License-Identifier: AGPL-3.0-or-later
import { expect, mock, test } from 'claude-code/testing'

// `claude plugin test tool-spill`. Nothing sits beneath the plugin: the hooks each test registers stand for the
// engine, so a `tool.call` hook answers what the tool "returned", an `fs.write` hook records what was spilled and a
// `session.surfaces` hook says whether the session is interactive. Defaults throughout unless a test sets options:
// limit 5000, head 1400, tail 800, headless runs left alone.

const lines = (n: number) => Array.from({ length: n }, (_, i) => `line ${i}`).join('\n')
// ASCII text of exactly `n` bytes.
const exact = (n: number) => lines(2_000).slice(0, n)
const size = (s: string) => new TextEncoder().encode(s).length
const BIG_BASH = (stdout: string, extra: object = {}) => ({ result: { stdout, stderr: '', ...extra } })
const NOTE = /\n\[tool-spill:[^\]]*\]\n?/

// `env` is what $TMPDIR, $USER and friends answer (unlisted ones are unset); `surfaces` what the session draws on;
// `checkExit` what the private-directory check exits with. The returned list carries `checks`, the argv of each check.
function spy(on: any, env: Record<string, string> = {}, surfaces: string[] = ['terminal'], checkExit = 0) {
  mock.env(on, env)
  on('session.surfaces', () => ({ value: surfaces }))
  const writes: { path: string; text: string }[] = []
  on('fs.write', (_: unknown, e: { path: string; text: string }) => {
    writes.push({ path: e.path, text: e.text })
    return { value: undefined }
  })
  const checks = privateDirCheck(on, checkExit)
  return Object.assign(writes, { checks })
}

function privateDirCheck(on: any, exitCode = 0) {
  const checks: string[][] = []
  on('process.run', (_: unknown, e: { argv: readonly string[] }) => {
    checks.push([...e.argv])
    return { value: { exitCode, stdout: '', stderr: '' } }
  })
  return checks
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

test('a Bash result of exactly limit bytes passes unchanged (default limit 5000)', async ($, on) => {
  const writes = spy(on)
  const stdout = exact(5_000)
  expect(size(stdout)).toBe(5_000)
  on('tool.call', () => BIG_BASH(stdout))

  const r: any = await $.tool.call({ tool: 'Bash', command: 'ls' })

  expect(r.result).toEqual({ stdout, stderr: '' })
  expect(writes).toHaveLength(0)
})

test('a Bash result of limit + 1 bytes spills (default limit 5000)', async ($, on) => {
  const writes = spy(on)
  const stdout = exact(5_001)
  on('tool.call', () => BIG_BASH(stdout))

  const r: any = await $.tool.call({ tool: 'Bash', command: 'ls' })

  expect(writes).toHaveLength(1)
  expect(writes[0].text).toBe(stdout)
  expect(r.result.stdout).toContain('[tool-spill:')
  expect(size(r.result.stdout)).toBeLessThan(5_001)
})

test('stdout and stderr count together toward the limit', async ($, on) => {
  const writes = spy(on)
  on('tool.call', () => ({ result: { stdout: exact(3_000), stderr: exact(2_500) } }))

  const r: any = await $.tool.call({ tool: 'Bash', command: 'x' })

  // stdout alone (3000) is under the limit; only the 5500-byte sum makes it spill
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
  // the note names the cut lines by number, and they are exactly the ones between head and tail
  const [, from, to] = /lines (\d+)-(\d+) cut/.exec(note)!.map(Number)
  expect(from).toBe(head.split('\n').length + 1)
  expect(to - from + 1 + head.split('\n').length + tail.split('\n').length).toBe(1_000)
})

test('a failure on the last line of an exit-0 result is still in what the model reads', async ($, on) => {
  spy(on)
  const stdout = lines(1_000) + '\nFAILED: 3 tests, exit code hidden by tee'
  on('tool.call', () => BIG_BASH(stdout))

  const r: any = await $.tool.call({ tool: 'Bash', command: 'make test | tee log' })

  expect(r.result.stdout.endsWith('FAILED: 3 tests, exit code hidden by tee')).toBe(true)
})

test('error lines in the cut middle are named by their line numbers in the file', async ($, on) => {
  spy(on)
  const all = lines(1_000).split('\n')
  all[500] = 'ERROR: disk full'
  all[501] = 'Traceback (most recent call last):'
  all[502] = 'java.lang.IllegalStateException: boom'
  on('tool.call', () => BIG_BASH(all.join('\n')))

  const r: any = await $.tool.call({ tool: 'Bash', command: 'x' })

  const { head, tail, note } = parts(r.result.stdout)
  expect(head + tail).not.toContain('disk full') // really in the cut part
  expect(note).toContain('error-shaped lines 501, 502, 503')
})

test('at most three error lines are named, and the rest are counted', async ($, on) => {
  spy(on)
  const all = lines(1_000).split('\n')
  for (const i of [400, 410, 420, 430, 440]) all[i] = `FAILED tests/test_x.py::test_${i} - AssertionError: no`
  on('tool.call', () => BIG_BASH(all.join('\n')))

  const r: any = await $.tool.call({ tool: 'Bash', command: 'pytest' })

  expect(parts(r.result.stdout).note).toContain('error-shaped lines 401, 411, 421 and 2 more')
})

test('a clean middle names no error lines', async ($, on) => {
  spy(on)
  on('tool.call', () => BIG_BASH(lines(1_000)))

  const r: any = await $.tool.call({ tool: 'Bash', command: 'x' })

  expect(parts(r.result.stdout).note).not.toContain('error')
})

// Precision fixtures for the error shapes (tool-spill census 2026-11-07, T2 nexus/tool-spill-census-2026-11-07: the
// old word list flagged 1467 cut lines of which 40 were real errors). Each REAL line is an error a reader must not miss;
// each NOT line is prose, code or a passing log line that only uses an error word.
const REAL_ERRORS = [
  'E       AssertionError: expected 3, got 4',
  'FAILED tests/test_a.py::test_b - KeyError: x',
  'Traceback (most recent call last):',
  'ValueError: bad input',
  'java.lang.IllegalStateException: boom',
  'fatal: not a git repository (or any of the parent directories): .git',
  "cat: /nope: No such file or directory",
  'ERROR: Could not find a version that satisfies the requirement',
  'error: failed to push some refs',
  'src/x.c:12:5: error: expected ;',
  'panic: runtime error: index out of range',
]
const NOT_ERRORS = [
  'def handle_error(self, exc):',
  '    # errors are retried; a failure here is not fatal',
  'test_fail_closed_when_unreachable PASSED',
  '2 failed, 40 passed',
  'ok: 0 errors, 0 warnings',
  '  --fail-fast    stop at the first failure',
  'raise ValueError(msg)',
  'except FileNotFoundError:',
  '    except (KeyError, ValueError):',
  'if not found: return cannot_happen',
  'Permission model: access denied paths are listed below',
  'timed out waiting is a normal outcome here',
  'the error budget is 5%',
]

test('every real error shape in the cut middle is named', async ($, on) => {
  spy(on)
  const all = lines(1_000).split('\n')
  REAL_ERRORS.forEach((l, i) => { all[300 + i * 20] = l })
  on('tool.call', () => BIG_BASH(all.join('\n')))

  const r: any = await $.tool.call({ tool: 'Bash', command: 'x' })

  expect(parts(r.result.stdout).note).toContain(`error-shaped lines 301, 321, 341 and ${REAL_ERRORS.length - 3} more`)
})

test('lines that only use an error word are not error-shaped', async ($, on) => {
  spy(on)
  const all = lines(1_000).split('\n')
  NOT_ERRORS.forEach((l, i) => { all[300 + i * 20] = l })
  on('tool.call', () => BIG_BASH(all.join('\n')))

  const r: any = await $.tool.call({ tool: 'Bash', command: 'x' })

  expect(parts(r.result.stdout).note).not.toContain('error-shaped')
})

test('the Read the note names returns exactly the cut lines from the spill file', async ($, on) => {
  const writes = spy(on)
  on('tool.call', () => BIG_BASH(lines(1_000)))

  const r: any = await $.tool.call({ tool: 'Bash', command: 'seq 1000' })

  const { head, tail, note } = parts(r.result.stdout)
  const [, offset, limit] = /Read offset (\d+) limit (\d+)/.exec(note)!.map(Number)
  const file = writes[0].text.split('\n')
  const read = file.slice(offset - 1, offset - 1 + limit)
  expect(read[0]).toBe(file[head.split('\n').length])
  expect([...head.split('\n'), ...read, ...tail.split('\n')]).toEqual(file)
})

test('a cut too big for one Read is pointed at in slices', async ($, on) => {
  const writes = spy(on)
  on('tool.call', () => BIG_BASH(lines(20_000)))

  const r: any = await $.tool.call({ tool: 'Bash', command: 'seq 20000' })

  const { note } = parts(r.result.stdout)
  const [, from, to] = /lines (\d+)-(\d+) cut/.exec(note)!.map(Number)
  const [, offset, limit] = /Read offset (\d+) limit (\d+)/.exec(note)!.map(Number)
  expect(offset).toBe(from)
  expect(limit).toBeLessThan(to - from + 1)
  // about 4x the limit per slice, as the Read note suggests
  const slice = writes[0].text.split('\n').slice(offset - 1, offset - 1 + limit).join('\n')
  expect(size(slice)).toBeLessThanOrEqual(4 * 5_000)
  expect(size(slice)).toBeGreaterThan(3 * 5_000)
  expect(note).toContain('in slices')
})

test('a stderr cut names its lines as they sit in the spill file, after stdout', async ($, on) => {
  const writes = spy(on)
  on('tool.call', () => ({ result: { stdout: lines(500), stderr: lines(700) } }))

  const r: any = await $.tool.call({ tool: 'Bash', command: 'noisy' })

  const { head, note } = parts(r.result.stderr)
  const [, offset] = /Read offset (\d+)/.exec(note)!.map(Number)
  const file = writes[0].text.split('\n')
  const errStart = file.indexOf('--- stderr ---') + 1
  expect(offset - 1).toBe(errStart + head.split('\n').length)
  expect(file[offset - 1]).toBe(lines(700).split('\n')[head.split('\n').length])
})

test('the note is short: under 170 bytes beside its path (the old one was about 230)', async ($, on) => {
  const writes = spy(on)
  const all = lines(1_000).split('\n')
  all[500] = 'ValueError: x'
  on('tool.call', () => BIG_BASH(all.join('\n')))

  const r: any = await $.tool.call({ tool: 'Bash', command: 'x' })

  const { note } = parts(r.result.stdout)
  expect(size(note) - size(writes[0].path)).toBeLessThan(170)
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

  const r: any = await $.tool.call({ tool: 'Bash', command: 'grep -n . README.md' })

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
  privateDirCheck(on)
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

test('the limit counts UTF-8 bytes: 1800 three-byte characters spill', async ($, on) => {
  const writes = spy(on)
  const stdout = '日本語'.repeat(600) // 1800 characters, 5400 bytes
  on('tool.call', () => BIG_BASH(stdout))

  const r: any = await $.tool.call({ tool: 'Bash', command: 'x' })

  expect(writes).toHaveLength(1)
  expect(size(r.result.stdout)).toBeLessThan(5_400)
})

test('the byte boundary holds for multi-byte text: 4998 bytes pass, 5001 spill', async ($, on) => {
  const writes = spy(on)
  // 1666 x 3 bytes = 4998: passes. 1667 x 3 bytes = 5001: spills.
  on('tool.call', (_: unknown, e: any) => BIG_BASH('語'.repeat(e.command === 'under' ? 1_666 : 1_667)))

  const under: any = await $.tool.call({ tool: 'Bash', command: 'under' })
  expect(under.result.stdout).toBe('語'.repeat(1_666))
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

test('nonsense options fall back to the defaults: 5000 passes, 5001 spills', { options: { limit: -5, head: 0, tail: -3 } }, async ($, on) => {
  const writes = spy(on)
  on('tool.call', (_: unknown, e: any) => BIG_BASH(exact(e.command === 'at' ? 5_000 : 5_001)))

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

test('the first spill makes the directory private: /bin/sh runs the check with the directory as $1', async ($, on) => {
  const writes = spy(on, { TMPDIR: '/tmp', USER: 'sam' })
  on('tool.call', () => BIG_BASH(lines(1_000)))

  await $.tool.call({ tool: 'Bash', command: 'x' })

  expect(writes.checks).toHaveLength(1)
  const [sh, flag, script, name, dir] = writes.checks[0]
  expect([sh, flag, name, dir]).toEqual(['/bin/sh', '-c', 'tool-spill', '/tmp/claude-tool-spill-sam'])
  for (const step of ['umask 077', '[ -L "$1" ] && exit 3', '$(id -u)', 'chmod 700 "$1"']) expect(script).toContain(step)
  expect(writes[0].path.startsWith('/tmp/claude-tool-spill-sam/')).toBe(true)
})

test('a directory the check refuses (a symlink, another user owns it) turns spilling off: output passes whole', async ($, on) => {
  const writes = spy(on, { USER: 'sam' }, ['terminal'], 5)
  const toasts: string[] = []
  on('ui.toast', (_: unknown, e: { text: string }) => {
    toasts.push(e.text)
    return { value: undefined }
  })
  const stdout = lines(1_000)
  on('tool.call', () => BIG_BASH(stdout))

  const first: any = await $.tool.call({ tool: 'Bash', command: 'one' })
  const second: any = await $.tool.call({ tool: 'Bash', command: 'two' })

  expect(first.result.stdout).toBe(stdout)
  expect(second.result.stdout).toBe(stdout)
  expect(writes).toHaveLength(0)
  expect(writes.checks).toHaveLength(1) // the refusal is kept, not checked per output
  expect(toasts).toHaveLength(1) // said once, not per output
  expect(toasts[0]).toContain('/tmp/claude-tool-spill-sam')
  expect(toasts[0]).toContain('exit 5')
})

test('the check runs before every spill: temp cleanup can remove the directory mid-session', async ($, on) => {
  const writes = spy(on)
  on('tool.call', () => BIG_BASH(lines(1_000)))

  await $.tool.call({ tool: 'Bash', command: 'one' })
  await $.tool.call({ tool: 'Bash', command: 'two' })

  expect(writes).toHaveLength(2)
  expect(writes.checks).toHaveLength(2)
})

test('a POSIX temp dir with a backslash in its name is still checked', async ($, on) => {
  const writes = spy(on, { TMPDIR: '/tmp/a\\b', USER: 'sam' })
  on('tool.call', () => BIG_BASH(lines(1_000)))

  await $.tool.call({ tool: 'Bash', command: 'x' })

  expect(writes.checks).toHaveLength(1)
  expect(writes.checks[0][4]).toBe('/tmp/a\\b/claude-tool-spill-sam')
})

test('a check that cannot run is not kept: output passes whole and the next spill checks again', async ($, on) => {
  mock.env(on, {})
  on('session.surfaces', () => ({ value: ['terminal'] }))
  const writes: { path: string }[] = []
  on('fs.write', (_: unknown, e: { path: string }) => {
    writes.push({ path: e.path })
    return { value: undefined }
  })
  let runs = 0
  on('process.run', () => {
    if (runs++ === 0) throw new Error('cannot start /bin/sh')
    return { value: { exitCode: 0, stdout: '', stderr: '' } }
  })
  const stdout = lines(1_000)
  on('tool.call', () => BIG_BASH(stdout))

  const first: any = await $.tool.call({ tool: 'Bash', command: 'one' })
  expect(first.result.stdout).toBe(stdout)
  expect(writes).toHaveLength(0)

  await $.tool.call({ tool: 'Bash', command: 'two' })
  expect(runs).toBe(2)
  expect(writes).toHaveLength(1)
})

test('a Windows temp dir is per-user already: no check runs and the output spills', async ($, on) => {
  const writes = spy(on, { TEMP: 'C:\\Users\\sam\\AppData\\Local\\Temp', USERNAME: 'sam' })
  on('tool.call', () => BIG_BASH(lines(1_000)))

  await $.tool.call({ tool: 'Bash', command: 'x' })

  expect(writes.checks).toHaveLength(0)
  expect(writes).toHaveLength(1)
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
  on('tool.call', (_: unknown, e: any) => readResult(exact(e.file_path === '/repo/at.txt' ? 5_000 : 5_001), e.file_path))

  const at: any = await $.tool.call({ tool: 'Read', file_path: '/repo/at.txt' })
  expect(at.result.file.content).toBe(exact(5_000))

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
    expect(writes.checks).toHaveLength(0) // nothing is written, so the directory check does not run
  })
}

test('a Bash read of a spill file over 4x the limit is cut and saved like any other', async ($, on) => {
  const writes = spy(on, { TMPDIR: '/var/tmp/x/', USER: 'sam' })
  const stdout = lines(2_500)
  expect(size(stdout)).toBeGreaterThan(20_000)
  on('tool.call', () => BIG_BASH(stdout))

  const r: any = await $.tool.call({ tool: 'Bash', command: `cat ${SPILL}/toolu_1.txt` })

  expect(writes).toHaveLength(1)
  expect(writes[0].text).toBe(stdout)
  expect(r.result.stdout).toContain('[tool-spill:')
})

for (const command of ['head -c 10000 /var/tmp/x/claude-tool-spill-samuel/a.txt', 'head -c 10000 /var/tmp/x/claude-tool-spill-sam.bak']) {
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
  expect(note).toMatch(/Read offset \d+ limit \d+/)
})

// ---------------------------------------------------------------- Defaults Sam chose (ask tool-spill-defaults, 2026-11-07)

test('B: an output of 5000 bytes is never cut by default', async ($, on) => {
  const writes = spy(on)
  on('tool.call', () => BIG_BASH(exact(5_000)))
  const r: any = await $.tool.call({ tool: 'Bash', command: 'x' })
  expect(r.result.stdout).toBe(exact(5_000))
  expect(writes).toHaveLength(0)
})

test('B: 5001 bytes spill by default', async ($, on) => {
  const writes = spy(on)
  on('tool.call', () => BIG_BASH(exact(5_001)))
  const r: any = await $.tool.call({ tool: 'Bash', command: 'x' })
  expect(r.result.stdout).toContain('[tool-spill:')
  expect(writes).toHaveLength(1)
})

// A: a plain read of one file is the model's own slice, as a sliced Read is.
for (const command of ['cat /repo/a.txt', 'cat -- "/repo/my file.txt"', "sed -n 10,400p /repo/a.py",
                       "sed -n '1,200p' src/x.ts", '  cat /repo/a.txt  ']) {
  test(`A: a plain one-file read passes whole up to 3x the limit: ${command}`, async ($, on) => {
    const writes = spy(on)
    const stdout = exact(15_000)
    on('tool.call', () => BIG_BASH(stdout))
    const r: any = await $.tool.call({ tool: 'Bash', command })
    expect(r.result.stdout).toBe(stdout)
    expect(writes).toHaveLength(0)
  })
}

test('A: a plain one-file read over 3x the limit is cut', async ($, on) => {
  const writes = spy(on)
  on('tool.call', () => BIG_BASH(exact(15_001)))
  const r: any = await $.tool.call({ tool: 'Bash', command: 'cat /repo/a.txt' })
  expect(r.result.stdout).toContain('[tool-spill:')
  expect(writes).toHaveLength(1)
})

for (const command of ['cat /repo/a.txt | head -400', 'cat a.txt b.txt', 'cat /repo/a.txt; ls', 'cat $(ls *.txt)',
                       'sed -n 1,400p a.py && echo done', "sed -n '/foo/p' a.py", 'sed -i 1d a.py', 'cat < a.txt',
                       'cat a.txt > b.txt', 'grep -n x a.py', 'cat `pick`', 'sed -n 1,400p a.py b.py']) {
  test(`A: anything but a plain one-file cat or sed -n range is cut as before: ${command}`, async ($, on) => {
    const writes = spy(on)
    on('tool.call', () => BIG_BASH(exact(10_000)))
    const r: any = await $.tool.call({ tool: 'Bash', command })
    expect(r.result.stdout).toContain('[tool-spill:')
    expect(writes).toHaveLength(1)
  })
}

test('C stays: a non-zero-exit Bash result still passes whole', async ($, on) => {
  const writes = spy(on)
  const stdout = exact(20_000)
  on('tool.call', () => ({ ...BIG_BASH(stdout), isError: true }))
  const r: any = await $.tool.call({ tool: 'Bash', command: 'pytest' })
  expect(r.result.stdout).toBe(stdout)
  expect(writes).toHaveLength(0)
})

// ---------------------------------------------------------------- Read: a whole Read of a spill file (census P2)

test('a whole Read of a spill file under 4x the limit passes whole', async ($, on) => {
  spy(on, { TMPDIR: '/var/tmp/x/', USER: 'sam' })
  const content = exact(10_000)
  on('tool.call', () => readResult(content, `${SPILL}/toolu_1.txt`))

  const r: any = await $.tool.call({ tool: 'Read', file_path: `${SPILL}/toolu_1.txt` })

  expect(r.result.file.content).toBe(content)
})

test('a whole Read of a spill file over 4x the limit is cut like any other', async ($, on) => {
  spy(on, { TMPDIR: '/var/tmp/x/', USER: 'sam' })
  const content = lines(2_500)
  expect(size(content)).toBeGreaterThan(20_000)
  on('tool.call', () => readResult(content, `${SPILL}/toolu_1.txt`))

  const r: any = await $.tool.call({ tool: 'Read', file_path: `${SPILL}/toolu_1.txt` })

  expect(r.result.file.content).toContain('[tool-spill:')
})

for (const filePath of ['/var/tmp/x/claude-tool-spill-samuel/a.txt', '/repo/claude-tool-spill-sam.bak/a.txt', '/repo/big.txt']) {
  test(`a whole Read of a 10 KB file outside the spill directory is still cut: ${filePath}`, async ($, on) => {
    spy(on, { TMPDIR: '/var/tmp/x/', USER: 'sam' })
    on('tool.call', () => readResult(exact(10_000), filePath))

    const r: any = await $.tool.call({ tool: 'Read', file_path: filePath })

    expect(r.result.file.content).toContain('[tool-spill:')
  })
}
