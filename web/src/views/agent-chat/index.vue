<template>
  <AppPage :show-footer="false" scroll-in-parent class="agent-chat-page !p-0">
    <div class="agent-chat-layout">
      <n-spin :show="pageLoading" class="agent-chat-spin">
          <div v-if="loadError" class="agent-chat-error">
            {{ $t('views.agents.chat_error_load_agent') }}
          </div>
          <template v-else>
            <div ref="bodyScrollRef" class="agent-chat-body" @scroll.passive="onBodyScroll">
            <transition name="agent-chat-fade">
              <div v-if="sessionPhase === 'intro'" key="intro" class="agent-chat-intro">
                <n-avatar
                  round
                  :size="120"
                  :src="agentAvatarSrc"
                  object-fit="cover"
                  class="agent-chat-intro-avatar"
                />
                <h2 class="agent-chat-intro-name">{{ agent?.name }}</h2>
                <p class="agent-chat-intro-sub">{{ introDescription }}</p>
                <p class="agent-chat-intro-creator">
                  <TheIcon icon="mdi:account-outline" :size="16" />
                  <span>{{ $t('views.agents.chat_label_creator') }} · {{ userStore.name }}</span>
                </p>
                <div
                  v-if="hasIntroOpeningText"
                  class="agent-chat-intro-opening"
                >
                  <n-spin v-if="introOpeningRunning && !introOpeningDisplayed" size="small" />
                  <div
                    v-else-if="introOpeningDisplayed"
                    class="agent-chat-intro-opening-md"
                    v-html="renderAgentChatMarkdown(introOpeningDisplayed)"
                  />
                </div>
              </div>
            </transition>

            <div v-if="sessionPhase === 'chat'" class="agent-chat-feed">
              <div class="agent-chat-feed-list">
                <div
                  v-for="m in messages"
                  :key="m.id"
                  class="agent-chat-feed-item"
                  :class="{ 'agent-chat-feed-item--user': m.role === 'user' }"
                >
                  <template v-if="m.role === 'user'">
                    <div class="agent-chat-user-row">
                      <div class="agent-chat-user-bubble">
                        <div class="agent-chat-user-text">{{ m.content }}</div>
                      </div>
                      <div v-if="m.attachments?.length" class="agent-chat-msg-files agent-chat-msg-files--user">
                        {{ m.attachments.map((a) => a.name).join(', ') }}
                      </div>
                    </div>
                  </template>
                  <template v-else>
                    <n-avatar
                      round
                      :size="40"
                      :src="agentAvatarSrc"
                      object-fit="cover"
                      class="agent-chat-feed-avatar"
                    />
                    <div class="agent-chat-feed-col">
                      <div class="agent-chat-feed-head">
                        <span class="agent-chat-feed-name">{{ agent?.name || '—' }}</span>
                        <span class="agent-chat-feed-ai-badge">{{
                          $t('views.agents.chat_feed_ai_badge')
                        }}</span>
                      </div>

                      <div class="agent-chat-thinking-wrap">
                        <button
                          type="button"
                          class="agent-chat-thinking-pill"
                          :aria-expanded="m.thinkingOpen"
                          @click="toggleThinking(m)"
                        >
                          <TheIcon icon="mdi:lightbulb-outline" :size="16" class="agent-chat-thinking-icon" />
                          <span>{{
                            m.pending
                              ? $t('views.agents.chat_feed_thinking')
                              : $t('views.agents.chat_feed_thinking_done')
                          }}</span>
                          <TheIcon
                            :icon="m.thinkingOpen ? 'mdi:chevron-up' : 'mdi:chevron-down'"
                            :size="18"
                            class="agent-chat-thinking-chevron"
                          />
                        </button>
                        <div v-show="m.thinkingOpen" class="agent-chat-thinking-panel">
                          {{ $t('views.agents.chat_feed_thinking_placeholder') }}
                        </div>
                      </div>

                      <div class="agent-chat-feed-body">
                        <template v-if="m.pending">
                          <n-spin size="small" />
                        </template>
                        <template v-else>
                          <div class="agent-chat-md" v-html="renderAgentChatMarkdown(m.content)" />
                        </template>
                      </div>
                      <div
                        v-if="!m.pending && (m.content || '').trim()"
                        class="agent-chat-assistant-actions"
                      >
                        <n-tooltip :show-arrow="false" placement="top">
                          <template #trigger>
                            <n-button
                              quaternary
                              circle
                              size="small"
                              class="agent-chat-copy-btn"
                              :aria-label="$t('views.agents.chat_copy_plain_tooltip')"
                              @click="copyAssistantPlain(m)"
                            >
                              <TheIcon icon="lucide:copy" :size="18" />
                            </n-button>
                          </template>
                          {{ $t('views.agents.chat_copy_plain_tooltip') }}
                        </n-tooltip>
                        <n-tooltip :show-arrow="false" placement="top">
                          <template #trigger>
                            <n-button
                              quaternary
                              circle
                              size="small"
                              class="agent-chat-copy-btn"
                              :aria-label="$t('views.agents.chat_copy_md_tooltip')"
                              @click="copyAssistantMarkdown(m)"
                            >
                              <TheIcon icon="simple-icons:markdown" :size="18" />
                            </n-button>
                          </template>
                          {{ $t('views.agents.chat_copy_md_tooltip') }}
                        </n-tooltip>
                      </div>
                      <div v-if="m.attachments?.length" class="agent-chat-msg-files">
                        {{ m.attachments.map((a) => a.name).join(', ') }}
                      </div>
                    </div>
                  </template>
                </div>
              </div>
            </div>
          </div>

          <footer class="agent-chat-footer">
            <div class="agent-chat-footer-inner">
              <div class="agent-chat-toolbar">
                <n-button size="small" round quaternary @click="restartChat">
                  <template #icon>
                    <TheIcon icon="mdi:plus-circle-outline" :size="18" />
                  </template>
                  {{ $t('views.agents.chat_button_restart') }}
                </n-button>
                <span v-if="showGoBottomButton" class="agent-chat-toolbar-go">
                  <n-tooltip :show-arrow="false" placement="top-end">
                    <template #trigger>
                      <n-button
                        quaternary
                        circle
                        size="small"
                        :aria-label="$t('views.agents.chat_go_bottom')"
                        @click="() => scrollBodyToBottom(true)"
                      >
                        <TheIcon icon="mdi:chevron-double-down" :size="20" />
                      </n-button>
                    </template>
                    {{ $t('views.agents.chat_go_bottom') }}
                  </n-tooltip>
                </span>
              </div>

              <div class="agent-chat-composer">
                <n-input
                  v-model:value="inputText"
                  type="textarea"
                  :bordered="false"
                  :autosize="{ minRows: 2, maxRows: 8 }"
                  :placeholder="$t('views.agents.chat_placeholder_input')"
                  :disabled="pageLoading || !!loadError || sending"
                  class="agent-chat-input"
                  @keydown="onInputKeydown"
                />
                <div class="agent-chat-composer-bar">
                  <n-upload
                    :key="uploadResetKey"
                    :show-file-list="false"
                    :max="5"
                    multiple
                    accept="*/*"
                    :custom-request="handleUploadRequest"
                    @change="onUploadChange"
                  >
                    <n-button
                      quaternary
                      circle
                      :disabled="sending"
                      :title="$t('views.agents.chat_attach_file')"
                    >
                      <TheIcon icon="mdi:paperclip" :size="22" />
                    </n-button>
                  </n-upload>
                  <div class="agent-chat-files-preview">
                    <n-tag
                      v-for="(f, idx) in pendingFiles"
                      :key="f.id"
                      closable
                      size="small"
                      @close="removePendingFile(idx)"
                    >
                      {{ f.name }}
                    </n-tag>
                  </div>
                  <n-button
                    type="primary"
                    circle
                    class="agent-chat-send"
                    :disabled="sendDisabled"
                    :loading="sending"
                    :title="$t('views.agents.chat_button_send')"
                    @click="submitMessage"
                  >
                    <TheIcon icon="mdi:send" :size="20" />
                  </n-button>
                </div>
              </div>
            </div>
          </footer>
        </template>
      </n-spin>
    </div>
  </AppPage>
