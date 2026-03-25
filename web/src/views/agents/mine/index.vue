<template>
  <AppPage :show-footer="false">
    <header class="agent-page-header">
      <h1 class="agent-page-title">{{ $t('views.agents.title_mine_agents') }}</h1>
    </header>
    <n-spin :show="loading">
      <div v-if="!list.length && !loading" class="empty-tip">
        {{ $t('views.agents.text_empty_agents') }}
      </div>
      <div v-else class="agent-grid">
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
            <div class="agent-card-main" flex gap-4>
              <n-avatar round :size="56" :src="item.avatar_url" object-fit="cover" />
              <div min-w-0 flex-1>
                <div pr-14 text-16 font-semibold>{{ item.name }}</div>
                <div mt-1 text-13 op-70 line-clamp-2>{{ item.description || $t('views.agents.text_no_description') }}</div>
              </div>
            </div>
          </div>
        </n-card>
      </div>
    </n-spin>
  </AppPage>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { NAvatar, NButton, NCard, NPopconfirm, NSpin } from 'naive-ui'
import AppPage from '@/components/page/AppPage.vue'
import TheIcon from '@/components/icon/TheIcon.vue'
import api from '@/api'

const { t } = useI18n()
const router = useRouter()
const loading = ref(false)
const list = ref([])

async function fetchList() {
  loading.value = true
  try {
    const res = await api.getUserAgentList({ page: 1, page_size: 100 })
    list.value = res.data || []
  } finally {
    loading.value = false
  }
}

function goEdit(id) {
  router.push({ path: '/agents/create', query: { id: String(id) } })
}

async function handleDelete(id) {
  await api.deleteUserAgent({ agent_id: id })
  $message.success(t('views.agents.msg_delete_ok'))
  await fetchList()
}

onMounted(fetchList)
</script>

<style scoped>
/* 与创建页一致：html 根为 4px，标题用 px */
.agent-page-header {
  margin-bottom: 20px;
  padding-bottom: 16px;
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

.empty-tip {
  padding: 40px 0;
  text-align: center;
  font-size: 15px;
  opacity: 0.65;
}

.agent-grid {
  display: grid;
  gap: 16px;
  grid-template-columns: 1fr;
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
  transition:
    box-shadow 0.2s ease,
    border-color 0.2s ease;
}

.agent-card:hover {
  box-shadow: 0 10px 28px rgba(15, 23, 42, 0.12);
}

html.dark .agent-card:hover {
  box-shadow: 0 10px 32px rgba(0, 0, 0, 0.45);
}

.agent-card-body {
  position: relative;
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
  min-width: 0;
}
</style>
