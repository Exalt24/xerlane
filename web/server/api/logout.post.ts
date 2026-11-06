import { defineEventHandler, getCookie } from 'h3'
import { clearSessionCookie, COOKIE, endSession } from '../utils/auth'

export default defineEventHandler(async (event) => {
  await endSession(getCookie(event, COOKIE))
  clearSessionCookie(event)
  return { ok: true }
})
