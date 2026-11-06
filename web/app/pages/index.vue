<script setup lang="ts">
interface Project { id: number; name: string; dataDate: string | null; tasks: number; costLines: number; setAside: number }
const { data: projects, error } = await useFetch<Project[]>('/api/projects')
</script>

<template>
  <h1>Projects</h1>
  <p v-if="error" class="err">Could not load the projects.</p>
  <p v-else-if="!projects?.length" class="muted">No project is loaded for you yet.</p>
  <table v-else>
    <thead><tr><th>Project</th><th>Data date</th><th>Tasks</th><th>Cost lines</th><th>Set aside</th></tr></thead>
    <tbody>
      <tr v-for="p in projects" :key="p.id">
        <td><NuxtLink :to="`/projects/${p.id}`">{{ p.name }}</NuxtLink></td>
        <td>{{ p.dataDate?.slice(0, 10) ?? '' }}</td>
        <td>{{ p.tasks }}</td>
        <td>{{ p.costLines }}</td>
        <td :class="{ bad: p.setAside > 0 }">{{ p.setAside }}</td>
      </tr>
    </tbody>
  </table>
</template>
