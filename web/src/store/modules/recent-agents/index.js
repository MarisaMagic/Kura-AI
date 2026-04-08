import { defineStore } from 'pinia'
import api from '@/api'
import { getToken } from '@/utils'

/** 服务端：当前用户最近使用的智能体（最多 3 个） */
export const useRecentAgentsStore = defineStore('recentAgents', {
  state: () => ({
    list: [],
    loading: false,
  }),
  actions: {
    clear() {
      this.list = []
    },
    async fetch() {
      if (!getToken()) {
        this.list = []
        return
      }
      this.loading = true
      try {
        const res = await api.getRecentAgents()
        this.list = res.data?.agents || []
      } catch {
        this.list = []
      } finally {
        this.loading = false
      }
    },
    /** 记录使用并刷新列表（与 POST touch 返回一致） */
    async touch(agentId) {
      if (!getToken() || !agentId) return
      try {
        const res = await api.touchRecentAgent({ agent_id: agentId })
        this.list = res.data?.agents || []
      } catch {
        /* ignore */
      }
    },
  },
})
