// Connection settings for the tests: real environment variables win, then the .env that python tools/pg.py up writes one folder above.
import { existsSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'

export const WEB_DB = process.env.XERLANE_WEB_TEST_DB || 'xerlane_web_test'

function dotenv(): Record<string, string> {
  const out: Record<string, string> = {}
  for (const path of [resolve(process.cwd(), '.env'), resolve(process.cwd(), '..', '.env')]) {
    if (!existsSync(path)) continue
    for (const line of readFileSync(path, 'utf8').split(/\r?\n/)) {
      const i = line.indexOf('=')
      if (i > 0) out[line.slice(0, i).trim()] ??= line.slice(i + 1).trim()
    }
  }
  return out
}

export function secret(name: string): string {
  const v = process.env[name] ?? dotenv()[name]
  if (!v) throw new Error(`${name} is not set: run python tools/pg.py up from the repository root, which writes .env, or export it`)
  return v
}

export const host = () => process.env.XERLANE_PG_HOST || 'localhost'
export const port = () => Number(process.env.XERLANE_PG_PORT || 5435)
