import { defineStore } from 'pinia'

/** 顶栏/对话页与侧栏「最近对话」列表同步刷新 */
export const useAgentSidebarStore = defineStore('agentSidebar', {
  state: () => ({
    refreshTick: 0,
  }),
  actions: {
    bumpRefresh() {
      this.refreshTick += 1
    },
  },
})
