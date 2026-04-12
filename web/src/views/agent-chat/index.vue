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
                            m.pending && m.ragSteps?.length
                              ? m.ragSteps[m.ragSteps.length - 1].label
                              : m.pending
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
                          <div v-if="m.ragSteps?.length" class="agent-chat-thinking-steps">
                            <div
                              v-for="(step, sIdx) in m.ragSteps"
                              :key="sIdx"
                              class="agent-chat-thinking-step-line"
                            >
                              <span class="agent-chat-thinking-step-icon">{{ step.icon || '▸' }}</span>
                              <span class="agent-chat-thinking-step-label">{{ step.label }}</span>
                              <span v-if="step.detail" class="agent-chat-thinking-step-detail">{{
                                step.detail
                              }}</span>
                            </div>
                          </div>
                          <p v-else class="agent-chat-thinking-placeholder">
                            {{ $t('views.agents.chat_feed_thinking_placeholder') }}
                          </p>
                          <details v-if="m.ragTrace && Object.keys(m.ragTrace).length" class="agent-chat-rag-trace">
                            <summary>{{ $t('views.agents.chat_thinking_trace_summary') }}</summary>
                            <div class="agent-chat-rag-trace-lines">
                              <div v-if="m.ragTrace.retrieval_mode" class="agent-chat-trace-line">
                                {{ $t('views.agents.chat_thinking_trace_mode') }}：{{
                                  m.ragTrace.retrieval_mode
                                }}
                              </div>
                              <div v-if="m.ragTrace.retrieval_stage" class="agent-chat-trace-line">
                                {{ $t('views.agents.chat_thinking_trace_stage') }}：{{
                                  m.ragTrace.retrieval_stage
                                }}
                              </div>
                              <div v-if="m.ragTrace.grade_score" class="agent-chat-trace-line">
                                {{ $t('views.agents.chat_thinking_trace_grade') }}：{{
                                  m.ragTrace.grade_score
                                }}
                              </div>
                              <div v-if="m.ragTrace.rewrite_strategy" class="agent-chat-trace-line">
                                {{ m.ragTrace.rewrite_strategy }}
                              </div>
                            </div>
                          </details>
                        </div>
                      </div>

                      <div class="agent-chat-feed-body">
                        <div
                          v-if="!m.pending"
                          class="agent-chat-md"
                          v-html="renderAgentChatMarkdown(m.content)"
                        />
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
                <div class="agent-chat-kb-toggle">
                  <n-switch
                    :value="useKnowledgeRetrieval"
                    :disabled="sending"
                    size="small"
                    @update:value="onKbToggle"
                  />
                  <span class="agent-chat-kb-toggle-label">{{ $t('views.agents.chat_kb_retrieval') }}</span>
                </div>
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
import { NAvatar, NButton, NInput, NSpin, NSwitch, NTag, NTooltip, NUpload } from 'naive-ui'
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
/** 开启时允许后端注册知识库检索工具；关闭则仅通用知识（按会话持久化，新会话默认关） */
const useKnowledgeRetrieval = ref(false)
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
let fileIdSeq = 0
const baseDocTitle = import.meta.env.VITE_TITLE || ''
const baseApi = import.meta.env.VITE_BASE_API || '/api/v1'

const SESSION_KEY_PREFIX = 'kura_ai_chat_session_'
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

/** 按 agentId + sessionId 记忆知识库开关，刷新后保持；新 session 无记录时默认关 */
const KB_TOGGLE_PREFIX = 'kura_ai_kb_'

