<template>
  <AppPage :show-footer="false" scroll-in-parent class="agent-chat-page !p-0">
    <div class="agent-chat-layout">
      <n-spin :show="pageLoading" class="agent-chat-spin">
          <div v-if="loadError" class="agent-chat-error">
            {{ $t('views.agents.chat_error_load_agent') }}
          </div>
          <template v-else>
            <div class="agent-chat-main">
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
                  <span>{{ $t('views.agents.chat_label_creator') }} · {{ creatorName }}</span>
                </p>
                <div
                  v-if="hasIntroOpeningText"
                  class="agent-chat-intro-opening"
                >
                  <n-spin v-if="introOpeningRunning && !introOpeningDisplayed" size="small" />
                  <div
                    v-else-if="introOpeningDisplayed"
                    class="agent-chat-intro-opening-md agent-chat-md"
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
                      <div
                        v-if="m.attachments?.length"
                        class="agent-chat-attachment-boxes agent-chat-attachment-boxes--user"
                      >
                        <ChatAttachmentItem
                          v-for="(a, ai) in m.attachments"
                          :key="`ua-${m.id}-${ai}`"
                          :agent-id="chatAgentId"
                          :session-id="sessionId"
                          :attachment="a"
                        />
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
                          <div
                            v-if="(m.thinkingText || '').trim()"
                            class="agent-chat-thinking-text"
                          >
                            <div class="agent-chat-thinking-text-label">
                              {{ $t('views.agents.chat_thinking_text_label') }}
                            </div>
                            <div
                              class="agent-chat-thinking-text-body agent-chat-md"
                              v-html="renderAgentChatMarkdown(m.thinkingText)"
                            ></div>
                          </div>
                          <p
                            v-if="!m.ragSteps?.length && !(m.thinkingText || '').trim()"
                            class="agent-chat-thinking-placeholder"
                          >
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
                          v-if="!m.pending && (m.errorText || '').trim()"
                          class="agent-chat-msg-error"
                          role="alert"
                        >
                          <div class="agent-chat-msg-error-label">
                            {{ $t('views.agents.chat_feed_error_title') }}
                          </div>
                          <div class="agent-chat-msg-error-text">{{ m.errorText }}</div>
                        </div>
                        <div
                          v-if="!m.pending && m.stoppedByUser && !(m.errorText || '').trim()"
                          class="agent-chat-msg-stopped"
                          role="status"
                        >
                          {{ $t('views.agents.chat_msg_aborted') }}
                        </div>
                        <div v-if="m.mcpConfirmations?.length" class="agent-chat-mcp-confirm">
                          <div
                            v-for="item in m.mcpConfirmations"
                            :key="item.pending_id"
                            class="agent-chat-mcp-confirm-item"
                          >
                            <div class="agent-chat-mcp-confirm-title">高危 MCP 工具调用确认</div>
                            <div class="agent-chat-mcp-confirm-text">
                              {{ item.server_name }} / {{ item.tool_name }}
                            </div>
                            <pre class="agent-chat-mcp-confirm-args">{{ item.args_preview }}</pre>
                            <div class="agent-chat-mcp-confirm-actions">
                              <n-button
                                size="tiny"
                                type="primary"
                                :disabled="sending || confirmingMcpIds.has(item.pending_id)"
                                @click="approveMcpConfirmation(m, item, true)"
                              >
                                允许一次
                              </n-button>
                              <n-button
                                size="tiny"
                                quaternary
                                :disabled="sending || confirmingMcpIds.has(item.pending_id)"
                                @click="approveMcpConfirmation(m, item, false)"
                              >
                                拒绝
                              </n-button>
                            </div>
                          </div>
                        </div>
                        <div
                          v-if="!m.pending && (m.content || '').trim()"
                          class="agent-chat-md"
                          @click="onAgentMdClick"
                          v-html="renderAgentChatMarkdown(m.content, m.sources)"
                        />
                        <div v-if="!m.pending && m.sources?.length" class="agent-chat-sources">
                          <span class="agent-chat-sources-label">{{
                            $t('views.agents.chat_sources_label')
                          }}</span>
                          <template v-for="src in m.sources" :key="`src-${src.chunk_id || src.index}`">
                            <a
                              v-if="src.url"
                              :href="src.url"
                              target="_blank"
                              rel="noopener noreferrer"
                              class="agent-chat-source-chip"
                              :data-kcite-chip="src.index"
                            >
                              [{{ src.index }}] {{ src.title || src.url }}
                            </a>
                            <a
                              v-else-if="src.image_url"
                              :href="toSameOriginMediaUrl(src.image_url)"
                              target="_blank"
                              rel="noopener noreferrer"
                              class="agent-chat-source-chip"
                              :data-kcite-chip="src.index"
                            >
                              [{{ src.index }}] {{ src.filename
                              }}<template
                                v-if="src.page_number && src.page_number !== 'N/A'"
                              >
                                · P{{ src.page_number }}</template
                              >
                            </a>
                            <span
                              v-else
                              class="agent-chat-source-chip"
                              :data-kcite-chip="src.index"
                            >
                              [{{ src.index }}] {{ src.filename
                              }}<template
                                v-if="src.page_number && src.page_number !== 'N/A'"
                              >
                                · P{{ src.page_number }}</template
                              >
                            </span>
                          </template>
                        </div>
                      </div>
                      <div
                        v-if="
                          !m.pending &&
                          ((m.content || '').trim() ||
                            (m.errorText || '').trim() ||
                            m.stoppedByUser)
                        "
                        class="agent-chat-assistant-actions"
                      >
                        <n-tooltip :show-arrow="false" placement="top">
                          <template #trigger>
                            <n-button
                              quaternary
                              circle
                              size="small"
                              class="agent-chat-copy-btn"
                              :disabled="sending"
                              :aria-label="$t('views.agents.chat_regenerate_tooltip')"
                              @click="regenerateAssistant(m)"
                            >
                              <TheIcon icon="mdi:refresh" :size="18" />
                            </n-button>
                          </template>
                          {{ $t('views.agents.chat_regenerate_tooltip') }}
                        </n-tooltip>
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
                      <div v-if="m.attachments?.length" class="agent-chat-attachment-boxes">
                        <ChatAttachmentItem
                          v-for="(a, ai) in m.attachments"
                          :key="`aa-${m.id}-${ai}`"
                          :agent-id="chatAgentId"
                          :session-id="sessionId"
                          :attachment="a"
                        />
                      </div>
                    </div>
                  </template>
                </div>
              </div>
            </div>
          </div>
          <div class="agent-chat-toolbar">
            <div class="agent-chat-toolbar-fade"></div>
            <div class="agent-chat-toolbar-inner">
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
                      @click="() => scrollBodyToBottom(true, { force: true })"
                    >
                      <TheIcon icon="mdi:chevron-double-down" :size="20" />
                    </n-button>
                  </template>
                  {{ $t('views.agents.chat_go_bottom') }}
                </n-tooltip>
              </span>
            </div>
          </div>
          </div>

          <footer class="agent-chat-footer">
            <div class="agent-chat-footer-inner">
              <div class="agent-chat-composer">
                <div v-if="pendingFiles.length" class="agent-chat-pending-attachments">
                  <div
                    v-for="(f, idx) in pendingFiles"
                    :key="f.id"
                    class="agent-chat-file-box agent-chat-file-box--pending"
                    :title="f.name"
                  >
                    <TheIcon :icon="chatAttachmentIcon(f)" :size="18" class="agent-chat-file-box-icon" />
                    <span class="agent-chat-file-box-name">{{ f.name }}</span>
                    <button
                      type="button"
                      class="agent-chat-file-box-remove"
                      :aria-label="$t('views.agents.chat_remove_attachment')"
                      :disabled="sending"
                      @click="removePendingFile(idx)"
                    >
                      <TheIcon icon="mdi:close" :size="16" />
                    </button>
                  </div>
                </div>
                <n-input
                  v-model:value="inputText"
                  type="textarea"
                  :bordered="false"
                  :autosize="{ minRows: 1, maxRows: 8 }"
                  :placeholder="$t('views.agents.chat_placeholder_input')"
                  :disabled="pageLoading || !!loadError || sending"
                  class="agent-chat-input"
                  @keydown="onInputKeydown"
                />
                <div class="agent-chat-composer-bar">
                  <n-upload
                    :key="uploadResetKey"
                    :show-file-list="false"
                    :max="MAX_CHAT_ATTACHMENTS"
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
                  <div class="agent-chat-kb-toggle">
                    <n-switch
                      :value="useKnowledgeRetrieval"
                      :disabled="sending"
                      size="small"
                      @update:value="onKbToggle"
                    />
                    <span class="agent-chat-kb-toggle-label">{{ $t('views.agents.chat_kb_retrieval') }}</span>
                  </div>
                  <div class="agent-chat-kb-toggle">
                    <n-switch
                      :value="useWebSearch"
                      :disabled="sending"
                      size="small"
                      @update:value="onWebToggle"
                    />
                    <span class="agent-chat-kb-toggle-label">{{ $t('views.agents.chat_web_search') }}</span>
                  </div>
                  <n-button
                    :type="sending ? 'warning' : 'primary'"
                    circle
                    class="agent-chat-send"
                    :disabled="!sending && sendDisabled"
                    :title="sending ? $t('views.agents.chat_button_stop') : $t('views.agents.chat_button_send')"
                    @click="sending ? stopActiveChatGeneration() : submitMessage()"
                  >
                    <TheIcon :icon="sending ? 'mdi:stop' : 'mdi:arrow-up'" :size="20" />
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
import { NAvatar, NButton, NInput, NSpin, NSwitch, NTooltip, NUpload } from 'naive-ui'
import AppPage from '@/components/page/AppPage.vue'
import TheIcon from '@/components/icon/TheIcon.vue'
import ChatAttachmentItem from './ChatAttachmentItem.vue'
import { useChatStickToBottom } from './useChatStickToBottom.js'
import api from '@/api'
import { getToken } from '@/utils'
import { renderAgentChatMarkdown, toSameOriginMediaUrl } from '@/utils/agentChatMarkdown'
import { useAgentChatHeaderStore, useAgentSidebarStore, useRecentAgentsStore, useUserStore } from '@/store'
import { DEFAULT_AVATAR } from '@/views/agents/composables/agentFormCommon.js'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const userStore = useUserStore()
const chatAgentId = computed(() => Number(route.params.agentId))
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
const confirmingMcpIds = ref(new Set())
/** 流式请求 AbortController，用于停止生成 */
const streamAbortController = ref(null)
const activeJobId = ref(null)
const activeAssistantIdx = ref(-1)
/** 用户主动停止时置 true，避免 catch 里按网络错误处理 */
const streamStoppedByUser = ref(false)
/** 开启时允许后端注册知识库检索工具；关闭则仅通用知识（按会话持久化，新会话默认关） */
const useKnowledgeRetrieval = ref(false)
/** 开启时允许后端注册联网搜索工具（与知识库检索互斥；按会话持久化，新会话默认关） */
const useWebSearch = ref(false)
/** 新建对话 intro 内本地流式展示的开场白（不入库） */
const introOpeningDisplayed = ref('')
const introOpeningRunning = ref(false)
let introOpeningGeneration = 0

