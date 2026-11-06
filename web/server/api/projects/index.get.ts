import { defineEventHandler } from 'h3'
import { requireUser } from '../../utils/auth'
import { withTenant } from '../../utils/db'

export default defineEventHandler(async (event) => {
  const u = await requireUser(event)
  return withTenant(u.tenant, async (c) => {
    // COPY-OK: SQL, not screen text
    const r = await c.query(`SELECT p.proj_id, p.proj_short_name AS name, p.last_recalc_date::text AS data_date,
        (SELECT count(*) FROM task t WHERE t.proj_id = p.proj_id) AS tasks,
        (SELECT count(*) FROM cost_line l WHERE l.proj_id = p.proj_id) AS cost_lines,
        (SELECT count(*) FROM quarantine q WHERE q.proj_id = p.proj_id) AS set_aside
      FROM project p ORDER BY p.proj_id`)
    return r.rows.map((x) => ({ id: Number(x.proj_id), name: x.name as string, dataDate: x.data_date as string | null, tasks: Number(x.tasks), costLines: Number(x.cost_lines), setAside: Number(x.set_aside) }))
  })
})
