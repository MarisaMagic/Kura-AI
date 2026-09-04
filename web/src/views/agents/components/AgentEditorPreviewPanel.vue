<template>
  <div class="agent-chat-layout agent-editor-preview">
    <div class="agent-chat-main">
      <div ref="bodyScrollRef" class="agent-chat-body" @scroll.passive="onBodyScroll">
        <n-alert
          v-if="configStale && chatEnabled"
          type="warning"
          :bordered="false"
          class="agent-editor-preview__banner"
          :title="$t('views.agents.preview_config_stale_title')"
        >
          <p class="agent-editor-preview__banner-text">
            {{ $t('views.agents.preview_config_stale') }}
          </p>
          <n-button size="tiny" type="primary" @click="$emit('request-save')">
            {{ $t('views.agents.button_save_config') }}
          </n-button>
        </n-alert>

        <n-alert
          v-if="!chatEnabled"
          type="info"
          :bordered="false"
          class="agent-editor-preview__banner"
        >
          {{ $t('views.agents.preview_save_to_chat') }}
        </n-alert>

        <transition name="agent-chat-fade">
          <div v-if="sessionPhase === 'intro'" key="intro" class="agent-chat-intro">
            <n-avatar
              round
              :size="120"
              :src="avatarSrc"
              object-fit="cover"
              class="agent-chat-intro-avatar"
            />
            <h2 class="agent-chat-intro-name">{{ displayName }}</h2>
            <p class="agent-chat-intro-sub">{{ introDescription }}</p>
            <p v-if="creatorName" class="agent-chat-intro-creator">
              <TheIcon icon="mdi:account-outline" :size="16" />
              <span>{{ $t('views.agents.chat_label_creator') }} · {{ creatorName }}</span>
            </p>
            <div v-if="hasIntroOpeningText" class="agent-chat-intro-opening">
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
              v-memo="[
                m.content,
                m.thinkingItems,
                m.thinkingOpen,
                m.pending,
                m.queuedWaiting,
                m.errorText,
                m.stoppedByUser,
                m.sources,
                m.mcpConfirmations,
                m.ragTrace,
              ]"
              class="agent-chat-feed-item"
              :class="{ 'agent-chat-feed-item--user': m.role === 'user' }"
            >
              <template v-if="m.role === 'user'">
                <div class="agent-chat-user-row">
                  <div class="agent-chat-user-bubble">
                    <div class="agent-chat-user-text">{{ m.content }}</div>
                  </div>
                </div>
              </template>
              <template v-else>
                <n-avatar
                  round
                  :size="40"
                  :src="avatarSrc"
                  object-fit="cover"
                  class="agent-chat-feed-avatar"
                />
                <div class="agent-chat-feed-col">
                  <div class="agent-chat-feed-head">
                    <span class="agent-chat-feed-name">{{ displayName }}</span>
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
                      <TheIcon
                        icon="mdi:lightbulb-outline"
                        :size="16"
                        class="agent-chat-thinking-icon"
                      />
                      <span>{{ thinkingPillLabel(m, $t) }}</span>
                      <TheIcon
                        :icon="m.thinkingOpen ? 'mdi:chevron-up' : 'mdi:chevron-down'"
                        :size="18"
                        class="agent-chat-thinking-chevron"
                      />
                    </button>
                    <div v-show="m.thinkingOpen" class="agent-chat-thinking-panel">
                      <div v-if="m.thinkingItems?.length" class="agent-chat-thinking-steps">
                        <template v-for="(item, sIdx) in m.thinkingItems" :key="sIdx">
                          <div
                            v-if="item.type === 'step'"
                            v-memo="[item]"
                            class="agent-chat-thinking-step-line"
                          >
                            <span class="agent-chat-thinking-step-icon">{{
                              item.icon || '▸'
                            }}</span>
                            <span class="agent-chat-thinking-step-label">{{ item.label }}</span>
                            <span v-if="item.detail" class="agent-chat-thinking-step-detail">{{
                              item.detail
                            }}</span>
                          </div>
                          <div
                            v-else-if="item.type === 'text'"
                            v-memo="[item]"
                            class="agent-chat-thinking-step-line agent-chat-thinking-step-line--text"
                          >
                            <span class="agent-chat-thinking-step-icon">💭</span>
                            <span class="agent-chat-thinking-step-label">{{
                              $t('views.agents.chat_thinking_text_label')
                            }}</span>
                            <div
                              class="agent-chat-thinking-step-body agent-chat-md"
                              v-html="renderAgentChatMarkdown(item.text)"
                            ></div>
                          </div>
                        </template>
                      </div>
                      <p v-if="!m.thinkingItems?.length" class="agent-chat-thinking-placeholder">
                        {{ $t('views.agents.chat_feed_thinking_placeholder') }}
                      </p>
                      <details
                        v-if="m.ragTrace && Object.keys(m.ragTrace).length"
                        class="agent-chat-rag-trace"
                      >
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
                      v-html="renderAgentChatMarkdown(m.content, m.sources)"
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
          <n-button
            size="small"
            round
            quaternary
            :disabled="chatDisabled || sending"
            @click="handleRestartChat"
          >
            <template #icon>
              <TheIcon icon="mdi:plus-circle-outline" :size="18" />
            </template>
            {{ $t('views.agents.chat_button_restart') }}
          </n-button>
        </div>
      </div>
    </div>

    <footer class="agent-chat-footer">
      <div class="agent-chat-footer-inner">
        <div class="agent-chat-composer">
          <n-input
            v-model:value="inputText"
            type="textarea"
            :bordered="false"
            :autosize="{ minRows: 1, maxRows: 8 }"
            :placeholder="inputPlaceholder"
            :disabled="chatDisabled || sending"
            class="agent-chat-input"
            @keydown="onInputKeydown"
          />
          <div class="agent-chat-composer-bar">
            <n-button quaternary circle disabled :title="$t('views.agents.chat_attach_file')">
              <TheIcon icon="mdi:paperclip" :size="22" />
            </n-button>
            <div class="agent-chat-kb-toggle">
              <n-switch
                :value="useKnowledgeRetrieval"
                :disabled="chatDisabled || sending"
                size="small"
                @update:value="onKbToggle"
              />
              <span class="agent-chat-kb-toggle-label">{{
                $t('views.agents.chat_kb_retrieval')
              }}</span>
            </div>
            <div class="agent-chat-kb-toggle">
              <n-switch
                :value="useWebSearch"
                :disabled="chatDisabled || sending"
                size="small"
                @update:value="onWebToggle"
              />
              <span class="agent-chat-kb-toggle-label">{{
                $t('views.agents.chat_web_search')
              }}</span>
            </div>
            <n-button
              :type="sending ? 'warning' : 'primary'"
              circle
              class="agent-chat-send"
              :disabled="chatDisabled || (!sending && sendDisabled)"
              :title="
                sending ? $t('views.agents.chat_button_stop') : $t('views.agents.chat_button_send')
              "
              @click="onSendClick"
            >
              <TheIcon :icon="sending ? 'mdi:stop' : 'mdi:arrow-up'" :size="20" />
            </n-button>
          </div>
        </div>
      </div>
    </footer>
  </div>
