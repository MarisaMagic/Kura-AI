<template>
  <div class="layout-sider-history" :class="{ 'layout-sider-history--collapsed': collapsed }">
    <template v-if="!collapsed">
      <div class="layout-sider-history-label layout-sider-history-label--row">
        <span>{{ $t('views.agents.chat_sidebar_section_history') }}</span>
        <span v-if="refreshing" class="layout-sider-history-label-spin-wrap">
          <n-spin size="small" class="layout-sider-history-label-spin" />
        </span>
      </div>
      <div class="layout-sider-history-scroll" @scroll.passive="onScroll">
        <div v-if="initialLoading" class="layout-sider-history-placeholder">
          {{ $t('views.agents.chat_sidebar_loading') }}
        </div>
        <div
          v-else-if="sidebarLoadError"
          class="layout-sider-history-placeholder layout-sider-history-placeholder--muted"
        >
          {{ $t('views.agents.chat_error_load_agent') }}
        </div>
        <template v-else>
            <div
              v-if="sidebarSessions.length === 0"
              class="layout-sider-history-placeholder layout-sider-history-placeholder--muted"
            >
              {{ $t('views.agents.chat_sidebar_empty') }}
            </div>
            <template v-else>
              <div
                v-for="s in sidebarSessions"
                :key="sessionRowKey(s)"
                class="layout-sider-history-session"
                :class="{
                  'layout-sider-history-session--active': isSessionActive(s),
                  'layout-sider-history-session--enter': isNewlyAdded(s),
                  'layout-sider-history-session--menu-open': menuOpenKey === sessionRowKey(s),
                }"
              >
                <button
                  type="button"
                  class="layout-sider-history-session-main"
                  :title="displayTitle(s)"
                  @click="openSession(s)"
                >
                  <span class="layout-sider-history-session-title">{{ displayTitle(s) }}</span>
                  <span v-if="s.agent_name" class="layout-sider-history-session-meta">{{ s.agent_name }}</span>
                </button>
                <n-dropdown
                  trigger="click"
                  placement="bottom-end"
                  :options="sessionMenuOptions"
                  @update:show="(show) => onMenuDropdownShow(show, s)"
                  @select="(key) => onMenuSelect(key, s)"
                >
                <n-button
                  quaternary
                  circle
                  size="tiny"
                  class="layout-sider-history-more"
                  :aria-label="$t('views.agents.chat_popover_history_delete_aria')"
                  @click.stop
                >
                  <TheIcon icon="mdi:dots-horizontal" :size="18" />
                </n-button>
              </n-dropdown>
            </div>
            <div v-if="sidebarLoadingMore" class="layout-sider-history-footer-hint">
              <n-spin size="small" />
              <span>{{ $t('views.agents.chat_sidebar_load_more') }}</span>
            </div>
          </template>
        </template>
      </div>
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
          :aria-label="$t('views.agents.chat_sidebar_section_history')"
        >
          <TheIcon icon="mdi:history" :size="22" />
        </button>
      </template>
      <div class="layout-sider-history-popover">
        <div class="layout-sider-history-label layout-sider-history-label--row">
          <span>{{ $t('views.agents.chat_sidebar_section_history') }}</span>
          <span v-if="refreshing" class="layout-sider-history-label-spin-wrap">
            <n-spin size="small" class="layout-sider-history-label-spin" />
          </span>
        </div>
        <div
          class="layout-sider-history-scroll layout-sider-history-scroll--popover"
          @scroll.passive="onScroll"
        >
          <div v-if="initialLoading" class="layout-sider-history-placeholder">
            {{ $t('views.agents.chat_sidebar_loading') }}
          </div>
          <div
            v-else-if="sidebarLoadError"
            class="layout-sider-history-placeholder layout-sider-history-placeholder--muted"
          >
            {{ $t('views.agents.chat_error_load_agent') }}
          </div>
          <template v-else>
            <div
              v-if="sidebarSessions.length === 0"
              class="layout-sider-history-placeholder layout-sider-history-placeholder--muted"
            >
              {{ $t('views.agents.chat_sidebar_empty') }}
            </div>
            <template v-else>
              <div
                v-for="s in sidebarSessions"
                :key="'p-' + sessionRowKey(s)"
                class="layout-sider-history-session"
                :class="{
                  'layout-sider-history-session--active': isSessionActive(s),
                  'layout-sider-history-session--enter': isNewlyAdded(s),
                  'layout-sider-history-session--menu-open': menuOpenKey === sessionRowKey(s),
                }"
              >
                <button
                  type="button"
                  class="layout-sider-history-session-main"
                  :title="displayTitle(s)"
                  @click="openSession(s)"
                >
                  <span class="layout-sider-history-session-title">{{ displayTitle(s) }}</span>
                  <span v-if="s.agent_name" class="layout-sider-history-session-meta">{{ s.agent_name }}</span>
                </button>
                <n-dropdown
                  trigger="click"
                  placement="bottom-end"
                  :options="sessionMenuOptions"
                  @update:show="(show) => onMenuDropdownShow(show, s)"
                  @select="(key) => onMenuSelect(key, s)"
                >
                  <n-button
                    quaternary
                    circle
                    size="tiny"
                    class="layout-sider-history-more"
                    :aria-label="$t('views.agents.chat_popover_history_delete_aria')"
                    @click.stop
                  >
                    <TheIcon icon="mdi:dots-horizontal" :size="18" />
                  </n-button>
                </n-dropdown>
              </div>
              <div v-if="sidebarLoadingMore" class="layout-sider-history-footer-hint">
                <n-spin size="small" />
                <span>{{ $t('views.agents.chat_sidebar_load_more') }}</span>
              </div>
            </template>
          </template>
        </div>
      </div>
    </n-popover>
  </div>