</template>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useEventListener } from '@vueuse/core'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { NAvatar, NButton, NInput, NSpin, NTag, NTooltip, NUpload } from 'naive-ui'
import AppPage from '@/components/page/AppPage.vue'
import TheIcon from '@/components/icon/TheIcon.vue'
import api from '@/api'
import { getToken } from '@/utils'
import { renderAgentChatMarkdown } from '@/utils/agentChatMarkdown'
import { useAgentChatHeaderStore, useAgentSidebarStore, useRecentAgentsStore, useUserStore } from '@/store'
import { DEFAULT_AVATAR } from '@/views/agents/composables/agentFormCommon.js'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const userStore = useUserStore()
const agentChatHeaderStore = useAgentChatHeaderStore()
const agentSidebarStore = useAgentSidebarStore()
const recentAgentsStore = useRecentAgentsStore()

const pageLoading = ref(true)
const loadError = ref(false)
const agent = ref(null)
const sessionPhase = ref('intro')
const messages = ref([])
const inputText = ref('')
const sending = ref(false)
/** 新建对话 intro 内本地流式展示的开场白（不入库） */
const introOpeningDisplayed = ref('')
const introOpeningRunning = ref(false)
let introOpeningGeneration = 0

const INTRO_OPENING_START_DELAY_MS = 450
const INTRO_OPENING_CHAR_DELAY_MS = 72

const pendingFiles = ref([])
const bodyScrollRef = ref(null)
/** 是否在滚动容器底部附近（用于隐藏「回到底部」） */
const scrollAtBottom = ref(true)

