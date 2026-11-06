import { createError, defineEventHandler, getRouterParam } from 'h3'
import { requireUser } from '../../../utils/auth'
import { withTenant } from '../../../utils/db'

const LIMIT = 200

export default defineEventHandler(async (event) => {
  const u = await requireUser(event)
  const id = Number(getRouterParam(event, 'id'))
  if (!Number.isSafeInteger(id) || id < 0) throw createError({ statusCode: 404, statusMessage: 'No such project.' })
  return withTenant(u.tenant, async (c) => {
    const exists = (await c.query('SELECT 1 FROM project WHERE proj_id = $1', [id])).rowCount
    if (!exists) throw createError({ statusCode: 404, statusMessage: 'No such project.' })
    // COPY-OK: SQL, not screen text
    const r = await c.query('SELECT table_name, line_no, reason_code, reason, source_label FROM quarantine WHERE proj_id = $1 ORDER BY source_label, table_name, line_no LIMIT $2', [id, LIMIT])
    return r.rows.map((x) => ({ table: x.table_name as string, line: Number(x.line_no), code: x.reason_code as string, reason: x.reason as string, source: (x.source_label as string).replace(/^cost:/, '') }))
  })
})
