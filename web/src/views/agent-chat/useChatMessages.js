import { nextTick, ref } from 'vue'
import api from '@/api'
import { getToken } from '@/utils'
import { buildThinkingItemsFromRow } from '@/utils/agentChatThinking'
import { useAgentSidebarStore } from '@/store'
import { clearPendingChatJob } from './useChatJobStream.js'

export function userContentFromHistoryRow(row) {
  const cj = row.content_json
  if (cj && cj.lc != null) {
    const lc = cj.lc
    if (typeof lc === 'string') return lc
    if (Array.isArray(lc)) {
      const parts = []
      for (const b of lc) {
        if (typeof b === 'string') parts.push(b)
        else if (b && b.type === 'text' && b.text) parts.push(b.text)
        else if (b && (b.type === 'image_ref' || b.type === 'file_ref')) {
          /* 附件见 attachmentsFromHistoryRow */
        }
      }
      const s = parts.join('\n').trim()
      if (s) return s
    }
  }
  return row.content || ''
}

export function attachmentsFromHistoryRow(row, t) {
  if (row.type !== 'human') return undefined
  const cj = row.content_json
  if (!cj || cj.lc == null) return undefined
  const lc = cj.lc
  if (!Array.isArray(lc)) return undefined
  const out = []
  for (const b of lc) {
    if (!b || typeof b !== 'object') continue
    if (b.type === 'image_ref') {
      out.push({
        name:
          (b.filename && String(b.filename).trim()) ||
          t('views.agents.chat_attachment_image_fallback'),
        kind: 'image',
        mime: b.mime || '',
        attachmentId: b.attachment_id != null ? String(b.attachment_id) : '',
      })
    } else if (b.type === 'file_ref') {
      out.push({
        name:
          (b.filename && String(b.filename).trim()) ||
          (b.attachment_id && String(b.attachment_id)) ||
          t('views.agents.chat_attachment_file_fallback'),
        kind: b.kind || 'other',
        mime: b.mime || '',
        attachmentId: b.attachment_id != null ? String(b.attachment_id) : '',
      })
    }
  }
  return out.length ? out : undefined
}

export function mapHistoryRow(row, i, t) {
  const role = row.type === 'human' ? 'user' : 'assistant'
  const base = {
    id: row.message_id ? `hist-${row.message_id}` : `hist-${i}-${row.timestamp}`,
    messageId: row.message_id ?? null,
    role,
    content: userContentFromHistoryRow(row),
    pending: false,
    thinkingOpen: row.type === 'human' ? undefined : false,
    thinkingItems: buildThinkingItemsFromRow(row),
    ragTrace: row.rag_trace || null,
    errorText: row.error_text || undefined,
    sources: Array.isArray(row.sources) ? row.sources : [],
    versionIndex: row.version_index || 1,
    versionCount: row.version_count || 1,
    siblingIds: Array.isArray(row.sibling_ids) ? row.sibling_ids : [],
  }
  if (role === 'user') {
    const att = attachmentsFromHistoryRow(row, t)
    if (att) base.attachments = att
  }
  return base
}

/**
 * 历史映射、版本切换、重新生成。
 */