</template>

<script setup>
import { computed, onUnmounted, ref, toRef, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { NAlert, NAvatar, NButton, NInput, NSpin, NSwitch } from 'naive-ui'
import TheIcon from '@/components/icon/TheIcon.vue'
import { renderAgentChatMarkdown } from '@/utils/agentChatMarkdown'
import { thinkingPillLabel } from '@/utils/agentChatThinking'
import { useChatStickToBottom } from '@/views/agent-chat/useChatStickToBottom.js'
import { DEFAULT_AVATAR } from '@/views/agents/composables/agentFormCommon.js'
import { editorPreviewSessionId } from '@/views/agents/composables/useAgentConfigDiff.js'
import { useAgentPreviewChat } from '@/views/agents/composables/useAgentPreviewChat.js'
import { useIntroOpening } from '@/views/agents/composables/useIntroOpening.js'

const props = defineProps({
  form: { type: Object, required: true },
  agentId: { type: [Number, String, null], default: null },
  avatarPreview: { type: String, default: '' },
  creatorName: { type: String, default: '' },
  configStale: { type: Boolean, default: false },
  chatEnabled: { type: Boolean, default: false },
})

defineEmits(['request-save'])

const { t } = useI18n()
const inputText = ref('')
const bodyScrollRef = ref(null)
const { onBodyScroll, scrollBodyToBottom } = useChatStickToBottom(bodyScrollRef)
const useKnowledgeRetrieval = ref(false)
const useWebSearch = ref(false)

// 知识库检索与联网搜索互斥
function onKbToggle(val) {
  useKnowledgeRetrieval.value = val
  if (val) useWebSearch.value = false
}

function onWebToggle(val) {
  useWebSearch.value = val
  if (val) useKnowledgeRetrieval.value = false
}

const avatarSrc = computed(() => props.avatarPreview || DEFAULT_AVATAR)

const displayName = computed(() => {
  const n = String(props.form?.name || '').trim()
  return n || 'MyAgent'
})

const introDescription = computed(() => {
  const d = String(props.form?.description || '').trim()
  return d || t('views.agents.text_no_description')
})

const hasIntroOpeningText = computed(
  () => String(props.form?.opening_message || '').trim().length > 0
)

const sessionId = computed(() => editorPreviewSessionId(props.agentId))

const resolvedAgentId = computed(() => {
  const id = props.agentId
  if (id == null || id === '') return null
  const n = Number(id)
  return Number.isFinite(n) ? n : null
})

const {
  messages,
  sessionPhase,
  sending,
  resetChat,
  sendMessage,
  stopGeneration,
  toggleThinking,
  approveMcpConfirmation,
  confirmingMcpIds,
} = useAgentPreviewChat({
  agentId: resolvedAgentId,
  sessionId,
  useKnowledgeRetrieval,
  useWebSearch,
  t,
})

const openingRef = toRef(() => props.form?.opening_message)
const {
  displayed: introOpeningDisplayed,
  running: introOpeningRunning,
  start,
  stop,
} = useIntroOpening(() => props.form?.opening_message)

watch(
  openingRef,
  () => {
    stop()
    if (sessionPhase.value === 'intro' && hasIntroOpeningText.value) start()
  },
  { immediate: true }
)

const chatDisabled = computed(() => !props.chatEnabled || props.configStale)

const inputPlaceholder = computed(() => {
  if (!props.chatEnabled) return t('views.agents.preview_save_to_chat')
  if (props.configStale) return t('views.agents.preview_config_stale_short')
  return t('views.agents.chat_placeholder_input')
})

const sendDisabled = computed(() => !String(inputText.value || '').trim())

function handleRestartChat() {
  stop()
  resetChat()
  if (hasIntroOpeningText.value) start()
  scrollBodyToBottom(false, { force: true })
}

function onSendClick() {
  if (chatDisabled.value) return
  if (sending.value) {
    stopGeneration()
    return
  }
  const text = String(inputText.value || '').trim()
  if (!text) return
  if (sessionPhase.value === 'intro') stop()
  inputText.value = ''
  sendMessage(text)
  scrollBodyToBottom(false, { force: true })
}

function onInputKeydown(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    if (sending.value) return
    onSendClick()
  }
}

