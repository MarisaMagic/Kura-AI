<script setup>
import { computed } from 'vue'
import { NScrollbar } from 'naive-ui'
import { usePermissionStore } from '@/store'
import SideLogo from './components/SideLogo.vue'
import SideMenu from './components/SideMenu.vue'
import SidebarAgentBlock from './SidebarAgentBlock.vue'
import SidebarRecentConversations from './SidebarRecentConversations.vue'

const permissionStore = usePermissionStore()

const menus = computed(() => permissionStore.menus)

/** 系统管理置顶（管理员） */
const systemMenuRoutes = computed(() => menus.value.filter((r) => r.name === '系统管理'))

/** 智能体中心改由自定义区块「更多智能体」承担，避免与菜单重复 */
const restMenuRoutes = computed(() =>
  menus.value.filter((r) => r.name !== '系统管理' && r.name !== '智能体中心'),
)
</script>

<template>
  <div class="layout-sidebar-root">
    <SideLogo />
    <n-scrollbar class="layout-sidebar-scroll" trigger="none">
      <SideMenu v-if="systemMenuRoutes.length" :menu-routes="systemMenuRoutes" class="sidebar-menu-section" />
      <SidebarAgentBlock />
      <SidebarRecentConversations />
      <SideMenu v-if="restMenuRoutes.length" :menu-routes="restMenuRoutes" class="sidebar-menu-section" />
    </n-scrollbar>
  </div>
</template>

<style scoped>
.layout-sidebar-root {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}

.layout-sidebar-scroll {
  flex: 1;
  min-height: 0;
}

.layout-sidebar-scroll :deep(.n-scrollbar-content) {
  padding-bottom: 12px;
}
</style>
