<template>
  <AppPage :show-footer="false" scroll-in-parent class="agent-chat-page !p-0">
    <div class="agent-chat-layout">
      <n-spin :show="pageLoading" class="agent-chat-spin">
        <div v-if="loadError" class="agent-chat-error">
          {{ $t('views.agents.chat_error_load_agent') }}
        </div>
        <template v-else>
          <div ref="bodyScrollRef" class="agent-chat-body">
            <transition name="agent-chat-fade">
              <div v-if="sessionPhase === 'intro'" key="intro" class="agent-chat-intro">
                <n-avatar
                  round
                  :size="120"
                  :src="agentAvatarSrc"
                  object-fit="cover"
                  class="agent-chat-intro-avatar"
                />
                <h2 class="agent-chat-intro-name">{{ agent?.name }}</h2>
                <p class="agent-chat-intro-sub">{{ introDescription }}</p>
                <p class="agent-chat-intro-creator">
                  <TheIcon icon="mdi:account-outline" :size="16" />
                  <span>{{ $t('views.agents.chat_label_creator') }} · {{ userStore.name }}</span>
                </p>
              </div>
            </transition>

            <div v-if="sessionPhase === 'chat'" class="agent-chat-messages">
              <div
                v-for="m in messages"
                :key="m.id"
                class="agent-chat-msg-row"
                :class="{ 'agent-chat-msg-row--user': m.role === 'user' }"
              >
                <div class="agent-chat-msg-bubble">
                  <template v-if="m.pending">
                    <n-spin size="small" />
                  </template>
                  <template v-else>
                    {{ m.content }}
                  </template>
                </div>
                <div v-if="m.attachments?.length" class="agent-chat-msg-files">
                  {{ m.attachments.map((a) => a.name).join(', ') }}
                </div>
              </div>
            </div>
          </div>

          <footer class="agent-chat-footer">
            <div class="agent-chat-footer-inner">
              <div class="agent-chat-toolbar">
                <n-button size="small" round quaternary @click="restartChat">
                  <template #icon>
                    <TheIcon icon="mdi:plus-circle-outline" :size="18" />
                  </template>
                  {{ $t('views.agents.chat_button_restart') }}
                </n-button>
              </div>

              <div class="agent-chat-composer">
                <n-input
                  v-model:value="inputText"
                  type="textarea"
                  :bordered="false"
                  :autosize="{ minRows: 2, maxRows: 8 }"
                  :placeholder="$t('views.agents.chat_placeholder_input')"
                  :disabled="pageLoading || !!loadError || sending"
                  class="agent-chat-input"
                  @keydown="onInputKeydown"
                />
                <div class="agent-chat-composer-bar">
                  <n-upload
                    :key="uploadResetKey"
                    :show-file-list="false"
                    :max="5"
                    multiple
                    accept="*/*"
                    :custom-request="handleUploadRequest"
                    @change="onUploadChange"
                  >
                    <n-button
                      quaternary
                      circle
                      :disabled="sending"
                      :title="$t('views.agents.chat_attach_file')"
                    >
                      <TheIcon icon="mdi:paperclip" :size="22" />
                    </n-button>
                  </n-upload>
                  <div class="agent-chat-files-preview">
                    <n-tag
                      v-for="(f, idx) in pendingFiles"
                      :key="f.id"
                      closable
                      size="small"
                      @close="removePendingFile(idx)"
                    >
                      {{ f.name }}
                    </n-tag>
                  </div>
                  <n-button
                    type="primary"
                    circle
                    class="agent-chat-send"
                    :disabled="sendDisabled"
                    :loading="sending"
                    :title="$t('views.agents.chat_button_send')"
                    @click="submitMessage"
                  >
                    <TheIcon icon="mdi:send" :size="20" />
                  </n-button>
                </div>
              </div>
            </div>
          </footer>
        </template>
      </n-spin>
    </div>
  </AppPage>
</template>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { NAvatar, NButton, NInput, NSpin, NTag, NUpload } from 'naive-ui'
import AppPage from '@/components/page/AppPage.vue'
import TheIcon from '@/components/icon/TheIcon.vue'
import api from '@/api'
import { useAgentChatHeaderStore, useUserStore } from '@/store'
import { DEFAULT_AVATAR } from '@/views/agents/composables/agentFormCommon.js'

const { t } = useI18n()
const route = useRoute()
const userStore = useUserStore()
const agentChatHeaderStore = useAgentChatHeaderStore()

const pageLoading = ref(true)
const loadError = ref(false)
const agent = ref(null)
const sessionPhase = ref('intro')
const messages = ref([])
const inputText = ref('')
const sending = ref(false)
const pendingFiles = ref([])
const bodyScrollRef = ref(null)
const uploadResetKey = ref(0)
let fileIdSeq = 0
const baseDocTitle = import.meta.env.VITE_TITLE || ''

