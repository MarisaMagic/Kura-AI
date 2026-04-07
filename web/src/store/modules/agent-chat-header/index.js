import { defineStore } from 'pinia'

/** 供顶栏智能体对话标题与信息弹层（与 agent-chat 视图同步） */
export const useAgentChatHeaderStore = defineStore('agentChatHeader', {
  state: () => ({
    agentTitle: '',
    avatarUrl: '',
    /** 名称下方副标题（简介或名称） */
    subtitle: '',
    creatorName: '',
    agentId: null,
  }),
  actions: {
    setAgentMeta({ title, avatarUrl, subtitle, creatorName, agentId } = {}) {
      this.agentTitle = title || ''
      this.avatarUrl = avatarUrl || ''
      this.subtitle = subtitle || ''
      this.creatorName = creatorName || ''
      this.agentId = agentId != null ? agentId : null
    },
    setAgentTitle(title) {
      this.agentTitle = title || ''
    },
    clear() {
      this.agentTitle = ''
      this.avatarUrl = ''
      this.subtitle = ''
      this.creatorName = ''
      this.agentId = null
    },
  },
})
