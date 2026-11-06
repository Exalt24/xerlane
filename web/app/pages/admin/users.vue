<script setup lang="ts">
interface U { id: number; email: string; role: string; disabled: boolean; created: string }
const { data: users, refresh } = await useFetch<U[]>('/api/admin/users')
const email = ref('')
const password = ref('')
const role = ref<'viewer' | 'admin'>('viewer')
const error = ref('')

async function add() {
  error.value = ''
  try {
    await $fetch('/api/admin/users', { method: 'POST', body: { email: email.value, password: password.value, role: role.value } })
    email.value = ''
    password.value = ''
    await refresh()
  } catch (e: unknown) {
    error.value = (e as { statusMessage?: string })?.statusMessage || 'Could not add the user.'
  }
}

async function disable(id: number) {
  error.value = ''
  try {
    await $fetch(`/api/admin/users/${id}/disable`, { method: 'POST', body: {} })
    await refresh()
  } catch (e: unknown) {
    error.value = (e as { statusMessage?: string })?.statusMessage || 'Could not disable the user.'
  }
}
</script>

<template>
  <h1>Users</h1>
  <table>
    <thead><tr><th>Email</th><th>Role</th><th>Status</th><th /></tr></thead>
    <tbody>
      <tr v-for="u in users" :key="u.id">
        <td>{{ u.email }}</td><td>{{ u.role }}</td><td :class="{ muted: u.disabled }">{{ u.disabled ? 'Disabled' : 'Active' }}</td>
        <td><button v-if="!u.disabled" class="plain" @click="disable(u.id)">Disable</button></td>
      </tr>
    </tbody>
  </table>
  <h2>Add a user</h2>
  <form class="stack" @submit.prevent="add">
    <label>Email <input v-model="email" type="email" required></label>
    <label>Password <input v-model="password" type="password" autocomplete="new-password" minlength="12" required></label>
    <label>Role <select v-model="role"><option value="viewer">Viewer</option><option value="admin">Admin</option></select></label>
    <button>Add user</button>
    <p class="err" role="alert">{{ error }}</p>
  </form>
</template>
