import { spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { resolve } from 'node:path'
import pg from 'pg'
import { adminClient, seed } from './seed'
import { host, port, secret, WEB_DB } from './env'

const ROOT = resolve(process.cwd(), '..')

async function recreateDatabase(): Promise<void> {
  const c = new pg.Client({ host: host(), port: port(), database: 'postgres', user: 'postgres', password: secret('XERLANE_ADMIN_PASSWORD') })
  await c.connect()
  await c.query(`DROP DATABASE IF EXISTS "${WEB_DB}" WITH (FORCE)`)
  await c.query(`CREATE DATABASE "${WEB_DB}"`)
  await c.end()
}

export default async function setup(): Promise<() => Promise<void>> {
  await recreateDatabase()
  // The schema comes from the Python side on purpose: the web app must work on the tables xerlane itself creates.
  const init = spawnSync(process.env.PYTHON || 'python', ['-m', 'xerlane', 'init-db'], {
    cwd: ROOT, encoding: 'utf8', env: { ...process.env, XERLANE_PG_DB: WEB_DB, XERLANE_ADMIN_DSN: '', XERLANE_DSN: '', XERLANE_APP_PASSWORD: secret('XERLANE_APP_PASSWORD'), XERLANE_OWNER_PASSWORD: secret('XERLANE_OWNER_PASSWORD'), XERLANE_ADMIN_PASSWORD: secret('XERLANE_ADMIN_PASSWORD') },
  })
  if (init.status !== 0) throw new Error(`xerlane init-db failed: ${init.stderr || init.stdout}`)
  const admin = await adminClient()
  await seed(admin)
  await admin.end()
  process.env.XERLANE_PG_DB = WEB_DB
  process.env.XERLANE_APP_PASSWORD = secret('XERLANE_APP_PASSWORD')
  if (!existsSync(resolve(process.cwd(), '.output/server/index.mjs'))) throw new Error('The built server is missing: run npm run build before npm test.')
  return async () => {
    const c = new pg.Client({ host: host(), port: port(), database: 'postgres', user: 'postgres', password: secret('XERLANE_ADMIN_PASSWORD') })
    await c.connect()
    await c.query(`DROP DATABASE IF EXISTS "${WEB_DB}" WITH (FORCE)`)
    await c.end()
  }
}
