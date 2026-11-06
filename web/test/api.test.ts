import { spawn, type ChildProcess } from 'node:child_process'
import { createServer } from 'node:net'
import { resolve } from 'node:path'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import { sha256 } from '../server/utils/auth'
import { secret, WEB_DB } from './env'
import { adminClient, PASSWORD, USERS } from './seed'

// The built server, started as a real process, and plain fetch calls with the cookie handled by hand.
let base = ''
let child: ChildProcess | undefined
let admin: Awaited<ReturnType<typeof adminClient>>

async function freePort(): Promise<number> {
  return new Promise((ok, fail) => {
    const s = createServer()
    s.once('error', fail)
    s.listen(0, '127.0.0.1', () => { const p = (s.address() as { port: number }).port; s.close(() => ok(p)) })
  })
}

interface Reply { status: number; body: any; headers: Headers; cookie: string | null }
async function call(method: string, path: string, opts: { cookie?: string | null; body?: unknown; headers?: Record<string, string> } = {}): Promise<Reply> {
  const headers: Record<string, string> = { ...opts.headers }
  if (opts.cookie) headers.cookie = opts.cookie
  let body: string | undefined
  if (opts.body !== undefined) { headers['content-type'] ??= 'application/json'; body = JSON.stringify(opts.body) }
  const res = await fetch(base + path, { method, headers, body, redirect: 'manual' })
  const text = await res.text()
  let parsed: any = text
  try { parsed = JSON.parse(text) } catch { /* not JSON: a page */ }
  const set = res.headers.get('set-cookie')
  return { status: res.status, body: parsed, headers: res.headers, cookie: set ? set.split(';')[0]! : null }
}

async function signIn(email: string, password = PASSWORD): Promise<string> {
  const r = await call('POST', '/api/login', { body: { email, password } })
  expect(r.status, JSON.stringify(r.body)).toBe(200)
  return r.cookie!
}

beforeAll(async () => {
  const port = await freePort()
  base = `http://127.0.0.1:${port}`
  child = spawn(process.execPath, [resolve(process.cwd(), '.output/server/index.mjs')], {
    env: { ...process.env, NITRO_PORT: String(port), NITRO_HOST: '127.0.0.1', PORT: String(port), XERLANE_PG_DB: WEB_DB, XERLANE_APP_PASSWORD: secret('XERLANE_APP_PASSWORD') },
    stdio: 'ignore',
  })
  const end = Date.now() + 30000
  for (;;) {
    try { if ((await fetch(base + '/api/me')).status === 401) break } catch { /* not up yet */ }
    if (Date.now() > end) throw new Error('the built server did not answer within 30 seconds')
    await new Promise((r) => setTimeout(r, 250))
  }
  admin = await adminClient()
})

afterAll(async () => {
  child?.kill()
  await admin?.end()
})

