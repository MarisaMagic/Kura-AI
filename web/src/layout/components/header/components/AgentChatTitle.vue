<template>
  <div v-if="visible" class="agent-chat-header-title min-w-0">
    <n-popover
      v-model:show="popoverShow"
      trigger="click"
      placement="bottom-start"
      :show-arrow="false"
      raw
      :content-style="{ padding: 0 }"
      display-directive="if"
    >
      <template #trigger>
        <button
          type="button"
          class="agent-chat-title-btn"
          :aria-label="displayName || $t('views.agents.title_agent_chat')"
        >
          <span class="agent-chat-title-text">{{ displayName }}</span>
          <TheIcon icon="mdi:chevron-down" :size="18" class="agent-chat-title-chevron" />
        </button>
      </template>
      <div class="agent-chat-popover-card">
        <div class="agent-chat-popover-profile">
          <n-avatar
            round
            :size="80"
            :src="avatarSrc"
            object-fit="cover"
            class="agent-chat-popover-avatar"
          />
          <div class="agent-chat-popover-name">{{ displayName }}</div>
          <div v-if="subtitleLine" class="agent-chat-popover-sub">{{ subtitleLine }}</div>
          <div class="agent-chat-popover-creator">
            <TheIcon icon="mdi:account-outline" :size="16" />
            <span>{{ creatorLine }}</span>
          </div>
        </div>

        <div class="agent-chat-popover-actions">
          <button type="button" class="agent-chat-popover-action" @click="goHub">
            <span class="agent-chat-popover-action-circle">
              <TheIcon icon="mdi:home-outline" :size="22" />
            </span>
            <span class="agent-chat-popover-action-label">{{
              $t('views.agents.chat_popover_back_hub_short')
            }}</span>
          </button>
          <button
            v-if="headerStore.isOwner"
            type="button"
            class="agent-chat-popover-action"
            :disabled="!headerStore.agentId"
            @click="goEdit"
          >
            <span class="agent-chat-popover-action-circle">
              <TheIcon icon="mdi:pencil-outline" :size="22" />
            </span>
            <span class="agent-chat-popover-action-label">{{
              $t('views.agents.chat_popover_edit_short')
            }}</span>
          </button>
          <button
            v-if="headerStore.isOwner"
            type="button"
            class="agent-chat-popover-action"
            :disabled="!headerStore.agentId"
            @click="goKnowledgeBase"
          >
            <span class="agent-chat-popover-action-circle">
              <TheIcon icon="mdi:book-open-variant" :size="22" />
            </span>
            <span class="agent-chat-popover-action-label">{{
              $t('views.agents.chat_popover_kb_short')
            }}</span>
          </button>
        </div>

        <div class="agent-chat-popover-divider" />

        <div class="agent-chat-popover-history">
          <div class="agent-chat-popover-history-title">
            {{ $t('views.agents.chat_popover_history_title') }}
          </div>
          <ul v-if="historyLoading" class="agent-chat-popover-history-list">
            <li class="agent-chat-popover-history-placeholder">
              {{ $t('views.agents.chat_popover_history_loading') }}
            </li>
          </ul>
          <ul v-else-if="historySessions.length === 0" class="agent-chat-popover-history-list">
            <li class="agent-chat-popover-history-placeholder">
              {{ $t('views.agents.chat_popover_history_empty') }}
            </li>
          </ul>
          <ul
            v-else
            class="agent-chat-popover-history-list agent-chat-popover-history-list--scroll"
          >
            <li
              v-for="s in historySessions"
              :key="s.session_id"
              class="agent-chat-popover-history-item"
            >
              <button
                type="button"
                class="agent-chat-popover-history-item-main"
                @click="openHistorySession(s.session_id)"
              >
                <span class="agent-chat-popover-history-title-text">{{
                  displaySessionTitle(s)
                }}</span>
                <span class="agent-chat-popover-history-meta">{{ sessionMetaLine(s) }}</span>
              </button>
              <button
                type="button"
                class="agent-chat-popover-history-delete"
                :aria-label="$t('views.agents.chat_popover_history_delete_aria')"
                @click.stop="deleteHistorySession(s.session_id)"
              >
                <TheIcon icon="mdi:delete-outline" :size="16" />
              </button>
            </li>
          </ul>
        </div>
      </div>
    </n-popover>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { NAvatar, NPopover } from 'naive-ui'
import TheIcon from '@/components/icon/TheIcon.vue'
import api from '@/api'
import { useAgentChatHeaderStore } from '@/store'
import { DEFAULT_AVATAR } from '@/views/agents/composables/agentFormCommon.js'

const route = useRoute()
const router = useRouter()
const { t } = useI18n()
const headerStore = useAgentChatHeaderStore()

const popoverShow = ref(false)
const historyLoading = ref(false)
const historySessions = ref([])