const showGoBottomButton = computed(() => {
  if (pageLoading.value || loadError.value) return false
  if (sessionPhase.value !== 'chat') return false
  if (!messages.value.length) return false
  return !scrollAtBottom.value
})

function updateScrollBottomState() {
  const el = bodyScrollRef.value
  if (!el) {
    scrollAtBottom.value = true
    return
  }
  const threshold = 8
  const gap = el.scrollHeight - el.scrollTop - el.clientHeight
  scrollAtBottom.value = gap <= threshold
}

function onBodyScroll() {
  updateScrollBottomState()
}
const uploadResetKey = ref(0)
const sessionId = ref(`session_${Date.now()}`)
let abortController = null
let fileIdSeq = 0
const baseDocTitle = import.meta.env.VITE_TITLE || ''
const baseApi = import.meta.env.VITE_BASE_API || '/api/v1'

const SESSION_KEY_PREFIX = 'mg_agent_chat_session_'
const ignoreNextQueryWatch = ref(false)

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

async function loadMessagesForSession(agentId, sid) {
  const res = await api.getAgentChatSessionMessages(agentId, sid)
  const rows = res.data?.messages || []
  const list = rows.map((row, i) => ({
    id: `hist-${i}-${row.timestamp}`,
    role: row.type === 'human' ? 'user' : 'assistant',
    content: row.content || '',
    pending: false,
    thinkingOpen: row.type === 'human' ? undefined : false,
  }))
  messages.value = list
  sessionId.value = sid
  sessionPhase.value = list.length > 0 ? 'chat' : 'intro'
  await nextTick()
  scrollBodyToBottom()
}

const hasIntroOpeningText = computed(() => {
  const s = String(agent.value?.opening_message || '').trim()
  return s.length > 0
})

function stopIntroOpening() {
  introOpeningGeneration += 1
  introOpeningDisplayed.value = ''
  introOpeningRunning.value = false
}

async function maybeStartIntroOpening() {
  if (!getToken()) return
  if (!agent.value) return
  if (pageLoading.value || loadError.value) return
  const full = String(agent.value.opening_message || '').trim()
  if (!full) return
  if (sessionPhase.value !== 'intro') return
  if (messages.value.length > 0) return

  introOpeningGeneration += 1
  const gen = introOpeningGeneration
  introOpeningDisplayed.value = ''
  introOpeningRunning.value = true

  await nextTick()
  await new Promise((r) => setTimeout(r, INTRO_OPENING_START_DELAY_MS))
  if (gen !== introOpeningGeneration) return

  for (let i = 0; i < full.length; i += 1) {
    if (gen !== introOpeningGeneration) return
    introOpeningDisplayed.value += full[i]
    await new Promise((r) => setTimeout(r, INTRO_OPENING_CHAR_DELAY_MS))
  }
  if (gen !== introOpeningGeneration) return
  introOpeningRunning.value = false
}

async function initChatSessionState() {
  if (!agent.value) return
  const agentId = agent.value.id
  const token = getToken()
  if (!token) return

  try {
    if (route.query.new === '1' || route.query.new === 1) {
      clearStoredSessionId(agentId)
      sessionId.value = `session_${Date.now()}`
      messages.value = []
      sessionPhase.value = 'intro'
      ignoreNextQueryWatch.value = true
      await router.replace({
        name: 'AgentChat',
        params: { agentId: String(agentId) },
        query: {},
      })
      return
    }

    const sid = route.query.session
    if (sid && typeof sid === 'string') {
      try {
        await loadMessagesForSession(agentId, sid)
        persistSessionId(agentId, sid)
      } catch {
        sessionId.value = `session_${Date.now()}`
        messages.value = []
        sessionPhase.value = 'intro'
      }
      return
    }

    const stored = readStoredSessionId(agentId)
    if (stored) {
      sessionId.value = stored
      try {
        await loadMessagesForSession(agentId, stored)
      } catch {
        sessionId.value = `session_${Date.now()}`
        messages.value = []
        sessionPhase.value = 'intro'
      }
      return
    }

    sessionId.value = `session_${Date.now()}`
    messages.value = []
    sessionPhase.value = 'intro'
  } finally {
    await maybeStartIntroOpening()
  }
}

const agentAvatarSrc = computed(() => agent.value?.avatar_url || DEFAULT_AVATAR)

/** 顶部会话标题：首条用户消息摘要，否则智能体名 */
const chatSessionTitle = computed(() => {
  const firstUser = messages.value.find((m) => m.role === 'user' && (m.content || '').trim())
  if (firstUser) {
    const line = (firstUser.content || '').trim().replace(/\s+/g, ' ')
    if (line.length > 48) return `${line.slice(0, 48)}…`
    return line
  }
  return agent.value?.name || ''
})

function toggleThinking(m) {
  if (m.role !== 'assistant') return
  m.thinkingOpen = !(m.thinkingOpen ?? false)
}