</template>

<script setup>
import { computed, ref, shallowRef, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter } from 'vue-router'
import { NButton, NDropdown, NPopover, NSpin } from 'naive-ui'
import TheIcon from '@/components/icon/TheIcon.vue'
import api from '@/api'
import { getToken } from '@/utils'
import { useAgentSidebarStore, useAppStore, useUserStore } from '@/store'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const appStore = useAppStore()
const agentSidebarStore = useAgentSidebarStore()
const userStore = useUserStore()

const collapsed = computed(() => appStore.collapsed)

/** 折叠侧栏 hover 弹窗：鼠标离开触发器后延迟关闭，便于移入弹窗（Naive duration 语义） */
const SIDEBAR_POPOVER_HIDE_DELAY_MS = 300

const SIDEBAR_PAGE_SIZE = 30

const sidebarSessions = ref([])
/** 首次无数据时的全屏加载 */
const initialLoading = ref(false)
/** 已有列表时的静默刷新（标题旁小菊花） */
const refreshing = ref(false)
const sidebarLoadingMore = ref(false)
const sidebarLoadError = ref(false)
const sidebarHasMore = ref(true)
/** 本次刷新相对上一帧新增的会话 key，用于进入动画（首屏不标记） */
const newlyAddedKeys = shallowRef(new Set())
/** 下拉打开时保持「更多」按钮可见（菜单在 body 内时行可能失去 hover） */
const menuOpenKey = ref('')

const sessionMenuOptions = computed(() => [
  { label: t('views.agents.chat_sidebar_delete'), key: 'delete' },
])

function sessionRowKey(s) {
  return `${s.agent_id}-${s.session_id}`
}

function onMenuDropdownShow(show, s) {
  const k = sessionRowKey(s)
  if (show) {
    menuOpenKey.value = k
  } else if (menuOpenKey.value === k) {
    menuOpenKey.value = ''
  }
}

function isNewlyAdded(s) {
  return newlyAddedKeys.value.has(sessionRowKey(s))
}

function isSessionActive(s) {
  if (route.name !== 'AgentChat') return false
  const q = route.query.session
  if (typeof q !== 'string' || q !== s.session_id) return false
  return Number(route.params.agentId) === Number(s.agent_id)
}

