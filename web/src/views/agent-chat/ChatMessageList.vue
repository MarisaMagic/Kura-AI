<template>
  <n-virtual-list
    ref="listRef"
    class="agent-chat-feed-virtual"
    :style="{ height: '100%' }"
    :items="messages"
    :item-size="180"
    item-resizable
    key-field="id"
    :padding-top="24"
    :padding-bottom="76"
    :items-style="{ paddingLeft: '20px', paddingRight: '20px', boxSizing: 'border-box' }"
    @scroll="onNativeScroll"
  >
    <template #default="{ item: m }">
      <div
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
              <span class="agent-chat-feed-name">{{ agentName }}</span>
              <span class="agent-chat-feed-ai-badge">{{
                $t('views.agents.chat_feed_ai_badge')
              }}</span>
            </div>

            <div class="agent-chat-thinking-wrap">
              <button
                type="button"
                class="agent-chat-thinking-pill"
                :aria-expanded="m.thinkingOpen"
                @click="$emit('toggle-thinking', m)"
              >
                <TheIcon icon="mdi:lightbulb-outline" :size="16" class="agent-chat-thinking-icon" />
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
                    <div v-if="item.type === 'step'" class="agent-chat-thinking-step-line">
                      <span class="agent-chat-thinking-step-icon">{{ item.icon || '▸' }}</span>
                      <span class="agent-chat-thinking-step-label">{{ item.label }}</span>
                      <span v-if="item.detail" class="agent-chat-thinking-step-detail">{{
                        item.detail
                      }}</span>
                    </div>
                    <div
                      v-else-if="item.type === 'text'"
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
                      @click="$emit('mcp-approve', m, item, true)"
                    >
                      允许一次
                    </n-button>
                    <n-button
                      size="tiny"
                      quaternary
                      :disabled="sending || confirmingMcpIds.has(item.pending_id)"
                      @click="$emit('mcp-approve', m, item, false)"
                    >
                      拒绝
                    </n-button>
                  </div>
                </div>
              </div>
              <div
                v-if="!m.pending && (m.content || '').trim()"
                class="agent-chat-md"
                @click="$emit('md-click', $event)"
                v-html="renderAgentChatMarkdown(m.content, m.sources)"
              />
              <div v-if="!m.pending && m.sources?.length" class="agent-chat-sources">
                <span class="agent-chat-sources-label">{{
                  $t('views.agents.chat_sources_label')
                }}</span>
                <template v-for="src in m.sources" :key="`src-${src.chunk_id || src.index}`">
                  <a
                    v-if="safeExternalHref(src.url)"
                    :href="safeExternalHref(src.url)"
                    target="_blank"
                    rel="noopener noreferrer"
                    class="agent-chat-source-chip"
                    :data-kcite-chip="src.index"
                  >
                    [{{ src.index }}] {{ src.title || src.url }}
                  </a>
                  <a
                    v-else-if="safeExternalHref(toSameOriginMediaUrl(src.image_url))"
                    :href="safeExternalHref(toSameOriginMediaUrl(src.image_url))"
                    target="_blank"
                    rel="noopener noreferrer"
                    class="agent-chat-source-chip"
                    :data-kcite-chip="src.index"
                  >
                    [{{ src.index }}] {{ src.filename
                    }}<template v-if="src.page_number && src.page_number !== 'N/A'">
                      · P{{ src.page_number }}</template
                    >
                  </a>
                  <span v-else class="agent-chat-source-chip" :data-kcite-chip="src.index">
                    [{{ src.index }}] {{ src.filename
                    }}<template v-if="src.page_number && src.page_number !== 'N/A'">
                      · P{{ src.page_number }}</template
                    >
                  </span>
                </template>
              </div>
            </div>
            <div
              v-if="
                !m.pending &&
                ((m.content || '').trim() || (m.errorText || '').trim() || m.stoppedByUser)
              "
              class="agent-chat-assistant-actions"
            >
              <span v-if="(m.versionCount || 1) > 1" class="agent-chat-version-switch">
                <n-button
                  quaternary
                  circle
                  size="small"
                  class="agent-chat-copy-btn"
                  :disabled="sending || switchingBranch || (m.versionIndex || 1) <= 1"
                  :aria-label="$t('views.agents.chat_version_prev_tooltip')"
                  @click="$emit('switch-version', m, -1)"
                >
                  <TheIcon icon="mdi:chevron-left" :size="16" />
                </n-button>
                <span class="agent-chat-version-label">
                  {{ m.versionIndex || 1 }}/{{ m.versionCount }}
                </span>
                <n-button
                  quaternary
                  circle
                  size="small"
                  class="agent-chat-copy-btn"
                  :disabled="
                    sending || switchingBranch || (m.versionIndex || 1) >= (m.versionCount || 1)
                  "
                  :aria-label="$t('views.agents.chat_version_next_tooltip')"
                  @click="$emit('switch-version', m, 1)"
                >
                  <TheIcon icon="mdi:chevron-right" :size="16" />
                </n-button>
              </span>
              <n-tooltip :show-arrow="false" placement="top">
                <template #trigger>
                  <n-button
                    quaternary
                    circle
                    size="small"
                    class="agent-chat-copy-btn"
                    :disabled="sending || switchingBranch || !m.messageId"
                    :aria-label="$t('views.agents.chat_regenerate_tooltip')"
                    @click="$emit('regenerate', m)"
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
                    @click="$emit('copy-plain', m)"
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
                    @click="$emit('copy-md', m)"
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
    </template>
  </n-virtual-list>
