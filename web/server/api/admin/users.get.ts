import { defineEventHandler } from 'h3'
import { requireAdmin } from '../../utils/auth'
import { withTenant } from '../../utils/db'

export default defineEventHandler(async (event) => {
  const u = await requireAdmin(event)
  return withTenant(u.tenant, async (c) => {
    const r = await c.query('SELECT user_id, email, role, disabled, created_at::text AS created FROM app_user ORDER BY user_id')
    return r.rows.map((x) => ({ id: Number(x.user_id), email: x.email as string, role: x.role as string, disabled: x.disabled as boolean, created: x.created as string }))
  })
})
