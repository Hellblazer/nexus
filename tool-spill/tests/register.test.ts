// SPDX-License-Identifier: AGPL-3.0-or-later
import { expect, mock, test } from 'claude-code/testing'

// `claude plugin test tool-spill`. Nothing sits beneath the plugin: the hooks each test registers stand for the
// engine, so a `tool.call` hook answers what the tool "returned" and an `fs.write` hook records what was spilled.

const lines = (n: number) => Array.from({ length: n }, (_, i) => `line ${i}`).join('\n')

// `env` is what $TMPDIR and friends answer; none of them set by default.
function spy(on: any, env: Record<string, string> = {}) {
  mock.env(on, env)
  const writes: { path: string; text: string }[] = []
  on('fs.write', (_: unknown, e: { path: string; text: string }) => {
    writes.push({ path: e.path, text: e.text })
    return { value: undefined }
  })
  return writes
}

test('a Bash result under the limit passes unchanged', async ($, on) => {
  const writes = spy(on)
  const stdout = lines(100).slice(0, 3_900)
  on('tool.call', () => ({ result: { stdout, stderr: '' } }))

  const r: any = await $.tool.call({ tool: 'Bash', command: 'ls' })

  expect(r.result).toEqual({ stdout, stderr: '' })
  expect(writes).toHaveLength(0)
})

test('a Bash result over the limit is cut at a line end, with a note, and the whole text is written', async ($, on) => {
  const writes = spy(on, { TMPDIR: '/var/tmp/x/' })
  const stdout = lines(1_000)
  on('tool.call', () => ({ result: { stdout, stderr: '' } }))

  const r: any = await $.tool.call({ tool: 'Bash', command: 'seq 1000' })

  expect(writes).toHaveLength(1)
  expect(writes[0].path).toMatch(/^\/var\/tmp\/x\/claude-tool-spill\/[A-Za-z0-9_.-]+\.txt$/)
  expect(writes[0].text).toBe(stdout)
  const shown: string = r.result.stdout
  expect(shown.startsWith('line 0\nline 1\n')).toBe(true)
  expect(shown.length).toBeLessThan(2_300)
  expect(shown).toContain('[tool-spill:')
  expect(shown).toContain('1000 lines')
  expect(shown).toContain(`Full output: ${writes[0].path}`)
  // the cut lands on a line end: the last shown line is whole
  const body = shown.slice(0, shown.indexOf('\n[tool-spill:'))
  expect(stdoutLines(stdout)).toContain(body.split('\n').at(-1))
})

function stdoutLines(s: string): string[] {
  return s.split('\n')
}

test('stderr over the head is cut too and both land in the file', async ($, on) => {
  const writes = spy(on)
  on('tool.call', () => ({ result: { stdout: lines(500), stderr: lines(500) } }))

  const r: any = await $.tool.call({ tool: 'Bash', command: 'noisy' })

  expect(writes[0].text).toContain('--- stderr ---')
  expect(r.result.stderr).toContain('stderr cut')
  expect(r.result.stderr.length).toBeLessThan(2_300)
})

test('output Claude Code already persisted is left alone', async ($, on) => {
  const writes = spy(on)
  const persisted = { stdout: '<persisted-output>\n' + lines(1_000).slice(0, 1_900) + '\n</persisted-output>', stderr: '' }
  const raw = { stdout: lines(1_000), stderr: '', rawOutputPath: '/somewhere/out.txt' }
  on('tool.call', (_: unknown, e: any) => ({ result: e.command === 'big' ? persisted : raw }))

  expect(((await $.tool.call({ tool: 'Bash', command: 'big' })) as any).result).toEqual(persisted)
  expect(((await $.tool.call({ tool: 'Bash', command: 'big2' })) as any).result).toEqual(raw)
  expect(writes).toHaveLength(0)
})

test('a subagent call (agentId set) is untouched', async ($, on) => {
  const writes = spy(on)
  const stdout = lines(1_000)
  on('tool.call', () => ({ result: { stdout, stderr: '' } }))

  const r: any = await $.tool.call({ tool: 'Bash', command: 'seq 1000', agentId: 'a1' })

  expect(r.result.stdout).toBe(stdout)
  expect(writes).toHaveLength(0)
})

test('a Read over the limit is cut, with totalLines and the next offset in the note', async ($, on) => {
  const writes = spy(on)
  const content = lines(2_000)
  on('tool.call', () => ({
    result: { type: 'text', file: { filePath: '/repo/big.txt', content, startLine: 1, numLines: 2_000, totalLines: 2_000 } },
  }))

  const r: any = await $.tool.call({ tool: 'Read', file_path: '/repo/big.txt' })

  const f = r.result.file
  expect(f.content.startsWith('line 0\n')).toBe(true)
  expect(f.content).toContain('2000 lines')
  expect(f.content).toContain('/repo/big.txt')
  expect(f.content).toContain(`offset ${1 + f.numLines}`)
  expect(f.numLines).toBeLessThan(2_000)
  expect(r.result.type).toBe('text')
  expect(writes).toHaveLength(0)
})