/** 复制为纯文本：弱化常见 Markdown 语法；行内空白压成单空格，保留换行与段落 */
function assistantPlainTextFromMarkdown(md) {
  if (!md) return ''
  let s = String(md).replace(/\r\n/g, '\n').replace(/\r/g, '\n')
  s = s.replace(/```[\s\S]*?```/g, '\n')
  s = s.replace(/`([^`]+)`/g, '$1')
  s = s.replace(/\*\*([^*]+)\*\*/g, '$1')
  s = s.replace(/\*([^*]+)\*/g, '$1')
  s = s.replace(/^#{1,6}\s+/gm, '')
  s = s.replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
  s = s
    .split('\n')
    .map((line) => line.replace(/[ \t]+/g, ' ').trimEnd())
    .join('\n')
  s = s.replace(/\n{3,}/g, '\n\n')
  return s.trim()
}

async function copyAssistantPlain(m) {
  try {
    await navigator.clipboard.writeText(assistantPlainTextFromMarkdown(m.content || ''))
    window.$message?.success(t('views.agents.chat_copy_success'))
  } catch {
    window.$message?.error(t('views.agents.chat_copy_fail'))
  }
}

async function copyAssistantMarkdown(m) {
  try {
    await navigator.clipboard.writeText(m.content || '')
    window.$message?.success(t('views.agents.chat_copy_success'))
  } catch {
    window.$message?.error(t('views.agents.chat_copy_fail'))
  }
}

const introDescription = computed(() => {
  const d = agent.value?.description?.trim()
  if (d) return d
  return t('views.agents.text_no_description')
})

const sendDisabled = computed(() => {
  const text = inputText.value?.trim() || ''
  if (text.length > 0) return false
  if (pendingFiles.value.length > 0) return false
  return true
})

function onInputKeydown(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    submitMessage()
  }
}

function handleUploadRequest({ onFinish }) {
  onFinish()
}

function onUploadChange(options) {
  const fileList = options.fileList || []
  pendingFiles.value = fileList.map((item) => {
    const raw = item.file
    const fileObj = raw instanceof File ? raw : raw?.file
    return {
      id: ++fileIdSeq,
      name: fileObj?.name || item.name || 'file',
      file: fileObj,
    }
  })
}

function removePendingFile(index) {
  pendingFiles.value.splice(index, 1)
}

function scrollBodyToBottom(smooth = false) {
  nextTick(() => {
    const el = bodyScrollRef.value
    if (!el) {
      nextTick(() => updateScrollBottomState())
      return
    }
    const top = el.scrollHeight
    if (smooth) {
      el.scrollTo({ top, behavior: 'smooth' })
      window.setTimeout(() => updateScrollBottomState(), 500)
    } else {
      el.scrollTop = top
      nextTick(() => updateScrollBottomState())
    }
  })
}

async function loadAgent() {
  const id = Number(route.params.agentId)
  if (!Number.isFinite(id)) {
    loadError.value = true
    pageLoading.value = false
    return
  }
  pageLoading.value = true
  loadError.value = false
  agent.value = null
  try {
    const res = await api.getUserAgent({ agent_id: id })
    agent.value = res.data
  } catch {
    loadError.value = true
    agent.value = null
  } finally {
    pageLoading.value = false
  }
  if (agent.value) {
    await initChatSessionState()
    agentSidebarStore.bumpRefresh()
  }
}

async function restartChat() {
  stopIntroOpening()
  if (abortController) {
    abortController.abort()
    abortController = null
  }
  const aid = agent.value?.id
  if (aid) clearStoredSessionId(aid)
  messages.value = []
  sessionPhase.value = 'intro'
  inputText.value = ''
  pendingFiles.value = []
  uploadResetKey.value += 1
  sessionId.value = `session_${Date.now()}`
  if (aid) persistSessionId(aid, sessionId.value)
  ignoreNextQueryWatch.value = true
  await router.replace({
    name: 'AgentChat',
    params: { agentId: String(route.params.agentId) },
    query: {},
  })
  agentSidebarStore.bumpRefresh()
  window.$message?.success(t('views.agents.chat_msg_restart_ok'))
  await nextTick()
  await maybeStartIntroOpening()
}

async function submitMessage() {
  if (sendDisabled.value || sending.value) return
  const token = getToken()
  if (!token) {
    window.$message?.warning(t('views.agents.chat_msg_need_login'))
    return
  }

  const rawInput = inputText.value.trim()
  let text = rawInput
  const attachments = pendingFiles.value.map((p) => ({ name: p.name }))
  if (!text && attachments.length === 0) return
  if (!text && attachments.length > 0) {
    text = attachments.map((a) => a.name).join(', ')
  }

  const agentId = Number(route.params.agentId)
  if (!Number.isFinite(agentId)) {
    window.$message?.error(t('views.agents.chat_error_load_agent'))
    return
  }

  const userMsg = {
    id: `u-${Date.now()}`,
    role: 'user',
    content: rawInput || text,
    attachments: attachments.length ? attachments : undefined,
  }

  if (sessionPhase.value === 'intro') {
    stopIntroOpening()
  }

  const wasIntro = sessionPhase.value === 'intro'
  messages.value.push(userMsg)
  if (wasIntro) {
    sessionPhase.value = 'chat'
  }
  inputText.value = ''
  pendingFiles.value = []
  uploadResetKey.value += 1
  scrollBodyToBottom()

  const assistantId = `a-${Date.now()}`
  messages.value.push({
    id: assistantId,
    role: 'assistant',
    content: '',
    pending: true,
    thinkingOpen: false,
  })
  sending.value = true
  scrollBodyToBottom()

  const idx = messages.value.findIndex((m) => m.id === assistantId)
  abortController = new AbortController()

  try {
    const response = await fetch(`${baseApi}/user-agent/chat/stream`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        token,
      },
      body: JSON.stringify({
        agent_id: agentId,
        message: text,
        session_id: sessionId.value,
      }),
      signal: abortController.signal,
    })

    if (!response.ok) {
      let detail = `HTTP ${response.status}`
      try {
        const errBody = await response.json()
        detail = errBody.detail || errBody.msg || detail
      } catch {
        /* ignore */
      }
      throw new Error(detail)
    }

    await recentAgentsStore.touch(agentId)

    const reader = response.body?.getReader()
    const decoder = new TextDecoder()
    let buffer = ''

    if (!reader) {
      throw new Error('No response body')
    }

    while (true) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })

      let eventEndIndex
      while ((eventEndIndex = buffer.indexOf('\n\n')) !== -1) {
        const eventStr = buffer.slice(0, eventEndIndex)
        buffer = buffer.slice(eventEndIndex + 2)

        if (!eventStr.startsWith('data: ')) continue
        const dataStr = eventStr.slice(6)
        if (dataStr === '[DONE]') continue
        try {
          const data = JSON.parse(dataStr)
          if (data.type === 'content' && idx !== -1) {
            const row = messages.value[idx]
            messages.value[idx] = {
              ...row,
              content: (row.content || '') + (data.content || ''),
              pending: false,
              thinkingOpen: row.thinkingOpen ?? false,
            }
          } else if (data.type === 'error' && idx !== -1) {
            const cur = messages.value[idx]
            messages.value[idx] = {
              ...cur,
              content: `${cur.content || ''}\n[Error: ${data.content}]`,
              pending: false,
              thinkingOpen: cur.thinkingOpen ?? false,
            }
          } else if (idx !== -1) {
            const cur = messages.value[idx]
            messages.value[idx] = { ...cur, pending: false, thinkingOpen: cur.thinkingOpen ?? false }
          }
        } catch (e) {
          console.warn('SSE parse error:', e)
        }
      }
      scrollBodyToBottom()
    }
  } catch (error) {
    if (error?.name === 'AbortError') {
      if (idx !== -1) {
        const row = messages.value[idx]
        const cur = row.content
        messages.value[idx] = {
          ...row,
          content: cur ? `${cur}\n\n_${t('views.agents.chat_msg_aborted')}_` : t('views.agents.chat_msg_aborted'),
          pending: false,
          thinkingOpen: row.thinkingOpen ?? false,
        }
      }
    } else if (idx !== -1) {
      const row = messages.value[idx]
      messages.value[idx] = {
        ...row,
        content: t('views.agents.chat_msg_stream_error') + `：${error?.message || error}`,
        pending: false,
        thinkingOpen: row.thinkingOpen ?? false,
      }
    }
  } finally {
    sending.value = false
    abortController = null
    if (agentId && sessionId.value) persistSessionId(agentId, sessionId.value)
    scrollBodyToBottom()
    agentSidebarStore.bumpRefresh()
  }
}

watch(
  () => route.params.agentId,
  (newId, oldId) => {
    stopIntroOpening()
    if (abortController) {
      abortController.abort()
      abortController = null
    }
    if (oldId !== undefined && newId !== oldId) {
      agentChatHeaderStore.clear()
      messages.value = []
      sessionPhase.value = 'intro'
      inputText.value = ''
      pendingFiles.value = []
      uploadResetKey.value += 1
    }
    loadAgent()
  }
)

watch(
  () => [route.query.session, route.query.new],
  async () => {
    if (ignoreNextQueryWatch.value) {
      ignoreNextQueryWatch.value = false
      return
    }
    if (!agent.value) return
    const id = Number(route.params.agentId)
    if (!Number.isFinite(id) || id !== agent.value.id) return
    await initChatSessionState()
  }
)

function syncAgentToHeader() {
  const a = agent.value
  if (!a) {
    agentChatHeaderStore.clear()
    return
  }
  const desc = (a.description && String(a.description).trim()) || ''
  agentChatHeaderStore.setAgentMeta({
    title: a.name || '',
    avatarUrl: a.avatar_url || '',
    subtitle: desc || a.name || '',
    creatorName: userStore.name || '',
    agentId: a.id,
  })
  if (a.name) {
    document.title = `${a.name} | ${baseDocTitle}`
  }
}

watch(
  () => agent.value,
  () => {
    syncAgentToHeader()
  },
  { immediate: true, deep: true }
)

watch(
  () => userStore.name,
  () => {
    if (agent.value) syncAgentToHeader()
  }
)

watch(
  chatSessionTitle,
  (title) => {
    agentChatHeaderStore.setSessionTitle(title)
  },
  { immediate: true }
)

useEventListener(window, 'resize', () => {
  nextTick(updateScrollBottomState)
})

watch(
  () => messages.value.length,
  () => nextTick(() => updateScrollBottomState())
)

watch(sessionPhase, () => nextTick(() => updateScrollBottomState()))

watch(introOpeningDisplayed, () => {
  nextTick(() => scrollBodyToBottom())
})

onMounted(async () => {
  if (getToken()) {
    try {
      await userStore.getUserInfo()
    } catch {
      /* ignore */
    }
  }
  await loadAgent()
  nextTick(() => updateScrollBottomState())
})

onUnmounted(() => {
  stopIntroOpening()
  if (abortController) {
    abortController.abort()
    abortController = null
  }
  document.title = baseDocTitle
  agentChatHeaderStore.clear()
})
</script>

<style scoped>
/* 与 layout 主区域 `dark:bg-dark`（Uno: #18181c）一致，避免暗黑下 --n-color 随主题偏橙 */
.agent-chat-page {
  height: 100%;
  min-height: 0;
  /* 对话列表与底部输入区同一列宽，避免左右不对齐 */
  --agent-chat-column-max: 880px;
}

.agent-chat-layout {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  box-sizing: border-box;
  background: #ffffff;
}

html.dark .agent-chat-layout {
  background: #18181c;
}

.agent-chat-spin {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

.agent-chat-spin :deep(.n-spin-content) {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: #ffffff;
}

html.dark .agent-chat-spin :deep(.n-spin-content) {
  background: #18181c;
}

/* 加载遮罩与主色区分，避免大面积偏橙 */
html.dark .agent-chat-spin :deep(.n-spin-body) {
  background: rgba(24, 24, 28, 0.65);
}

.agent-chat-error {
  padding: 32px;
  text-align: center;
  color: var(--n-text-color-3);
}

.agent-chat-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 24px 20px 16px;
  box-sizing: border-box;
  background: #ffffff;
  scrollbar-width: thin;
  scrollbar-color: rgba(15, 23, 42, 0.14) transparent;
}

html.dark .agent-chat-body {
  background: #18181c;
  scrollbar-color: rgba(255, 255, 255, 0.12) transparent;
}

/* WebKit：去掉滚动条上下箭头，滑块为淡灰 */
.agent-chat-body::-webkit-scrollbar {
  width: 8px;
}

.agent-chat-body::-webkit-scrollbar-button {
  display: none;
  width: 0;
  height: 0;
}

.agent-chat-body::-webkit-scrollbar-track {
  background: transparent;
}

.agent-chat-body::-webkit-scrollbar-thumb {
  background-color: rgba(15, 23, 42, 0.14);
  border-radius: 100px;
  border: 2px solid transparent;
  background-clip: padding-box;
}

.agent-chat-body::-webkit-scrollbar-thumb:hover {
  background-color: rgba(15, 23, 42, 0.22);
}

html.dark .agent-chat-body::-webkit-scrollbar-thumb {
  background-color: rgba(255, 255, 255, 0.12);
}

html.dark .agent-chat-body::-webkit-scrollbar-thumb:hover {
  background-color: rgba(255, 255, 255, 0.2);
}

.agent-chat-fade-enter-active,
.agent-chat-fade-leave-active {
  transition: opacity 0.2s ease;
}

.agent-chat-fade-enter-from,
.agent-chat-fade-leave-to {
  opacity: 0;
}

.agent-chat-intro {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  text-align: center;
  padding: 24px 16px 48px;
  min-height: min(52vh, 420px);
}

.agent-chat-intro-avatar {
  box-shadow: 0 8px 32px rgba(15, 23, 42, 0.12);
}

html.dark .agent-chat-intro-avatar {
  box-shadow: 0 8px 28px rgba(0, 0, 0, 0.45);
}

.agent-chat-intro-name {
  margin: 20px 0 0;
  font-size: 26px;
  font-weight: 700;
  letter-spacing: 0.02em;
  color: var(--n-text-color);
}

.agent-chat-intro-sub {
  margin: 8px 0 0;
  max-width: 560px;
  font-size: 15px;
  line-height: 1.5;
  color: var(--n-text-color-3);
  word-break: break-word;
}

.agent-chat-intro-creator {
  margin: 16px 0 0;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: var(--n-text-color-3);
}

/* 与 .agent-chat-intro-sub 同宽：不 stretch，避免 <p> 撑满整列 */
.agent-chat-intro-opening {
  margin-top: 20px;
  max-width: 560px;
  text-align: left;
  min-height: 24px;
}

.agent-chat-intro-opening-md {
  font-size: 15px;
  line-height: 1.65;
  color: var(--n-text-color-2);
  word-break: break-word;
}

.agent-chat-intro-opening-md :deep(p) {
  margin: 0.35em 0;
}

.agent-chat-intro-opening-md :deep(p:first-child) {
  margin-top: 0;
}

/* —— 信息流（助手左对齐；用户右对齐气泡）—— */
.agent-chat-feed {
  max-width: var(--agent-chat-column-max);
  margin: 0 auto;
  padding-bottom: 16px;
}

.agent-chat-assistant-actions {
  display: flex;
  align-items: center;
  gap: 2px;
  margin-top: 8px;
}

.agent-chat-copy-btn {
  color: var(--n-text-color-3);
}

.agent-chat-feed-list {
  display: flex;
  flex-direction: column;
  gap: 28px;
}

.agent-chat-feed-item {
  display: flex;
  flex-direction: row;
  align-items: flex-start;
  gap: 12px;
}

.agent-chat-feed-item--user {
  justify-content: flex-end;
}

.agent-chat-user-row {
  width: 100%;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 6px;
}

.agent-chat-user-bubble {
  max-width: min(85%, 520px);
  padding: 10px 14px;
  border-radius: 16px;
  background: #f3f4f6;
  box-sizing: border-box;
}

html.dark .agent-chat-user-bubble {
  background: rgba(255, 255, 255, 0.07);
}

.agent-chat-msg-files--user {
  max-width: min(85%, 520px);
  text-align: right;
}

.agent-chat-feed-avatar {
  flex-shrink: 0;
}

.agent-chat-feed-col {
  flex: 1;
  min-width: 0;
}

.agent-chat-feed-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}

.agent-chat-feed-name {
  font-size: 15px;
  font-weight: 700;
  color: #0f172a;
}

html.dark .agent-chat-feed-name {
  color: rgba(255, 255, 255, 0.92);
}

.agent-chat-feed-ai-badge {
  display: inline-flex;
  align-items: center;
  padding: 1px 8px;
  border-radius: 6px;
  font-size: 11px;
  font-weight: 600;
  line-height: 1.3;
  color: #64748b;
  background: #e2e8f0;
}

html.dark .agent-chat-feed-ai-badge {
  color: rgba(255, 255, 255, 0.75);
  background: rgba(255, 255, 255, 0.12);
}

.agent-chat-thinking-wrap {
  margin-bottom: 10px;
}

.agent-chat-thinking-pill {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  max-width: 100%;
  padding: 6px 12px;
  border: none;
  border-radius: 999px;
  font-size: 13px;
  color: #475569;
  background: #f1f5f9;
  cursor: pointer;
  transition: background 0.15s ease;
}

.agent-chat-thinking-pill:hover {
  background: #e2e8f0;
}

html.dark .agent-chat-thinking-pill {
  color: rgba(255, 255, 255, 0.75);
  background: rgba(255, 255, 255, 0.08);
}

html.dark .agent-chat-thinking-pill:hover {
  background: rgba(255, 255, 255, 0.12);
}

.agent-chat-thinking-icon {
  flex-shrink: 0;
  opacity: 0.85;
}

.agent-chat-thinking-chevron {
  flex-shrink: 0;
  opacity: 0.7;
  margin-left: auto;
}

.agent-chat-thinking-panel {
  margin-top: 8px;
  padding: 10px 12px;
  font-size: 13px;
  line-height: 1.5;
  color: #64748b;
  background: #f8fafc;
  border-radius: 8px;
  border: 1px solid #e2e8f0;
}

html.dark .agent-chat-thinking-panel {
  color: rgba(255, 255, 255, 0.55);
  background: rgba(0, 0, 0, 0.25);
  border-color: rgba(255, 255, 255, 0.08);
}

.agent-chat-feed-body {
  font-size: 16px;
  line-height: 1.6;
  word-break: break-word;
}

.agent-chat-user-text {
  color: #334155;
  white-space: pre-wrap;
}

html.dark .agent-chat-user-text {
  color: rgba(255, 255, 255, 0.9);
}

.agent-chat-msg-files {
  margin-top: 8px;
  font-size: 12px;
  color: #64748b;
}

html.dark .agent-chat-msg-files {
  color: rgba(255, 255, 255, 0.45);
}

.agent-chat-footer {
  flex-shrink: 0;
  padding: 12px 20px 20px;
  border-top: 1px solid var(--n-divider-color);
  background: #ffffff;
}

html.dark .agent-chat-footer {
  background: #18181c;
}

.agent-chat-footer-inner {
  max-width: var(--agent-chat-column-max);
  margin: 0 auto;
  width: 100%;
  box-sizing: border-box;
}

.agent-chat-toolbar {
  display: flex;
  justify-content: flex-start;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}

.agent-chat-toolbar-go {
  margin-left: auto;
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
}

.agent-chat-composer {
  border: 1px solid #e5e7eb;
  border-radius: 20px;
  padding: 12px 14px 10px;
  background: #ffffff;
  box-shadow: none;
}

html.dark .agent-chat-composer {
  border-color: rgba(255, 255, 255, 0.12);
  background: #18181c;
}

.agent-chat-input :deep(.n-input-wrapper) {
  --n-border: transparent !important;
  --n-border-hover: transparent !important;
  --n-border-focus: transparent !important;
  --n-box-shadow-focus: none !important;
  background: transparent !important;
}

.agent-chat-input :deep(.n-input__state-border) {
  border: none !important;
  box-shadow: none !important;
}

.agent-chat-input :deep(textarea) {
  --n-border: none !important;
  --n-box-shadow: none !important;
  background: transparent !important;
  padding: 4px 2px !important;
  font-size: 15px;
  color: #000000 !important;
  caret-color: #000000;
}

html.dark .agent-chat-input :deep(textarea) {
  color: rgba(255, 255, 255, 0.9) !important;
  caret-color: rgba(255, 255, 255, 0.9);
}

.agent-chat-input :deep(.n-input__placeholder) {
  color: #9ca3af !important;
}

html.dark .agent-chat-input :deep(.n-input__placeholder) {
  color: var(--n-text-color-3) !important;
}

.agent-chat-composer-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 4px;
}

.agent-chat-files-preview {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}

.agent-chat-send {
  flex-shrink: 0;
  margin-left: auto;
}

/* Markdown + KaTeX（浅色：近黑字；暗黑：浅灰字） */
.agent-chat-md {
  color: #0f172a;
  font-size: 16px;
  line-height: 1.55;
  /* 宽表格横向滚动；与 pre 内滚动互不冲突 */
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
}

html.dark .agent-chat-md {
  color: rgba(255, 255, 255, 0.88);
}

.agent-chat-md :deep(p) {
  margin: 0 0 0.5em;
}

.agent-chat-md :deep(p:last-child) {
  margin-bottom: 0;
}

.agent-chat-md :deep(a) {
  color: #2563eb;
}

html.dark .agent-chat-md :deep(a) {
  color: #93c5fd;
}

.agent-chat-md :deep(pre) {
  margin: 0.5em 0;
  padding: 12px;
  overflow-x: auto;
  border-radius: 8px;
  background: rgba(0, 0, 0, 0.06);
  font-size: 13px;
}

html.dark .agent-chat-md :deep(pre) {
  background: rgba(0, 0, 0, 0.35);
}

.agent-chat-md :deep(code) {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New',
    monospace;
}

.agent-chat-md :deep(pre code) {
  background: transparent;
  padding: 0;
}

.agent-chat-md :deep(ul),
.agent-chat-md :deep(ol) {
  margin: 0.35em 0;
  padding-left: 1.5em;
}

/* 覆盖全局 reset 的 list-style: none，恢复列表圆点/序号 */
.agent-chat-md :deep(ul) {
  list-style-type: disc;
}

.agent-chat-md :deep(ol) {
  list-style-type: decimal;
}

.agent-chat-md :deep(ul ul) {
  list-style-type: circle;
}

.agent-chat-md :deep(ul ul ul) {
  list-style-type: square;
}

.agent-chat-md :deep(blockquote) {
  margin: 0.5em 0;
  padding-left: 12px;
  border-left: 3px solid rgba(0, 0, 0, 0.15);
  color: inherit;
}

html.dark .agent-chat-md :deep(blockquote) {
  border-left-color: rgba(255, 255, 255, 0.2);
}

/* GFM 表格：显式恢复 table 布局（避免预检/全局把 tr/td 设成 block 导致“有标签无表格形态”） */
.agent-chat-md :deep(table) {
  display: table;
  width: max-content;
  min-width: 100%;
  border-collapse: collapse;
  border-spacing: 0;
  margin: 0.65em 0;
  font-size: 0.95em;
  line-height: 1.45;
  table-layout: auto;
}

.agent-chat-md :deep(thead) {
  display: table-header-group;
}

.agent-chat-md :deep(tbody) {
  display: table-row-group;
}

.agent-chat-md :deep(tfoot) {
  display: table-footer-group;
}

.agent-chat-md :deep(tr) {
  display: table-row;
}

.agent-chat-md :deep(th),
.agent-chat-md :deep(td) {
  display: table-cell;
  border: 1px solid #e2e8f0;
  padding: 6px 10px;
  vertical-align: top;
  text-align: left;
}

.agent-chat-md :deep(th) {
  font-weight: 600;
  background: #f8fafc;
  color: inherit;
}

html.dark .agent-chat-md :deep(th),
html.dark .agent-chat-md :deep(td) {
  border-color: rgba(255, 255, 255, 0.14);
}

html.dark .agent-chat-md :deep(th) {
  background: rgba(255, 255, 255, 0.06);
}

.agent-chat-md :deep(.katex) {
  font-size: 1em;
  color: inherit;
}
</style>
