// A single page app in front of a Nitro API. Everything that decides who sees what lives in server/, never in a page.
export default defineNuxtConfig({
  compatibilityDate: '2026-11-01',
  ssr: false,
  devtools: { enabled: false },
  telemetry: false,
  css: ['~/assets/main.css'],
  app: { head: { title: 'xerlane', meta: [{ name: 'viewport', content: 'width=device-width, initial-scale=1' }] } },
  typescript: { strict: true },
})