watch(
  () => props.agentId,
  () => {
    stop()
    resetChat()
    if (hasIntroOpeningText.value) start()
  }
)

watch(
  () => messages.value.length,
  () => scrollBodyToBottom()
)

// 最后一条消息内容变化时跟随滚动：预览面板非虚拟列表，
// 节流至 ~100ms 一次，避免每个 SSE 事件都触发一次滚动写入
let contentScrollTimer = null
let contentScrollLast = 0
function scheduleContentScroll() {
  const now = Date.now()
  const elapsed = now - contentScrollLast
  if (elapsed >= 100) {
    contentScrollLast = now
    if (contentScrollTimer != null) {
      clearTimeout(contentScrollTimer)
      contentScrollTimer = null
    }
    scrollBodyToBottom()
    return
  }
  if (contentScrollTimer == null) {
    contentScrollTimer = setTimeout(() => {
      contentScrollTimer = null
      contentScrollLast = Date.now()
      scrollBodyToBottom()
    }, 100 - elapsed)
  }
}

watch(
  () => messages.value[messages.value.length - 1]?.content ?? '',
  () => scheduleContentScroll()
)

watch(introOpeningDisplayed, () => scrollBodyToBottom())

onUnmounted(() => {
  if (contentScrollTimer != null) clearTimeout(contentScrollTimer)
})
</script>

<style scoped>
.agent-editor-preview {
  height: 100%;
  min-height: 0;
  --agent-chat-column-max: 100%;
}

.agent-editor-preview__banner {
  max-width: var(--agent-chat-column-max);
  margin: 0 auto 12px;
}

.agent-editor-preview__banner-text {
  margin: 0 0 8px;
  font-size: 13px;
  line-height: 1.5;
}

.agent-editor-preview :deep(.agent-chat-intro-name) {
  color: var(--primary-color, #f4511e);
}

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
</style>
<style src="@/views/agent-chat/agent-chat-ui.css"></style>
