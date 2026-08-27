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
    /** 当前登录用户是否为该智能体属主（非属主隐藏编辑/知识库入口） */
    isOwner: true,
    /** 当前会话标题（首条用户消息摘要），顶栏居中展示 */
    sessionTitle: '',
  }),
  actions: {
    setAgentMeta({ title, avatarUrl, subtitle, creatorName, agentId, isOwner } = {}) {
      this.agentTitle = title || ''
      this.avatarUrl = avatarUrl || ''
      this.subtitle = subtitle || ''
      this.creatorName = creatorName || ''
      this.agentId = agentId != null ? agentId : null
      if (isOwner !== undefined) this.isOwner = !!isOwner
    },
    setAgentTitle(title) {
      this.agentTitle = title || ''
    },
    setSessionTitle(title) {
      this.sessionTitle = (title && String(title).trim()) || ''
    },
    clear() {
      this.agentTitle = ''
      this.avatarUrl = ''
      this.subtitle = ''
      this.creatorName = ''
      this.agentId = null
      this.isOwner = true
      this.sessionTitle = ''
    },
  },
})
