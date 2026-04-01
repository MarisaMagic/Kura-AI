<template>
  <AppPage :show-footer="false" scroll-in-parent class="!p-0">
    <div class="agent-editor-layout">
      <header class="agent-page-header">
        <h1 class="agent-page-title">{{ $t('views.agents.title_edit_agent') }}</h1>
        <div class="agent-page-header-actions">
          <n-button type="primary" :loading="saving" :disabled="pageLoading" @click="handleSubmit">
            {{ $t('views.agents.button_save_config') }}
          </n-button>
        </div>
      </header>

      <n-spin :show="pageLoading" class="agent-edit-spin">
        <AgentFormFields
          v-show="!pageLoading"
          ref="formFieldsRef"
          :form="form"
          :rules="rules"
          :has-saved-api-key="hasSavedApiKey"
          :dialogue-open="dialogueOpen"
          :advanced-open="advancedOpen"
          :avatar-preview="avatarPreview"
          :avatar-upload-key="avatarUploadKey"
          :temperature-slider-label="temperatureSliderLabel"
          @update:dialogue-open="dialogueOpen = $event"
          @update:advanced-open="advancedOpen = $event"
          @avatar-change="onAvatarFileChange"
        />
      </n-spin>
    </div>
  </AppPage>
</template>

<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { NButton, NSpin } from 'naive-ui'
import AppPage from '@/components/page/AppPage.vue'
import AgentFormFields from '@/views/agents/components/AgentFormFields.vue'
import api from '@/api'
import {
  emptyForm,
  DEFAULT_AVATAR,
  buildAgentFormRules,
} from '@/views/agents/composables/agentFormCommon.js'

const { t } = useI18n()
const route = useRoute()

const formFieldsRef = ref(null)
const saving = ref(false)
const pageLoading = ref(true)
const agentId = ref(null)
const pendingAvatarFile = ref(null)
const serverAvatarUrl = ref('')
const avatarUploadKey = ref(0)
const dialogueOpen = ref(false)
const advancedOpen = ref(false)
const hasSavedApiKey = ref(false)

const form = ref(emptyForm())
const rules = computed(() => buildAgentFormRules(t, { isEdit: true }))

const avatarPreview = computed(() => {
  if (serverAvatarUrl.value) return serverAvatarUrl.value
  return DEFAULT_AVATAR
})

const temperatureSliderLabel = computed(() => {
  const v = form.value.temperature
  const n = typeof v === 'number' && !Number.isNaN(v) ? v : 0.1
  return n.toFixed(2)
})

function revokeAvatarBlobIfAny() {
  if (serverAvatarUrl.value?.startsWith('blob:')) {
    URL.revokeObjectURL(serverAvatarUrl.value)
  }
}

async function loadAgent(id) {
  pageLoading.value = true
  try {
    const res = await api.getUserAgent({ agent_id: id })
    const d = res.data
    agentId.value = d.id
    hasSavedApiKey.value = !!d.has_api_key
    form.value = {
      name: d.name,
      model_name: d.model_name,
      api_key: '',
      description: d.description || '',
      system_prompt: d.system_prompt || '',
      enable_web: !!d.enable_web,
      enable_code: !!d.enable_code,
      opening_message: d.opening_message || '',
      temperature: d.temperature ?? 0.1,
    }
    revokeAvatarBlobIfAny()
    serverAvatarUrl.value = d.avatar_url || ''
    pendingAvatarFile.value = null
  } catch {
    agentId.value = null
    hasSavedApiKey.value = false
    form.value = emptyForm()
    serverAvatarUrl.value = ''
  } finally {
    pageLoading.value = false
    nextTick(() => {
      formFieldsRef.value?.restoreValidation?.()
    })
  }
}

function onAvatarFileChange(options) {
  const f = options.fileList?.[0]?.file
  const list = options.fileList || []
  if (!f && list.length === 0 && pendingAvatarFile.value) {
    return
  }
  if (f) {
    revokeAvatarBlobIfAny()
    pendingAvatarFile.value = f
    serverAvatarUrl.value = URL.createObjectURL(f)
    avatarUploadKey.value += 1
    return
  }
  pendingAvatarFile.value = null
  revokeAvatarBlobIfAny()
  if (agentId.value) {
    loadAgent(agentId.value)
  } else {
    serverAvatarUrl.value = ''
  }
}

async function handleSubmit() {
  await formFieldsRef.value?.validate()
  if (!agentId.value) return
  saving.value = true
  try {
    const payload = { ...form.value, id: agentId.value }
    await api.updateUserAgent(payload)
    $message.success(t('views.agents.msg_update_ok'))
    if (pendingAvatarFile.value) {
      const fd = new FormData()
      fd.append('file', pendingAvatarFile.value)
      const up = await api.uploadUserAgentAvatar(agentId.value, fd)
      if (up.data?.avatar_url) {
        serverAvatarUrl.value = up.data.avatar_url
      }
      pendingAvatarFile.value = null
    }
    await loadAgent(agentId.value)
  } finally {
    saving.value = false
  }
}

watch(
  () => route.params.id,
  async (raw) => {
    const id = raw != null && raw !== '' ? Number(raw) : null
    if (id != null && !Number.isNaN(id)) {
      await loadAgent(id)
    } else {
      pageLoading.value = false
    }
  },
  { immediate: true }
)
</script>

<style scoped>
.agent-editor-layout {
  box-sizing: border-box;
  width: 100%;
  padding: 28px 32px 40px;
}

@media (max-width: 639px) {
  .agent-editor-layout {
    padding: 20px 18px 28px;
  }
}

.agent-page-header {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 24px;
  padding-bottom: 20px;
  border-bottom: 1px solid var(--n-divider-color);
}

.agent-page-title {
  flex: 1;
  min-width: 0;
  margin: 0;
  font-size: clamp(26px, 2.8vw, 32px);
  font-weight: 700;
  line-height: 1.25;
  letter-spacing: 0.04em;
  color: var(--n-text-color);
}

.agent-page-header-actions {
  display: flex;
  flex-shrink: 0;
  align-items: center;
  gap: 12px;
}

.agent-edit-spin {
  display: block;
  width: 100%;
  min-height: 200px;
}

.agent-edit-spin :deep(.n-spin-content) {
  display: block;
  width: 100%;
}
</style>
