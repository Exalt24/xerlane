// Every page except the sign in page needs a signed in user; the admin pages need an administrator. The server checks again on every request.
export default defineNuxtRouteMiddleware(async (to) => {
  const me = useMe()
  if (!me.value) {
    try {
      me.value = await $fetch<Me>('/api/me')
    } catch {
      me.value = null
    }
  }
  if (to.path === '/login') return me.value ? navigateTo('/') : undefined
  if (!me.value) return navigateTo('/login')
  if (to.path.startsWith('/admin') && me.value.role !== 'admin') return navigateTo('/')
})
