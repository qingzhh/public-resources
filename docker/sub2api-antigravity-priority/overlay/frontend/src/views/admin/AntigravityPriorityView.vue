<template>
  <AppLayout>
    <section class="flex min-h-[calc(100dvh-8rem)] flex-col overflow-hidden rounded-xl border border-gray-200 bg-white dark:border-dark-700 dark:bg-dark-900">
      <iframe
        src="/api/v1/antigravity-priority-ui"
        title="Antigravity Priority"
        class="min-h-[calc(100dvh-8rem)] w-full flex-1 border-0"
        referrerpolicy="same-origin"
      />
    </section>
  </AppLayout>
</template>

<script setup lang="ts">
import AppLayout from '@/components/layout/AppLayout.vue'
</script>
