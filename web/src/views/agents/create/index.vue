<template>
  <AppPage :show-footer="false">
    <header class="agent-page-header">
      <h1 class="agent-page-title">{{ pageTitle }}</h1>
      <div class="agent-page-header-actions">
        <n-button v-if="!agentId" secondary @click="clearAllConfig">{{ $t('views.agents.button_clear_config') }}</n-button>
        <n-button type="primary" :loading="saving" @click="handleSubmit">
          {{ $t('views.agents.button_save_config') }}
        </n-button>
      </div>
    </header>

    <n-form
      ref="formRef"
      class="agent-form"
      :model="form"
      :rules="rules"
      label-placement="top"
      :show-require-mark="true"
    >
      <!-- 基础配置 -->
      <section class="agent-section">
        <header class="section-header">{{ $t('views.agents.label_basic') }}</header>
        <div class="section-body">
          <n-form-item :label="$t('views.agents.label_agent_avatar')">
            <div class="agent-avatar-box">
              <div class="agent-avatar-circle">
                <n-image
                  v-if="avatarPreview"
                  :src="avatarPreview"
                  width="88"
                  height="88"
                  object-fit="cover"
                  preview-disabled
                  class="agent-avatar-img"
                />
              </div>
              <n-upload
                :key="avatarUploadKey"
                accept="image/png,image/jpeg,image/gif,image/webp"
                :max="1"
                :show-file-list="false"
                :default-upload="false"
                @change="onAvatarFileChange"
              >
                <n-button
                  class="agent-avatar-edit-btn"
                  type="primary"
                  size="small"
                  circle
                  secondary
                  :title="$t('views.agents.title_edit_avatar')"
                >
                  <TheIcon icon="material-symbols:edit-outline" :size="18" />
                </n-button>
              </n-upload>
            </div>
          </n-form-item>

          <n-form-item path="name" :label="$t('views.agents.label_name')">
            <n-input v-model:value="form.name" :placeholder="$t('views.agents.placeholder_name')" />
          </n-form-item>
          <n-form-item path="model_name" :label="$t('views.agents.label_model_name')">
            <n-input v-model:value="form.model_name" :placeholder="$t('views.agents.placeholder_model_name')" />
          </n-form-item>
          <n-form-item path="api_key_env_name" :label="$t('views.agents.label_api_key_env_name')">
            <n-input
              v-model:value="form.api_key_env_name"
              :placeholder="$t('views.agents.placeholder_api_key_env_name')"
            />
          </n-form-item>
          <n-form-item :label="$t('views.agents.label_description')">
            <n-input v-model:value="form.description" type="textarea" :rows="2" :placeholder="$t('views.agents.placeholder_description')" />
          </n-form-item>
          <n-form-item :label="$t('views.agents.label_system_prompt')">
            <n-input v-model:value="form.system_prompt" type="textarea" :rows="4" :placeholder="$t('views.agents.placeholder_system_prompt')" />
          </n-form-item>
          <n-form-item :show-label="false" class="agent-enable-row-item">
            <div class="agent-enable-row">
              <div class="agent-enable-pair">
                <span class="agent-enable-label">{{ $t('views.agents.label_enable_web') }}</span>
                <n-switch v-model:value="form.enable_web" />
              </div>
              <div class="agent-enable-pair">
                <span class="agent-enable-label">{{ $t('views.agents.label_enable_code') }}</span>
                <n-switch v-model:value="form.enable_code" />
              </div>
            </div>
          </n-form-item>
        </div>
      </section>

      <!-- 对话配置（可折叠，样式同基础配置 section-header） -->
      <section class="agent-section">
        <header
          class="section-header section-header--collapsible"
          role="button"
          tabindex="0"
          :aria-expanded="dialogueOpen"
          @click="dialogueOpen = !dialogueOpen"
          @keydown.enter.prevent="dialogueOpen = !dialogueOpen"
        >
          <span class="section-header__title">{{ $t('views.agents.label_dialogue') }}</span>
          <span class="section-header__chevron" :class="{ 'is-collapsed': !dialogueOpen }" aria-hidden="true">
            <TheIcon icon="material-symbols:keyboard-arrow-down-rounded" :size="22" />
          </span>
        </header>
        <div v-show="dialogueOpen" class="section-body">
          <n-form-item :label="$t('views.agents.label_opening_message')" :show-require-mark="false">
            <n-input
              v-model:value="form.opening_message"
              type="textarea"
              :rows="3"
              :placeholder="$t('views.agents.placeholder_opening_message')"
            />
          </n-form-item>
        </div>
      </section>

      <!-- 高级配置（可折叠，样式同基础配置 section-header） -->
      <section class="agent-section">
        <header
          class="section-header section-header--collapsible"
          role="button"
          tabindex="0"
          :aria-expanded="advancedOpen"
          @click="advancedOpen = !advancedOpen"
          @keydown.enter.prevent="advancedOpen = !advancedOpen"
        >
          <span class="section-header__title">{{ $t('views.agents.label_advanced') }}</span>
          <span class="section-header__chevron" :class="{ 'is-collapsed': !advancedOpen }" aria-hidden="true">
            <TheIcon icon="material-symbols:keyboard-arrow-down-rounded" :size="22" />
          </span>
        </header>
        <div v-show="advancedOpen" class="section-body">
          <n-form-item :label="$t('views.agents.label_temperature')" :show-require-mark="false">
            <div class="agent-temperature-row">
              <n-slider
                v-model:value="form.temperature"
                :min="0"
                :max="2"
                :step="0.01"
                :tooltip="true"
                class="agent-temperature-slider"
              />
              <span class="agent-temperature-value" aria-live="polite">{{ temperatureSliderLabel }}</span>
            </div>
          </n-form-item>
        </div>
      </section>
    </n-form>
  </AppPage>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { watchDebounced } from '@vueuse/core'