const agentAvatarSrc = computed(() => agent.value?.avatar_url || DEFAULT_AVATAR)

const introDescription = computed(() => {
  const d = agent.value?.description?.trim()
  if (d) return d
  return t('views.agents.text_no_description')
})

const sendDisabled = computed(() => {
  const text = inputText.value?.trim() || ''
  if (text.length > 0) return false
  if (pendingFiles.value.length > 0) return false
  return true
})

function onInputKeydown(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    submitMessage()
  }
}

function handleUploadRequest({ onFinish }) {
  onFinish()
}

function onUploadChange(options) {
  const fileList = options.fileList || []
  pendingFiles.value = fileList.map((item) => {
    const raw = item.file
    const fileObj = raw instanceof File ? raw : raw?.file
    return {
      id: ++fileIdSeq,
      name: fileObj?.name || item.name || 'file',
      file: fileObj,
    }
  })
}

function removePendingFile(index) {
  pendingFiles.value.splice(index, 1)
}

function scrollBodyToBottom() {
  nextTick(() => {
    const el = bodyScrollRef.value
    if (el) {
      el.scrollTop = el.scrollHeight
    }
  })
}

async function loadAgent() {
  const id = Number(route.params.agentId)
  if (!Number.isFinite(id)) {
    loadError.value = true
    pageLoading.value = false
    return
  }
  pageLoading.value = true
  loadError.value = false
  agent.value = null
  try {
    const res = await api.getUserAgent({ agent_id: id })
    agent.value = res.data
  } catch {
    loadError.value = true
    agent.value = null
  } finally {
    pageLoading.value = false
  }
}

function restartChat() {
  messages.value = []
  sessionPhase.value = 'intro'
  inputText.value = ''
  pendingFiles.value = []
  uploadResetKey.value += 1
  window.$message?.success(t('views.agents.chat_msg_restart_ok'))
}

async function submitMessage() {
  if (sendDisabled.value || sending.value) return
  const text = inputText.value.trim()
  const attachments = pendingFiles.value.map((p) => ({ name: p.name }))
  if (!text && attachments.length === 0) return

  const userMsg = {
    id: `u-${Date.now()}`,
    role: 'user',
    content: text,
    attachments: attachments.length ? attachments : undefined,
  }

  const wasIntro = sessionPhase.value === 'intro'
  messages.value.push(userMsg)
  if (wasIntro) {
    sessionPhase.value = 'chat'
  }
  inputText.value = ''
  pendingFiles.value = []
  uploadResetKey.value += 1
  scrollBodyToBottom()

  const assistantId = `a-${Date.now()}`
  messages.value.push({
    id: assistantId,
    role: 'assistant',
    content: '',
    pending: true,
  })
  sending.value = true
  scrollBodyToBottom()

  await new Promise((r) => setTimeout(r, 450))

  const idx = messages.value.findIndex((m) => m.id === assistantId)
  if (idx !== -1) {
    messages.value[idx] = {
      ...messages.value[idx],
      content: t('views.agents.chat_msg_demo_reply'),
      pending: false,
    }
  }
  sending.value = false
  scrollBodyToBottom()
}

watch(
  () => route.params.agentId,
  () => {
    agentChatHeaderStore.clear()
    sessionPhase.value = 'intro'
    messages.value = []
    inputText.value = ''
    pendingFiles.value = []
    uploadResetKey.value += 1
    loadAgent()
  }
)

function syncAgentToHeader() {
  const a = agent.value
  if (!a) {
    agentChatHeaderStore.clear()
    return
  }
  const desc = (a.description && String(a.description).trim()) || ''
  agentChatHeaderStore.setAgentMeta({
    title: a.name || '',
    avatarUrl: a.avatar_url || '',
    subtitle: desc || a.name || '',
    creatorName: userStore.name || '',
    agentId: a.id,
  })
  if (a.name) {
    document.title = `${a.name} | ${baseDocTitle}`
  }
}

watch(
  () => agent.value,
  () => {
    syncAgentToHeader()
  },
  { immediate: true, deep: true }
)

watch(
  () => userStore.name,
  () => {
    if (agent.value) syncAgentToHeader()
  }
)

onMounted(() => {
  loadAgent()
})

onUnmounted(() => {
  document.title = baseDocTitle
  agentChatHeaderStore.clear()
})
</script>

<style scoped>
/* 与 layout 主区域 `dark:bg-dark`（Uno: #18181c）一致，避免暗黑下 --n-color 随主题偏橙 */
.agent-chat-page {
  height: 100%;
  min-height: 0;
}

.agent-chat-layout {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  box-sizing: border-box;
  background: #ffffff;
}

html.dark .agent-chat-layout {
  background: #18181c;
}

