<template>
  <AppPage :show-footer="false" class="!p-0">
    <div class="agent-hub-layout">
      <header class="agent-page-header">
        <h1 class="agent-page-title">{{ $t('views.agents.title_agent_hub') }}</h1>
      </header>
      <n-tabs v-model:value="activeTab" type="segment" size="large" class="agent-hub-tabs">
        <n-tab name="mine">{{ $t('views.agents.tab_mine') }}</n-tab>
        <n-tab name="shared">{{ $t('views.agents.tab_shared') }}</n-tab>
      </n-tabs>

      <n-spin v-if="activeTab === 'mine'" :show="loading">
        <div v-if="!list.length && !loading" class="empty-tip">
          {{ $t('views.agents.text_empty_agents') }}
        </div>
        <div class="agent-grid">
          <n-card
            class="agent-card agent-card--create"
            size="small"
            :bordered="true"
            hoverable
            role="button"
            tabindex="0"
            @click="goCreate"
            @keydown.enter.prevent="goCreate"
          >
            <div class="agent-card-create-inner">
              <TheIcon
                icon="material-symbols:add-circle-outline"
                :size="28"
                class="agent-card-create-icon"
              />
              <span class="agent-card-create-text">{{ $t('views.agents.card_create_agent') }}</span>
            </div>
          </n-card>
          <n-card
            v-for="item in list"
            :key="item.id"
            size="small"
            class="agent-card"
            :bordered="true"
          >
            <div class="agent-card-body">
              <div class="agent-card-actions" @click.stop>
                <n-button
                  quaternary
                  circle
                  size="small"
                  :title="$t('views.agents.kb_configure')"
                  :aria-label="$t('views.agents.kb_configure')"
                  @click="goKnowledgeBase(item.id)"
                >
                  <TheIcon icon="material-symbols:menu-book-outline" :size="20" />
                </n-button>
                <n-button
                  quaternary
                  circle
                  size="small"
                  :title="$t('common.buttons.edit')"
                  :aria-label="$t('common.buttons.edit')"
                  @click="goEdit(item.id)"
                >
                  <TheIcon icon="material-symbols:edit-outline" :size="20" />
                </n-button>
                <n-popconfirm @positive-click="() => handleDelete(item.id)">
                  <template #trigger>
                    <n-button
                      quaternary
                      circle
                      size="small"
                      type="error"
                      :title="$t('common.buttons.delete')"
                      :aria-label="$t('common.buttons.delete')"
                    >
                      <TheIcon icon="material-symbols:delete-outline" :size="20" />
                    </n-button>
                  </template>
                  {{ $t('views.agents.confirm_delete') }}
                </n-popconfirm>
              </div>
              <div
                class="agent-card-main"
                role="button"
                tabindex="0"
                @click="goChat(item.id)"
                @keydown.enter.prevent="goChat(item.id)"
              >
                <n-avatar
                  round
                  :size="48"
                  :src="item.avatar_url"
                  object-fit="cover"
                  class="agent-card-avatar"
                />
                <div class="agent-card-text">
                  <div class="agent-card-title">
                    <span class="agent-card-title-text">{{ item.name }}</span>
                    <n-tag
                      v-if="item.is_published"
                      size="tiny"
                      type="success"
                      :bordered="false"
                      class="agent-card-published-tag"
                    >
                      {{ $t('views.agents.shared_count', { n: item.shared_count || 0 }) }}
                    </n-tag>
                  </div>
                  <div class="agent-card-desc">
                    {{ item.description || $t('views.agents.text_no_description') }}
                  </div>
                </div>
              </div>
            </div>
          </n-card>
        </div>
      </n-spin>

      <n-spin v-else :show="sharedLoading">
        <div v-if="!sharedList.length && !sharedLoading" class="empty-tip">
          {{ $t('views.agents.shared_empty') }}
        </div>
        <div class="agent-grid">
          <n-card
            v-for="item in sharedList"
            :key="item.id"
            size="small"
            class="agent-card"
            :bordered="true"
          >
            <div class="agent-card-body">
              <div
                class="agent-card-main"
                role="button"
                tabindex="0"
                @click="goChat(item.id)"
                @keydown.enter.prevent="goChat(item.id)"
              >
                <n-avatar
                  round
                  :size="48"
                  :src="item.avatar_url"
                  object-fit="cover"
                  class="agent-card-avatar"
                />
                <div class="agent-card-text">
                  <div class="agent-card-title">
                    <span class="agent-card-title-text">{{ item.name }}</span>
                  </div>
                  <div class="agent-card-desc">
                    {{ item.description || $t('views.agents.text_no_description') }}
                  </div>
                  <div class="agent-card-owner">
                    <TheIcon icon="mdi:account-outline" :size="14" />
                    <span class="agent-card-owner-name">{{ item.owner_username || '—' }}</span>
                  </div>
                </div>
              </div>
            </div>
          </n-card>
        </div>
      </n-spin>
    </div>
  </AppPage>
</template>