import { useI18n } from 'vue-i18n'
import { NButton, NForm, NFormItem, NImage, NInput, NSlider, NSwitch, NUpload } from 'naive-ui'
import AppPage from '@/components/page/AppPage.vue'
import TheIcon from '@/components/icon/TheIcon.vue'
import api from '@/api'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()

const DEFAULT_AVATAR = `${import.meta.env.BASE_URL}logo.svg`.replace(/\/{2,}/, '/')

/** 新建页草稿（刷新不丢）；编辑页带 ?id= 时不用 */
const CREATE_DRAFT_KEY = 'mg-agent:user-agent-create-draft'
const CREATE_DRAFT_VERSION = 1
/** 草稿中头像 base64 上限，避免撑爆 sessionStorage */
const MAX_AVATAR_DRAFT_BYTES = 1.5 * 1024 * 1024

function emptyForm() {
  return {
    name: '',
    model_name: '',
    api_key_env_name: '',
    description: '',
    system_prompt: '',
    enable_web: false,
    enable_code: false,
    opening_message: '',
    temperature: 0.1,
  }
}

function dataURLtoBlob(dataurl) {
  const arr = dataurl.split(',')
  const mime = arr[0].match(/:(.*?);/)?.[1] || 'image/png'
  const bstr = atob(arr[1])
  let n = bstr.length
  const u8arr = new Uint8Array(n)
  while (n--) {
    u8arr[n] = bstr.charCodeAt(n)
  }
  return new Blob([u8arr], { type: mime })
}

const formRef = ref(null)
const saving = ref(false)
const agentId = ref(null)
const pendingAvatarFile = ref(null)
const serverAvatarUrl = ref('')
/** 用于草稿持久化（与 pending 对应） */
const draftAvatarDataUrl = ref(null)
/** 每次选图后递增，重挂载 NUpload，避免 max=1 占满后无法再次选择 */
const avatarUploadKey = ref(0)

const dialogueOpen = ref(false)
const advancedOpen = ref(false)
/** 首次根据路由恢复/重置表单完成后再写入草稿，避免覆盖 sessionStorage 中的草稿 */
const draftPersistReady = ref(false)

const form = ref(emptyForm())

const pageTitle = computed(() =>
  agentId.value ? t('views.agents.title_edit_agent') : t('views.agents.title_create_agent')
)

const avatarPreview = computed(() => {
  if (serverAvatarUrl.value) return serverAvatarUrl.value
  return DEFAULT_AVATAR
})

/** 滑块旁展示当前温度数值 */
const temperatureSliderLabel = computed(() => {
  const v = form.value.temperature
  const n = typeof v === 'number' && !Number.isNaN(v) ? v : 0.1
  return n.toFixed(2)
})

const rules = {
  name: { required: true, message: () => t('views.agents.rule_name'), trigger: ['input', 'blur'] },
  model_name: { required: true, message: () => t('views.agents.rule_model_name'), trigger: ['input', 'blur'] },
  api_key_env_name: {
    required: true,
    message: () => t('views.agents.rule_api_key_env_name'),
    trigger: ['input', 'blur'],
  },
}

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
  if (route.query.id) return
  if (agentId.value) return
  const payload = {
    v: CREATE_DRAFT_VERSION,
    form: { ...form.value },
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
    formRef.value?.restoreValidation()
  })
}

function onAvatarFileChange(options) {
  const f = options.fileList?.[0]?.file
  const list = options.fileList || []
  // 选图后重挂载 NUpload 时可能收到空列表，勿撤销刚生成的 blob
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
  if (agentId.value) {
    loadAgent(agentId.value)
  } else {
    serverAvatarUrl.value = ''
  }
}