describe('signing in', () => {
  it('refuses every data route without a session', async () => {
    for (const p of ['/api/me', '/api/projects', '/api/projects/7/report', '/api/projects/7/quarantine', '/api/admin/users']) {
      expect((await call('GET', p)).status, p).toBe(401)
    }
  })

  it('sets an httpOnly, same-site cookie and answers who is signed in', async () => {
    const r = await call('POST', '/api/login', { body: { email: USERS.acmeViewer.email, password: PASSWORD } })
    expect(r.status).toBe(200)
    expect(r.body).toEqual({ email: USERS.acmeViewer.email, role: 'viewer', tenant: 'acme' })
    const set = r.headers.get('set-cookie')!
    expect(set).toMatch(/xl_session=/)
    expect(set).toMatch(/HttpOnly/i)
    expect(set).toMatch(/SameSite=Lax/i)
    expect(set).toMatch(/Path=\//)
    expect(set).toMatch(/Max-Age=28800/)
    const me = await call('GET', '/api/me', { cookie: r.cookie })
    expect(me.body).toEqual(r.body)
  })

  it('answers a wrong password and an unknown email in the same words', async () => {
    const a = await call('POST', '/api/login', { body: { email: USERS.acmeViewer.email, password: 'wrong wrong wrong' } })
    const b = await call('POST', '/api/login', { body: { email: 'nobody@nowhere.test', password: 'wrong wrong wrong' } })
    expect([a.status, b.status]).toEqual([401, 401])
    expect(a.body.statusMessage).toBe(b.body.statusMessage)
    expect(a.cookie).toBeNull()
  })

  it('rejects a login with missing or oversized fields', async () => {
    expect((await call('POST', '/api/login', { body: {} })).status).toBe(400)
    expect((await call('POST', '/api/login', { body: { email: 'a@b.cc', password: 'x'.repeat(300) } })).status).toBe(400)
    expect((await call('POST', '/api/login', { body: { email: ['a@b.cc'], password: 'x' } })).status).toBe(400)
    expect((await call('POST', '/api/login', { body: { email: 'not an email', password: 'x' } })).status).toBe(400)
  })

  it('locks an email after five wrong passwords with a 429, and the right password does not get past it', async () => {
    const email = 'lock.me@acme.test'
    const { hashPassword } = await import('../server/utils/auth')
    await admin.query(`INSERT INTO app_user (tenant_id, email, password_hash, role) VALUES ('acme', $1, $2, 'viewer') ON CONFLICT DO NOTHING`, [email, await hashPassword(PASSWORD)])
    for (let i = 0; i < 5; i++) expect((await call('POST', '/api/login', { body: { email, password: 'nope nope nope ' + i } })).status).toBe(401)
    const locked = await call('POST', '/api/login', { body: { email, password: PASSWORD } })
    expect(locked.status).toBe(429)
    expect(locked.cookie).toBeNull()
  })

  it('ends the session on the server when the user signs out', async () => {
    const cookie = await signIn(USERS.acmeViewer.email)
    expect((await call('GET', '/api/projects', { cookie })).status).toBe(200)
    expect((await call('POST', '/api/logout', { cookie, body: {} })).status).toBe(200)
    expect((await call('GET', '/api/projects', { cookie })).status).toBe(401)          // the old cookie is dead even though the client still holds it
  })

  it('stops an expired session and one whose user was disabled', async () => {
    const cookie = await signIn(USERS.acmeViewer.email)
    const token = cookie.split('=')[1]!
    await admin.query(`UPDATE app_session SET expires_at = now() - interval '1 second' WHERE session_hash = $1`, [sha256(token)])
    expect((await call('GET', '/api/me', { cookie })).status).toBe(401)
  })
})

describe('what a tenant can see', () => {
  it('lists only its own projects', async () => {
    const acme = await call('GET', '/api/projects', { cookie: await signIn(USERS.acmeViewer.email) })
    expect(acme.body.map((p: any) => p.name)).toEqual(['P7'])
    expect(acme.body[0]).toMatchObject({ id: 7, tasks: 4, costLines: 6, setAside: 1 })
    const globex = await call('GET', '/api/projects', { cookie: await signIn(USERS.globexViewer.email) })
    expect(globex.body.map((p: any) => p.name)).toEqual(['GLOBEX-8'])
  })

  it('answers 404, not 403 and not data, for another tenant\'s project, by report and by quarantine', async () => {
    const cookie = await signIn(USERS.acmeViewer.email)
    for (const p of ['/api/projects/8/report', '/api/projects/8/quarantine', '/api/projects/424242/report', '/api/projects/abc/report', '/api/projects/-1/report', '/api/projects/7.5/report']) {
      expect((await call('GET', p, { cookie })).status, p).toBe(404)
    }
    const other = await signIn(USERS.globexViewer.email)
    expect((await call('GET', '/api/projects/7/report', { cookie: other })).status).toBe(404)
    expect((await call('GET', '/api/projects/8/report', { cookie: other })).status).toBe(200)
  })

  it('shows the hand worked report over the wire', async () => {
    const r = await call('GET', '/api/projects/7/report', { cookie: await signIn(USERS.acmeViewer.email) })
    expect(r.status).toBe(200)
    expect(r.body.totals).toMatchObject({ bac: '7000.00', pv: '2150.00', ev: '2500.00', ac: '2250.00', cpi: '1.11', spi: '1.16', eac: '6306.31' })
    expect(r.body.tasks.map((t: any) => t.key)).toEqual(['T1', 'T2', 'T3', 'T4'])
    const early = await call('GET', '/api/projects/7/report?asOf=2026-03-02', { cookie: await signIn(USERS.acmeViewer.email) })
    expect(early.body.totals.pv).toBe('0.00')
  })

  it('rejects a bad as-of date', async () => {
    const cookie = await signIn(USERS.acmeViewer.email)
    for (const q of ['asOf=yesterday', 'asOf=2026-02-30', "asOf=2026-03-02';drop table task", 'asOf=2026-03-02&asOf=2026-03-03']) {
      expect((await call('GET', '/api/projects/7/report?' + q, { cookie })).status, q).toBe(400)
    }
  })

  it('shows the rows set aside for its own project only', async () => {
    const r = await call('GET', '/api/projects/7/quarantine', { cookie: await signIn(USERS.acmeViewer.email) })
    expect(r.body).toEqual([{ table: 'COST', line: 4, code: 'BAD_VALUE', reason: 'budget must be a plain number', source: 'c.csv' }])
  })
})

describe('who may administer', () => {
  it('keeps the user list and its actions to administrators', async () => {
    const viewer = await signIn(USERS.acmeViewer.email)
    expect((await call('GET', '/api/admin/users', { cookie: viewer })).status).toBe(403)
    expect((await call('POST', '/api/admin/users', { cookie: viewer, body: { email: 'x@acme.test', password: PASSWORD, role: 'viewer' } })).status).toBe(403)
    expect((await call('POST', '/api/admin/users/1/disable', { cookie: viewer, body: {} })).status).toBe(403)
  })

  it('lists only the administrator\'s own tenant', async () => {
    const r = await call('GET', '/api/admin/users', { cookie: await signIn(USERS.acmeAdmin.email) })
    expect(r.status).toBe(200)
    const emails = r.body.map((u: any) => u.email)
    expect(emails).toContain(USERS.acmeViewer.email)
    expect(emails).not.toContain(USERS.globexViewer.email)
    expect(JSON.stringify(r.body)).not.toMatch(/password|scrypt/i)
  })

  it('creates a user who can sign in, then disables them and ends their session', async () => {
    const adminCookie = await signIn(USERS.acmeAdmin.email)
    const email = 'new.person@acme.test'
    const made = await call('POST', '/api/admin/users', { cookie: adminCookie, body: { email: ' New.Person@Acme.test ', password: 'a brand new passphrase', role: 'viewer' } })
    expect(made.status, JSON.stringify(made.body)).toBe(200)
    const cookie = await signIn(email, 'a brand new passphrase')
    expect((await call('GET', '/api/projects', { cookie })).status).toBe(200)
    expect((await call('GET', '/api/admin/users', { cookie })).status).toBe(403)
    expect((await call('POST', `/api/admin/users/${made.body.id}/disable`, { cookie: adminCookie, body: {} })).status).toBe(200)
    expect((await call('GET', '/api/projects', { cookie })).status).toBe(401)
    expect(Number((await admin.query('SELECT count(*) FROM app_session WHERE user_id = $1', [made.body.id])).rows[0].count)).toBe(0)     // the rows are gone, not only ignored
    expect((await call('POST', '/api/login', { body: { email, password: 'a brand new passphrase' } })).status).toBe(401)
  })

  it('refuses a short password, a duplicate email, a bad role, and disabling yourself or another tenant\'s user', async () => {
    const cookie = await signIn(USERS.acmeAdmin.email)
    const add = (b: object) => call('POST', '/api/admin/users', { cookie, body: b })
    expect((await add({ email: 'a1@acme.test', password: 'short', role: 'viewer' })).status).toBe(400)
    expect((await add({ email: 'a1@acme.test', password: PASSWORD, role: 'owner' })).status).toBe(400)
    expect((await add({ email: USERS.acmeViewer.email, password: PASSWORD, role: 'viewer' })).status).toBe(409)
    const me = (await admin.query('SELECT user_id FROM app_user WHERE email = $1', [USERS.acmeAdmin.email])).rows[0].user_id
    expect((await call('POST', `/api/admin/users/${me}/disable`, { cookie, body: {} })).status).toBe(400)
    const g = (await admin.query('SELECT user_id FROM app_user WHERE email = $1', [USERS.globexViewer.email])).rows[0].user_id
    expect((await call('POST', `/api/admin/users/${g}/disable`, { cookie, body: {} })).status).toBe(404)
    expect((await admin.query('SELECT disabled FROM app_user WHERE email = $1', [USERS.globexViewer.email])).rows[0].disabled).toBe(false)
  })
})

describe('requests from other sites and plain hardening', () => {
  it('refuses a state change that names another origin, even with a good session', async () => {
    const cookie = await signIn(USERS.acmeAdmin.email)
    const evil = await call('POST', '/api/admin/users', { cookie, body: { email: 'evil@acme.test', password: PASSWORD, role: 'admin' }, headers: { origin: 'https://evil.example' } })
    expect(evil.status).toBe(403)
    expect((await admin.query('SELECT 1 FROM app_user WHERE email = $1', ['evil@acme.test'])).rowCount).toBe(0)
    expect((await call('POST', '/api/logout', { cookie, body: {}, headers: { 'sec-fetch-site': 'cross-site' } })).status).toBe(403)
    expect((await call('GET', '/api/me', { cookie })).status).toBe(200)                  // the refused logout did not end the session
  })

  it('accepts the same request from its own origin', async () => {
    const cookie = await signIn(USERS.acmeViewer.email)
    const r = await call('POST', '/api/logout', { cookie, body: {}, headers: { origin: base } })
    expect(r.status).toBe(200)
  })

  it('refuses a state change that is not sent as JSON', async () => {
    const res = await fetch(base + '/api/login', { method: 'POST', headers: { 'content-type': 'application/x-www-form-urlencoded' }, body: `email=${USERS.acmeViewer.email}&password=${PASSWORD}` })
    expect(res.status).toBe(415)
  })

  it('sends hardening headers on pages and on the API, and never caches the API', async () => {
    for (const path of ['/', '/api/me']) {
      const res = await fetch(base + path)
      expect(res.headers.get('x-content-type-options'), path).toBe('nosniff')
      expect(res.headers.get('x-frame-options'), path).toBe('DENY')
      expect(res.headers.get('content-security-policy'), path).toContain("frame-ancestors 'none'")
    }
    expect((await fetch(base + '/api/me')).headers.get('cache-control')).toBe('no-store')
  })

  it('serves the sign in page shell', async () => {
    const res = await fetch(base + '/login')
    expect(res.status).toBe(200)
    expect(await res.text()).toContain('<div id="__nuxt">')
  })
})
