<script setup lang="ts">
const me = useMe()
const email = ref('')
const password = ref('')
const error = ref('')
const busy = ref(false)

async function submit() {
  error.value = ''
  busy.value = true
  try {
    me.value = await $fetch<Me>('/api/login', { method: 'POST', body: { email: email.value, password: password.value } })
    await navigateTo('/')
  } catch (e: unknown) {
    error.value = (e as { statusMessage?: string })?.statusMessage || 'Could not sign in.'
  } finally {
    busy.value = false
    password.value = ''
  }
}
</script>

<template>
  <h1>Sign in</h1>
  <form class="stack" @submit.prevent="submit">
    <label>Email <input v-model="email" type="email" autocomplete="username" required></label>
    <label>Password <input v-model="password" type="password" autocomplete="current-password" required></label>
    <button :disabled="busy">Sign in</button>
    <p class="err" role="alert">{{ error }}</p>
  </form>
</template>
