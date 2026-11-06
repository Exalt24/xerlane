<script setup lang="ts">
const me = useMe()
async function signOut() {
  await $fetch('/api/logout', { method: 'POST', body: {} })
  me.value = null
  await navigateTo('/login')
}
</script>

<template>
  <div>
    <header>
      <NuxtLink to="/">xerlane</NuxtLink>
      <nav v-if="me">
        <NuxtLink v-if="me.role === 'admin'" to="/admin/users">Users</NuxtLink>
        <span class="muted">{{ me.email }} ({{ me.tenant }})</span>
        <button class="plain" @click="signOut">Sign out</button>
      </nav>
    </header>
    <main><NuxtPage /></main>
  </div>
</template>
