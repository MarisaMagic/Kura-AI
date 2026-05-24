<template>
  <AppPage :show-footer="false" scroll-in-parent class="!p-0">
    <div class="agent-editor-split">
      <div class="agent-editor-split__left">
        <div class="agent-editor-layout">
          <div class="agent-editor-form-column">
            <header class="agent-page-header">
              <h1 class="agent-page-title">{{ $t('views.agents.title_create_agent') }}</h1>
              <div class="agent-page-header-actions">
                <n-button secondary @click="clearAllConfig">{{ $t('views.agents.button_clear_config') }}</n-button>
                <n-button type="primary" :loading="saving" @click="handleSubmit">
                  {{ $t('views.agents.button_save_config') }}
                </n-button>
              </div>
            </header>

            <AgentFormFields
              ref="formFieldsRef"
              :form="form"
              :rules="rules"
              :has-saved-api-key="false"
              :dialogue-open="dialogueOpen"
              :advanced-open="advancedOpen"
              :avatar-preview="avatarPreview"
              :avatar-upload-key="avatarUploadKey"
              :temperature-slider-label="temperatureSliderLabel"
              @update:dialogue-open="dialogueOpen = $event"
              @update:advanced-open="advancedOpen = $event"
              @avatar-change="onAvatarFileChange"
            />
          </div>
        </div>
      </div>

      <div class="agent-editor-split__right">
        <AgentEditorPreviewPanel
          :form="form"
          :agent-id="null"
          :avatar-preview="avatarPreview"
          :creator-name="userStore.name"
          :config-stale="false"
          :chat-enabled="false"
          @request-save="handleSubmit"
        />
      </div>
    </div>
  </AppPage>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { watchDebounced } from '@vueuse/core'
import { useI18n } from 'vue-i18n'
import { NButton } from 'naive-ui'
import AppPage from '@/components/page/AppPage.vue'
import AgentFormFields from '@/views/agents/components/AgentFormFields.vue'
import AgentEditorPreviewPanel from '@/views/agents/components/AgentEditorPreviewPanel.vue'
import { useUserStore } from '@/store'
import api from '@/api'
import {
  CREATE_DRAFT_KEY,
  CREATE_DRAFT_VERSION,
  MAX_AVATAR_DRAFT_BYTES,
  dataURLtoBlob,
  emptyForm,
  DEFAULT_AVATAR,
  buildAgentFormRules,
} from '@/views/agents/composables/agentFormCommon.js'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const userStore = useUserStore()

const formFieldsRef = ref(null)
const saving = ref(false)
const agentId = ref(null)
const pendingAvatarFile = ref(null)
const serverAvatarUrl = ref('')
const draftAvatarDataUrl = ref(null)
const avatarUploadKey = ref(0)
const dialogueOpen = ref(false)
const advancedOpen = ref(false)
const draftPersistReady = ref(false)

const form = ref(emptyForm())
const rules = computed(() => buildAgentFormRules(t, { isEdit: false }))

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

function clearCreateDraft() {
  try {
    sessionStorage.removeItem(CREATE_DRAFT_KEY)
  } catch {
    /* ignore */
  }
  draftAvatarDataUrl.value = null
}

function saveCreateDraft() {
  if (agentId.value) return
  const payload = {
    v: CREATE_DRAFT_VERSION,
      form: { ...form.value, api_key: '' },
    dialogueOpen: dialogueOpen.value,
    advancedOpen: advancedOpen.value,
    avatarDataUrl: draftAvatarDataUrl.value || null,
  }
  try {
    sessionStorage.setItem(CREATE_DRAFT_KEY, JSON.stringify(payload))
  } catch {
    try {
      sessionStorage.setItem(
        CREATE_DRAFT_KEY,
        JSON.stringify({ ...payload, avatarDataUrl: null })
      )
    } catch {
      /* ignore */
    }
  }
}

