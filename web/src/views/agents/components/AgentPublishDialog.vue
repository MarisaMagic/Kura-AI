<template>
  <n-modal
    :show="show"
    preset="card"
    :title="
      mode === 'publish'
        ? $t('views.agents.publish_dialog_title_publish')
        : $t('views.agents.publish_dialog_title_manage')
    "
    style="width: 560px"
    :mask-closable="false"
    @update:show="onUpdateShow"
  >
    <div class="publish-dialog-body">
      <n-alert type="warning" :bordered="false" class="publish-warning">
        {{ $t('views.agents.publish_warning') }}
      </n-alert>

      <n-input
        v-model:value="keyword"
        clearable
        :placeholder="$t('views.agents.publish_search_ph')"
        class="publish-search"
      >
        <template #prefix>
          <TheIcon icon="material-symbols:search-rounded" :size="18" />
        </template>
      </n-input>

      <n-spin :show="searching" class="publish-results-spin">
        <div class="publish-results">
          <div
            v-for="u in results"
            :key="u.id"
            class="publish-user-row"
            :class="{ 'is-selected': selectedMap.has(u.id) }"
            role="button"
            tabindex="0"
            @click="toggleUser(u)"
            @keydown.enter.prevent="toggleUser(u)"
          >
            <n-avatar
              round
              :size="32"
              :src="u.avatar"
              object-fit="cover"
              class="publish-user-avatar"
            />
            <div class="publish-user-text">
              <span class="publish-user-name">{{ displayName(u) }}</span>
              <span class="publish-user-email">{{ u.email }}</span>
            </div>
            <TheIcon
              v-if="selectedMap.has(u.id)"
              icon="material-symbols:check-circle-rounded"
              :size="20"
              class="publish-user-check"
            />
          </div>
          <div v-if="!results.length && !searching" class="publish-results-empty">
            {{ $t('views.agents.publish_search_empty') }}
          </div>
        </div>
      </n-spin>

      <div class="publish-selected">
        <div class="publish-selected-header">
          {{ $t('views.agents.publish_selected') }}（{{ selectedMap.size }}）
        </div>
        <div v-if="selectedMap.size" class="publish-selected-chips">
          <span v-for="u in selectedList" :key="u.id" class="publish-chip">
            <n-avatar round :size="20" :src="u.avatar" object-fit="cover" />
            <span class="publish-chip-name">{{ displayName(u) }}</span>
            <button
              type="button"
              class="publish-chip-close"
              :aria-label="$t('common.buttons.delete')"
              @click.stop="toggleUser(u)"
            >
              <TheIcon icon="material-symbols:close-rounded" :size="14" />
            </button>
          </span>
        </div>
        <div v-else class="publish-selected-empty">
          {{ $t('views.agents.publish_selected_empty') }}
        </div>
      </div>
    </div>

    <template #footer>
      <div class="publish-dialog-footer">
        <n-button @click="onUpdateShow(false)">{{ $t('views.agents.mcp_cancel') }}</n-button>
        <n-button
          type="primary"
          :loading="saving"
          :disabled="mode === 'publish' && !selectedMap.size"
          @click="onConfirm"
        >
          {{
            mode === 'publish'
              ? $t('views.agents.publish_confirm')
              : $t('views.agents.publish_save')
          }}
        </n-button>
      </div>
    </template>
  </n-modal>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { watchDebounced } from '@vueuse/core'
import { useI18n } from 'vue-i18n'
import { NAlert, NAvatar, NButton, NInput, NModal, NSpin } from 'naive-ui'
import TheIcon from '@/components/icon/TheIcon.vue'
import api from '@/api'

const props = defineProps({
  show: { type: Boolean, default: false },
  agentId: { type: Number, default: null },
  mode: { type: String, default: 'publish' }, // publish | manage
})

const emit = defineEmits(['update:show', 'changed'])

const { t } = useI18n()

const keyword = ref('')
const results = ref([])
const searching = ref(false)
const saving = ref(false)
const selectedMap = ref(new Map())
const initialIds = ref(new Set())

const selectedList = computed(() => [...selectedMap.value.values()])

function displayName(u) {
  return u.alias || u.username || u.email || `#${u.id}`
}

function onUpdateShow(val) {
  emit('update:show', val)
}

function toggleUser(u) {
  const m = new Map(selectedMap.value)
  if (m.has(u.id)) {
    m.delete(u.id)
  } else {
    m.set(u.id, u)
  }
  selectedMap.value = m
}