async function loadAgent(id) {
  try {
    const res = await api.getUserAgent({ agent_id: id })
    const d = res.data
    agentId.value = d.id
    form.value = {
      name: d.name,
      model_name: d.model_name,
      api_key_env_name: d.api_key_env_name,
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
    draftAvatarDataUrl.value = null
  } catch {
    agentId.value = null
    resetForm()
  }
}

async function handleSubmit() {
  await formRef.value?.validate()
  saving.value = true
  try {
    const payload = { ...form.value }
    let id = agentId.value
    if (id) {
      await api.updateUserAgent({ ...payload, id })
      $message.success(t('views.agents.msg_update_ok'))
    } else {
      const res = await api.createUserAgent(payload)
      id = res.data?.id
      agentId.value = id
      $message.success(t('views.agents.msg_create_ok'))
    }
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
    if (agentId.value) {
      if (!route.query.id) {
        await router.replace({ path: route.path, query: { ...route.query, id: String(agentId.value) } })
      } else {
        await loadAgent(agentId.value)
      }
    }
  } finally {
    saving.value = false
  }
}

watch(
  () => route.query.id,
  async (id) => {
    draftPersistReady.value = false
    if (id) {
      clearCreateDraft()
      await loadAgent(Number(id))
    } else {
      agentId.value = null
      if (!loadCreateDraft()) {
        resetForm()
      }
    }
    draftPersistReady.value = true
  },
  { immediate: true }
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

<style scoped>
/*
 * 项目 global.scss 将 html 设为 font-size: 4px（方便 UnoCSS），
 * 故此处字号一律用 px，避免 rem 按 4px 根计算导致极小。
 */
.agent-page-header {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 20px;
  padding-bottom: 16px;
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

.agent-form {
  width: 100%;
  max-width: 720px;
  font-size: 15px;
}

.agent-section {
  margin-bottom: 20px;
}

.agent-section:last-of-type {
  margin-bottom: 0;
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

/* 与基础配置一致的标题栏 + 右侧展开/收起（不使用 n-collapse） */
.section-header--collapsible {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  cursor: pointer;
  user-select: none;
  margin-bottom: 14px;
}

.section-header--collapsible:focus-visible {
  outline: 2px solid var(--n-primary-color);
  outline-offset: 2px;
}

.section-header__title {
  flex: 1;
  min-width: 0;
  font-weight: 700;
  letter-spacing: 0.03em;
}

.section-header__chevron {
  display: flex;
  flex-shrink: 0;
  align-items: center;
  color: var(--n-text-color-3);
  transition: transform 0.2s ease;
}

.section-header__chevron.is-collapsed {
  transform: rotate(-90deg);
}

/* 与 section-header 文字左缘对齐：4px 左边条 + 12px 内边距 = 16px */
.section-body {
  padding-left: 16px;
  box-sizing: border-box;
}

.agent-form :deep(.n-form-item-label) {
  font-size: 14px !important;
  font-weight: 500 !important;
  line-height: 1.5 !important;
  color: var(--n-text-color-2) !important;
  letter-spacing: 0.02em;
}

.agent-form :deep(.n-form-item-asterisk) {
  font-size: 14px;
  color: var(--n-error-color);
}

.agent-form :deep(.n-form-item-blank) {
  min-height: 0;
}

.agent-form :deep(.n-input),
.agent-form :deep(.n-input-wrapper) {
  width: 100%;
  font-size: 15px;
}

.agent-form :deep(.n-input__input-el),
.agent-form :deep(.n-input__textarea-el),
.agent-form :deep(.n-input__placeholder),
.agent-form :deep(.n-input__placeholder span) {
  font-size: 15px !important;
}

.agent-temperature-row {
  display: flex;
  align-items: center;
  gap: 16px;
  width: 100%;
  max-width: 560px;
}
.agent-temperature-slider {
  flex: 1;
  min-width: 0;
}
/* 轨道：左蓝 → 右红；填充层略提亮已选区间 */
.agent-temperature-slider :deep(.n-slider-rail) {
  height: 10px;
  border-radius: 5px;
  background: linear-gradient(90deg, #2080f0 0%, #f5222d 100%);
}
.agent-temperature-slider :deep(.n-slider-rail__fill) {
  border-radius: 5px;
  background: rgba(255, 255, 255, 0.35);
}
.agent-temperature-value {
  flex-shrink: 0;
  min-width: 3.25em;
  font-size: 15px;
  font-variant-numeric: tabular-nums;
  color: var(--n-text-color);
}

/* 联网 / 写代码：同一行 */
.agent-enable-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 24px 40px;
  width: 100%;
}

.agent-enable-pair {
  display: inline-flex;
  align-items: center;
  gap: 10px;
}

.agent-enable-label {
  font-size: 14px;
  font-weight: 500;
  color: var(--n-text-color-2);
  white-space: nowrap;
}

.agent-enable-row-item {
  margin-bottom: 0;
}

/* 头像：圆形仅裁切图片；外层不 overflow:hidden，避免右下角按钮被裁掉 */
.agent-avatar-box {
  position: relative;
  display: inline-block;
  width: 88px;
  height: 88px;
}
.agent-avatar-circle {
  width: 88px;
  height: 88px;
  border-radius: 50%;
  overflow: hidden;
}
.agent-avatar-img {
  border-radius: 50%;
  overflow: hidden;
  display: block;
}

.agent-avatar-edit-btn {
  position: absolute;
  right: -4px;
  bottom: -4px;
  z-index: 1;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.12);
}

</style>
