// Passwords, sessions and the checks every route starts with.
//
// Password hash: scrypt, stored as  scrypt$N$r$p$salt$hash  with unpadded URL-safe base64. Python's xerlane/users.py writes and reads the same format
// (test/auth.test.ts checks both directions against it).
// Session: a random 32 byte token in an httpOnly cookie; the database keeps only its SHA-256, so a copy of the table is not a set of logins.
import { createHash, randomBytes, scrypt as scryptCb, timingSafeEqual } from 'node:crypto'
import { promisify } from 'node:util'
import { createError, deleteCookie, getCookie, getRequestHeader, getRequestProtocol, setCookie, type H3Event } from 'h3'
import { inTx } from './db'

const scrypt = promisify(scryptCb) as (pw: string, salt: Buffer, len: number, opts: { N: number; r: number; p: number; maxmem: number }) => Promise<Buffer>

export const COOKIE = 'xl_session'
export const SESSION_HOURS = 8
export const MAX_FAILURES = 5
export const FAILURE_WINDOW_MINUTES = 15
export const MIN_PASSWORD = 12
export const MAX_PASSWORD = 256
const N = 16384
const R = 8
const P = 1
const KEYLEN = 32
const MAXMEM = 128 * 1024 * 1024

export type Role = 'viewer' | 'admin'
export interface SessionUser { tenant: string; userId: number; email: string; role: Role }

const b64 = (b: Buffer) => b.toString('base64url')

export async function hashPassword(password: string, salt: Buffer = randomBytes(16)): Promise<string> {
  const dk = await scrypt(password, salt, KEYLEN, { N, r: R, p: P, maxmem: MAXMEM })
  return `scrypt$${N}$${R}$${P}$${b64(salt)}$${b64(dk)}`
}

export async function verifyPassword(password: string, stored: string): Promise<boolean> {
  const parts = stored.split('$')
  if (parts.length !== 6 || parts[0] !== 'scrypt') return false
  const [n, r, p] = [Number(parts[1]), Number(parts[2]), Number(parts[3])]
  if (![n, r, p].every((x) => Number.isInteger(x) && x > 0) || n > 1 << 20 || r > 32 || p > 16) return false
  const salt = Buffer.from(parts[4]!, 'base64url')
  const want = Buffer.from(parts[5]!, 'base64url')
  if (want.length !== KEYLEN) return false          // a shorter scrypt output is a prefix of the full one, so a cut-off hash would accept a short guess
  const got = await scrypt(password, salt, want.length, { N: n, r, p, maxmem: MAXMEM })
  return got.length === want.length && timingSafeEqual(got, want)
}

export const sha256 = (s: string) => createHash('sha256').update(s).digest('hex')
export const newToken = () => randomBytes(32).toString('base64url')

export function checkNewPassword(password: unknown): string {
  if (typeof password !== 'string' || password.length < MIN_PASSWORD) throw createError({ statusCode: 400, statusMessage: `Use at least ${MIN_PASSWORD} characters.` })
  if (password.length > MAX_PASSWORD) throw createError({ statusCode: 400, statusMessage: `Use at most ${MAX_PASSWORD} characters.` })
  return password
}

export function normalizeEmail(email: unknown): string {
  if (typeof email !== 'string') throw createError({ statusCode: 400, statusMessage: 'Enter an email address.' })
  const e = email.trim().toLowerCase()
  if (e.length > 254 || !/^[^@\s]+@[^@\s]+$/.test(e)) throw createError({ statusCode: 400, statusMessage: 'That is not an email address.' })
  return e
}

// A hash of a password nobody has, checked when the email is unknown, so an unknown address and a wrong password take the same time.
let dummy: Promise<string> | undefined
const dummyHash = () => (dummy ??= hashPassword(randomBytes(12).toString('hex')))

export type LoginResult =
  | { ok: true; token: string; user: SessionUser }
  | { ok: false; reason: 'bad_credentials' | 'locked' }