async function runSearch() {
  searching.value = true
  try {
    const res = await api.searchShareUsers({ q: keyword.value.trim() })
    results.value = res.data || []
  } finally {
    searching.value = false
  }
}

async function loadInitial() {
  keyword.value = ''
  saving.value = false
  selectedMap.value = new Map()
  initialIds.value = new Set()
  if (props.agentId) {
    try {
      const res = await api.getAgentShareList({ agent_id: props.agentId })
      const list = res.data || []
      const m = new Map()
      for (const u of list) m.set(u.id, u)
      selectedMap.value = m
      initialIds.value = new Set(list.map((u) => u.id))
    } catch {
      /* 名单加载失败时按空处理，不阻塞发布操作 */
    }
  }
  await runSearch()
}

async function onConfirm() {
  if (!props.agentId) return
  if (props.mode === 'publish' && !selectedMap.value.size) return
  saving.value = true
  try {
    const currentIds = new Set(selectedMap.value.keys())
    if (props.mode === 'publish') {
      await api.publishUserAgent({ agent_id: props.agentId, user_ids: [...currentIds] })
      $message.success(t('views.agents.msg_publish_ok'))
    } else {
      const added = [...currentIds].filter((id) => !initialIds.value.has(id))
      const removed = [...initialIds.value].filter((id) => !currentIds.has(id))
      if (added.length) {
        await api.addAgentShareUsers({ agent_id: props.agentId, user_ids: added })
      }
      if (removed.length) {
        await api.removeAgentShareUsers({ agent_id: props.agentId, user_ids: removed })
      }
      if (added.length || removed.length) {
        $message.success(t('views.agents.msg_share_updated'))
      }
    }
    emit('changed')
    emit('update:show', false)
  } finally {
    saving.value = false
  }
}

watch(
  () => props.show,
  (val) => {
    if (val) loadInitial()
  }
)

watchDebounced(
  keyword,
  () => {
    if (props.show) runSearch()
  },
  { debounce: 300 }
)
</script>

<style scoped>
.publish-dialog-body {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.publish-warning {
  border-radius: 8px;
}

.publish-search {
  width: 100%;
}

.publish-results {
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-height: 260px;
  overflow-y: auto;
  border: 1px solid var(--n-border-color);
  border-radius: 10px;
  padding: 6px;
}

.publish-user-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  border-radius: 8px;
  cursor: pointer;
  transition: background-color 0.15s ease;
  -webkit-tap-highlight-color: transparent;
}

.publish-user-row:hover {
  background: rgba(32, 128, 240, 0.08);
}

html.dark .publish-user-row:hover {
  background: rgba(255, 255, 255, 0.06);
}

.publish-user-row.is-selected {
  background: rgba(32, 128, 240, 0.12);
}

html.dark .publish-user-row.is-selected {
  background: rgba(32, 128, 240, 0.22);
}

.publish-user-avatar {
  flex-shrink: 0;
}

.publish-user-text {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.publish-user-name {
  font-size: 14px;
  font-weight: 500;
  color: var(--n-text-color);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.publish-user-email {
  font-size: 12px;
  color: var(--n-text-color-3);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.publish-user-check {
  flex-shrink: 0;
  color: var(--n-primary-color);
}

.publish-results-empty {
  padding: 24px 0;
  text-align: center;
  font-size: 13px;
  color: var(--n-text-color-3);
}

.publish-selected-header {
  margin-bottom: 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--n-text-color-2);
}

.publish-selected-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  max-height: 120px;
  overflow-y: auto;
}

.publish-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 6px 3px 3px;
  border-radius: 999px;
  border: 1px solid var(--n-border-color);
  background: var(--n-card-color);
  font-size: 13px;
  color: var(--n-text-color);
}

.publish-chip-name {
  max-width: 10em;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.publish-chip-close {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  padding: 0;
  border: none;
  border-radius: 50%;
  background: transparent;
  color: var(--n-text-color-3);
  cursor: pointer;
  transition: background-color 0.15s ease, color 0.15s ease;
}

.publish-chip-close:hover {
  background: rgba(0, 0, 0, 0.08);
  color: var(--n-text-color);
}

html.dark .publish-chip-close:hover {
  background: rgba(255, 255, 255, 0.12);
}

.publish-selected-empty {
  font-size: 13px;
  color: var(--n-text-color-3);
}

.publish-dialog-footer {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
}
</style>