function displaySessionTitle(s) {
  const p = (s.last_user_preview || '').trim()
  if (p) return p
  return t('views.agents.chat_popover_history_no_title')
}

function sessionMetaLine(s) {
  const name = (s.agent_name || headerStore.agentTitle || '').trim()
  const dt = (s.updated_at_display || '').trim()
  if (name && dt) return `${name} · ${dt}`
  return name || dt || '—'
}

async function openHistorySession(sessionId) {
  if (!headerStore.agentId) return
  popoverShow.value = false
  await router.push({
    name: 'AgentChat',
    params: { agentId: String(headerStore.agentId) },
    query: { session: sessionId },
  })
}

async function loadHistorySessions() {
  if (!headerStore.agentId) {
    historySessions.value = []
    return
  }
  historyLoading.value = true
  try {
    const res = await api.getAgentChatSessions({ agent_id: headerStore.agentId })
    historySessions.value = res.data?.sessions || []
  } catch {
    historySessions.value = []
  } finally {
    historyLoading.value = false
  }
}

async function deleteHistorySession(sessionId) {
  if (!headerStore.agentId) return
  if (!window.confirm(t('views.agents.chat_popover_history_delete_confirm'))) return
  try {
    await api.deleteAgentChatSession({ agent_id: headerStore.agentId, session_id: sessionId })
    window.$message?.success(t('views.agents.chat_popover_history_delete_ok'))
    await loadHistorySessions()
    if (route.query.session === sessionId) {
      await router.replace({
        name: 'AgentChat',
        params: { agentId: String(headerStore.agentId) },
        query: { new: '1' },
      })
    }
  } catch {
    window.$message?.error(t('views.agents.chat_popover_history_delete_fail'))
  }
}

watch(popoverShow, (show) => {
  if (show) loadHistorySessions()
})

const visible = computed(() => route.name === 'AgentChat')

const displayName = computed(() => headerStore.agentTitle || '—')

const avatarSrc = computed(() => headerStore.avatarUrl || DEFAULT_AVATAR)

const subtitleLine = computed(() => headerStore.subtitle?.trim() || '')

const creatorLine = computed(() => {
  const c = headerStore.creatorName?.trim()
  if (c) return `${t('views.agents.chat_label_creator')} · ${c}`
  return t('views.agents.chat_label_creator')
})

function goHub() {
  popoverShow.value = false
  router.push('/agent-hub')
}

function goEdit() {
  if (!headerStore.agentId) return
  popoverShow.value = false
  router.push({ name: 'AgentEdit', params: { id: String(headerStore.agentId) } })
}

function goKnowledgeBase() {
  if (!headerStore.agentId) return
  popoverShow.value = false
  router.push({ name: 'AgentKnowledgeBase', params: { agentId: String(headerStore.agentId) } })
}
</script>

<style scoped>
.agent-chat-header-title {
  margin-left: 16px;
  display: flex;
  align-items: center;
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
}

.agent-chat-title-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  max-width: 100%;
  min-width: 0;
  padding: 6px 10px;
  margin: 0;
  border: none;
  border-radius: 8px;
  background: transparent;
  cursor: pointer;
  color: var(--n-text-color);
  font-size: 17px;
  font-weight: 600;
}

.agent-chat-title-btn:hover {
  background: rgba(128, 128, 128, 0.08);
}

