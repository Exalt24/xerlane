// Runs before every request: security headers on every answer, and the same-site check on anything that changes state.
import { defineEventHandler, setResponseHeaders } from 'h3'
import { checkSameSite } from '../utils/auth'

// COPY-OK: a header value, not screen text. Nuxt writes one inline script into the page shell, so script-src keeps 'unsafe-inline'; everything else is same-origin only.
const CSP = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"

export default defineEventHandler((event) => {
  setResponseHeaders(event, {
    'X-Content-Type-Options': 'nosniff',
    'X-Frame-Options': 'DENY',
    'Referrer-Policy': 'same-origin',
    'Cross-Origin-Opener-Policy': 'same-origin',
    'Permissions-Policy': 'camera=(), microphone=(), geolocation=()',
    'Content-Security-Policy': CSP,
  })
  if (event.path.startsWith('/api/')) {
    setResponseHeaders(event, { 'Cache-Control': 'no-store' })
    checkSameSite(event)
  }
})
