import { spawnSync } from 'node:child_process'
import { resolve } from 'node:path'
import { beforeAll, describe, expect, it } from 'vitest'

// The unit tests run xterm unminified, where the DECRQM handler works. This one runs
// the xterm that the project's own vite config minifies, which is what a browser
// gets: there the handler throws a ReferenceError out of xterm's write loop.
const WEB = resolve(import.meta.dirname, '..')
const BUILDER = resolve(import.meta.dirname, 'fixtures/terminalBundleBuild.mjs')
const RUNNER = resolve(import.meta.dirname, 'fixtures/terminalBundleRun.mjs')

const ESC = '\x1b'
// The sequences agy writes in its first bytes, with text on each side.
const AGY_STARTUP = `x\r\n${ESC}[?2026$p${ESC}[?2027$p${ESC}[c${ESC}[6n${ESC}]10;?${ESC}\\${ESC}]11;?\x07after\r\nlast`

let bundle

beforeAll(() => {
  const built = spawnSync(process.execPath, [BUILDER], {
    cwd: WEB, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, timeout: 120000,
  })
  expect(built.status, built.stderr).toBe(0)
  bundle = built.stdout
}, 130000)

function run(mode, data) {
  const result = spawnSync(
    process.execPath,
    [RUNNER, mode, JSON.stringify(data)],
    { input: bundle, cwd: WEB, encoding: 'utf8', timeout: 60000 },
  )
  expect(result.status, result.stderr).toBe(0)
  return JSON.parse(result.stdout)
}

describe('xterm as the production build minifies it', () => {
  // If this fails, the minifier no longer breaks DECRQM and the cases below no
  // longer prove anything about the fix: drop this control and keep them.
  it('control: unprotected, agy\'s startup throws and loses the text after it', () => {
    const result = run('control', AGY_STARTUP)
    expect(result.uncaught.join('\n')).toMatch(/ReferenceError/)
    expect(result.finished).toBe(false)
    expect(result.rows).not.toContain('after')
  })

  it('renders everything after agy\'s startup, throws nothing and replies with nothing', () => {
    const result = run('silenced', AGY_STARTUP)
    expect(result.uncaught).toEqual([])
    expect(result.finished).toBe(true)
    expect(result.replies).toEqual([])
    expect(result.rows).toEqual(['x', 'after', 'last'])
  })

  it('handles the minimal byte string from the report', () => {
    const result = run('silenced', `x\r\n${ESC}[?2026$pafter`)
    expect(result.uncaught).toEqual([])
    expect(result.rows).toEqual(['x', 'after'])
  })
})