.agent-chat-title-text {
  flex: 1 1 auto;
  min-width: 0;
  font-size: 17px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.agent-chat-title-chevron {
  flex-shrink: 0;
  opacity: 0.55;
}

.agent-chat-popover-card {
  width: min(360px, 92vw);
  box-sizing: border-box;
  padding: 20px 18px 18px;
  border-radius: 12px;
  background: #ffffff;
  box-shadow: 0 8px 32px rgba(15, 23, 42, 0.14);
}

html.dark .agent-chat-popover-card {
  background: #25252d;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.55);
}

.agent-chat-popover-profile {
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
}

.agent-chat-popover-avatar {
  flex-shrink: 0;
}

.agent-chat-popover-name {
  margin-top: 14px;
  font-size: 18px;
  font-weight: 700;
  color: #0f172a;
  line-height: 1.3;
  word-break: break-word;
}

html.dark .agent-chat-popover-name {
  color: rgba(255, 255, 255, 0.95);
}

.agent-chat-popover-sub {
  margin-top: 6px;
  font-size: 14px;
  color: #64748b;
  line-height: 1.4;
  word-break: break-word;
}

html.dark .agent-chat-popover-sub {
  color: rgba(255, 255, 255, 0.45);
}

.agent-chat-popover-creator {
  margin-top: 12px;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: #64748b;
}

html.dark .agent-chat-popover-creator {
  color: rgba(255, 255, 255, 0.45);
}

.agent-chat-popover-actions {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px 6px;
  margin-top: 22px;
}

.agent-chat-popover-action {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  min-width: 0;
  max-width: none;
  padding: 0;
  border: none;
  background: none;
  cursor: pointer;
  color: inherit;
}

.agent-chat-popover-action:disabled {
  cursor: not-allowed;
  opacity: 0.45;
}

.agent-chat-popover-action-circle {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 52px;
  height: 52px;
  border-radius: 999px;
  background: #f1f5f9;
  color: #0f172a;
  transition: background 0.15s ease;
}

html.dark .agent-chat-popover-action-circle {
  background: rgba(255, 255, 255, 0.1);
  color: rgba(255, 255, 255, 0.9);
}

.agent-chat-popover-action:not(:disabled):hover .agent-chat-popover-action-circle {
  background: #e2e8f0;
}

html.dark .agent-chat-popover-action:not(:disabled):hover .agent-chat-popover-action-circle {
  background: rgba(255, 255, 255, 0.16);
}

.agent-chat-popover-action-label {
  font-size: 12px;
  line-height: 1.35;
  text-align: center;
  color: #334155;
}

html.dark .agent-chat-popover-action-label {
  color: rgba(255, 255, 255, 0.75);
}

.agent-chat-popover-divider {
  height: 1px;
  margin: 18px 0 14px;
  background: #e2e8f0;
}

html.dark .agent-chat-popover-divider {
  background: rgba(255, 255, 255, 0.08);
}

.agent-chat-popover-history-title {
  font-size: 15px;
  font-weight: 700;
  color: #0f172a;
  margin-bottom: 10px;
}

html.dark .agent-chat-popover-history-title {
  color: rgba(255, 255, 255, 0.95);
}

.agent-chat-popover-history-list {
  margin: 0;
  padding: 0;
  list-style: none;
}

.agent-chat-popover-history-placeholder {
  font-size: 14px;
  line-height: 1.5;
  color: #64748b;
}

html.dark .agent-chat-popover-history-placeholder {
  color: rgba(255, 255, 255, 0.45);
}

.agent-chat-popover-history-list--scroll {
  /* 约 4 条会话高度，其余滚动；隐藏滚动条但保留滚动 */
  max-height: 288px;
  overflow-y: auto;
  padding-right: 2px;
  scrollbar-width: none;
  -ms-overflow-style: none;
}

.agent-chat-popover-history-list--scroll::-webkit-scrollbar {
  width: 0;
  height: 0;
  display: none;
}

.agent-chat-popover-history-item {
  display: flex;
  flex-direction: row;
  align-items: center;
  gap: 6px;
  margin-bottom: 8px;
  border-radius: 10px;
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  overflow: hidden;
  padding-right: 4px;
}

.agent-chat-popover-history-item:hover {
  background: #f1f5f9;
}

html.dark .agent-chat-popover-history-item {
  background: rgba(255, 255, 255, 0.06);
  border-color: rgba(255, 255, 255, 0.1);
}

html.dark .agent-chat-popover-history-item:hover {
  background: rgba(255, 255, 255, 0.1);
}

.agent-chat-popover-history-item-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
  padding: 10px 4px 10px 12px;
  margin: 0;
  border: none;
  background: transparent;
  cursor: pointer;
  text-align: left;
  color: inherit;
  font: inherit;
}

.agent-chat-popover-history-title-text {
  font-size: 13px;
  font-weight: 600;
  color: #0f172a;
  word-break: break-word;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

html.dark .agent-chat-popover-history-title-text {
  color: rgba(255, 255, 255, 0.92);
}

.agent-chat-popover-history-meta {
  font-size: 12px;
  color: #64748b;
}

html.dark .agent-chat-popover-history-meta {
  color: rgba(255, 255, 255, 0.45);
}

.agent-chat-popover-history-delete {
  flex-shrink: 0;
  width: 28px;
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  margin: 0;
  padding: 0;
  border: none;
  border-radius: 50%;
  background: transparent;
  cursor: pointer;
  color: #94a3b8;
  transition: color 0.15s ease, background 0.15s ease;
}

.agent-chat-popover-history-delete:hover {
  color: #ef4444;
  background: rgba(239, 68, 68, 0.1);
}

html.dark .agent-chat-popover-history-delete {
  color: rgba(255, 255, 255, 0.35);
}

html.dark .agent-chat-popover-history-delete:hover {
  color: #f87171;
  background: rgba(248, 113, 113, 0.15);
}
</style>

<!-- Popover 挂载到 body，需非 scoped 覆盖 Naive 的 n-popover-shared 默认底板与阴影 -->
<style>
.n-popover.n-popover--raw.n-popover-shared {
  box-shadow: none !important;
  background: transparent !important;
}
</style>
