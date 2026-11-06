import { createError, defineEventHandler, readBody, setResponseStatus } from 'h3'
import { login, MAX_PASSWORD, setSessionCookie } from '../utils/auth'

export default defineEventHandler(async (event) => {
  const body = await readBody<{ email?: unknown; password?: unknown }>(event)
  if (typeof body?.email !== 'string' || typeof body?.password !== 'string' || !body.password || body.password.length > MAX_PASSWORD) {
    throw createError({ statusCode: 400, statusMessage: 'Enter your email and password.' })
  }
  const result = await login(body.email, body.password)
  if (!result.ok) {
    if (result.reason === 'locked') throw createError({ statusCode: 429, statusMessage: 'Too many tries. Wait fifteen minutes.' })
    throw createError({ statusCode: 401, statusMessage: 'Wrong email or password.' })
  }
  setSessionCookie(event, result.token)
  setResponseStatus(event, 200)
  return { email: result.user.email, role: result.user.role, tenant: result.user.tenant }
})
