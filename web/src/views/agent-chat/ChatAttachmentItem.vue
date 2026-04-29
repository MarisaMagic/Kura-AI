<template>
  <div v-if="mode === 'image'" class="agent-chat-attachment-media">
    <div
      v-if="loadError"
      class="agent-chat-file-box agent-chat-file-box--readonly"
      :title="attachmentName"
    >
      <TheIcon :icon="fileIcon" :size="18" class="agent-chat-file-box-icon" />
      <span class="agent-chat-file-box-name">{{ attachmentName }}</span>
    </div>
    <n-spin v-else-if="loading" size="small" class="agent-chat-attachment-spinner" />
    <img
      v-else-if="imgSrc"
      class="agent-chat-attachment-thumb"
      :src="imgSrc"
      :alt="attachmentName"
      loading="lazy"
    />
  </div>
  <div v-else class="agent-chat-file-box agent-chat-file-box--readonly" :title="attachmentName">
    <TheIcon :icon="fileIcon" :size="18" class="agent-chat-file-box-icon" />
    <span class="agent-chat-file-box-name">{{ attachmentName }}</span>
  </div>
</template>

<script setup>
import { computed, onUnmounted, ref, watch } from 'vue'
import { NSpin } from 'naive-ui'
import TheIcon from '@/components/icon/TheIcon.vue'
import api from '@/api'

const props = defineProps({
  agentId: { type: Number, required: true },
  sessionId: { type: String, required: true },
  attachment: { type: Object, required: true },
})

function isImageMimeOrKind(att) {
  const kind = String(att?.kind || '').toLowerCase()
  const mime = String(att?.mime || '').toLowerCase()
  return kind === 'image' || mime.startsWith('image/')
}

function fileIconFromAttachment(att) {
  const kind = String(att?.kind || '').toLowerCase()
  const mime = String(att?.mime || '').toLowerCase()
  const name = String(att?.name || '').toLowerCase()
  if (kind === 'image' || mime.startsWith('image/')) return 'mdi:file-image-outline'
  if (kind === 'table' || mime.includes('spreadsheet') || /\.(csv|xlsx?|xls)$/i.test(name))
    return 'mdi:table-large'
  if (kind === 'document' || mime === 'application/pdf' || /\.pdf$/i.test(name))
    return 'mdi:file-pdf-box'
  return 'mdi:file-document-outline'
}

const attachmentName = computed(() => String(props.attachment?.name || '').trim() || '—')
const fileIcon = computed(() => fileIconFromAttachment(props.attachment))

const hasServerId = computed(() => {
  const id = props.attachment?.attachmentId
  return !!(id != null && String(id).trim())
})

const mode = computed(() => {
  if (isImageMimeOrKind(props.attachment) && hasServerId.value) return 'image'
  return 'file'
})

const loading = ref(false)
const loadError = ref(false)
const imgSrc = ref('')
let ownedBlobUrl = ''

function revokeOwned() {
  if (ownedBlobUrl) {
    URL.revokeObjectURL(ownedBlobUrl)
    ownedBlobUrl = ''
  }
  imgSrc.value = ''
}

async function fetchPreview() {
  if (mode.value !== 'image' || !hasServerId.value) return
  if (!Number.isFinite(props.agentId)) {
    loadError.value = true
    return
  }
  revokeOwned()
  loading.value = true
  loadError.value = false
  try {
    const blob = await api.fetchChatAttachmentPreviewBlob(
      props.agentId,
      props.sessionId,
      String(props.attachment.attachmentId).trim()
    )
    ownedBlobUrl = URL.createObjectURL(blob)
    imgSrc.value = ownedBlobUrl
  } catch {
    loadError.value = true
  } finally {
    loading.value = false
  }
}

watch(
  () => [props.agentId, props.sessionId, props.attachment?.attachmentId, mode.value],
  () => {
    if (mode.value !== 'image' || !hasServerId.value) {
      loading.value = false
      loadError.value = false
      revokeOwned()
      return
    }
    fetchPreview()
  },
  { immediate: true }
)

onUnmounted(() => {
  revokeOwned()
})
</script>

<style scoped>
.agent-chat-attachment-media {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
}

.agent-chat-attachment-thumb {
  display: block;
  max-width: min(220px, 100%);
  max-height: 200px;
  width: auto;
  height: auto;
  border-radius: 10px;
  border: 1px solid #e5e7eb;
  object-fit: contain;
  background: #f9fafb;
  box-sizing: border-box;
}

html.dark .agent-chat-attachment-thumb {
  border-color: rgba(255, 255, 255, 0.12);
  background: rgba(255, 255, 255, 0.06);
}

.agent-chat-attachment-spinner {
  min-height: 48px;
  min-width: 48px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.agent-chat-file-box {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  max-width: 220px;
  padding: 6px 10px;
  border-radius: 10px;
  border: 1px solid #e5e7eb;
  background: #f9fafb;
  box-sizing: border-box;
}

html.dark .agent-chat-file-box {
  border-color: rgba(255, 255, 255, 0.12);
  background: rgba(255, 255, 255, 0.06);
}

.agent-chat-file-box--readonly {
  cursor: default;
}

.agent-chat-file-box-icon {
  flex-shrink: 0;
  color: #64748b;
}

html.dark .agent-chat-file-box-icon {
  color: rgba(255, 255, 255, 0.55);
}

.agent-chat-file-box-name {
  flex: 1;
  min-width: 0;
  font-size: 13px;
  line-height: 1.35;
  color: #334155;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

html.dark .agent-chat-file-box-name {
  color: rgba(255, 255, 255, 0.88);
}
</style>
