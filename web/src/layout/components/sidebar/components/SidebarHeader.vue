<template>
  <div
    class="layout-sidebar-header"
    :class="{ 'layout-sidebar-header--collapsed': appStore.collapsed }"
  >
    <router-link v-show="!appStore.collapsed" class="layout-sidebar-logo" to="/">
      <img :src="brandLogoSrc" alt="" width="36" height="36" class="layout-sidebar-brand-logo" />
      <h2 ml-2 mr-8 max-w-150 flex-shrink-0 text-16 font-bold color-primary>
        {{ title }}
      </h2>
    </router-link>
    <MenuCollapse />
  </div>
</template>

<script setup>
import { useAppStore } from '@/store'
import MenuCollapse from './MenuCollapse.vue'

const title = import.meta.env.VITE_TITLE
const brandLogoSrc = `${import.meta.env.BASE_URL}logo.svg`.replace(/\/{2,}/, '/')

const appStore = useAppStore()
</script>

<style scoped>
.layout-sidebar-header {
  flex-shrink: 0;
  height: 60px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 16px 0 14px;
  box-sizing: border-box;
}

/* 收起时 logo 隐藏，只保留居中的缩放按钮 */
.layout-sidebar-header--collapsed {
  justify-content: center;
  padding: 0;
}

.layout-sidebar-logo {
  display: flex;
  align-items: center;
  min-width: 0;
  overflow: hidden;
}

.layout-sidebar-brand-logo {
  display: block;
  width: 36px;
  height: 36px;
  flex-shrink: 0;
  object-fit: contain;
}
</style>