function loadCreateDraft() {
  try {
    const raw = sessionStorage.getItem(CREATE_DRAFT_KEY)
    if (!raw) return false
    const parsed = JSON.parse(raw)
    if (parsed.v !== CREATE_DRAFT_VERSION || !parsed.form) {
      sessionStorage.removeItem(CREATE_DRAFT_KEY)
      return false
    }
    form.value = {
      ...emptyForm(),
      ...parsed.form,
      temperature:
        typeof parsed.form.temperature === 'number'
          ? parsed.form.temperature
          : Number(parsed.form.temperature) || 0.1,
      api_key: '',
    }
    dialogueOpen.value = parsed.dialogueOpen === true
    advancedOpen.value = parsed.advancedOpen === true
    draftAvatarDataUrl.value = parsed.avatarDataUrl || null
    pendingAvatarFile.value = null
    revokeAvatarBlobIfAny()
    serverAvatarUrl.value = ''
    if (parsed.avatarDataUrl) {
      try {
        const blob = dataURLtoBlob(parsed.avatarDataUrl)
        const ext = (blob.type.split('/')[1] || 'png').replace(/\+.*$/, '')
        const file = new File([blob], `draft-avatar.${ext}`, { type: blob.type || 'image/png' })
        pendingAvatarFile.value = file
        serverAvatarUrl.value = URL.createObjectURL(file)
      } catch {
        draftAvatarDataUrl.value = null
        pendingAvatarFile.value = null
        serverAvatarUrl.value = ''
      }
    }
    nextTick(() => {
      avatarUploadKey.value += 1
    })
    return true
  } catch {
    try {
      sessionStorage.removeItem(CREATE_DRAFT_KEY)
    } catch {
      /* ignore */
    }
    return false
  }
}

function resetForm() {
  form.value = emptyForm()
  pendingAvatarFile.value = null
  revokeAvatarBlobIfAny()
  serverAvatarUrl.value = ''
  draftAvatarDataUrl.value = null
}

function bumpAvatarUpload() {
  nextTick(() => {
    avatarUploadKey.value += 1
  })
}

function clearAllConfig() {
  resetForm()
  dialogueOpen.value = false
  advancedOpen.value = false
  clearCreateDraft()
  avatarUploadKey.value += 1
  nextTick(() => {
    formFieldsRef.value?.restoreValidation()
  })
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
    bumpAvatarUpload()
    if (f.size <= MAX_AVATAR_DRAFT_BYTES) {
      const reader = new FileReader()
      reader.onload = () => {
        draftAvatarDataUrl.value = typeof reader.result === 'string' ? reader.result : null
      }
      reader.readAsDataURL(f)
    } else {
      draftAvatarDataUrl.value = null
    }
    return
  }
  pendingAvatarFile.value = null
  draftAvatarDataUrl.value = null
  revokeAvatarBlobIfAny()
  serverAvatarUrl.value = ''
}

async function handleSubmit() {
  await formFieldsRef.value?.validate()
  saving.value = true
  try {
    const payload = { ...form.value }
    const res = await api.createUserAgent(payload)
    const id = res.data?.id
    agentId.value = id
    $message.success(t('views.agents.msg_create_ok'))
    if (pendingAvatarFile.value && id) {
      const fd = new FormData()
      fd.append('file', pendingAvatarFile.value)
      const up = await api.uploadUserAgentAvatar(id, fd)
      if (up.data?.avatar_url) {
        serverAvatarUrl.value = up.data.avatar_url
      }
      pendingAvatarFile.value = null
    }
    clearCreateDraft()
    if (id) {
      await router.replace({ name: 'AgentEdit', params: { id: String(id) } })
    }
  } finally {
    saving.value = false
  }
}

function initFromRoute() {
  draftPersistReady.value = false
  const q = route.query.id
  if (q != null && q !== '') {
    router.replace({ name: 'AgentEdit', params: { id: String(q) } })
    return
  }
  agentId.value = null
  if (!loadCreateDraft()) {
    resetForm()
  }
  draftPersistReady.value = true
}

initFromRoute()

watch(
  () => route.query.id,
  () => {
    initFromRoute()
  }
)

watchDebounced(
  () => ({
    form: form.value,
    dialogueOpen: dialogueOpen.value,
    advancedOpen: advancedOpen.value,
    draftAvatarDataUrl: draftAvatarDataUrl.value,
    ready: draftPersistReady.value,
  }),
  () => {
    if (!draftPersistReady.value) return
    saveCreateDraft()
  },
  { deep: true, debounce: 400 }
)

onBeforeUnmount(() => {
  if (draftPersistReady.value) saveCreateDraft()
})
</script>

<style scoped src="@/views/agents/styles/agent-editor-split.css"></style>
