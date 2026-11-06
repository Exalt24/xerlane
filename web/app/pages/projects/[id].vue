<script setup lang="ts">
interface Line { key: string; name: string; bac: string; pv: string; ev: string; ac: string; cv: string; sv: string; cpi: string | null; spi: string | null; eac: string | null }
interface Report { project: string; asOf: string; totals: Line; tasks: Line[]; notes: string[]; byCode: { costCode: string; budget: string; actual: string; committed: string; invoiced: string; over: boolean }[] }
interface Aside { table: string; line: number; code: string; reason: string; source: string }

const route = useRoute()
const id = computed(() => route.params.id)
const asOf = ref('')
const query = computed(() => (asOf.value ? { asOf: asOf.value } : {}))
const { data: report, error, refresh } = await useFetch<Report>(() => `/api/projects/${id.value}/report`, { query, watch: false })
const { data: aside } = await useFetch<Aside[]>(() => `/api/projects/${id.value}/quarantine`)

const fmt = (x: string | null) => (x === null ? 'n/a' : Number(x).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }))
const top = computed(() => Math.max(1, ...['pv', 'ev', 'ac'].map((k) => Number(report.value?.totals[k as 'pv'] ?? 0))))
const bars = computed(() => (['pv', 'ev', 'ac'] as const).map((k) => ({ k, label: k.toUpperCase(), value: report.value?.totals[k] ?? '0', width: (Number(report.value?.totals[k] ?? 0) / top.value) * 100 })))
const worst = computed(() => [...(report.value?.tasks ?? [])].sort((a, b) => Number(a.cv) - Number(b.cv)).slice(0, 25))
const barColor = { pv: '#8a94a0', ev: '#2f7d4f', ac: '#b5483a' }
</script>

<template>
  <p><NuxtLink to="/">All projects</NuxtLink></p>
  <p v-if="error" class="err">This project is not available.</p>
  <template v-else-if="report">
    <h1>{{ report.project }}</h1>
    <form class="row" @submit.prevent="refresh()">
      <label>As of <input v-model="asOf" type="date" :placeholder="report.asOf.slice(0, 10)"></label>
      <button class="plain">Update</button>
      <span class="muted">Showing {{ report.asOf.slice(0, 16) }}</span>
    </form>
    <div class="tiles">
      <div class="tile"><span>Budget (BAC)</span><b>{{ fmt(report.totals.bac) }}</b></div>
      <div class="tile"><span>Planned (PV)</span><b>{{ fmt(report.totals.pv) }}</b></div>
      <div class="tile"><span>Earned (EV)</span><b>{{ fmt(report.totals.ev) }}</b></div>
      <div class="tile"><span>Actual (AC)</span><b>{{ fmt(report.totals.ac) }}</b></div>
      <div class="tile"><span>Cost index (CPI)</span><b :class="Number(report.totals.cpi) < 1 ? 'bad' : 'good'">{{ fmt(report.totals.cpi) }}</b></div>
      <div class="tile"><span>Schedule index (SPI)</span><b :class="Number(report.totals.spi) < 1 ? 'bad' : 'good'">{{ fmt(report.totals.spi) }}</b></div>
      <div class="tile"><span>Forecast (EAC)</span><b>{{ fmt(report.totals.eac) }}</b></div>
    </div>
    <div class="chart" aria-label="Planned, earned and actual cost">
      <div v-for="b in bars" :key="b.k"><span>{{ b.label }}</span><i :style="{ width: b.width + '%', background: barColor[b.k] }" /><span class="muted">{{ fmt(b.value) }}</span></div>
    </div>
    <p v-for="n in report.notes" :key="n" class="muted">{{ n }}</p>

    <h2>Tasks furthest over cost</h2>
    <table>
      <thead><tr><th>Task</th><th class="left">Name</th><th>BAC</th><th>PV</th><th>EV</th><th>AC</th><th>CPI</th><th>SPI</th></tr></thead>
      <tbody>
        <tr v-for="t in worst" :key="t.key">
          <td>{{ t.key }}</td><td class="left">{{ t.name }}</td><td>{{ fmt(t.bac) }}</td><td>{{ fmt(t.pv) }}</td><td>{{ fmt(t.ev) }}</td><td>{{ fmt(t.ac) }}</td>
          <td :class="{ bad: t.cpi !== null && Number(t.cpi) < 1 }">{{ fmt(t.cpi) }}</td><td :class="{ bad: t.spi !== null && Number(t.spi) < 1 }">{{ fmt(t.spi) }}</td>
        </tr>
      </tbody>
    </table>

    <h2>Cost codes</h2>
    <table>
      <thead><tr><th>Code</th><th>Budget</th><th>Actual</th><th>Committed</th><th>Invoiced</th><th /></tr></thead>
      <tbody>
        <tr v-for="c in report.byCode" :key="c.costCode">
          <td>{{ c.costCode }}</td><td>{{ fmt(c.budget) }}</td><td>{{ fmt(c.actual) }}</td><td>{{ fmt(c.committed) }}</td><td>{{ fmt(c.invoiced) }}</td>
          <td :class="{ bad: c.over }">{{ c.over ? 'Over budget' : '' }}</td>
        </tr>
      </tbody>
    </table>

    <h2>Rows set aside</h2>
    <p v-if="!aside?.length" class="muted">Nothing was set aside for this project.</p>
    <table v-else>
      <thead><tr><th>File</th><th>Type</th><th>Line</th><th>Reason</th></tr></thead>
      <tbody>
        <tr v-for="(a, i) in aside" :key="i"><td>{{ a.source }}</td><td>{{ a.table }}</td><td>{{ a.line }}</td><td class="left">{{ a.reason }}</td></tr>
      </tbody>
    </table>
  </template>
</template>
