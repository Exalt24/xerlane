import { createError, defineEventHandler, getRouterParam } from 'h3'
import { requireAdmin } from '../../../../utils/auth'
import { withTenant } from '../../../../utils/db'

export default defineEventHandler(async (event) => {
  const admin = await requireAdmin(event)
  const id = Number(getRouterParam(event, 'id'))
  if (!Number.isSafeInteger(id) || id < 0) throw createError({ statusCode: 404, statusMessage: 'No such user.' })
  if (id === admin.userId) throw createError({ statusCode: 400, statusMessage: 'You cannot disable yourself.' })
  return withTenant(admin.tenant, async (c) => {
    const r = await c.query('UPDATE app_user SET disabled = true WHERE user_id = $1', [id])
    if (!r.rowCount) throw createError({ statusCode: 404, statusMessage: 'No such user.' })
    await c.query('DELETE FROM app_session WHERE user_id = $1', [id])
    return { ok: true }
  })
})
