import { ref } from 'vue'

const SESSION_KEY_PREFIX = 'kura_ai_chat_session_'
const KB_TOGGLE_PREFIX = 'kura_ai_kb_'
const WEB_TOGGLE_PREFIX = 'kura_ai_web_'

function sessionStorageKey(agentId) {
  return `${SESSION_KEY_PREFIX}${agentId}`
}

function persistSessionId(agentId, sid) {
  try {
    if (agentId && sid) sessionStorage.setItem(sessionStorageKey(agentId), sid)
  } catch {
    /* ignore */
  }
}

function readStoredSessionId(agentId) {
  try {
    return sessionStorage.getItem(sessionStorageKey(agentId)) || ''
  } catch {
    return ''
  }
}

function clearStoredSessionId(agentId) {
  try {
    sessionStorage.removeItem(sessionStorageKey(agentId))
  } catch {
    /* ignore */
  }
}

function kbToggleStorageKey(agentId, sid) {
  return `${KB_TOGGLE_PREFIX}${agentId}_${sid}`
}

function webToggleStorageKey(agentId, sid) {
  return `${WEB_TOGGLE_PREFIX}${agentId}_${sid}`
}

function readKbPreference(agentId, sid) {
  if (!agentId || !sid) return false
  try {
    const v = sessionStorage.getItem(kbToggleStorageKey(agentId, sid))
    if (v === null) return false
    return v === 'true'
  } catch {
    return false
  }
}

function writeKbPreference(agentId, sid, val) {
  try {
    if (agentId && sid) sessionStorage.setItem(kbToggleStorageKey(agentId, sid), val ? 'true' : 'false')
  } catch {
    /* ignore */
  }
}

function readWebPreference(agentId, sid) {
  if (!agentId || !sid) return false
  try {
    const v = sessionStorage.getItem(webToggleStorageKey(agentId, sid))
    if (v === null) return false
    return v === 'true'
  } catch {
    return false
  }
}

function writeWebPreference(agentId, sid, val) {
  try {
    if (agentId && sid) sessionStorage.setItem(webToggleStorageKey(agentId, sid), val ? 'true' : 'false')
  } catch {
    /* ignore */
  }
}

/**
 * 会话 id 与知识库/联网开关的 sessionStorage 持久化。
 */
export function useChatSession() {
  const sessionId = ref(`session_${Date.now()}`)
  const ignoreNextQueryWatch = ref(false)
  const useKnowledgeRetrieval = ref(false)
  const useWebSearch = ref(false)

  function applyKbPreferenceForCurrentSession(agentId) {
    useKnowledgeRetrieval.value = readKbPreference(agentId, sessionId.value)
    useWebSearch.value = readWebPreference(agentId, sessionId.value)
  }

  function onKbToggle(agentId, val) {
    if (useWebSearch.value && val) useWebSearch.value = false
    useKnowledgeRetrieval.value = val
    writeKbPreference(agentId, sessionId.value, val)
    if (val) writeWebPreference(agentId, sessionId.value, false)
  }

  function onWebToggle(agentId, val) {
    if (useKnowledgeRetrieval.value && val) useKnowledgeRetrieval.value = false
    useWebSearch.value = val
    writeWebPreference(agentId, sessionId.value, val)
    if (val) writeKbPreference(agentId, sessionId.value, false)
  }

  return {
    sessionId,
    ignoreNextQueryWatch,
    useKnowledgeRetrieval,
    useWebSearch,
    persistSessionId,
    readStoredSessionId,
    clearStoredSessionId,
    applyKbPreferenceForCurrentSession,
    onKbToggle,
    onWebToggle,
  }
}
