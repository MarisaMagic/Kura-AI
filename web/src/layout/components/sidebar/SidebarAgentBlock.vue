<template>
  <div class="layout-sider-agent" :class="{ 'layout-sider-agent--collapsed': collapsed }">
    <template v-if="!collapsed">
      <div class="layout-sider-agent-label">
        {{ $t('views.agents.chat_sidebar_section_agents') }}
      </div>

      <div v-if="recentAgentsStore.loading" class="layout-sider-agent-hint">
        {{ $t('views.agents.chat_sidebar_loading') }}
      </div>
      <template v-else>
        <button
          v-for="a in displayAgents"
          :key="a.id"
          type="button"
          class="layout-sider-agent-row"
          :class="{ 'layout-sider-agent-row--current': isCurrentAgent(a.id) }"
          :disabled="isCurrentAgent(a.id)"
          @click="goAgentChat(a.id)"
        >
          <n-avatar
            round
            :size="32"
            :src="a.avatar_url || DEFAULT_AVATAR"
            object-fit="cover"
            class="layout-sider-agent-avatar"
          />
          <span class="layout-sider-agent-row-text">{{ (a.name || '').trim() || '—' }}</span>
        </button>
        <div
          v-if="displayAgents.length === 0"
          class="layout-sider-agent-hint layout-sider-agent-hint--muted"
        >
          {{ $t('views.agents.chat_sidebar_no_recent_agents') }}
        </div>
      </template>

      <button
        type="button"
        class="layout-sider-agent-row layout-sider-agent-row--ghost"
        @click="goAgentHub"
      >
        <span class="layout-sider-agent-icon-wrap">
          <n-icon :size="20">
            <SvgIcon icon="agent" />
          </n-icon>
        </span>
        <span class="layout-sider-agent-row-text">{{
          $t('views.agents.chat_sidebar_more_agents')
        }}</span>
      </button>
    </template>

    <n-popover
      v-else
      trigger="hover"
      placement="right-start"
      :show-arrow="false"
      raw
      :content-style="{ padding: 0 }"
      to="body"
      :keep-alive-on-hover="true"
      :duration="SIDEBAR_POPOVER_HIDE_DELAY_MS"
      display-directive="if"
    >
      <template #trigger>
        <button
          type="button"
          class="layout-sider-collapsed-trigger"
          :aria-label="$t('views.agents.chat_sidebar_section_agents')"
        >
          <n-icon :size="22">
            <SvgIcon icon="agent" />
          </n-icon>
        </button>
      </template>
      <div class="layout-sider-agent-popover">
        <div class="layout-sider-agent-label">
          {{ $t('views.agents.chat_sidebar_section_agents') }}
        </div>

        <div v-if="recentAgentsStore.loading" class="layout-sider-agent-hint">
          {{ $t('views.agents.chat_sidebar_loading') }}
        </div>
        <template v-else>
          <button
            v-for="a in displayAgents"
            :key="a.id"
            type="button"
            class="layout-sider-agent-row"
            :class="{ 'layout-sider-agent-row--current': isCurrentAgent(a.id) }"
            :disabled="isCurrentAgent(a.id)"
            @click="goAgentChat(a.id)"
          >
            <n-avatar
              round
              :size="32"
              :src="a.avatar_url || DEFAULT_AVATAR"
              object-fit="cover"
              class="layout-sider-agent-avatar"
            />
            <span class="layout-sider-agent-row-text">{{ (a.name || '').trim() || '—' }}</span>
          </button>
          <div
            v-if="displayAgents.length === 0"
            class="layout-sider-agent-hint layout-sider-agent-hint--muted"
          >
            {{ $t('views.agents.chat_sidebar_no_recent_agents') }}
          </div>
        </template>

        <button
          type="button"
          class="layout-sider-agent-row layout-sider-agent-row--ghost"
          @click="goAgentHub"
        >
          <span class="layout-sider-agent-icon-wrap">
            <n-icon :size="20">
              <SvgIcon icon="agent" />
            </n-icon>
          </span>
          <span class="layout-sider-agent-row-text">{{
            $t('views.agents.chat_sidebar_more_agents')
          }}</span>
        </button>
      </div>
    </n-popover>
  </div>
</template>

<script setup>
import { computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NAvatar, NIcon, NPopover } from 'naive-ui'
import SvgIcon from '@/components/icon/SvgIcon.vue'
import { useAppStore, useRecentAgentsStore, useUserStore } from '@/store'
import { getToken } from '@/utils'
import { DEFAULT_AVATAR } from '@/views/agents/composables/agentFormCommon.js'

const route = useRoute()
const router = useRouter()
const appStore = useAppStore()
const recentAgentsStore = useRecentAgentsStore()
const userStore = useUserStore()