.agent-chat-spin {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

.agent-chat-spin :deep(.n-spin-content) {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: #ffffff;
}

html.dark .agent-chat-spin :deep(.n-spin-content) {
  background: #18181c;
}

/* 加载遮罩与主色区分，避免大面积偏橙 */
html.dark .agent-chat-spin :deep(.n-spin-body) {
  background: rgba(24, 24, 28, 0.65);
}

.agent-chat-error {
  padding: 32px;
  text-align: center;
  color: var(--n-text-color-3);
}

.agent-chat-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 24px 20px 16px;
  box-sizing: border-box;
  background: #ffffff;
}

html.dark .agent-chat-body {
  background: #18181c;
}

.agent-chat-fade-enter-active,
.agent-chat-fade-leave-active {
  transition: opacity 0.2s ease;
}

.agent-chat-fade-enter-from,
.agent-chat-fade-leave-to {
  opacity: 0;
}

.agent-chat-intro {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  text-align: center;
  padding: 24px 16px 48px;
  min-height: min(52vh, 420px);
}

.agent-chat-intro-avatar {
  box-shadow: 0 8px 32px rgba(15, 23, 42, 0.12);
}

html.dark .agent-chat-intro-avatar {
  box-shadow: 0 8px 28px rgba(0, 0, 0, 0.45);
}

.agent-chat-intro-name {
  margin: 20px 0 0;
  font-size: 26px;
  font-weight: 700;
  letter-spacing: 0.02em;
  color: var(--n-text-color);
}

.agent-chat-intro-sub {
  margin: 8px 0 0;
  max-width: 560px;
  font-size: 15px;
  line-height: 1.5;
  color: var(--n-text-color-3);
  word-break: break-word;
}

.agent-chat-intro-creator {
  margin: 16px 0 0;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: var(--n-text-color-3);
}

.agent-chat-messages {
  max-width: 800px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding-bottom: 8px;
}

.agent-chat-msg-row {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
}

.agent-chat-msg-row--user {
  align-items: flex-end;
}

.agent-chat-msg-bubble {
  max-width: 85%;
  padding: 12px 14px;
  border-radius: 14px;
  font-size: 15px;
  line-height: 1.5;
  word-break: break-word;
  background: var(--n-color-embedded);
  color: var(--n-text-color);
}

html.dark .agent-chat-msg-bubble {
  background: rgba(255, 255, 255, 0.08);
}

.agent-chat-msg-row--user .agent-chat-msg-bubble {
  background: rgba(59, 130, 246, 0.14);
  color: var(--n-text-color);
}

html.dark .agent-chat-msg-row--user .agent-chat-msg-bubble {
  background: rgba(59, 130, 246, 0.22);
}

.agent-chat-msg-files {
  font-size: 12px;
  color: var(--n-text-color-3);
  max-width: 85%;
  text-align: right;
}

.agent-chat-msg-row:not(.agent-chat-msg-row--user) .agent-chat-msg-files {
  text-align: left;
}

.agent-chat-footer {
  flex-shrink: 0;
  padding: 12px 20px 20px;
  border-top: 1px solid var(--n-divider-color);
  background: #ffffff;
}

html.dark .agent-chat-footer {
  background: #18181c;
}

.agent-chat-footer-inner {
  max-width: 880px;
  margin: 0 auto;
  width: 100%;
  box-sizing: border-box;
}

.agent-chat-toolbar {
  display: flex;
  justify-content: flex-start;
  gap: 10px;
  margin-bottom: 10px;
}

.agent-chat-composer {
  border: 1px solid #e5e7eb;
  border-radius: 20px;
  padding: 12px 14px 10px;
  background: #ffffff;
  box-shadow: none;
}

html.dark .agent-chat-composer {
  border-color: rgba(255, 255, 255, 0.12);
  background: #18181c;
}

.agent-chat-input :deep(.n-input-wrapper) {
  --n-border: transparent !important;
  --n-border-hover: transparent !important;
  --n-border-focus: transparent !important;
  --n-box-shadow-focus: none !important;
  background: transparent !important;
}

.agent-chat-input :deep(.n-input__state-border) {
  border: none !important;
  box-shadow: none !important;
}

.agent-chat-input :deep(textarea) {
  --n-border: none !important;
  --n-box-shadow: none !important;
  background: transparent !important;
  padding: 4px 2px !important;
  font-size: 15px;
  color: #000000 !important;
  caret-color: #000000;
}

html.dark .agent-chat-input :deep(textarea) {
  color: rgba(255, 255, 255, 0.9) !important;
  caret-color: rgba(255, 255, 255, 0.9);
}

.agent-chat-input :deep(.n-input__placeholder) {
  color: #9ca3af !important;
}

html.dark .agent-chat-input :deep(.n-input__placeholder) {
  color: var(--n-text-color-3) !important;
}

.agent-chat-composer-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 4px;
}

.agent-chat-files-preview {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}

.agent-chat-send {
  flex-shrink: 0;
  margin-left: auto;
}
</style>
