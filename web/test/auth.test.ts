import { spawnSync } from 'node:child_process'
import { resolve } from 'node:path'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import { checkNewPassword, endSession, hashPassword, login, MAX_FAILURES, normalizeEmail, sha256, userForToken, verifyPassword } from '../server/utils/auth'
import { closePool, getPool, inTx, withTenant } from '../server/utils/db'
import { host, port, secret, WEB_DB } from './env'
import { adminClient, PASSWORD, USERS } from './seed'

// A hash made by Python (tests/test_users.py holds the same string and checks it from that side).
const PYTHON_VECTOR = 'scrypt$16384$8$1$AAECAwQFBgcICQoLDA0ODw$11kKyiyYAc8G7rp3KmncMc44YlkdllIqxOa7pq0fMaU'
const PYTHON_PASSWORD = 'correct horse battery staple'

describe('password hashes', () => {
  it('verifies the hash Python made', async () => {
    expect(await verifyPassword(PYTHON_PASSWORD, PYTHON_VECTOR)).toBe(true)
    expect(await verifyPassword(PYTHON_PASSWORD + 'x', PYTHON_VECTOR)).toBe(false)
    expect(await verifyPassword('', PYTHON_VECTOR)).toBe(false)
  })

  it('makes the same hash as Python for the same salt, and Python verifies a hash made here', async () => {
    expect(await hashPassword(PYTHON_PASSWORD, Buffer.from([...Array(16).keys()]))).toBe(PYTHON_VECTOR)
    const mine = await hashPassword('another passphrase here')
    const py = spawnSync(process.env.PYTHON || 'python', ['-c', 'import sys; from xerlane import users; print(users.verify_password(sys.argv[1], sys.argv[2]))', 'another passphrase here', mine], { cwd: resolve(process.cwd(), '..'), encoding: 'utf8' })
    expect(py.stdout.trim()).toBe('True')
    const wrong = spawnSync(process.env.PYTHON || 'python', ['-c', 'import sys; from xerlane import users; print(users.verify_password(sys.argv[1], sys.argv[2]))', 'not the passphrase', mine], { cwd: resolve(process.cwd(), '..'), encoding: 'utf8' })
    expect(wrong.stdout.trim()).toBe('False')
  })

  it('gives every hash its own salt', async () => {
    const [a, b] = [await hashPassword(PASSWORD), await hashPassword(PASSWORD)]
    expect(a).not.toBe(b)
    expect(await verifyPassword(PASSWORD, a)).toBe(true)
  })

  it('refuses damaged or hostile hashes without throwing', async () => {
    const bad = ['', 'scrypt', 'scrypt$1$2$3$4', PYTHON_VECTOR.slice(0, -4), PYTHON_VECTOR + '$x', PYTHON_VECTOR.replace('scrypt', 'bcrypt'), PYTHON_VECTOR.replace('$16384$', '$0$'),
      PYTHON_VECTOR.replace('$16384$8$1$', '$1073741824$8$1$'), PYTHON_VECTOR.replace('$16384$8$1$', '$16384$99$1$'), 'scrypt$x$8$1$AA$AA']
    for (const h of bad) expect(await verifyPassword(PYTHON_PASSWORD, h), h).toBe(false)
  })
})

describe('input checks', () => {
  it('normalizes emails and refuses what is not one', () => {
    expect(normalizeEmail('  Ann@Example.COM ')).toBe('ann@example.com')
    for (const e of ['', 'no-at', 'a@', '@b', 'a b@c.d', 'a@b@c', 'x'.repeat(250) + '@y.zz', 5, null, undefined]) expect(() => normalizeEmail(e), String(e)).toThrow()
  })

  it('wants twelve to 256 characters for a new password', () => {
    expect(() => checkNewPassword('x'.repeat(11))).toThrow()
    expect(checkNewPassword('x'.repeat(12))).toHaveLength(12)
    expect(checkNewPassword('x'.repeat(256))).toHaveLength(256)
    expect(() => checkNewPassword('x'.repeat(257))).toThrow()
    expect(() => checkNewPassword(12345678901234)).toThrow()
  })
})

