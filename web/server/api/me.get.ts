import { defineEventHandler } from 'h3'
import { requireUser } from '../utils/auth'

export default defineEventHandler(async (event) => {
  const u = await requireUser(event)
  return { email: u.email, role: u.role, tenant: u.tenant }
})