const INTRO_OPENING_START_DELAY_MS = 450
const INTRO_OPENING_CHAR_DELAY_MS = 72

const pendingFiles = ref([])
const bodyScrollRef = ref(null)
const { scrollAtBottom, onBodyScroll, scrollBodyToBottom, updateScrollBottomState } =
  useChatStickToBottom(bodyScrollRef)

const showGoBottomButton = computed(() => {
  if (pageLoading.value || loadError.value) return false
  if (sessionPhase.value !== 'chat') return false
  if (!messages.value.length) return false
  return !scrollAtBottom.value
})
const uploadResetKey = ref(0)
const sessionId = ref(`session_${Date.now()}`)
let fileIdSeq = 0
/** 与后端 CHAT_UPLOAD_MAX_FILES_PER_MESSAGE 对齐 */
const MAX_CHAT_ATTACHMENTS = 5

/** 气泡内仅展示文本块；附件用下方卡片展示（与历史 content_json 一致） */
function userContentFromHistoryRow(row) {
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

/** 从历史消息的 content_json 解析附件列表（含刷新/重开会话） */
function attachmentsFromHistoryRow(row) {
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
        name: (b.filename && String(b.filename).trim()) || t('views.agents.chat_attachment_image_fallback'),
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

function chatAttachmentIcon(att) {
  const kind = String(att.kind || '').toLowerCase()
  const mime = String(att.mime || '').toLowerCase()
  const name = String(att.name || '').toLowerCase()
  if (kind === 'image' || mime.startsWith('image/')) return 'mdi:file-image-outline'
  if (kind === 'table' || mime.includes('spreadsheet') || /\.(csv|xlsx?|xls)$/i.test(name))
    return 'mdi:table-large'
  if (kind === 'document' || mime === 'application/pdf' || /\.pdf$/i.test(name)) return 'mdi:file-pdf-box'
  return 'mdi:file-document-outline'
}
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
/** 联网搜索开关持久化前缀（与知识库开关同一套模式） */
const WEB_TOGGLE_PREFIX = 'kura_ai_web_'

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

function applyKbPreferenceForCurrentSession() {
  const aid = agent.value?.id
  if (!aid) return
  useKnowledgeRetrieval.value = readKbPreference(aid, sessionId.value)
  useWebSearch.value = readWebPreference(aid, sessionId.value)
}

function onKbToggle(val) {
  useKnowledgeRetrieval.value = val
  const aid = agent.value?.id
  if (aid && sessionId.value) writeKbPreference(aid, sessionId.value, val)
  // 知识库检索与联网搜索互斥：开启知识库时强制关闭联网
  if (val && useWebSearch.value) {
    useWebSearch.value = false
    if (aid && sessionId.value) writeWebPreference(aid, sessionId.value, false)
  }
}

function onWebToggle(val) {
  useWebSearch.value = val
  const aid = agent.value?.id
  if (aid && sessionId.value) writeWebPreference(aid, sessionId.value, val)
  // 联网搜索与知识库检索互斥：开启联网时强制关闭知识库
  if (val && useKnowledgeRetrieval.value) {
    useKnowledgeRetrieval.value = false
    if (aid && sessionId.value) writeKbPreference(aid, sessionId.value, false)
  }
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
    const resumed = !!row.mcpExecuting
    messages.value[idx] = {
      ...row,
      content: resumed ? (data.content || '') : (row.content || '') + (data.content || ''),
      mcpExecuting: resumed ? false : row.mcpExecuting,
      pending: false,
      thinkingOpen: row.thinkingOpen ?? false,
      ragSteps: row.ragSteps || [],
      ragTrace: row.ragTrace ?? null,
    }
  } else if (data.type === 'thinking_move') {
    const cur = messages.value[idx]
    const text = data.text || ''
    const curContent = cur.content || ''
    messages.value[idx] = {
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
    const cur = messages.value[idx]
    messages.value[idx] = {
      ...cur,
      thinkingText: (cur.thinkingText || '') + (data.content || ''),
      thinkingOpen: true,
      pending: cur.pending,
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
  } else if (data.type === 'sources') {
    const cur = messages.value[idx]
    messages.value[idx] = {
      ...cur,
      sources: Array.isArray(data.sources) ? data.sources : [],
      pending: cur.pending,
    }
  } else if (data.type === 'error') {
    const cur = messages.value[idx]
    messages.value[idx] = {
      ...cur,
      errorText: data.content || '',
      pending: false,
      mcpExecuting: false,
      thinkingOpen: cur.thinkingOpen ?? false,
      ragSteps: cur.ragSteps || [],
      ragTrace: cur.ragTrace ?? null,
    }
  } else if (data.type === 'cancelled') {
    const cur = messages.value[idx]
    messages.value[idx] = {
      ...cur,
      stoppedByUser: true,
      pending: false,
      mcpExecuting: false,
      thinkingOpen: cur.thinkingOpen ?? false,
      ragSteps: cur.ragSteps || [],
      ragTrace: cur.ragTrace ?? null,
      errorText: undefined,
    }
  } else if (data.type === 'mcp_confirmation_required') {
    const cur = messages.value[idx]
    const item = data.confirmation || {}
    const list = Array.isArray(cur.mcpConfirmations) ? cur.mcpConfirmations : []
    if (item.pending_id && !list.some((x) => x.pending_id === item.pending_id)) {
      list.push(item)
    }
    messages.value[idx] = {
      ...cur,
      mcpConfirmations: list,
      pending: cur.pending,
    }
  } else if (data.type === 'done') {
    const row = messages.value[idx]
    messages.value[idx] = {
      ...row,
      pending: false,
      mcpExecuting: false,
      stoppedByUser: data.cancelled ? true : row.stoppedByUser,
    }
  }
}

async function readChatJobSseStream(reader, decoder, idx, jobId, agentId, initialSeq = 0) {
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

async function stopActiveChatGeneration() {
  if (!sending.value) return
  streamStoppedByUser.value = true
  const jid = activeJobId.value
  const ix = activeAssistantIdx.value
  const token = getToken()
  const aid = agent.value?.id
  if (jid && token) {
    try {
      await fetch(`${baseApi}/user-agent/chat/jobs/${jid}/cancel`, {
        method: 'POST',
        headers: { token, 'Content-Type': 'application/json' },
      })
    } catch {
      /* ignore */
    }
  }
  streamAbortController.value?.abort()
  streamAbortController.value = null
  activeJobId.value = null
  activeAssistantIdx.value = -1
  if (aid && sessionId.value) clearPendingChatJob(aid, sessionId.value)
  if (ix >= 0 && messages.value[ix]?.role === 'assistant') {
    const row = messages.value[ix]
    messages.value[ix] = {
      ...row,
      pending: false,
      stoppedByUser: true,
      errorText: undefined,
    }
  }
  sending.value = false
}

async function postChatJobAndConsumeStream({
  agentId,
  token,
  regenerate,
  message,
  attachmentIds,
  assistantIdx,
  mcpApprovedPendingId = null,
}) {
  let jobId
  let startSeq = 0
  streamStoppedByUser.value = false
  const ac = new AbortController()
  streamAbortController.value = ac
  activeAssistantIdx.value = assistantIdx
  const idx = assistantIdx
  try {
    const postRes = await fetch(`${baseApi}/user-agent/chat/jobs`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        token,
      },
      body: JSON.stringify({
        agent_id: agentId,
        message,
        session_id: sessionId.value,
        use_knowledge_retrieval: useKnowledgeRetrieval.value,
        use_web_search: useWebSearch.value,
        attachment_ids: attachmentIds,
        regenerate,
        mcp_approved_pending_id: mcpApprovedPendingId || undefined,
      }),
      signal: ac.signal,
    })

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

    activeJobId.value = jobId

    const streamRes = await fetch(
      `${baseApi}/user-agent/chat/jobs/${jobId}/stream?since_seq=${startSeq}`,
      {
        headers: { token },
        signal: ac.signal,
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
    await recentAgentsStore.touch(agentId)
  } finally {
    streamAbortController.value = null
    activeJobId.value = null
    activeAssistantIdx.value = -1
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
      errorText: undefined,
      pending: true,
      thinkingOpen: true,
      ragSteps: [],
      ragTrace: null,
      sources: [],
      thinkingText: '',
    })
    idx = messages.value.length - 1
    sessionPhase.value = 'chat'
  }

  sending.value = true
  const sinceSeq = reuseAssistantRow ? (pj.seq ?? 0) : 0
  streamStoppedByUser.value = false
  const ac = new AbortController()
  streamAbortController.value = ac
  activeJobId.value = pj.job_id
  activeAssistantIdx.value = idx
  try {
    const streamRes = await fetch(
      `${baseApi}/user-agent/chat/jobs/${pj.job_id}/stream?since_seq=${sinceSeq}`,
      {
        headers: { token },
        signal: ac.signal,
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
    streamAbortController.value = null
    activeJobId.value = null
    activeAssistantIdx.value = -1
    sending.value = false
    scrollBodyToBottom(false, { force: true })
    agentSidebarStore.bumpRefresh()
  }
}

async function loadMessagesForSession(agentId, sid) {
  const res = await api.getAgentChatSessionMessages(agentId, sid)
  const rows = res.data?.messages || []
  const list = rows.map((row, i) => {
    const role = row.type === 'human' ? 'user' : 'assistant'
    const base = {
      id: `hist-${i}-${row.timestamp}`,
      role,
      content: userContentFromHistoryRow(row),
      pending: false,
      thinkingOpen: row.type === 'human' ? undefined : false,
      ragSteps: Array.isArray(row.rag_steps) ? row.rag_steps : [],
      ragTrace: row.rag_trace || null,
      errorText: row.error_text || undefined,
      sources: Array.isArray(row.sources) ? row.sources : [],
      thinkingText: row.thinking_text || '',
    }
    if (role === 'user') {
      const att = attachmentsFromHistoryRow(row)
      if (att) base.attachments = att
    }
    return base
  })
  messages.value = list
  sessionId.value = sid
  sessionPhase.value = list.length > 0 ? 'chat' : 'intro'
  await nextTick()
  scrollBodyToBottom(false, { force: true })
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

/** 智能体发布者名（非属主使用时展示发布者，自身智能体回退为当前用户名） */
const creatorName = computed(() => {
  const owner = agent.value?.owner_username && String(agent.value.owner_username).trim()
  return owner || userStore.name || ''
})

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

function assistantMarkdownForCopy(m) {
  const parts = []
  if ((m.content || '').trim()) parts.push(m.content)
  if ((m.errorText || '').trim()) {
    parts.push(`[${t('views.agents.chat_feed_error_title')}] ${m.errorText}`)
  }
  if (m.stoppedByUser && !(m.errorText || '').trim()) {
    parts.push(t('views.agents.chat_msg_aborted'))
  }
  return parts.join('\n\n')
}

function assistantPlainForCopy(m) {
  const parts = []
  if ((m.content || '').trim()) parts.push(assistantPlainTextFromMarkdown(m.content))
  if ((m.errorText || '').trim()) {
    parts.push(`[${t('views.agents.chat_feed_error_title')}] ${m.errorText}`)
  }
  if (m.stoppedByUser && !(m.errorText || '').trim()) {
    parts.push(t('views.agents.chat_msg_aborted'))
  }
  return parts.join('\n\n')
}

async function copyAssistantPlain(m) {
  try {
    await navigator.clipboard.writeText(assistantPlainForCopy(m))
    window.$message?.success(t('views.agents.chat_copy_success'))
  } catch {
    window.$message?.error(t('views.agents.chat_copy_fail'))
  }
}

// v-html 内的引用胶囊无法直接绑 Vue 事件，用事件委托统一处理：
// 点击 [来源N] 胶囊 → 平滑滚动到本消息底部对应来源 chip 并闪烁提示。
function onAgentMdClick(e) {
  const badge = e.target.closest?.('.kcite')
  if (!badge) return
  const n = badge.getAttribute('data-kcite')
  if (!n) return
  const chip = e.currentTarget.parentElement?.querySelector(`[data-kcite-chip="${n}"]`)
  if (!chip) return
  chip.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
  chip.classList.remove('kcite-flash')
  void chip.offsetWidth
  chip.classList.add('kcite-flash')
  window.setTimeout(() => chip.classList.remove('kcite-flash'), 1400)
}

async function copyAssistantMarkdown(m) {
  try {
    await navigator.clipboard.writeText(assistantMarkdownForCopy(m))
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
    if (sending.value) return
    submitMessage()
  }
}

function handleUploadRequest({ onFinish }) {
  onFinish()
}

function onUploadChange(options) {
  const rawList = options.fileList || []
  if (rawList.length > MAX_CHAT_ATTACHMENTS) {
    window.$message?.warning(t('views.agents.chat_attachments_limit', { n: MAX_CHAT_ATTACHMENTS }))
  }
  const fileList = rawList.slice(0, MAX_CHAT_ATTACHMENTS)
  pendingFiles.value = fileList.map((item) => {
    const raw = item.file
    const fileObj = raw instanceof File ? raw : raw?.file
    return {
      id: ++fileIdSeq,
      name: fileObj?.name || item.name || 'file',
      file: fileObj,
      kind: '',
      mime: (fileObj && fileObj.type) || '',
    }
  })
}

function removePendingFile(index) {
  pendingFiles.value.splice(index, 1)
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
  const pendingSnapshot = [...pendingFiles.value]
  if (!rawInput && pendingSnapshot.length === 0) return

  const agentId = Number(route.params.agentId)
  if (!Number.isFinite(agentId)) {
    window.$message?.error(t('views.agents.chat_error_load_agent'))
    return
  }

  sending.value = true
  const attachmentIds = []
  const attachmentsForDisplay = []
  try {
    for (const p of pendingSnapshot) {
      if (!p.file) continue
      const res = await api.uploadChatAttachment(agentId, sessionId.value, p.file)
      const aid = res.data?.id
      if (!aid) continue
      attachmentIds.push(aid)
      const d = res.data || {}
      attachmentsForDisplay.push({
        name: d.filename || p.name,
        kind: d.kind || p.kind || '',
        mime: d.mime || p.mime || '',
        attachmentId: aid,
      })
    }
  } catch {
    sending.value = false
    return
  }

  if (!rawInput && attachmentIds.length === 0) {
    sending.value = false
    return
  }
  const userMsg = {
    id: `u-${Date.now()}`,
    role: 'user',
    content:
      rawInput ||
      (attachmentIds.length ? t('views.agents.chat_msg_attachment_only') : ''),
    attachments: attachmentsForDisplay.length ? attachmentsForDisplay : undefined,
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
  scrollBodyToBottom(false, { force: true })
  // 立即持久化 session_id，避免刷新后 pending job 的 session 与 sessionStorage 不一致
  persistSessionId(agentId, sessionId.value)

  const assistantId = `a-${Date.now()}`
  messages.value.push({
    id: assistantId,
    role: 'assistant',
    content: '',
    errorText: undefined,
    pending: true,
    thinkingOpen: false,
    ragSteps: [],
    ragTrace: null,
    sources: [],
    thinkingText: '',
  })
  scrollBodyToBottom(false, { force: true })

  const idx = messages.value.findIndex((m) => m.id === assistantId)

  try {
    await postChatJobAndConsumeStream({
      agentId,
      token,
      regenerate: false,
      message: rawInput,
      attachmentIds,
      assistantIdx: idx,
    })
  } catch (error) {
    clearPendingChatJob(agentId, sessionId.value)
    if (streamStoppedByUser.value) {
      if (idx !== -1 && messages.value[idx]?.pending) {
        const row = messages.value[idx]
        messages.value[idx] = {
          ...row,
          pending: false,
          stoppedByUser: true,
          errorText: undefined,
          thinkingOpen: row.thinkingOpen ?? false,
          ragSteps: row.ragSteps || [],
          ragTrace: row.ragTrace ?? null,
        }
      }
    } else if (idx !== -1) {
      const row = messages.value[idx]
      messages.value[idx] = {
        ...row,
        errorText: t('views.agents.chat_msg_stream_error') + `：${error?.message || error}`,
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

async function regenerateAssistant(assistantMsg) {
  if (sending.value) return
  const token = getToken()
  if (!token) {
    window.$message?.warning(t('views.agents.chat_msg_need_login'))
    return
  }
  const assistantIdx = messages.value.findIndex((x) => x.id === assistantMsg.id)
  if (assistantIdx <= 0) return
  const prev = messages.value[assistantIdx - 1]
  if (prev.role !== 'user') return

  const agentId = Number(route.params.agentId)
  if (!Number.isFinite(agentId)) {
    window.$message?.error(t('views.agents.chat_error_load_agent'))
    return
  }

  messages.value[assistantIdx] = {
    ...assistantMsg,
    content: '',
    errorText: undefined,
    stoppedByUser: false,
    pending: true,
    thinkingOpen: false,
    ragSteps: [],
    ragTrace: null,
    sources: [],
    thinkingText: '',
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
    })
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
          ragSteps: row.ragSteps || [],
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

async function resumeApprovedMcp(assistantMsg, pendingId) {
  const token = getToken()
  const assistantIdx = messages.value.findIndex((x) => x.id === assistantMsg.id)
  const agentId = Number(route.params.agentId)
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
    creatorName: (a.owner_username && String(a.owner_username).trim()) || userStore.name || '',
    agentId: a.id,
    isOwner: Number(a.user_id) === Number(userStore.userId),
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
  padding: 24px 20px 76px;
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
  color: var(--primary-color, #f4511e);
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
  line-height: 1.78;
}

.agent-chat-intro-opening-md :deep(p:first-child) {
  margin-top: 0;
}

.agent-chat-intro-opening-md :deep(h1),
.agent-chat-intro-opening-md :deep(h2),
.agent-chat-intro-opening-md :deep(h3),
.agent-chat-intro-opening-md :deep(h4),
.agent-chat-intro-opening-md :deep(h5),
.agent-chat-intro-opening-md :deep(h6) {
  font-weight: 600;
  line-height: 1.35;
  margin: 0.75em 0 0.4em;
  color: inherit;
}

.agent-chat-intro-opening-md :deep(h1) {
  font-size: 1.28em;
}

.agent-chat-intro-opening-md :deep(h2) {
  font-size: 1.18em;
}

.agent-chat-intro-opening-md :deep(h3) {
  font-size: 1.1em;
}

.agent-chat-intro-opening-md :deep(h4) {
  font-size: 1.04em;
}

.agent-chat-intro-opening-md :deep(h5),
.agent-chat-intro-opening-md :deep(h6) {
  font-size: 1em;
}

.agent-chat-intro-opening-md :deep(h1:first-child),
.agent-chat-intro-opening-md :deep(h2:first-child),
.agent-chat-intro-opening-md :deep(h3:first-child) {
  margin-top: 0;
}

.agent-chat-intro-opening-md :deep(img) {
  max-width: min(100%, 640px);
  max-height: min(360px, 45vh);
  width: auto;
  height: auto;
  object-fit: contain;
  display: block;
  margin: 0.5em 0;
  border-radius: 6px;
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

.agent-chat-thinking-text {
  margin-bottom: 8px;
}

.agent-chat-thinking-text-label {
  margin-bottom: 4px;
  font-size: 12px;
  font-weight: 500;
  color: #94a3b8;
}

.agent-chat-thinking-text-body {
  font-size: 13px;
  line-height: 1.6;
  color: #475569;
  white-space: pre-wrap;
  word-break: break-word;
}

.agent-chat-thinking-text-body :deep(p) {
  margin: 0 0 6px;
}

.agent-chat-thinking-text-body :deep(p:last-child) {
  margin-bottom: 0;
}

html.dark .agent-chat-thinking-text-label {
  color: rgba(255, 255, 255, 0.4);
}

html.dark .agent-chat-thinking-text-body {
  color: rgba(255, 255, 255, 0.65);
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

.agent-chat-footer {
  flex-shrink: 0;
  padding: 4px 20px 20px;
  border-top: none;
  background: transparent;
}

.agent-chat-footer-inner {
  max-width: var(--agent-chat-column-max);
  margin: 0 auto;
  width: 100%;
  box-sizing: border-box;
}

.agent-chat-main {
  position: relative;
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

.agent-chat-toolbar {
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  z-index: 5;
  padding: 28px 20px 4px;
  pointer-events: none;
}

.agent-chat-toolbar-fade {
  position: absolute;
  inset: 0;
  background: linear-gradient(to bottom, rgba(255, 255, 255, 0) 0%, rgba(255, 255, 255, 0.85) 45%, #ffffff 75%);
}

html.dark .agent-chat-toolbar-fade {
  background: linear-gradient(to bottom, rgba(24, 24, 28, 0) 0%, rgba(24, 24, 28, 0.85) 45%, #18181c 75%);
}

.agent-chat-toolbar-inner {
  position: relative;
  max-width: var(--agent-chat-column-max);
  margin: 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  pointer-events: none;
}

.agent-chat-toolbar-inner > * {
  pointer-events: auto;
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

.agent-chat-input,
.agent-chat-input.n-input {
  --n-border: transparent !important;
  --n-border-hover: transparent !important;
  --n-border-focus: transparent !important;
  --n-box-shadow-focus: none !important;
  --n-caret-color: currentColor;
}

.agent-chat-input :deep(.n-input-wrapper) {
  --n-border: transparent !important;
  --n-border-hover: transparent !important;
  --n-border-focus: transparent !important;
  --n-box-shadow-focus: none !important;
  background: transparent !important;
}

.agent-chat-input :deep(.n-input__border),
.agent-chat-input :deep(.n-input__state-border) {
  border: none !important;
  box-shadow: none !important;
}

.agent-chat-input :deep(textarea:focus) {
  outline: none;
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
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 4px;
}

.agent-chat-composer-bar :deep(.n-upload) {
  width: auto;
  flex: 0 0 auto;
}

.agent-chat-send {
  flex-shrink: 0;
  margin-left: auto;
}

.agent-chat-pending-attachments {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 10px;
}

.agent-chat-attachment-boxes {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 8px;
}

.agent-chat-attachment-boxes--user {
  justify-content: flex-end;
  max-width: min(85%, 520px);
  margin-left: auto;
}

.agent-chat-attachment-boxes--user :deep(.agent-chat-attachment-media) {
  align-items: flex-end;
}

.agent-chat-file-box {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  max-width: 220px;
  padding: 6px 10px;
  border-radius: 10px;
  border: 1px solid #e5e7eb;
  background: #f9fafb;
  box-sizing: border-box;
}

html.dark .agent-chat-file-box {
  border-color: rgba(255, 255, 255, 0.12);
  background: rgba(255, 255, 255, 0.06);
}

.agent-chat-file-box--pending {
  padding-right: 6px;
}

.agent-chat-file-box--readonly {
  cursor: default;
}

.agent-chat-file-box-icon {
  flex-shrink: 0;
  color: #64748b;
}

html.dark .agent-chat-file-box-icon {
  color: rgba(255, 255, 255, 0.55);
}

.agent-chat-file-box-name {
  flex: 1;
  min-width: 0;
  font-size: 13px;
  line-height: 1.35;
  color: #334155;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

html.dark .agent-chat-file-box-name {
  color: rgba(255, 255, 255, 0.88);
}

.agent-chat-file-box-remove {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  margin-left: 2px;
  padding: 2px;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: #94a3b8;
  cursor: pointer;
  line-height: 0;
  transition: color 0.15s ease, background 0.15s ease;
}

.agent-chat-file-box-remove:hover:not(:disabled) {
  color: #ef4444;
  background: rgba(239, 68, 68, 0.08);
}

.agent-chat-file-box-remove:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

/* 助手消息错误块（流式/历史） */
.agent-chat-msg-error {
  margin-bottom: 12px;
  padding: 10px 12px;
  border-radius: 8px;
  border: 1px solid rgba(239, 68, 68, 0.45);
  background: rgba(239, 68, 68, 0.08);
  box-sizing: border-box;
}

html.dark .agent-chat-msg-error {
  border-color: rgba(248, 113, 113, 0.4);
  background: rgba(239, 68, 68, 0.12);
}

.agent-chat-msg-error-label {
  font-size: 12px;
  font-weight: 600;
  color: var(--n-error-color);
  margin-bottom: 6px;
}

.agent-chat-msg-error-text {
  font-size: 14px;
  line-height: 1.45;
  color: #7f1d1d;
  white-space: pre-wrap;
  word-break: break-word;
}

html.dark .agent-chat-msg-error-text {
  color: rgba(254, 202, 202, 0.95);
}

.agent-chat-msg-stopped {
  margin-bottom: 12px;
  padding: 8px 12px;
  border-radius: 8px;
  font-size: 13px;
  color: var(--n-text-color-3);
  background: rgba(100, 116, 139, 0.12);
  border: 1px solid rgba(100, 116, 139, 0.25);
  box-sizing: border-box;
}

html.dark .agent-chat-msg-stopped {
  background: rgba(148, 163, 184, 0.12);
  border-color: rgba(148, 163, 184, 0.28);
}

/* Markdown + KaTeX（浅色：近黑字；暗黑：浅灰字） */
.agent-chat-md {
  color: #0f172a;
  font-size: 16px;
  line-height: 1.72;
  /* 宽表格横向滚动；与 pre 内滚动互不冲突 */
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
}

html.dark .agent-chat-md {
  color: rgba(255, 255, 255, 0.88);
}

/* 高危 MCP 工具调用确认卡 */
.agent-chat-mcp-confirm {
  margin-bottom: 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.agent-chat-mcp-confirm-item {
  border: 1px solid rgba(245, 158, 11, 0.45);
  background: rgba(245, 158, 11, 0.08);
  border-radius: 8px;
  padding: 10px 12px;
  box-sizing: border-box;
}

.agent-chat-mcp-confirm-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--n-warning-color);
  margin-bottom: 4px;
}

.agent-chat-mcp-confirm-text {
  font-size: 13px;
  margin-bottom: 6px;
}

.agent-chat-mcp-confirm-args {
  margin: 0 0 8px;
  max-height: 160px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 12px;
  color: var(--n-text-color-3);
}

.agent-chat-mcp-confirm-actions {
  display: flex;
  gap: 8px;
}

/* 知识库来源列表 */
.agent-chat-sources {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-top: 10px;
}

.agent-chat-sources-label {
  font-size: 12px;
  color: #64748b;
  margin-right: 2px;
}

.agent-chat-source-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 10px;
  border-radius: 999px;
  background: #eef2f7;
  color: #334155;
  font-size: 12px;
  line-height: 1.7;
  text-decoration: none;
  border: 1px solid #e2e8f0;
  /* 点击正文引用胶囊跳转后与容器边缘保持间距 */
  scroll-margin: 8px;
}

a.agent-chat-source-chip:hover {
  border-color: #94a3b8;
  color: #0f172a;
}

html.dark .agent-chat-sources-label {
  color: #94a3b8;
}

html.dark .agent-chat-source-chip {
  background: rgba(148, 163, 184, 0.14);
  border-color: rgba(148, 163, 184, 0.28);
  color: #cbd5e1;
}

html.dark a.agent-chat-source-chip:hover {
  border-color: #cbd5e1;
  color: #f1f5f9;
}

.agent-chat-md :deep(p) {
  margin: 0 0 0.5em;
  line-height: 1.78;
}

.agent-chat-md :deep(p:last-child) {
  margin-bottom: 0;
}

.agent-chat-md :deep(h1),
.agent-chat-md :deep(h2),
.agent-chat-md :deep(h3),
.agent-chat-md :deep(h4),
.agent-chat-md :deep(h5),
.agent-chat-md :deep(h6) {
  font-weight: 600;
  line-height: 1.35;
  margin: 0.85em 0 0.45em;
  color: inherit;
}

.agent-chat-md :deep(h1) {
  font-size: 1.28em;
}

.agent-chat-md :deep(h2) {
  font-size: 1.18em;
}

.agent-chat-md :deep(h3) {
  font-size: 1.1em;
}

.agent-chat-md :deep(h4) {
  font-size: 1.04em;
}

.agent-chat-md :deep(h5),
.agent-chat-md :deep(h6) {
  font-size: 1em;
}

.agent-chat-md :deep(h1:first-child),
.agent-chat-md :deep(h2:first-child),
.agent-chat-md :deep(h3:first-child) {
  margin-top: 0;
}

.agent-chat-md :deep(img) {
  max-width: min(100%, 640px);
  max-height: min(360px, 45vh);
  width: auto;
  height: auto;
  object-fit: contain;
  display: block;
  margin: 0.5em 0;
  border-radius: 6px;
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
  font-size: 13px;
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

.agent-chat-md :deep(li) {
  line-height: 1.78;
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