test('a Read under the limit passes unchanged', async ($, on) => {
  const result = { type: 'text', file: { filePath: '/repo/s.txt', content: lines(50), startLine: 1, numLines: 50, totalLines: 50 } }
  on('tool.call', () => ({ result }))

  const r: any = await $.tool.call({ tool: 'Read', file_path: '/repo/s.txt' })

  expect(r.result).toEqual(result)
})

test('an MCP-style tool over the limit is denied with the head and the path', async ($, on) => {
  const writes = spy(on)
  const text = lines(1_000)
  on('tool.call', () => ({ result: {}, text }))

  const r: any = await $.tool.call({ tool: 'mcp__srv__dump', foo: 1 } as any)

  expect(r.deny).toContain('output saved to a file instead.')
  expect(r.deny.startsWith('output saved to a file instead.\nline 0\n')).toBe(true)
  expect(writes).toHaveLength(1)
  expect(writes[0].text).toBe(text)
  expect(r.deny).toContain(`Full output: ${writes[0].path}`)
})

test('an MCP-style tool under the limit passes unchanged', async ($, on) => {
  const writes = spy(on)
  on('tool.call', () => ({ result: {}, text: 'short' }))

  const r: any = await $.tool.call({ tool: 'mcp__srv__dump' } as any)

  expect(r.deny).toBeUndefined()
  expect(r.text).toBe('short')
  expect(writes).toHaveLength(0)
})

test('an exempt tool is untouched however large its result', async ($, on) => {
  const writes = spy(on)
  const text = lines(2_000)
  on('tool.call', () => ({ result: {}, text }))

  for (const tool of ['Agent', 'Task', 'TodoWrite', 'AskUserQuestion', 'Edit', 'Write', 'NotebookEdit']) {
    const r: any = await $.tool.call({ tool } as any)
    expect(r.deny).toBeUndefined()
    expect(r.text).toBe(text)
  }
  expect(writes).toHaveLength(0)
})

test('a raised limit lets a 6 KB result through', { options: { limit: 10_000, head: 500 } }, async ($, on) => {
  const writes = spy(on)
  const stdout = lines(1_000).slice(0, 6_000)
  on('tool.call', () => ({ result: { stdout, stderr: '' } }))

  const r: any = await $.tool.call({ tool: 'Bash', command: 'x' })

  expect(r.result.stdout).toBe(stdout)
  expect(writes).toHaveLength(0)
})

test('a smaller head keeps fewer bytes', { options: { limit: 1_000, head: 300 } }, async ($, on) => {
  const writes = spy(on)
  on('tool.call', () => ({ result: { stdout: lines(500), stderr: '' } }))

  const r: any = await $.tool.call({ tool: 'Bash', command: 'x' })

  const body = r.result.stdout.slice(0, r.result.stdout.indexOf('\n[tool-spill:'))
  expect(body.length).toBeLessThanOrEqual(300)
  expect(body.length).toBeGreaterThan(150)
  expect(writes).toHaveLength(1)
})

test('a head larger than the limit is capped at the limit', { options: { limit: 1_000, head: 50_000 } }, async ($, on) => {
  spy(on)
  on('tool.call', () => ({ result: { stdout: lines(500), stderr: '' } }))

  const r: any = await $.tool.call({ tool: 'Bash', command: 'x' })

  const body = r.result.stdout.slice(0, r.result.stdout.indexOf('\n[tool-spill:'))
  expect(body.length).toBeLessThanOrEqual(1_000)
})

test('a nonsense option falls back to the default', { options: { limit: -5, head: 0 } }, async ($, on) => {
  const writes = spy(on)
  on('tool.call', () => ({ result: { stdout: lines(1_000), stderr: '' } }))

  const r: any = await $.tool.call({ tool: 'Bash', command: 'x' })

  expect(writes).toHaveLength(1)
  expect(r.result.stdout.length).toBeLessThan(2_300)
})

test('the spill directory is $TMPDIR, else $TEMP, else $TMP, else /tmp', async ($, on) => {
  const writes = spy(on, { TEMP: '/var/tmp/win//', TMP: '/var/tmp/other' })
  on('tool.call', () => ({ result: { stdout: lines(1_000), stderr: '' } }))

  await $.tool.call({ tool: 'Bash', command: 'x' })

  expect(writes[0].path).toMatch(/^\/var\/tmp\/win\/claude-tool-spill\/[A-Za-z0-9_.-]+\.txt$/)
})

test('with no temp variable set the directory is /tmp/claude-tool-spill', async ($, on) => {
  const writes = spy(on)
  on('tool.call', () => ({ result: { stdout: lines(1_000), stderr: '' } }))

  await $.tool.call({ tool: 'Bash', command: 'x' })

  expect(writes[0].path).toMatch(/^\/tmp\/claude-tool-spill\/[A-Za-z0-9_.-]+\.txt$/)
})
