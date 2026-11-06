import { createError, defineEventHandler, readBody } from 'h3'
import { checkNewPassword, hashPassword, normalizeEmail, requireAdmin } from '../../utils/auth'
import { withTenant } from '../../utils/db'

export default defineEventHandler(async (event) => {
  const admin = await requireAdmin(event)
  const body = await readBody<{ email?: unknown; password?: unknown; role?: unknown }>(event)
  const email = normalizeEmail(body?.email)
  const password = checkNewPassword(body?.password)
  const role = body?.role === 'admin' ? 'admin' : body?.role === 'viewer' ? 'viewer' : null
  if (!role) throw createError({ statusCode: 400, statusMessage: 'Pick viewer or admin.' })
  const hash = await hashPassword(password)
  try {
    return await withTenant(admin.tenant, async (c) => {
      // COPY-OK: SQL, not screen text
      const r = await c.query('INSERT INTO app_user (tenant_id, email, password_hash, role) VALUES ($1, $2, $3, $4) RETURNING user_id', [admin.tenant, email, hash, role])
      return { id: Number(r.rows[0].user_id), email, role }
    })
  } catch (e) {
    if ((e as { code?: string }).code === '23505') throw createError({ statusCode: 409, statusMessage: 'That email is already in use.' })
    throw e
  }
})
