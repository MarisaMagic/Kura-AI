<template>
  <n-layout has-sider wh-full>
    <n-layout-sider
      bordered
      collapse-mode="width"
      :collapsed-width="64"
      :width="235"
      :native-scrollbar="true"
      :collapsed="appStore.collapsed"
      :class="
        appStore.collapsed ? 'bg-white dark:bg-dark' : 'bg-hex-f5f6fb dark:bg-hex-101014'
      "
      class="layout-sider"
    >
      <SideBar />
    </n-layout-sider>

    <article flex-col flex-1 overflow-hidden>
      <header
        class="flex items-center border-b bg-white px-15 bc-eee"
        dark="bg-dark border-0"
        :style="`height: ${header.height}px`"
      >
        <AppHeader class="min-w-0 flex-1" />
      </header>
      <section flex-1 overflow-hidden bg-white dark:bg-dark>
        <AppMain />
      </section>
    </article>
  </n-layout>
</template>

<script setup>
import AppHeader from './components/header/index.vue'
import SideBar from './components/sidebar/index.vue'
import AppMain from './components/AppMain.vue'
import { useAppStore } from '@/store'
import { header } from '~/settings'

// 移动端适配
import { useBreakpoints } from '@vueuse/core'

const appStore = useAppStore()
const breakpointsEnum = {
  xl: 1600,
  lg: 1199,
  md: 991,
  sm: 666,
  xs: 575,
}
const breakpoints = reactive(useBreakpoints(breakpointsEnum))
const isMobile = breakpoints.smaller('sm')
const isPad = breakpoints.between('sm', 'md')
const isPC = breakpoints.greater('md')
watchEffect(() => {
  if (isMobile.value) {
    // Mobile
    appStore.setCollapsed(true)
    appStore.setFullScreen(false)
  }

  if (isPad.value) {
    // IPad
    appStore.setCollapsed(true)
    appStore.setFullScreen(false)
  }

  if (isPC.value) {
    // PC
    appStore.setCollapsed(false)
    appStore.setFullScreen(true)
  }
})
</script>

<style scoped>
/*
 * native-scrollbar=false 时侧栏用 NScrollbar 包裹，.n-layout-sider-scroll-container 不存在，
 * 外层仍会滚动。改为 native + 禁止本容器滚动，仅 Sidebar 内 .layout-sider-history-scroll 滚动。
 */
.layout-sider {
  height: 100%;
  max-height: 100%;
  overflow: hidden;
}

.layout-sider :deep(.n-layout-sider-scroll-container) {
  background: transparent !important;
  overflow: hidden !important;
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

.layout-sider :deep(.n-layout-sider-scroll-container > *) {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
</style>
