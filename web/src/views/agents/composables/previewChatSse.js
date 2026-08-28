/** 编辑器预览试聊 SSE 工具（与 agent-chat 字段一致） */

const PENDING_JOB_PREFIX = 'kura_ai_preview_job_'

export function previewPendingJobStorageKey(agentId, sid) {
  return `${PENDING_JOB_PREFIX}${agentId}_${sid}`
}

export function savePreviewPendingJob(agentId, sid, payload) {
  try {
    if (agentId && sid && payload?.job_id) {
      sessionStorage.setItem(previewPendingJobStorageKey(agentId, sid), JSON.stringify(payload))
    }
  } catch {
    /* ignore */
  }
}

export function readPreviewPendingJob(agentId, sid) {
  try {
    const s = sessionStorage.getItem(previewPendingJobStorageKey(agentId, sid))
    if (!s) return null
    return JSON.parse(s)
  } catch {
    return null
  }
}

export function clearPreviewPendingJob(agentId, sid) {
  try {
    if (agentId && sid) sessionStorage.removeItem(previewPendingJobStorageKey(agentId, sid))
  } catch {
    /* ignore */
  }
}

/** 将单条 SSE JSON 事件应用到助手消息行 */
export function applyPreviewChatSsePayload(data, messagesRef, idx) {
  if (idx === -1) return
  const list = messagesRef.value ?? messagesRef
  if (data.type === 'content') {
    const row = list[idx]
    list[idx] = {
      ...row,
      content: (row.content || '') + (data.content || ''),
      pending: false,
      thinkingOpen: row.thinkingOpen ?? false,
      ragSteps: row.ragSteps || [],
      ragTrace: row.ragTrace ?? null,
    }
  } else if (data.type === 'thinking_move') {
    const cur = list[idx]
    const text = data.text || ''
    const curContent = cur.content || ''
    list[idx] = {
      ...cur,
      content:
        text && curContent.endsWith(text)
          ? curContent.slice(0, curContent.length - text.length)
          : curContent,
      thinkingText: (cur.thinkingText || '') + text,
      thinkingOpen: true,
      pending: true,
    }
  } else if (data.type === 'thinking_text') {
    const cur = list[idx]
    list[idx] = {
      ...cur,
      thinkingText: (cur.thinkingText || '') + (data.content || ''),
      thinkingOpen: true,
      pending: cur.pending,
    }
  } else if (data.type === 'rag_step') {
    const cur = list[idx]
    list[idx] = {
      ...cur,
      ragSteps: [...(cur.ragSteps || []), data.step || {}],
      thinkingOpen: true,
      pending: cur.pending,
    }
  } else if (data.type === 'trace') {
    const cur = list[idx]
    list[idx] = {
      ...cur,
      ragTrace: data.rag_trace || null,
      pending: cur.pending,
    }
  } else if (data.type === 'sources') {
    const cur = list[idx]
    list[idx] = {
      ...cur,
      sources: Array.isArray(data.sources) ? data.sources : [],
      pending: cur.pending,
    }
  } else if (data.type === 'error') {
    const cur = list[idx]
    list[idx] = {
      ...cur,
      errorText: data.content || '',
      pending: false,
      thinkingOpen: cur.thinkingOpen ?? false,
      ragSteps: cur.ragSteps || [],
      ragTrace: cur.ragTrace ?? null,
    }
  } else if (data.type === 'cancelled') {
    const cur = list[idx]
    list[idx] = {
      ...cur,
      stoppedByUser: true,
      pending: false,
      thinkingOpen: cur.thinkingOpen ?? false,
      ragSteps: cur.ragSteps || [],
      ragTrace: cur.ragTrace ?? null,
      errorText: undefined,
    }
  } else if (data.type === 'done') {
    const row = list[idx]
    list[idx] = {
      ...row,
      pending: false,
      stoppedByUser: data.cancelled ? true : row.stoppedByUser,
    }
  }
  if (messagesRef.value) {
    messagesRef.value = [...list]
  }
}

export async function readPreviewChatJobSseStream({
  reader,
  decoder,
  idx,
  jobId,
  agentId,
  sessionId,
  messagesRef,
  initialSeq = 0,
  onChunk,
}) {
  let buffer = ''
  let seq = initialSeq

  while (true) {
    let chunk
    try {
      chunk = await reader.read()
    } catch {
      break
    }
    const { done, value } = chunk
    if (done) break

    buffer += decoder.decode(value, { stream: true })

    let eventEndIndex
    while ((eventEndIndex = buffer.indexOf('\n\n')) !== -1) {
      const eventStr = buffer.slice(0, eventEndIndex)
      buffer = buffer.slice(eventEndIndex + 2)

      if (!eventStr.startsWith('data: ')) continue
      const dataStr = eventStr.slice(6)
      if (dataStr === '[DONE]') {
        clearPreviewPendingJob(agentId, sessionId)
        continue
      }
      try {
        const data = JSON.parse(dataStr)
        applyPreviewChatSsePayload(data, messagesRef, idx)
        seq += 1
        savePreviewPendingJob(agentId, sessionId, { job_id: jobId, seq })
        if (data.type === 'done') {
          clearPreviewPendingJob(agentId, sessionId)
        }
      } catch (e) {
        console.warn('Preview SSE parse error:', e)
      }
    }
    onChunk?.()
  }
  if (idx !== -1) {
    const list = messagesRef.value ?? messagesRef
    if (list[idx]?.pending) {
      list[idx] = { ...list[idx], pending: false }
      if (messagesRef.value) messagesRef.value = [...list]
    }
  }
}
