// Database access. The web app connects as xerlane_app, a role without superuser and without BYPASSRLS, and refuses to run as anything else.
// Every query that touches a tenant's data runs inside withTenant(), which sets app.tenant_id for that one transaction. The tenant comes from the
// session row the server found, never from a request.
import { existsSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import pg from 'pg'

let pool: pg.Pool | undefined
let checked = false

function dotenv(): Record<string, string> {
  const out: Record<string, string> = {}
  for (const path of [resolve(process.cwd(), '.env'), resolve(process.cwd(), '..', '.env')]) {
    if (!existsSync(path)) continue
    for (const line of readFileSync(path, 'utf8').split(/\r?\n/)) {
      const i = line.indexOf('=')
      if (i > 0 && !line.trimStart().startsWith('#')) out[line.slice(0, i).trim()] ??= line.slice(i + 1).trim()
    }
  }
  return out
}

export function getPool(): pg.Pool {
  if (!pool) {
    const env = { ...dotenv(), ...process.env } as Record<string, string | undefined>
    if (env.XERLANE_DSN) {
      pool = new pg.Pool({ connectionString: env.XERLANE_DSN, max: 10, connectionTimeoutMillis: 10000 })
    } else {
      if (!env.XERLANE_APP_PASSWORD) throw new Error('XERLANE_APP_PASSWORD is not set (python tools/pg.py up writes ../.env) and XERLANE_DSN is empty')
      pool = new pg.Pool({
        host: env.XERLANE_PG_HOST || 'localhost', port: Number(env.XERLANE_PG_PORT || 5435), database: env.XERLANE_PG_DB || 'xerlane',
        user: 'xerlane_app', password: env.XERLANE_APP_PASSWORD, max: 10, connectionTimeoutMillis: 10000,
      })
    }
  }
  return pool
}

export async function closePool(): Promise<void> {
  const p = pool
  pool = undefined
  checked = false
  await p?.end()
}

async function refuseUnsafeRole(client: pg.PoolClient): Promise<void> {
  if (checked) return
  const r = await client.query('SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user')
  const row = r.rows[0]
  if (!row || row.rolsuper || row.rolbypassrls) throw new Error(`role ${row?.rolname} is a superuser or bypasses row level security; connect as xerlane_app`)
  checked = true
}

/** One transaction with the given settings applied for its duration only. Commits when fn returns, rolls back when it throws. */
export async function inTx<T>(settings: Record<string, string>, fn: (c: pg.PoolClient) => Promise<T>): Promise<T> {
  const client = await getPool().connect()
  try {
    await refuseUnsafeRole(client)
    await client.query('BEGIN')
    for (const [k, v] of Object.entries(settings)) await client.query('SELECT set_config($1, $2, true)', [k, v])
    const out = await fn(client)
    await client.query('COMMIT')
    return out
  } catch (e) {
    await client.query('ROLLBACK').catch(() => undefined)
    throw e
  } finally {
    client.release()
  }
}

export function withTenant<T>(tenant: string, fn: (c: pg.PoolClient) => Promise<T>): Promise<T> {
  if (!tenant) throw new Error('tenant must not be empty')
  return inTx({ 'app.tenant_id': tenant }, fn)
}
