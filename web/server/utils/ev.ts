// The earned value report, computed by PostgreSQL so the money never passes through a floating point number. The formulas are the ones in xerlane/evm.py
// (docs/cost.md); test/ev.test.ts checks this query against the same hand worked schedule as tests/test_evm.py.
// COPY-OK: every long string in this file is SQL, not screen text
import type pg from 'pg'

// COPY-OK: SQL
const DONE = `CASE WHEN t.act_end_date IS NOT NULL THEN 1::numeric WHEN t.act_start_date IS NULL THEN 0::numeric WHEN coalesce(t.target_drtn_hr_cnt, 0) <= 0 OR t.remain_drtn_hr_cnt IS NULL THEN 0::numeric ELSE greatest(0::numeric, least(1::numeric, 1 - t.remain_drtn_hr_cnt / t.target_drtn_hr_cnt)) END`
// COPY-OK: SQL
const PLANNED = `CASE WHEN t.target_start_date IS NULL OR t.target_end_date IS NULL THEN 0::numeric WHEN $2::timestamp >= t.target_end_date THEN 1::numeric WHEN $2::timestamp <= t.target_start_date THEN 0::numeric ELSE extract(epoch FROM ($2::timestamp - t.target_start_date))::numeric / extract(epoch FROM (t.target_end_date - t.target_start_date))::numeric END`
// COPY-OK: SQL
const COSTS = `SELECT task_code, sum(budget) AS bac, sum(actual) AS ac, sum(forecast) AS fc FROM cost_line WHERE proj_id = $1 AND task_code IS NOT NULL GROUP BY task_code`
// COPY-OK: SQL
const JOINED = `WITH c AS (${COSTS}), j AS (SELECT t.task_code, t.task_name, c.bac, c.ac, c.fc, ${DONE} AS done, ${PLANNED} AS planned FROM c JOIN task t ON t.proj_id = $1 AND t.task_code = c.task_code)`

export interface Line {
  key: string; name: string; bac: string; pv: string; ev: string; ac: string; forecast: string
  cv: string; sv: string; cpi: string | null; spi: string | null; eac: string | null; pctDone: string | null; pctPlanned: string | null
}
export interface Report {
  project: string; projId: number; asOf: string
  totals: Line; tasks: Line[]; notes: string[]
  byCode: { costCode: string; budget: string; forecast: string; actual: string; committed: string; invoiced: string; over: boolean }[]
}

const money = (e: string) => `round(${e}, 2)::text`
const ratio = (a: string, b: string) => `round((${a}) / nullif(${b}, 0), 2)::text`