/** Check an email and password. Failures are counted per email; five in fifteen minutes lock that email out until the oldest one ages out. */
export async function login(emailIn: string, password: string): Promise<LoginResult> {
  const email = normalizeEmail(emailIn)
  return inTx({ 'app.login_email': email }, async (c): Promise<LoginResult> => {
    // COPY-OK: SQL, not screen text
    const failures = Number((await c.query('SELECT count(*) FROM login_attempt WHERE email = $1 AND at > now() - make_interval(mins => $2)', [email, FAILURE_WINDOW_MINUTES])).rows[0].count)
    if (failures >= MAX_FAILURES) return { ok: false, reason: 'locked' }
    // COPY-OK: SQL, not screen text
    const found = (await c.query('SELECT tenant_id, user_id, role, password_hash, disabled FROM app_user WHERE email = $1', [email])).rows[0]
    let good = false
    if (found && !found.disabled) good = await verifyPassword(password, found.password_hash)
    else await verifyPassword(password, await dummyHash())
    if (!good) {
      await c.query('INSERT INTO login_attempt (email) VALUES ($1)', [email])
      return { ok: false, reason: 'bad_credentials' }
    }
    // COPY-OK: SQL, not screen text
    await c.query(`DELETE FROM login_attempt WHERE email = $1 OR at < now() - interval '1 hour'`, [email])
    await c.query(`SELECT set_config('app.tenant_id', $1, true)`, [found.tenant_id])
    await c.query('DELETE FROM app_session WHERE expires_at < now()')
    const token = newToken()
    // COPY-OK: SQL, not screen text
    await c.query('INSERT INTO app_session (tenant_id, session_hash, user_id, expires_at) VALUES ($1, $2, $3, now() + make_interval(hours => $4))', [found.tenant_id, sha256(token), found.user_id, SESSION_HOURS])
    return { ok: true, token, user: { tenant: found.tenant_id, userId: Number(found.user_id), email, role: found.role } }
  })
}

/** The user behind a cookie value, or null: unknown token, expired session, or a user who has since been disabled. */
export async function userForToken(token: string | undefined): Promise<SessionUser | null> {
  if (!token || token.length > 128) return null
  return inTx({ 'app.session_hash': sha256(token) }, async (c) => {
    // COPY-OK: SQL, not screen text
    const s = (await c.query('SELECT tenant_id, user_id FROM app_session WHERE session_hash = $1 AND expires_at > now()', [sha256(token)])).rows[0]
    if (!s) return null
    await c.query(`SELECT set_config('app.tenant_id', $1, true)`, [s.tenant_id])
    const u = (await c.query('SELECT email, role, disabled FROM app_user WHERE user_id = $1', [s.user_id])).rows[0]
    if (!u || u.disabled) return null
    return { tenant: s.tenant_id as string, userId: Number(s.user_id), email: u.email as string, role: u.role as Role }
  })
}

export async function endSession(token: string | undefined): Promise<void> {
  if (!token || token.length > 128) return
  await inTx({ 'app.session_hash': sha256(token) }, async (c) => {
    await c.query('DELETE FROM app_session WHERE session_hash = $1', [sha256(token)])
  })
}

export function setSessionCookie(event: H3Event, token: string): void {
  const secure = getRequestProtocol(event, { xForwardedProto: true }) === 'https'
  setCookie(event, COOKIE, token, { httpOnly: true, sameSite: 'lax', secure, path: '/', maxAge: SESSION_HOURS * 3600 })
}

export const clearSessionCookie = (event: H3Event) => deleteCookie(event, COOKIE, { path: '/' })

/** The signed in user of this request. Throws 401 when there is none. */
export async function requireUser(event: H3Event): Promise<SessionUser> {
  const cached = event.context.user as SessionUser | undefined
  if (cached) return cached
  const user = await userForToken(getCookie(event, COOKIE))
  if (!user) throw createError({ statusCode: 401, statusMessage: 'Sign in first.' })
  event.context.user = user
  return user
}

export async function requireAdmin(event: H3Event): Promise<SessionUser> {
  const user = await requireUser(event)
  if (user.role !== 'admin') throw createError({ statusCode: 403, statusMessage: 'Administrators only.' })
  return user
}

/** Refuse a state changing request that did not come from this site's own pages: it must be JSON, and an Origin header, when sent, must match the Host. */
export function checkSameSite(event: H3Event): void {
  const method = event.method
  if (method === 'GET' || method === 'HEAD' || method === 'OPTIONS') return
  const origin = getRequestHeader(event, 'origin')
  const host = getRequestHeader(event, 'host')
  if (origin) {
    let ok = false
    try { ok = new URL(origin).host === host } catch { ok = false }
    if (!ok) throw createError({ statusCode: 403, statusMessage: 'Request refused.' })
  }
  if (getRequestHeader(event, 'sec-fetch-site') === 'cross-site') throw createError({ statusCode: 403, statusMessage: 'Request refused.' })
  if (!(getRequestHeader(event, 'content-type') ?? '').toLowerCase().startsWith('application/json')) throw createError({ statusCode: 415, statusMessage: 'Request not accepted.' })
}