<script setup>
import { onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { NAvatar, NButton, NCard, NPopconfirm, NSpin, NTab, NTabs, NTag } from 'naive-ui'
import AppPage from '@/components/page/AppPage.vue'
import TheIcon from '@/components/icon/TheIcon.vue'
import api from '@/api'

const { t } = useI18n()
const router = useRouter()
const activeTab = ref('mine')
const loading = ref(false)
const list = ref([])
const sharedLoading = ref(false)
const sharedList = ref([])

async function fetchList() {
  loading.value = true
  try {
    const res = await api.getUserAgentList({ page: 1, page_size: 100 })
    list.value = res.data || []
  } finally {
    loading.value = false
  }
}

async function fetchSharedList() {
  sharedLoading.value = true
  try {
    const res = await api.getSharedAgentList({ page: 1, page_size: 100 })
    sharedList.value = res.data || []
  } finally {
    sharedLoading.value = false
  }
}

function goCreate() {
  router.push({ name: 'AgentCreate' })
}

function goEdit(id) {
  router.push({ name: 'AgentEdit', params: { id: String(id) } })
}

function goKnowledgeBase(id) {
  router.push({ name: 'AgentKnowledgeBase', params: { agentId: String(id) } })
}

function goChat(id) {
  router.push({ name: 'AgentChat', params: { agentId: String(id) }, query: { new: '1' } })
}

async function handleDelete(id) {
  await api.deleteUserAgent({ agent_id: id })
  $message.success(t('views.agents.msg_delete_ok'))
  await fetchList()
}

watch(activeTab, (tab) => {
  if (tab === 'shared') fetchSharedList()
})

onMounted(fetchList)
</script>

<style scoped>
.agent-hub-layout {
  box-sizing: border-box;
  min-height: 100%;
  padding: 28px 32px 40px;
}

@media (max-width: 639px) {
  .agent-hub-layout {
    padding: 20px 18px 28px;
  }
}

.agent-page-header {
  margin-bottom: 20px;
  padding-bottom: 20px;
  border-bottom: 1px solid var(--n-divider-color);
}

.agent-page-title {
  margin: 0;
  font-size: clamp(26px, 2.8vw, 32px);
  font-weight: 700;
  line-height: 1.25;
  letter-spacing: 0.04em;
  color: var(--n-text-color);
}

.agent-hub-tabs {
  margin-bottom: 18px;
}

.empty-tip {
  padding: 16px 0 24px;
  text-align: center;
  font-size: 15px;
  opacity: 0.65;
}

.agent-grid {
  display: grid;
  gap: 20px;
  grid-template-columns: 1fr;
  align-items: stretch;
}

@media (min-width: 640px) {
  .agent-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (min-width: 1024px) {
  .agent-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}

.agent-card {
  border-radius: 10px;
  height: 100%;
  display: flex;
  flex-direction: column;
  transition: box-shadow 0.2s ease, border-color 0.2s ease;
}

.agent-card :deep(.n-card) {
  height: 100%;
  display: flex;
  flex-direction: column;
}

.agent-card :deep(.n-card__content) {
  flex: 1;
  display: flex;
  flex-direction: column;
  box-sizing: border-box;
  height: 132px;
  min-height: 132px;
  padding: 14px 18px;
}

.agent-card:hover {
  box-shadow: 0 10px 28px rgba(15, 23, 42, 0.12);
}

html.dark .agent-card:hover {
  box-shadow: 0 10px 32px rgba(0, 0, 0, 0.45);
}

.agent-card--create {
  cursor: pointer;
  border-style: dashed;
  border-width: 1px;
  -webkit-tap-highlight-color: transparent;
}

.agent-card--create:hover {
  border-color: var(--n-primary-color);
}

.agent-card--create :deep(.n-card__content) {
  justify-content: center;
}

.agent-card-create-inner {
  display: flex;
  flex-direction: row;
  align-items: center;
  justify-content: center;
  gap: 10px;
  width: 100%;
  height: 100%;
  min-height: 0;
  padding: 2px 6px;
  box-sizing: border-box;
}

.agent-card-create-icon {
  flex-shrink: 0;
  color: var(--n-primary-color);
  opacity: 0.9;
}

.agent-card-create-text {
  font-size: 15px;
  font-weight: 600;
  color: var(--n-text-color-2);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.agent-card-body {
  position: relative;
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
  height: 100%;
}

.agent-card-actions {
  position: absolute;
  top: 0;
  right: 0;
  z-index: 1;
  display: flex;
  align-items: center;
  gap: 2px;
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.18s ease;
}

.agent-card:hover .agent-card-actions,
.agent-card:focus-within .agent-card-actions {
  opacity: 1;
  pointer-events: auto;
}

.agent-card-main {
  display: flex;
  flex-direction: row;
  align-items: flex-start;
  gap: 12px;
  flex: 1;
  min-height: 0;
  min-width: 0;
  cursor: pointer;
  border-radius: 8px;
  outline: none;
  -webkit-tap-highlight-color: transparent;
}

.agent-card-main:focus-visible {
  box-shadow: 0 0 0 2px var(--n-primary-color-suppl);
}

.agent-card-avatar {
  flex-shrink: 0;
}

.agent-card-text {
  flex: 1;
  min-width: 0;
  padding-right: 40px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.agent-card-title {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  font-size: 16px;
  font-weight: 600;
  line-height: 1.35;
  color: var(--n-text-color);
}

.agent-card-title-text {
  flex-shrink: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.agent-card-published-tag {
  flex-shrink: 0;
}

.agent-card-desc {
  font-size: 13px;
  line-height: 1.45;
  color: var(--n-text-color-2);
  overflow: hidden;
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  word-break: break-word;
}

.agent-card-owner {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  min-width: 0;
  font-size: 12.5px;
  color: var(--n-text-color-3);
}

.agent-card-owner-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