// COPY-OK: SQL
const TASKS_SQL = `${JOINED} SELECT task_code, task_name, ${money('bac')} AS bac, ${money('bac * planned')} AS pv, ${money('bac * done')} AS ev, ${money('ac')} AS ac, ${money('fc')} AS fc, ${money('bac * done - ac')} AS cv, ${money('bac * done - bac * planned')} AS sv, ${ratio('bac * done', 'ac')} AS cpi, ${ratio('bac * done', 'bac * planned')} AS spi, ${money('done')} AS pct_done, ${money('planned')} AS pct_planned FROM j ORDER BY task_code`
// COPY-OK: SQL
const SUMS_SQL = `${JOINED} SELECT (SELECT coalesce(sum(budget), 0) FROM cost_line WHERE proj_id = $1) AS bac, (SELECT coalesce(sum(actual), 0) FROM cost_line WHERE proj_id = $1) AS ac, (SELECT coalesce(sum(forecast), 0) FROM cost_line WHERE proj_id = $1) AS fc, coalesce((SELECT sum(bac * planned) FROM j), 0) AS pv, coalesce((SELECT sum(bac * done) FROM j), 0) AS ev, (SELECT count(*) FROM cost_line l WHERE l.proj_id = $1 AND l.task_code IS NULL) AS no_code, (SELECT count(*) FROM cost_line l WHERE l.proj_id = $1 AND l.task_code IS NOT NULL AND NOT EXISTS (SELECT 1 FROM task t WHERE t.proj_id = l.proj_id AND t.task_code = l.task_code)) AS no_task`
// COPY-OK: SQL
const TOTALS_SQL = `WITH s AS (SELECT $1::numeric AS bac, $2::numeric AS pv, $3::numeric AS ev, $4::numeric AS ac, $5::numeric AS fc), r AS (SELECT *, round(ev / nullif(ac, 0), 2) AS cpi FROM s) SELECT ${money('bac')} AS bac, ${money('pv')} AS pv, ${money('ev')} AS ev, ${money('ac')} AS ac, ${money('fc')} AS fc, ${money('ev - ac')} AS cv, ${money('ev - pv')} AS sv, cpi::text AS cpi, ${ratio('ev', 'pv')} AS spi, CASE WHEN cpi IS NULL OR cpi = 0 THEN NULL ELSE round(bac / cpi, 2)::text END AS eac FROM r`
// COPY-OK: SQL
const CODES_SQL = `SELECT k.cost_code, round(k.budget, 2)::text AS budget, round(k.forecast, 2)::text AS forecast, round(k.actual, 2)::text AS actual, round(coalesce(m.committed, 0), 2)::text AS committed, round(coalesce(m.invoiced, 0), 2)::text AS invoiced, coalesce(m.committed, 0) > k.budget AS over FROM (SELECT cost_code, sum(budget) AS budget, sum(forecast) AS forecast, sum(actual) AS actual FROM cost_line WHERE proj_id = $1 GROUP BY cost_code) k LEFT JOIN (SELECT cost_code, sum(committed) AS committed, sum(invoiced) AS invoiced FROM commitment WHERE proj_id = $1 AND status <> 'VOID' GROUP BY cost_code) m ON m.cost_code = k.cost_code ORDER BY k.cost_code`

/** The report for one project of the tenant the transaction is scoped to, or null when that tenant has no such project. `asOf` is 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM'. */
export async function loadReport(c: pg.PoolClient, projId: number, asOf: string | null): Promise<Report | null> {
  const p = (await c.query('SELECT proj_short_name, last_recalc_date::text AS recalc FROM project WHERE proj_id = $1', [projId])).rows[0]
  if (!p) return null
  const when: string | null = asOf ?? p.recalc
  if (!when) throw new Error('The project has no recalculation date. Pick a date.')
  const tasks = (await c.query(TASKS_SQL, [projId, when])).rows
  const sums = (await c.query(SUMS_SQL, [projId, when])).rows[0]
  const t = (await c.query(TOTALS_SQL, [sums.bac, sums.pv, sums.ev, sums.ac, sums.fc])).rows[0]
  const codes = (await c.query(CODES_SQL, [projId])).rows
  const notes: string[] = []
  if (Number(sums.no_code)) notes.push(`${sums.no_code} cost line(s) have no task code. They count in BAC and AC only.`)
  if (Number(sums.no_task)) notes.push(`${sums.no_task} cost line(s) name a task that is gone. They count in BAC and AC only.`)
  if (!tasks.length) notes.push('No task has cost lines yet.')
  const line = (r: Record<string, string | null>, key: string, name: string): Line => ({
    key, name, bac: r.bac!, pv: r.pv!, ev: r.ev!, ac: r.ac!, forecast: r.fc!, cv: r.cv!, sv: r.sv!, cpi: r.cpi ?? null, spi: r.spi ?? null, eac: r.eac ?? null,
    pctDone: r.pct_done ?? null, pctPlanned: r.pct_planned ?? null,
  })
  return {
    project: p.proj_short_name, projId, asOf: when,
    totals: line(t, 'TOTAL', p.proj_short_name),
    tasks: tasks.map((r) => line(r, r.task_code, r.task_name ?? '')),
    notes,
    byCode: codes.map((r) => ({ costCode: r.cost_code, budget: r.budget, forecast: r.forecast, actual: r.actual, committed: r.committed, invoiced: r.invoiced, over: r.over })),
  }
}