export function useChatMessages({
  t,
  messages,
  sessionId,
  sessionPhase,
  sending,
  chatAgentId,
  scrollBodyToBottom,
  persistSessionId,
  postChatJobAndConsumeStream,
  streamStoppedByUser,
}) {
  const switchingBranch = ref(false)
  const agentSidebarStore = useAgentSidebarStore()

  function mapRow(row, i) {
    return mapHistoryRow(row, i, t)
  }

  async function loadMessagesForSession(agentId, sid) {
    const res = await api.getAgentChatSessionMessages(agentId, sid)
    const rows = res.data?.messages || []
    const list = rows.map(mapRow)
    messages.value = list
    sessionId.value = sid
    sessionPhase.value = list.length > 0 ? 'chat' : 'intro'
    await nextTick()
    scrollBodyToBottom(false, { force: true })
  }

  async function reloadSessionMessages(agentId) {
    if (!agentId || !sessionId.value) return
    try {
      const res = await api.getAgentChatSessionMessages(agentId, sessionId.value)
      const rows = res.data?.messages || []
      messages.value = rows.map(mapRow)
      await nextTick()
      scrollBodyToBottom(false, { force: true })
    } catch (e) {
      console.warn('reload session messages:', e)
    }
  }

  async function switchAssistantVersion(assistantMsg, delta) {
    if (sending.value || switchingBranch.value) return
    const ids = assistantMsg.siblingIds || []
    const targetIdx = (assistantMsg.versionIndex || 1) - 1 + delta
    if (targetIdx < 0 || targetIdx >= ids.length) return
    const targetId = ids[targetIdx]
    const agentId = Number(chatAgentId.value)
    if (!getToken() || !Number.isFinite(agentId) || !sessionId.value) return
    switchingBranch.value = true
    try {
      const res = await api.selectAgentChatBranch(agentId, sessionId.value, targetId)
      const rows = res.data?.messages || []
      messages.value = rows.map(mapRow)
      await nextTick()
      scrollBodyToBottom(false, { force: true })
    } catch (error) {
      window.$message?.error(`${error?.message || error}`)
    } finally {
      switchingBranch.value = false
    }
  }

  async function regenerateAssistant(assistantMsg) {
    if (sending.value || switchingBranch.value) return
    const token = getToken()
    if (!token) {
      window.$message?.warning(t('views.agents.chat_msg_need_login'))
      return
    }
    if (!assistantMsg.messageId) return
    const assistantIdx = messages.value.findIndex((x) => x.id === assistantMsg.id)
    if (assistantIdx <= 0) return
    const prev = messages.value[assistantIdx - 1]
    if (prev.role !== 'user') return

    const agentId = Number(chatAgentId.value)
    if (!Number.isFinite(agentId)) {
      window.$message?.error(t('views.agents.chat_error_load_agent'))
      return
    }

    messages.value = messages.value.slice(0, assistantIdx + 1)
    messages.value[assistantIdx] = {
      ...assistantMsg,
      content: '',
      errorText: undefined,
      stoppedByUser: false,
      pending: true,
      thinkingOpen: false,
      thinkingItems: [],
      ragTrace: null,
      sources: [],
    }
    scrollBodyToBottom(false, { force: true })

    sending.value = true
    try {
      await postChatJobAndConsumeStream({
        agentId,
        token,
        regenerate: true,
        message: '',
        attachmentIds: [],
        assistantIdx,
        targetMessageId: assistantMsg.messageId,
      })
      if (!streamStoppedByUser.value && !messages.value[assistantIdx]?.mcpConfirmations?.length) {
        await reloadSessionMessages(agentId)
      }
    } catch (error) {
      clearPendingChatJob(agentId, sessionId.value)
      if (streamStoppedByUser.value) {
        if (messages.value[assistantIdx]?.pending) {
          const row = messages.value[assistantIdx]
          messages.value[assistantIdx] = {
            ...row,
            pending: false,
            stoppedByUser: true,
            errorText: undefined,
            thinkingOpen: row.thinkingOpen ?? false,
            ragTrace: row.ragTrace ?? null,
          }
        }
      } else {
        const row = messages.value[assistantIdx]
        messages.value[assistantIdx] = {
          ...row,
          errorText: t('views.agents.chat_msg_stream_error') + `：${error?.message || error}`,
          pending: false,
          thinkingOpen: row.thinkingOpen ?? false,
          ragTrace: row.ragTrace ?? null,
        }
      }
    } finally {
      sending.value = false
      if (agentId && sessionId.value) persistSessionId(agentId, sessionId.value)
      scrollBodyToBottom()
      agentSidebarStore.bumpRefresh()
    }
  }

  return {
    mapHistoryRow: mapRow,
    switchingBranch,
    loadMessagesForSession,
    reloadSessionMessages,
    switchAssistantVersion,
    regenerateAssistant,
  }
}
