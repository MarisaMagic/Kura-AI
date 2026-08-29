<template>
  <section class="agent-section">
    <header class="section-header">{{ $t('views.agents.section_publish') }}</header>
    <div class="section-body">
      <n-alert type="warning" :bordered="false" class="publish-panel-warning">
        {{ $t('views.agents.publish_warning') }}
      </n-alert>

      <div v-if="!isPublished" class="publish-panel-row">
        <n-button type="primary" :loading="acting" @click="dialogMode = 'publish'; dialogShow = true">
          <template #icon>
            <TheIcon icon="material-symbols:publish-rounded" :size="18" />
          </template>
          {{ $t('views.agents.button_publish_agent') }}
        </n-button>
        <span class="publish-panel-hint">{{ $t('views.agents.publish_hint') }}</span>
      </div>

      <div v-else class="publish-panel-row publish-panel-row--published">
        <div class="publish-shared-info">
          <n-avatar-group :options="avatarOptions" :size="28" :max="6" />
          <span class="publish-shared-count">
            {{ $t('views.agents.shared_count', { n: sharedCount }) }}
          </span>
        </div>
        <div class="publish-panel-actions">
          <n-button secondary type="primary" :loading="acting" @click="dialogMode = 'manage'; dialogShow = true">
            <template #icon>
              <TheIcon icon="material-symbols:group-add-outline-rounded" :size="18" />
            </template>
            {{ $t('views.agents.button_manage_share') }}
          </n-button>
          <n-popconfirm @positive-click="onOffline">
            <template #trigger>
              <n-button secondary type="error" :loading="acting">
                <template #icon>
                  <TheIcon icon="material-symbols:undo-rounded" :size="18" />
                </template>
                {{ $t('views.agents.button_offline_agent') }}
              </n-button>
            </template>
            {{ $t('views.agents.offline_confirm') }}
          </n-popconfirm>
        </div>
      </div>
    </div>

    <AgentPublishDialog
      v-model:show="dialogShow"
      :agent-id="agentId"
      :mode="dialogMode"
      @changed="onChanged"
    />
  </section>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { NAlert, NAvatarGroup, NButton, NPopconfirm } from 'naive-ui'
import TheIcon from '@/components/icon/TheIcon.vue'
import AgentPublishDialog from '@/views/agents/components/AgentPublishDialog.vue'
import api from '@/api'

const props = defineProps({
  agentId: { type: Number, required: true },
  isPublished: { type: Boolean, default: false },
  sharedCount: { type: Number, default: 0 },
})

const emit = defineEmits(['changed'])

const { t } = useI18n()

const dialogShow = ref(false)
const dialogMode = ref('publish')
const acting = ref(false)
const shareUsers = ref([])

const avatarOptions = computed(() =>
  shareUsers.value.map((u) => ({ src: u.avatar, name: u.alias || u.username }))
)

async function loadShares() {
  if (!props.isPublished || !props.agentId) {
    shareUsers.value = []
    return
  }
  try {
    const res = await api.getAgentShareList({ agent_id: props.agentId })
    shareUsers.value = res.data || []
  } catch {
    shareUsers.value = []
  }
}

async function onOffline() {
  acting.value = true
  try {
    await api.offlineUserAgent({ agent_id: props.agentId })
    $message.success(t('views.agents.msg_offline_ok'))
    emit('changed')
  } finally {
    acting.value = false
  }
}

function onChanged() {
  loadShares()
  emit('changed')
}

watch(
  () => [props.isPublished, props.agentId],
  () => loadShares(),
  { immediate: true }
)
</script>

<style scoped>
.agent-section {
  margin-bottom: 20px;
}

.section-header {
  margin-bottom: 14px;
  padding: 8px 12px 8px 12px;
  font-size: 17px;
  font-weight: 600;
  line-height: 1.45;
  letter-spacing: 0.04em;
  color: var(--n-text-color);
  border-left: 4px solid var(--n-primary-color);
  background: #ececef;
  border-radius: 8px;
}

html.dark .section-header {
  background: rgba(255, 255, 255, 0.08);
}

.section-body {
  padding-left: 16px;
  box-sizing: border-box;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.publish-panel-warning {
  border-radius: 8px;
}

.publish-panel-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px 16px;
}

.publish-panel-row--published {
  justify-content: space-between;
}

.publish-panel-hint {
  flex: 1;
  min-width: 200px;
  font-size: 13px;
  line-height: 1.5;
  color: var(--n-text-color-3);
}

.publish-shared-info {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.publish-shared-count {
  font-size: 13px;
  color: var(--n-text-color-2);
  white-space: nowrap;
}

.publish-panel-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
}
</style>