const collapsed = computed(() => appStore.collapsed)

/** 折叠侧栏 hover 弹窗：鼠标离开触发器后延迟关闭，便于移入弹窗（Naive duration 语义） */
const SIDEBAR_POPOVER_HIDE_DELAY_MS = 300

const displayAgents = computed(() => (recentAgentsStore.list || []).slice(0, 3))

function isCurrentAgent(id) {
  if (route.name !== 'AgentChat') return false
  return Number(route.params.agentId) === Number(id)
}

function goAgentChat(agentId) {
  if (!agentId) return
  router.push({ name: 'AgentChat', params: { agentId: String(agentId) } })
}

function goAgentHub() {
  router.push('/agent-hub')
}

function loadRecent() {
  if (getToken()) recentAgentsStore.fetch()
}

onMounted(loadRecent)

watch(
  () => userStore.userInfo?.id,
  (id) => {
    if (id) recentAgentsStore.fetch()
  }
)
</script>

<style scoped>
.layout-sider-agent {
  padding: 8px 10px 12px;
}

.layout-sider-agent--collapsed {
  padding: 6px 0 8px;
  display: flex;
  justify-content: center;
  align-items: center;
}

.layout-sider-agent-label {
  font-size: 12px;
  color: #94a3b8;
  margin-bottom: 8px;
  padding-left: 4px;
}

html.dark .layout-sider-agent-label {
  color: rgba(255, 255, 255, 0.38);
}

.layout-sider-agent-hint {
  font-size: 13px;
  color: #64748b;
  padding: 6px 4px 10px;
}

.layout-sider-agent-hint--muted {
  color: #94a3b8;
}

.layout-sider-agent-row {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  margin: 0 0 6px;
  padding: 8px 10px;
  border-radius: 10px;
  background: #ffffff;
  cursor: pointer;
  text-align: left;
  color: #0f172a;
  font-size: 14px;
  transition: background 0.15s ease;
  box-sizing: border-box;
  border: 1px solid #e2e8f0;
}

.layout-sider-agent-row--current {
  background: #f1f5f9;
  border-color: #e2e8f0;
}

html.dark .layout-sider-agent-row--current {
  background: rgba(255, 255, 255, 0.08);
  border-color: rgba(255, 255, 255, 0.1);
}

.layout-sider-agent-row:hover:not(:disabled) {
  background: #f1f5f9;
}

.layout-sider-agent-row:disabled {
  opacity: 0.85;
  cursor: default;
}

html.dark .layout-sider-agent-row {
  background: rgba(255, 255, 255, 0.04);
  border-color: rgba(255, 255, 255, 0.1);
  color: rgba(255, 255, 255, 0.92);
}

html.dark .layout-sider-agent-row:hover:not(:disabled) {
  background: rgba(255, 255, 255, 0.08);
}

.layout-sider-agent-row--ghost {
  background: transparent;
  border-style: dashed;
}

.layout-sider-agent-row--ghost:hover {
  background: rgba(148, 163, 184, 0.12);
}

.layout-sider-agent-icon-wrap {
  width: 32px;
  height: 32px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 999px;
  background: #e2e8f0;
  color: #475569;
  flex-shrink: 0;
}

html.dark .layout-sider-agent-icon-wrap {
  background: rgba(255, 255, 255, 0.1);
  color: rgba(255, 255, 255, 0.75);
}

.layout-sider-agent-row-text {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.layout-sider-collapsed-trigger {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 40px;
  margin: 0;
  padding: 0;
  border-radius: 10px;
  border: 1px solid #e2e8f0;
  background: #ffffff;
  color: #475569;
  cursor: pointer;
  transition: background 0.15s ease, border-color 0.15s ease;
  box-sizing: border-box;
}

.layout-sider-collapsed-trigger:hover {
  background: #f1f5f9;
  border-color: #cbd5e1;
}

html.dark .layout-sider-collapsed-trigger {
  background: rgba(255, 255, 255, 0.04);
  border-color: rgba(255, 255, 255, 0.1);
  color: rgba(255, 255, 255, 0.75);
}

html.dark .layout-sider-collapsed-trigger:hover {
  background: rgba(255, 255, 255, 0.08);
}

.layout-sider-agent-popover {
  width: 220px;
  max-width: min(220px, calc(100vw - 48px));
  padding: 10px 10px 12px;
  box-sizing: border-box;
  background: #ffffff;
  border-radius: 10px;
  border: 1px solid #e2e8f0;
  box-shadow: 0 8px 24px rgba(15, 23, 42, 0.12);
}

html.dark .layout-sider-agent-popover {
  background: #18181c;
  border-color: rgba(255, 255, 255, 0.12);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45);
}
</style>
