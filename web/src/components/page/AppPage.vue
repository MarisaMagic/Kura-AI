<template>
  <transition name="fade-slide" mode="out-in" appear>
    <section
      :class="
        scrollInParent
          ? 'w-full flex flex-col bg-white dark:bg-dark'
          : 'cus-scroll-y wh-full flex-col bg-white p-15 dark:bg-dark'
      "
    >
      <slot />
      <AppFooter v-if="showFooter" mt-15 />
      <n-back-top v-if="!scrollInParent" :bottom="20" />
    </section>
  </transition>
</template>

<script setup>
defineProps({
  showFooter: {
    type: Boolean,
    default: false,
  },
  /** 由外层布局（如 AgentEditorLayout 的 main）滚动；避免与 body overflow:hidden 叠加导致无法纵向滚动 */
  scrollInParent: {
    type: Boolean,
    default: false,
  },
})
</script>