function kbToggleStorageKey(agentId, sid) {
  return `${KB_TOGGLE_PREFIX}${agentId}_${sid}`
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

function applyKbPreferenceForCurrentSession() {
  const aid = agent.value?.id
  if (!aid) return
  useKnowledgeRetrieval.value = readKbPreference(aid, sessionId.value)
}

function onKbToggle(val) {
  useKnowledgeRetrieval.value = val
  const aid = agent.value?.id
  if (aid && sessionId.value) writeKbPreference(aid, sessionId.value, val)
}

const PENDING_JOB_PREFIX = 'kura_ai_chat_job_'

function pendingJobStorageKey(agentId, sid) {
  return `${PENDING_JOB_PREFIX}${agentId}_${sid}`
}

function savePendingChatJob(agentId, sid, payload) {
  try {
    if (agentId && sid && payload?.job_id) {
      sessionStorage.setItem(pendingJobStorageKey(agentId, sid), JSON.stringify(payload))
    }
  } catch {
    /* ignore */
  }
}

function readPendingChatJob(agentId, sid) {
  try {
    const s = sessionStorage.getItem(pendingJobStorageKey(agentId, sid))
    if (!s) return null
    return JSON.parse(s)
  } catch {
    return null
  }
}

function clearPendingChatJob(agentId, sid) {
  try {
    if (agentId && sid) sessionStorage.removeItem(pendingJobStorageKey(agentId, sid))
  } catch {
    /* ignore */
  }
}

/** 将单条 SSE JSON 事件应用到助手消息行（与流式接口字段一致） */
function applyChatSsePayload(data, idx) {
  if (idx === -1) return
  if (data.type === 'content') {
    const row = messages.value[idx]
    messages.value[idx] = {
      ...row,
      content: (row.content || '') + (data.content || ''),
      pending: false,
      thinkingOpen: row.thinkingOpen ?? false,
      ragSteps: row.ragSteps || [],
      ragTrace: row.ragTrace ?? null,
    }
  } else if (data.type === 'rag_step') {
    const cur = messages.value[idx]
    const nextSteps = [...(cur.ragSteps || []), data.step || {}]
    messages.value[idx] = {
      ...cur,
      ragSteps: nextSteps,
      thinkingOpen: true,
      pending: cur.pending,
    }
  } else if (data.type === 'trace') {
    const cur = messages.value[idx]
    messages.value[idx] = {
      ...cur,
      ragTrace: data.rag_trace || null,
      pending: cur.pending,
    }
  } else if (data.type === 'error') {
    const cur = messages.value[idx]
    messages.value[idx] = {
      ...cur,
      content: `${cur.content || ''}\n[Error: ${data.content}]`,
      pending: false,
      thinkingOpen: cur.thinkingOpen ?? false,
      ragSteps: cur.ragSteps || [],
      ragTrace: cur.ragTrace ?? null,
    }
  } else if (data.type === 'done') {
    const row = messages.value[idx]
    messages.value[idx] = { ...row, pending: false }
  }
}

async function readChatJobSseStream(reader, decoder, idx, jobId, agentId, initialSeq = 0) {
  let buffer = ''
  let seq = initialSeq

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
      if (dataStr === '[DONE]') {
        clearPendingChatJob(agentId, sessionId.value)
        continue
      }
      try {
        const data = JSON.parse(dataStr)
        applyChatSsePayload(data, idx)
        seq += 1
        savePendingChatJob(agentId, sessionId.value, { job_id: jobId, seq })
        if (data.type === 'done') {
          clearPendingChatJob(agentId, sessionId.value)
        }
      } catch (e) {
        console.warn('SSE parse error:', e)
      }
    }
    scrollBodyToBottom()
  }
  if (idx !== -1 && messages.value[idx]?.pending) {
    const row = messages.value[idx]
    messages.value[idx] = { ...row, pending: false }
  }
}

