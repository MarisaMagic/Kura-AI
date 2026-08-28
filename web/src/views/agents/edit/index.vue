<template>
  <AppPage :show-footer="false" scroll-in-parent class="!p-0">
    <div class="agent-editor-split">
      <div class="agent-editor-split__left">
        <div class="agent-editor-layout">
          <div class="agent-editor-form-column">
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
              <AgentMcpServersPanel v-if="agentId" v-show="!pageLoading" :agent-id="agentId" />
            </n-spin>
          </div>
        </div>
      </div>

      <div class="agent-editor-split__right">
        <AgentEditorPreviewPanel
          :form="form"
          :agent-id="agentId"
          :avatar-preview="avatarPreview"
          :creator-name="userStore.name"
          :config-stale="configStale"
          :chat-enabled="chatEnabled"
          @request-save="handleSubmit"
        />
      </div>
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
import AgentEditorPreviewPanel from '@/views/agents/components/AgentEditorPreviewPanel.vue'
import AgentMcpServersPanel from '@/views/agents/components/AgentMcpServersPanel.vue'
import { useUserStore } from '@/store'
import api from '@/api'
import {
  emptyForm,
  DEFAULT_AVATAR,
  buildAgentFormRules,
} from '@/views/agents/composables/agentFormCommon.js'
import {
  pickPreviewConfig,
  previewConfigEqual,
} from '@/views/agents/composables/useAgentConfigDiff.js'

const { t } = useI18n()
const route = useRoute()
const userStore = useUserStore()

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
const savedSnapshot = ref(null)
const pendingAvatarChanged = ref(false)

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

const chatEnabled = computed(() => agentId.value != null && !pageLoading.value)

const configStale = computed(() => {
  if (!savedSnapshot.value) return false
  if (pendingAvatarChanged.value) return true
  if (String(form.value.api_key || '').trim()) return true
  return !previewConfigEqual(pickPreviewConfig(form.value), savedSnapshot.value)
})

function snapshotFromForm(f) {
  return pickPreviewConfig(f)
}

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
      base_url: d.base_url || '',
      api_key: '',
      description: d.description || '',
      system_prompt: d.system_prompt || '',
      supports_vision: !!d.supports_vision,
      is_published: !!d.is_published,
      opening_message: d.opening_message || '',
      temperature: d.temperature ?? 0.1,
    }
    savedSnapshot.value = snapshotFromForm(form.value)
    pendingAvatarChanged.value = false
    revokeAvatarBlobIfAny()
    serverAvatarUrl.value = d.avatar_url || ''
    pendingAvatarFile.value = null
  } catch {
    agentId.value = null
    hasSavedApiKey.value = false
    form.value = emptyForm()
    savedSnapshot.value = null
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
    pendingAvatarChanged.value = true
    serverAvatarUrl.value = URL.createObjectURL(f)
    avatarUploadKey.value += 1
    return
  }
  pendingAvatarFile.value = null
  pendingAvatarChanged.value = true
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

<style scoped src="@/views/agents/styles/agent-editor-split.css"></style>