function displayTitle(s) {
  const p = (s.last_user_preview || '').trim()
  if (p) return p
  return t('views.agents.chat_popover_history_no_title')
}

async function loadSessions(reset = false) {
  if (!getToken()) {
    sidebarSessions.value = []
    initialLoading.value = false
    refreshing.value = false
    newlyAddedKeys.value = new Set()
    return
  }

  let hadDataBeforeFetch = false
  if (reset) {
    sidebarHasMore.value = true
    sidebarLoadError.value = false
    hadDataBeforeFetch = sidebarSessions.value.length > 0
    if (hadDataBeforeFetch) {
      refreshing.value = true
    } else {
      initialLoading.value = true
    }
  } else if (!sidebarHasMore.value) {
    return
  }

  const offset = reset ? 0 : sidebarSessions.value.length
  if (!reset) {
    sidebarLoadingMore.value = true
  }

  try {
    const res = await api.getAgentChatSessionsAll({
      limit: SIDEBAR_PAGE_SIZE,
      offset,
    })
    const rows = res.data?.sessions || []
    const hasMore = res.data?.has_more ?? false
    if (reset) {
      const prevKeys = new Set(sidebarSessions.value.map((x) => sessionRowKey(x)))
      sidebarSessions.value = rows
      if (prevKeys.size > 0) {
        const added = new Set()
        for (const s of rows) {
          const k = sessionRowKey(s)
          if (!prevKeys.has(k)) added.add(k)
        }
        newlyAddedKeys.value = new Set(added)
        window.setTimeout(() => {
          newlyAddedKeys.value = new Set()
        }, 350)
      }
    } else {
      sidebarSessions.value = [...sidebarSessions.value, ...rows]
    }
    sidebarHasMore.value = hasMore
  } catch {
    if (reset && !hadDataBeforeFetch) {
      sidebarLoadError.value = true
    }
  } finally {
    initialLoading.value = false
    refreshing.value = false
    sidebarLoadingMore.value = false
  }
}

function onScroll(e) {
  const el = e.target
  if (
    !el ||
    sidebarLoadingMore.value ||
    initialLoading.value ||
    refreshing.value ||
    !sidebarHasMore.value
  ) {
    return
  }
  const threshold = 72
  if (el.scrollTop + el.clientHeight >= el.scrollHeight - threshold) {
    loadSessions(false)
  }
}

async function openSession(s) {
  if (!s?.agent_id || !s?.session_id) return
  const aid = Number(s.agent_id)
  if (!Number.isFinite(aid)) return
  if (isSessionActive(s)) return
  await router.push({
    name: 'AgentChat',
    params: { agentId: String(aid) },
    query: { session: s.session_id },
  })
}

async function onMenuSelect(key, s) {
  if (key !== 'delete') return
  if (!window.confirm(t('views.agents.chat_popover_history_delete_confirm'))) return
  const aid = s?.agent_id
  const sid = s?.session_id
  if (aid == null || !sid) return
  try {
    await api.deleteAgentChatSession({ agent_id: aid, session_id: sid })
    window.$message?.success(t('views.agents.chat_popover_history_delete_ok'))
    await loadSessions(true)
    if (
      route.name === 'AgentChat' &&
      route.query.session === sid &&
      Number(route.params.agentId) === Number(aid)
    ) {
      await router.replace({
        name: 'AgentChat',
        params: { agentId: String(aid) },
        query: { new: '1' },
      })
    }
  } catch {
    window.$message?.error(t('views.agents.chat_popover_history_delete_fail'))
  }
}

watch(
  () => userStore.userInfo?.id,
  (id) => {
    if (!getToken()) {
      sidebarSessions.value = []
      initialLoading.value = false
      refreshing.value = false
      newlyAddedKeys.value = new Set()
      return
    }
    if (id) loadSessions(true)
  },
  { immediate: true }
)

watch(
  () => agentSidebarStore.refreshTick,
  () => {
    loadSessions(true)
  }
)
</script>

<style scoped>
.layout-sider-history {
  padding: 8px 10px 12px;
}