async function maybeResumePendingChatJob() {
  const agentId = agent.value?.id
  const token = getToken()
  if (!agentId || !token || sending.value || pageLoading.value || loadError.value) return

  const pj = readPendingChatJob(agentId, sessionId.value)
  if (!pj?.job_id) return

  try {
    const metaRes = await fetch(`${baseApi}/user-agent/chat/jobs/${pj.job_id}`, {
      headers: { token },
    })
    if (!metaRes.ok) {
      clearPendingChatJob(agentId, sessionId.value)
      return
    }
    const metaBody = await metaRes.json()
    const meta = metaBody.data ?? metaBody
    if (meta.status !== 'running') {
      clearPendingChatJob(agentId, sessionId.value)
      return
    }
  } catch {
    return
  }

  const last = messages.value[messages.value.length - 1]
  let idx = -1
  // 复用当前页里「仍在生成」的助手气泡时，只需从已收条数继续拉；刷新后从接口重载历史时没有未落库的助手行，会走 else 新建空气泡，必须从 Redis 下标 0 重放，否则会丢掉 pj.seq 之前的已生成内容。
  const reuseAssistantRow = last?.role === 'assistant' && last?.pending
  if (reuseAssistantRow) {
    idx = messages.value.length - 1
  } else {
    const assistantId = `a-resume-${Date.now()}`
    messages.value.push({
      id: assistantId,
      role: 'assistant',
      content: '',
      pending: true,
      thinkingOpen: true,
      ragSteps: [],
      ragTrace: null,
    })
    idx = messages.value.length - 1
    sessionPhase.value = 'chat'
  }

  sending.value = true
  const sinceSeq = reuseAssistantRow ? (pj.seq ?? 0) : 0
  try {
    const streamRes = await fetch(
      `${baseApi}/user-agent/chat/jobs/${pj.job_id}/stream?since_seq=${sinceSeq}`,
      {
        headers: { token },
      }
    )
    if (!streamRes.ok) {
      clearPendingChatJob(agentId, sessionId.value)
      return
    }
    const reader = streamRes.body?.getReader()
    const decoder = new TextDecoder()
    if (!reader) return
    await readChatJobSseStream(reader, decoder, idx, pj.job_id, agentId, sinceSeq)
    await recentAgentsStore.touch(agentId)
  } catch (e) {
    console.warn('resume job stream:', e)
  } finally {
    sending.value = false
    scrollBodyToBottom()
    agentSidebarStore.bumpRefresh()
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
    ragSteps: Array.isArray(row.rag_steps) ? row.rag_steps : [],
    ragTrace: row.rag_trace || null,
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
    applyKbPreferenceForCurrentSession()
    await maybeStartIntroOpening()
    await maybeResumePendingChatJob()
  }
}

const agentAvatarSrc = computed(() => agent.value?.avatar_url || DEFAULT_AVATAR)

