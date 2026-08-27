<script setup>
import { computed } from 'vue'
import { usePermissionStore, useAppStore } from '@/store'
import SidebarHeader from './components/SidebarHeader.vue'
import SideMenu from './components/SideMenu.vue'
import SidebarAgentBlock from './SidebarAgentBlock.vue'
import SidebarRecentConversations from './SidebarRecentConversations.vue'
import SidebarUserCard from './components/SidebarUserCard.vue'

const permissionStore = usePermissionStore()
const appStore = useAppStore()

const menus = computed(() => permissionStore.menus)
const sidebarCollapsed = computed(() => appStore.collapsed)

/** 系统管理置顶（管理员） */
const systemMenuRoutes = computed(() => menus.value.filter((r) => r.name === '系统管理'))

/** 智能体中心改由自定义区块「更多智能体」承担，避免与菜单重复 */
const restMenuRoutes = computed(() =>
  menus.value.filter((r) => r.name !== '系统管理' && r.name !== '智能体中心')
)
</script>

<template>
  <div class="layout-sidebar-root">
    <SidebarHeader />
    <div class="layout-sidebar-main">
      <div class="layout-sidebar-upper">
        <div
          class="layout-sidebar-block"
          :class="{ 'layout-sidebar-block--collapsed': sidebarCollapsed }"
        >
          <SideMenu
            v-if="systemMenuRoutes.length"
            :menu-routes="systemMenuRoutes"
            class="sidebar-menu-section"
          />
          <SidebarAgentBlock />
        </div>
      </div>
      <div
        class="layout-sidebar-recent-wrap"
        :class="{ 'layout-sidebar-recent-wrap--collapsed': sidebarCollapsed }"
      >
        <SidebarRecentConversations />
      </div>
      <div v-if="restMenuRoutes.length" class="layout-sidebar-lower">
        <div class="layout-sidebar-block">
          <SideMenu :menu-routes="restMenuRoutes" class="sidebar-menu-section" />
        </div>
      </div>
    </div>
    <SidebarUserCard />
  </div>
</template>

<style scoped>
.layout-sidebar-root {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}

.layout-sidebar-main {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

.layout-sidebar-upper,
.layout-sidebar-lower {
  flex-shrink: 0;
  overflow: hidden;
}

.layout-sidebar-block {
  padding-bottom: 12px;
}

/* 折叠时去掉为展开列表预留的底部空隙，拉近两个圆形按钮间距 */
.layout-sidebar-block--collapsed {
  padding-bottom: 0;
}

.layout-sidebar-recent-wrap {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* 折叠时中间区不再吃满剩余高度，避免「最近对话」按钮在 flex 区域内垂直居中、与上方按钮间距过大 */
.layout-sidebar-recent-wrap--collapsed {
  flex: 0 0 auto;
  min-height: 0;
  overflow: visible;
}
</style>