</template>

<script setup>
import { ref } from 'vue'
import { NAvatar, NButton, NTooltip, NVirtualList } from 'naive-ui'
import TheIcon from '@/components/icon/TheIcon.vue'
import ChatAttachmentItem from './ChatAttachmentItem.vue'
import {
  renderAgentChatMarkdown,
  safeExternalHref,
  toSameOriginMediaUrl,
} from '@/utils/agentChatMarkdown'
import { thinkingPillLabel } from '@/utils/agentChatThinking'

defineProps({
  messages: { type: Array, default: () => [] },
  chatAgentId: { type: Number, default: 0 },
  sessionId: { type: String, default: '' },
  agentAvatarSrc: { type: String, default: '' },
  agentName: { type: String, default: '—' },
  sending: { type: Boolean, default: false },
  switchingBranch: { type: Boolean, default: false },
  confirmingMcpIds: { type: Object, default: () => new Set() },
})

const emit = defineEmits([
  'toggle-thinking',
  'mcp-approve',
  'md-click',
  'switch-version',
  'regenerate',
  'copy-plain',
  'copy-md',
  'scroll',
])

const listRef = ref(null)

function getScrollEl() {
  const inst = listRef.value
  if (!inst) return null
  if (typeof inst.getScrollContainer === 'function') {
    return inst.getScrollContainer() || null
  }
  return inst.listElRef || inst.$el || null
}

function onNativeScroll(e) {
  emit('scroll', e)
}

defineExpose({ getScrollEl })
</script>

<style src="./agent-chat-ui.css"></style>
<style>
.agent-chat-feed-virtual,
.agent-chat-feed-virtual.n-scrollbar,
.agent-chat-feed-virtual .n-scrollbar-container,
.agent-chat-feed-virtual .v-vl {
  width: 100% !important;
  max-width: none !important;
  box-sizing: border-box;
  height: 100%;
}

.agent-chat-feed-virtual .n-virtual-list__content,
.agent-chat-feed-virtual .v-vl-items {
  width: 100%;
  max-width: none;
  box-sizing: border-box;
}

.agent-chat-feed-item {
  max-width: var(--agent-chat-column-max, 880px);
  margin-left: auto;
  margin-right: auto;
  padding-bottom: 28px;
}

/* Naive 浮层轨道会内缩；改回原生滚动条，贴主栏（窗口）最右侧 */
.agent-chat-feed-virtual .n-scrollbar-rail {
  display: none !important;
}

.agent-chat-feed-virtual > *:not(.n-scrollbar-rail),
.agent-chat-feed-virtual .v-vl {
  scrollbar-width: thin !important;
  scrollbar-color: rgba(15, 23, 42, 0.14) transparent;
}

.agent-chat-feed-virtual > *:not(.n-scrollbar-rail)::-webkit-scrollbar,
.agent-chat-feed-virtual .v-vl::-webkit-scrollbar {
  width: 8px !important;
  height: 8px !important;
  display: block !important;
}

.agent-chat-feed-virtual > *:not(.n-scrollbar-rail)::-webkit-scrollbar-button,
.agent-chat-feed-virtual .v-vl::-webkit-scrollbar-button {
  display: none;
  width: 0;
  height: 0;
}

.agent-chat-feed-virtual > *:not(.n-scrollbar-rail)::-webkit-scrollbar-track,
.agent-chat-feed-virtual .v-vl::-webkit-scrollbar-track {
  background: transparent;
}

.agent-chat-feed-virtual > *:not(.n-scrollbar-rail)::-webkit-scrollbar-thumb,
.agent-chat-feed-virtual .v-vl::-webkit-scrollbar-thumb {
  background-color: rgba(15, 23, 42, 0.14);
  border-radius: 100px;
  border: 2px solid transparent;
  background-clip: padding-box;
}

.agent-chat-feed-virtual > *:not(.n-scrollbar-rail)::-webkit-scrollbar-thumb:hover,
.agent-chat-feed-virtual .v-vl::-webkit-scrollbar-thumb:hover {
  background-color: rgba(15, 23, 42, 0.22);
}

html.dark .agent-chat-feed-virtual > *:not(.n-scrollbar-rail),
html.dark .agent-chat-feed-virtual .v-vl {
  scrollbar-color: rgba(255, 255, 255, 0.12) transparent;
}

html.dark .agent-chat-feed-virtual > *:not(.n-scrollbar-rail)::-webkit-scrollbar-thumb,
html.dark .agent-chat-feed-virtual .v-vl::-webkit-scrollbar-thumb {
  background-color: rgba(255, 255, 255, 0.12);
}

html.dark .agent-chat-feed-virtual > *:not(.n-scrollbar-rail)::-webkit-scrollbar-thumb:hover,
html.dark .agent-chat-feed-virtual .v-vl::-webkit-scrollbar-thumb:hover {
  background-color: rgba(255, 255, 255, 0.2);
}

.agent-chat-version-switch {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  margin-right: 4px;
}

.agent-chat-version-label {
  font-size: 12px;
  color: var(--n-text-color-3);
  min-width: 30px;
  text-align: center;
  user-select: none;
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
</style>