describe('login and sessions', () => {
  let admin: Awaited<ReturnType<typeof adminClient>>
  beforeAll(async () => { admin = await adminClient() })
  afterAll(async () => { await admin.end(); await closePool() })

  it('signs a real user in, keeps only a hash of the token, and finds the user again from it', async () => {
    const r = await login(USERS.acmeAdmin.email.toUpperCase(), PASSWORD)
    if (!r.ok) throw new Error('login failed')
    expect(r.user).toMatchObject({ tenant: 'acme', email: USERS.acmeAdmin.email, role: 'admin' })
    const rows = (await admin.query('SELECT session_hash FROM app_session')).rows.map((x) => x.session_hash)
    expect(rows).toContain(sha256(r.token))
    expect(rows).not.toContain(r.token)
    expect(r.token.length).toBeGreaterThanOrEqual(43)
    expect(await userForToken(r.token)).toEqual(r.user)
  })

  it('treats a wrong password and an unknown email the same way', async () => {
    expect(await login(USERS.acmeViewer.email, 'definitely wrong password')).toEqual({ ok: false, reason: 'bad_credentials' })
    expect(await login('nobody@nowhere.test', PASSWORD)).toEqual({ ok: false, reason: 'bad_credentials' })
  })

  it('locks an email after five failures, even for the right password, and clears the count on a good login', async () => {
    const email = USERS.globexViewer.email
    await admin.query('DELETE FROM login_attempt')
    for (let i = 0; i < MAX_FAILURES - 1; i++) expect((await login(email, 'wrong password ' + i)).ok).toBe(false)
    const ok = await login(email, PASSWORD)                                   // four failures are not a lock
    expect(ok.ok).toBe(true)
    expect(Number((await admin.query('SELECT count(*) FROM login_attempt WHERE email = $1', [email])).rows[0].count)).toBe(0)
    for (let i = 0; i < MAX_FAILURES; i++) await login(email, 'wrong password ' + i)
    expect(await login(email, PASSWORD)).toEqual({ ok: false, reason: 'locked' })
    await admin.query(`UPDATE login_attempt SET at = now() - interval '16 minutes' WHERE email = $1`, [email])
    expect((await login(email, PASSWORD)).ok).toBe(true)                      // the window moved on
  })

  it('does not find a user for an unknown, tampered, expired or logged out token, or after the user is disabled', async () => {
    const r = await login(USERS.acmeViewer.email, PASSWORD)
    if (!r.ok) throw new Error('login failed')
    expect(await userForToken(r.token)).not.toBeNull()
    expect(await userForToken(r.token.slice(0, -1) + (r.token.endsWith('A') ? 'B' : 'A'))).toBeNull()
    expect(await userForToken(undefined)).toBeNull()
    expect(await userForToken('x'.repeat(200))).toBeNull()
    await admin.query(`UPDATE app_session SET expires_at = now() - interval '1 second' WHERE session_hash = $1`, [sha256(r.token)])
    expect(await userForToken(r.token)).toBeNull()
    const r2 = await login(USERS.acmeViewer.email, PASSWORD)
    if (!r2.ok) throw new Error('login failed')
    await endSession(r2.token)
    expect(await userForToken(r2.token)).toBeNull()
    const r3 = await login(USERS.acmeViewer.email, PASSWORD)
    if (!r3.ok) throw new Error('login failed')
    await admin.query('UPDATE app_user SET disabled = true WHERE email = $1', [USERS.acmeViewer.email])
    expect(await userForToken(r3.token)).toBeNull()
    expect(await login(USERS.acmeViewer.email, PASSWORD)).toEqual({ ok: false, reason: 'bad_credentials' })
    await admin.query('UPDATE app_user SET disabled = false WHERE email = $1', [USERS.acmeViewer.email])
  })
})

describe('the database role', () => {
  afterAll(async () => { await closePool() })

  it('is not a superuser and does not bypass row level security', async () => {
    const r = await getPool().query('SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user')
    expect(r.rows[0]).toEqual({ rolsuper: false, rolbypassrls: false })
  })

  it('sees no row of any table without a tenant, and only its own rows with one', async () => {
    for (const t of ['project', 'task', 'cost_line', 'commitment', 'quarantine', 'app_user', 'app_session', 'login_attempt']) {
      expect(Number((await inTx({}, (c) => c.query(`SELECT count(*) FROM ${t}`))).rows[0].count), t).toBe(0)
    }
    const acme = await withTenant('acme', async (c) => (await c.query('SELECT DISTINCT tenant_id FROM cost_line')).rows.map((x) => x.tenant_id))
    expect(acme).toEqual(['acme'])
    const none = await withTenant('nobody', async (c) => Number((await c.query('SELECT count(*) FROM task')).rows[0].count))
    expect(none).toBe(0)
  })

  it('refuses to run as a superuser, which would see every tenant', async () => {
    const saved = process.env.XERLANE_DSN
    await closePool()
    process.env.XERLANE_DSN = `postgres://postgres:${secret('XERLANE_ADMIN_PASSWORD')}@${host()}:${port()}/${WEB_DB}`
    try {
      await expect(inTx({}, (c) => c.query('SELECT 1'))).rejects.toThrow(/superuser or bypasses row level security/)
    } finally {
      await closePool()
      if (saved === undefined) delete process.env.XERLANE_DSN
      else process.env.XERLANE_DSN = saved
    }
    expect((await withTenant('acme', (c) => c.query('SELECT 1'))).rowCount).toBe(1)          // and the normal role works again
  })

  it('cannot write a row into another tenant', async () => {
    await expect(withTenant('acme', (c) => c.query(`INSERT INTO cost_line (tenant_id, proj_id, line_id, cost_code, budget, forecast, actual) VALUES ('globex', 8, 99, 'x', 1, 1, 1)`))).rejects.toThrow(/row-level security/)
  })
})
