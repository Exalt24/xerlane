import { afterAll, describe, expect, it } from 'vitest'
import { closePool, withTenant } from '../server/utils/db'
import { loadReport } from '../server/utils/ev'

// The same hand worked schedule as tests/test_evm.py (project 7, as of 2026-03-12 00:00). The numbers below are the ones worked out on paper there.
afterAll(async () => { await closePool() })
const report = (tenant: string, proj: number, asOf: string | null = null) => withTenant(tenant, (c) => loadReport(c, proj, asOf))

describe('earned value in SQL equals the hand worked figures', () => {
  it('per task', async () => {
    const r = (await report('acme', 7))!
    expect(r.project).toBe('P7')
    expect(r.asOf.startsWith('2026-03-12')).toBe(true)
    expect(r.tasks.map((t) => [t.key, t.bac, t.pv, t.ev, t.ac])).toEqual([
      ['T1', '1500.00', '750.00', '1500.00', '1200.00'],
      ['T2', '2000.00', '1000.00', '1000.00', '900.00'],
      ['T3', '3000.00', '0.00', '0.00', '0.00'],
      ['T4', '400.00', '400.00', '0.00', '100.00'],
    ])
    const t2 = r.tasks[1]!
    expect([t2.forecast, t2.cpi, t2.spi, t2.pctDone, t2.pctPlanned]).toEqual(['2100.00', '1.11', '1.00', '0.50', '0.50'])
  })

  it('in total, with the cost line that has no task counted in budget and actual only', async () => {
    const t = (await report('acme', 7))!.totals
    expect([t.bac, t.pv, t.ev, t.ac, t.cv, t.sv, t.cpi, t.spi, t.eac]).toEqual(['7000.00', '2150.00', '2500.00', '2250.00', '250.00', '350.00', '1.11', '1.16', '6306.31'])
    expect((await report('acme', 7))!.notes).toEqual(['1 cost line(s) have no task code. They count in BAC and AC only.'])
  })

  it('moves only the planned value with the as-of date', async () => {
    const early = (await report('acme', 7, '2026-03-02'))!.totals
    expect([early.pv, early.ev, early.ac]).toEqual(['0.00', '2500.00', '2250.00'])
    const late = (await report('acme', 7, '2026-04-01 00:00'))!.totals
    expect(late.pv).toBe('6900.00')
  })

  it('by cost code, with voided commitments left out', async () => {
    const codes = (await report('acme', 7))!.byCode
    expect(codes.map((c) => c.costCode)).toEqual(['01-000', '03-100', '03-200', '03-300'])
    const c100 = codes[1]!
    expect([c100.budget, c100.actual, c100.committed, c100.invoiced, c100.over]).toEqual(['3000.00', '2100.00', '2500.00', '1000.00', false])
  })

  it('shows a tenant its own project only', async () => {
    expect(await report('globex', 7)).toBeNull()
    expect(await report('acme', 8)).toBeNull()
    const g = (await report('globex', 8))!
    expect(g.totals.bac).toBe('123456.00')
    expect(g.tasks.map((t) => t.key)).toEqual(['G1'])
  })

  it('gives no ratio when the denominator is zero', async () => {
    const t = (await report('acme', 7, '2026-03-02'))!.totals
    expect(t.spi).toBeNull()
  })
})
