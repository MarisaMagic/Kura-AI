<template>
  <n-form
    ref="formInnerRef"
    class="agent-form"
    :model="form"
    :rules="rules"
    label-placement="top"
    :show-require-mark="true"
  >
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
              @change="(o) => emit('avatar-change', o)"
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
        <n-form-item path="api_key" :label="$t('views.agents.label_api_key')">
          <n-input
            v-model:value="form.api_key"
            type="password"
            show-password-on="click"
            :placeholder="
              hasSavedApiKey ? $t('views.agents.placeholder_api_key_edit') : $t('views.agents.placeholder_api_key')
            "
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

    <section class="agent-section">
      <header
        class="section-header section-header--collapsible"
        role="button"
        tabindex="0"
        :aria-expanded="dialogueOpen"
        @click="toggleDialogue"
        @keydown.enter.prevent="toggleDialogue"
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

    <section class="agent-section">
      <header
        class="section-header section-header--collapsible"
        role="button"
        tabindex="0"
        :aria-expanded="advancedOpen"
        @click="toggleAdvanced"
        @keydown.enter.prevent="toggleAdvanced"
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
</template>

<script setup>
import { ref } from 'vue'
import { NButton, NForm, NFormItem, NImage, NInput, NSlider, NSwitch, NUpload } from 'naive-ui'
import TheIcon from '@/components/icon/TheIcon.vue'

const props = defineProps({
  form: { type: Object, required: true },
  rules: { type: Object, required: true },
  hasSavedApiKey: { type: Boolean, default: false },
  dialogueOpen: { type: Boolean, default: false },
  advancedOpen: { type: Boolean, default: false },
  avatarPreview: { type: String, default: '' },
  avatarUploadKey: { type: Number, default: 0 },
  temperatureSliderLabel: { type: String, default: '0.10' },
})

const emit = defineEmits(['avatar-change', 'update:dialogueOpen', 'update:advancedOpen'])

const formInnerRef = ref(null)

function toggleDialogue() {
  emit('update:dialogueOpen', !props.dialogueOpen)
}
function toggleAdvanced() {
  emit('update:advancedOpen', !props.advancedOpen)
}

defineExpose({
  validate: () => formInnerRef.value?.validate(),
  restoreValidation: () => formInnerRef.value?.restoreValidation?.(),
})
</script>

<style scoped>
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
