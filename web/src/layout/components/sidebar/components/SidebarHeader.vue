<template>
  <div
    class="layout-sidebar-header"
    :class="{ 'layout-sidebar-header--collapsed': appStore.collapsed }"
  >
    <router-link v-show="!appStore.collapsed" class="layout-sidebar-logo" to="/">
      <icon-custom-logo text-36 color-primary></icon-custom-logo>
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
</style>
