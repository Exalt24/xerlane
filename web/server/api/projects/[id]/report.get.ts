import { createError, defineEventHandler, getQuery, getRouterParam } from 'h3'
import { requireUser } from '../../../utils/auth'
import { withTenant } from '../../../utils/db'
import { loadReport } from '../../../utils/ev'

const AS_OF = /^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2})?$/

export default defineEventHandler(async (event) => {
  const u = await requireUser(event)
  const id = Number(getRouterParam(event, 'id'))
  if (!Number.isSafeInteger(id) || id < 0) throw createError({ statusCode: 404, statusMessage: 'No such project.' })
  const raw = getQuery(event).asOf
  if (raw !== undefined && (typeof raw !== 'string' || !AS_OF.test(raw))) throw createError({ statusCode: 400, statusMessage: 'Use a date like 2026-03-12.' })
  const asOf = typeof raw === 'string' ? raw.replace('T', ' ') : null
  const report = await withTenant(u.tenant, async (c) => {
    try {
      return await loadReport(c, id, asOf)
    } catch (e) {
      if ((e as { code?: string }).code === '22008' || (e as { code?: string }).code === '22007' || (e as { code?: string }).code === '22P02') throw createError({ statusCode: 400, statusMessage: 'That is not a real date.' })
      throw e
    }
  })
  if (!report) throw createError({ statusCode: 404, statusMessage: 'No such project.' })
  return report
})
