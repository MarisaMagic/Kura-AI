/**
 * 高危 MCP 工具确认：允许一次 / 拒绝。
 */
export function useChatMcpConfirm({
  messages,
  sending,
  confirmingMcpIds,
  chatAgentId,
  scrollBodyToBottom,
  postChatJobAndConsumeStream,
  getToken,
  t,
}) {
  const baseApi = import.meta.env.VITE_BASE_API || '/api/v1'

  async function resumeApprovedMcp(assistantMsg, pendingId) {
    const token = getToken()
    const assistantIdx = messages.value.findIndex((x) => x.id === assistantMsg.id)
    const agentId = Number(chatAgentId.value)
    if (!token || assistantIdx <= 0 || !Number.isFinite(agentId)) return
    const prev = messages.value[assistantIdx - 1]
    if (prev.role !== 'user') return

    const row = messages.value[assistantIdx]
    messages.value[assistantIdx] = {
      ...row,
      pending: true,
      mcpExecuting: true,
      mcpConfirmations: [],
      errorText: undefined,
      stoppedByUser: false,
    }
    sending.value = true
    try {
      await postChatJobAndConsumeStream({
        agentId,
        token,
        regenerate: true,
        message: '',
        attachmentIds: [],
        assistantIdx,
        mcpApprovedPendingId: pendingId,
        targetMessageId: assistantMsg.messageId || null,
      })
    } catch (error) {
      const cur = messages.value[assistantIdx]
      messages.value[assistantIdx] = {
        ...cur,
        pending: false,
        mcpExecuting: false,
        errorText: t('views.agents.chat_msg_stream_error') + `：${error?.message || error}`,
      }
    } finally {
      sending.value = false
      scrollBodyToBottom()
    }
  }

  async function approveMcpConfirmation(assistantMsg, item, approve) {
    const token = getToken()
    if (!token || !item?.pending_id) return
    const idx = messages.value.findIndex((x) => x.id === assistantMsg.id)
    if (confirmingMcpIds.value.has(item.pending_id)) return
    confirmingMcpIds.value = new Set([...confirmingMcpIds.value, item.pending_id])
    try {
      const res = await fetch(`${baseApi}/user-agent/chat/mcp/confirm`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json', token },
        body: JSON.stringify({ pending_id: item.pending_id, approve }),
      })
      if (!res.ok) {
        let detail = `HTTP ${res.status}`
        try {
          const body = await res.json()
          detail = body.detail || body.msg || detail
        } catch {
          /* ignore */
        }
        throw new Error(detail)
      }
      if (idx !== -1) {
        const row = messages.value[idx]
        messages.value[idx] = {
          ...row,
          mcpConfirmations: (row.mcpConfirmations || []).filter((x) => x.pending_id !== item.pending_id),
        }
      }
      if (approve && idx !== -1) {
        await resumeApprovedMcp(messages.value[idx], item.pending_id)
      }
    } catch (error) {
      window.$message?.error(`${error?.message || error}`)
    } finally {
      const next = new Set(confirmingMcpIds.value)
      next.delete(item.pending_id)
      confirmingMcpIds.value = next
    }
  }

  return { approveMcpConfirmation, resumeApprovedMcp }
}