/** 顶部会话标题：最新一条用户提问摘要，否则智能体名 */
const chatSessionTitle = computed(() => {
  const users = messages.value.filter((m) => m.role === 'user' && (m.content || '').trim())
  const lastUser = users.length ? users[users.length - 1] : null
  if (lastUser) {
    const line = (lastUser.content || '').trim().replace(/\s+/g, ' ')
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
  const aid = agent.value?.id
  if (aid) clearStoredSessionId(aid)
  messages.value = []
  sessionPhase.value = 'intro'
  inputText.value = ''
  pendingFiles.value = []
  uploadResetKey.value += 1
  sessionId.value = `session_${Date.now()}`
  if (aid) persistSessionId(aid, sessionId.value)
  applyKbPreferenceForCurrentSession()
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
  // 立即持久化 session_id，避免刷新后 pending job 的 session 与 sessionStorage 不一致
  persistSessionId(agentId, sessionId.value)

  const assistantId = `a-${Date.now()}`
  messages.value.push({
    id: assistantId,
    role: 'assistant',
    content: '',
    pending: true,
    thinkingOpen: false,
    ragSteps: [],
    ragTrace: null,
  })
  sending.value = true
  scrollBodyToBottom()

  const idx = messages.value.findIndex((m) => m.id === assistantId)

  try {
    const postRes = await fetch(`${baseApi}/user-agent/chat/jobs`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        token,
      },
      body: JSON.stringify({
        agent_id: agentId,
        message: text,
        session_id: sessionId.value,
        use_knowledge_retrieval: useKnowledgeRetrieval.value,
      }),
    })

    let jobId
    let startSeq = 0
    if (postRes.status === 409) {
      const errBody = await postRes.json()
      jobId = errBody.detail?.existing_job_id
      if (!jobId) {
        throw new Error(
          typeof errBody.detail === 'string' ? errBody.detail : errBody.detail?.message || '任务冲突'
        )
      }
      const pj = readPendingChatJob(agentId, sessionId.value)
      startSeq = pj?.seq ?? 0
    } else if (!postRes.ok) {
      let detail = `HTTP ${postRes.status}`
      try {
        const errBody = await postRes.json()
        detail = errBody.detail || errBody.msg || detail
      } catch {
        /* ignore */
      }
      throw new Error(detail)
    } else {
      const body = await postRes.json()
      jobId = body.data?.job_id
      if (!jobId) throw new Error('未返回 job_id')
      savePendingChatJob(agentId, sessionId.value, { job_id: jobId, seq: 0 })
    }

    await recentAgentsStore.touch(agentId)

    const streamRes = await fetch(
      `${baseApi}/user-agent/chat/jobs/${jobId}/stream?since_seq=${startSeq}`,
      {
        headers: { token },
      }
    )

    if (!streamRes.ok) {
      let detail = `HTTP ${streamRes.status}`
      try {
        const errBody = await streamRes.json()
        detail = errBody.detail || errBody.msg || detail
      } catch {
        /* ignore */
      }
      throw new Error(detail)
    }

    const reader = streamRes.body?.getReader()
    const decoder = new TextDecoder()
    if (!reader) {
      throw new Error('No response body')
    }

    await readChatJobSseStream(reader, decoder, idx, jobId, agentId, startSeq)
  } catch (error) {
    clearPendingChatJob(agentId, sessionId.value)
    if (idx !== -1) {
      const row = messages.value[idx]
      messages.value[idx] = {
        ...row,
        content: t('views.agents.chat_msg_stream_error') + `：${error?.message || error}`,
        pending: false,
        thinkingOpen: row.thinkingOpen ?? false,
        ragSteps: row.ragSteps || [],
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

watch(
  () => route.params.agentId,
  (newId, oldId) => {
    stopIntroOpening()
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

.agent-chat-thinking-placeholder {
  margin: 0 0 8px;
  font-size: 12px;
  color: #94a3b8;
}

html.dark .agent-chat-thinking-placeholder {
  color: rgba(255, 255, 255, 0.4);
}

.agent-chat-thinking-steps {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 10px;
}

.agent-chat-thinking-step-line {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 6px 10px;
  font-size: 12px;
  line-height: 1.45;
  color: #475569;
}

html.dark .agent-chat-thinking-step-line {
  color: rgba(255, 255, 255, 0.7);
}

.agent-chat-thinking-step-icon {
  flex-shrink: 0;
  font-size: 13px;
  line-height: 1;
}

.agent-chat-thinking-step-label {
  font-weight: 500;
  color: #334155;
}

html.dark .agent-chat-thinking-step-label {
  color: rgba(255, 255, 255, 0.85);
}

.agent-chat-thinking-step-detail {
  flex: 1 1 100%;
  min-width: 0;
  font-size: 11px;
  color: #64748b;
  word-break: break-word;
}

html.dark .agent-chat-thinking-step-detail {
  color: rgba(255, 255, 255, 0.45);
}

.agent-chat-rag-trace {
  margin-top: 4px;
  padding-top: 8px;
  border-top: 1px dashed #e2e8f0;
  font-size: 12px;
}

html.dark .agent-chat-rag-trace {
  border-top-color: rgba(255, 255, 255, 0.1);
}

.agent-chat-rag-trace summary {
  cursor: pointer;
  font-weight: 500;
  color: #475569;
  list-style: none;
}

.agent-chat-rag-trace summary::-webkit-details-marker {
  display: none;
}

html.dark .agent-chat-rag-trace summary {
  color: rgba(255, 255, 255, 0.65);
}

.agent-chat-rag-trace-lines {
  margin-top: 8px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.agent-chat-trace-line {
  font-size: 11px;
  line-height: 1.45;
  color: #64748b;
  word-break: break-word;
}

html.dark .agent-chat-trace-line {
  color: rgba(255, 255, 255, 0.45);
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

.agent-chat-kb-toggle {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--n-text-color-2);
}

.agent-chat-kb-toggle-label {
  user-select: none;
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
