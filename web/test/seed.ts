// Two tenants with a schedule, costs and users, written as administrator. Tenant "acme" holds the same hand worked project 7 as tests/test_evm.py.
import pg from 'pg'
import { hashPassword } from '../server/utils/auth'
import { host, port, secret, WEB_DB } from './env'

export const PASSWORD = 'correct horse battery staple'
export const USERS = {
  acmeAdmin: { tenant: 'acme', email: 'ann.admin@acme.test', role: 'admin' },
  acmeViewer: { tenant: 'acme', email: 'vic.viewer@acme.test', role: 'viewer' },
  globexViewer: { tenant: 'globex', email: 'gina.viewer@globex.test', role: 'viewer' },
} as const

export async function adminClient(db: string = WEB_DB): Promise<pg.Client> {
  const c = new pg.Client({ host: host(), port: port(), database: db, user: 'postgres', password: secret('XERLANE_ADMIN_PASSWORD') })
  await c.connect()
  await c.query('SET search_path = xerlane')
  return c
}

export async function seed(c: pg.Client): Promise<void> {
  await c.query('TRUNCATE app_session, app_user, login_attempt, cost_line, commitment, cost_load_run, quarantine, task, projwbs, project, calendar RESTART IDENTITY CASCADE')
  for (const [tenant, proj, short] of [['acme', 7, 'P7'], ['globex', 8, 'GLOBEX-8']] as const) {
    await c.query('INSERT INTO calendar (tenant_id, clndr_id) VALUES ($1, 1)', [tenant])
    await c.query(`INSERT INTO project (tenant_id, proj_id, proj_short_name, last_recalc_date, clndr_id) VALUES ($1, $2, $3, '2026-03-12 00:00', 1)`, [tenant, proj, short])
    await c.query(`INSERT INTO projwbs (tenant_id, wbs_id, proj_id, wbs_short_name) VALUES ($1, $2, $2, 'W')`, [tenant, proj])
  }
  const tasks: [number, string, string, string, string | null, string | null, number, number][] = [
    [1, 'T1', '2026-03-02', '2026-03-22', '2026-03-02', '2026-03-20', 80, 0],
    [2, 'T2', '2026-03-02', '2026-03-22', '2026-03-02', null, 80, 40],
    [3, 'T3', '2026-03-12', '2026-03-22', null, null, 80, 80],
    [4, 'T4', '2026-03-02', '2026-03-12', '2026-03-03', null, 100, 100],
  ]
  for (const [id, code, ts, te, as, ae, tgt, rem] of tasks) {
    await c.query(`INSERT INTO task (tenant_id, task_id, proj_id, wbs_id, clndr_id, task_code, task_name, target_start_date, target_end_date, act_start_date, act_end_date, target_drtn_hr_cnt, remain_drtn_hr_cnt)
                   VALUES ('acme', $1, 7, 7, 1, $2, $3, $4, $5, $6, $7, $8, $9)`, [id, code, `name ${code}`, ts, te, as, ae, tgt, rem])
  }
  await c.query(`INSERT INTO task (tenant_id, task_id, proj_id, wbs_id, clndr_id, task_code, task_name, target_start_date, target_end_date) VALUES ('globex', 1, 8, 8, 1, 'G1', 'secret globex task', '2026-03-02', '2026-03-22')`)
  const lines: [string, number, number, string, string | null, number, number, number][] = [
    ['acme', 7, 1, '03-100', 'T1', 1000, 1000, 1200], ['acme', 7, 2, '03-200', 'T1', 500, 500, 0], ['acme', 7, 3, '03-100', 'T2', 2000, 2100, 900],
    ['acme', 7, 4, '03-300', 'T3', 3000, 3000, 0], ['acme', 7, 5, '03-300', 'T4', 400, 400, 100], ['acme', 7, 6, '01-000', null, 100, 100, 50],
    ['globex', 8, 1, '99-999', 'G1', 123456, 123456, 654321],
  ]
  for (const [tenant, proj, id, code, task, b, f, a] of lines) {
    await c.query('INSERT INTO cost_line (tenant_id, proj_id, line_id, cost_code, task_code, budget, forecast, actual) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)', [tenant, proj, id, code, task, b, f, a])
  }
  await c.query(`INSERT INTO commitment (tenant_id, proj_id, commitment_id, vendor, cost_code, committed, invoiced, status) VALUES
    ('acme', 7, 1, 'V1', '03-100', 2500, 1000, 'OPEN'), ('acme', 7, 2, 'V2', '03-100', 900, 900, 'VOID'), ('acme', 7, 3, 'V3', '03-300', 2000, 100, 'OPEN')`)
  await c.query(`INSERT INTO quarantine (tenant_id, proj_id, source_label, source_sha256, table_name, line_no, reason_code, reason, raw) VALUES
    ('acme', 7, 'cost:c.csv', 'x', 'COST', 4, 'BAD_VALUE', 'budget must be a plain number', '[]'), ('globex', 8, 'cost:g.csv', 'y', 'COST', 9, 'BAD_VALUE', 'globex only', '[]')`)
  const hash = await hashPassword(PASSWORD)
  for (const u of Object.values(USERS)) {
    await c.query('INSERT INTO app_user (tenant_id, email, password_hash, role) VALUES ($1, $2, $3, $4)', [u.tenant, u.email, hash, u.role])
  }
}