.layout-sider-history--collapsed {
  padding: 6px 0 8px;
  display: flex;
  justify-content: center;
  align-items: center;
}

.layout-sider-history-label {
  font-size: 12px;
  color: #94a3b8;
  margin-bottom: 8px;
  padding-left: 4px;
}

.layout-sider-history-label--row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  padding-right: 4px;
}

.layout-sider-history-label-spin-wrap {
  flex-shrink: 0;
  width: 18px;
  height: 18px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

.layout-sider-history-label-spin {
  transform: scale(0.72);
  transform-origin: center center;
}

.layout-sider-history-label-spin :deep(.n-spin-body) {
  min-height: 0 !important;
  padding: 0 !important;
}

html.dark .layout-sider-history-label {
  color: rgba(255, 255, 255, 0.38);
}

.layout-sider-history-scroll {
  max-height: 280px;
  overflow-y: auto;
  padding-right: 2px;
  scrollbar-width: thin;
}

.layout-sider-history-scroll--popover {
  max-height: min(280px, 50vh);
}

.layout-sider-history-placeholder {
  font-size: 13px;
  color: #64748b;
  padding: 8px 6px;
}

.layout-sider-history-placeholder--muted {
  color: #94a3b8;
}

html.dark .layout-sider-history-placeholder {
  color: rgba(255, 255, 255, 0.45);
}

.layout-sider-history-session {
  display: flex;
  align-items: center;
  gap: 2px;
  margin-bottom: 4px;
  border-radius: 8px;
  background: transparent;
}

.layout-sider-history-session--enter {
  animation: hist-session-enter 0.25s ease forwards;
}

@keyframes hist-session-enter {
  from {
    opacity: 0;
    transform: translateY(-6px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

.layout-sider-history-session--active {
  background: rgba(148, 163, 184, 0.22);
}

html.dark .layout-sider-history-session--active {
  background: rgba(255, 255, 255, 0.1);
}

.layout-sider-history-session--active .layout-sider-history-session-title,
.layout-sider-history-session--active .layout-sider-history-session-meta {
  font-weight: 600;
}

.layout-sider-history-session-main {
  flex: 1;
  min-width: 0;
  margin: 0;
  padding: 8px 6px 8px 8px;
  border: none;
  border-radius: 8px;
  background: transparent;
  cursor: pointer;
  text-align: left;
  color: inherit;
  font: inherit;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
}

.layout-sider-history-session-main:hover {
  background: rgba(148, 163, 184, 0.12);
}

html.dark .layout-sider-history-session-main:hover {
  background: rgba(255, 255, 255, 0.06);
}

.layout-sider-history-session-title {
  font-size: 13px;
  line-height: 1.35;
  overflow: hidden;
  text-overflow: ellipsis;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  word-break: break-word;
  width: 100%;
}

.layout-sider-history-session-meta {
  font-size: 11px;
  line-height: 1.2;
  color: #94a3b8;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 100%;
}

html.dark .layout-sider-history-session-meta {
  color: rgba(255, 255, 255, 0.38);
}

.layout-sider-history-more {
  flex-shrink: 0;
  opacity: 0;
  transition: opacity 0.15s ease;
}

.layout-sider-history-session:hover .layout-sider-history-more,
.layout-sider-history-session:focus-within .layout-sider-history-more,
.layout-sider-history-session--menu-open .layout-sider-history-more {
  opacity: 0.65;
}

.layout-sider-history-footer-hint {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 10px 0;
  font-size: 12px;
  color: #94a3b8;
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

.layout-sider-history-popover {
  width: 240px;
  max-width: min(240px, calc(100vw - 48px));
  padding: 10px 10px 12px;
  box-sizing: border-box;
  background: #ffffff;
  border-radius: 10px;
  border: 1px solid #e2e8f0;
  box-shadow: 0 8px 24px rgba(15, 23, 42, 0.12);
}

html.dark .layout-sider-history-popover {
  background: #18181c;
  border-color: rgba(255, 255, 255, 0.12);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45);
}
</style>
