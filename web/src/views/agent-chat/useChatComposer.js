import { ref } from 'vue'

/** 与后端 CHAT_UPLOAD_MAX_FILES_PER_MESSAGE 对齐 */
export const MAX_CHAT_ATTACHMENTS = 5

export function chatAttachmentIcon(att) {
  const kind = String(att.kind || '').toLowerCase()
  const mime = String(att.mime || '').toLowerCase()
  const name = String(att.name || '').toLowerCase()
  if (kind === 'image' || mime.startsWith('image/')) return 'mdi:file-image-outline'
  if (kind === 'table' || mime.includes('spreadsheet') || /\.(csv|xlsx?|xls)$/i.test(name))
    return 'mdi:table-large'
  if (kind === 'document' || mime === 'application/pdf' || /\.pdf$/i.test(name))
    return 'mdi:file-pdf-box'
  return 'mdi:file-document-outline'
}

export function useChatComposer({ t }) {
  const pendingFiles = ref([])
  const uploadResetKey = ref(0)
  let fileIdSeq = 0

  function handleUploadRequest({ onFinish }) {
    onFinish()
  }

  function onUploadChange(options) {
    const rawList = options.fileList || []
    if (rawList.length > MAX_CHAT_ATTACHMENTS) {
      window.$message?.warning(
        t('views.agents.chat_attachments_limit', { n: MAX_CHAT_ATTACHMENTS })
      )
    }
    const fileList = rawList.slice(0, MAX_CHAT_ATTACHMENTS)
    pendingFiles.value = fileList.map((item) => {
      const raw = item.file
      const fileObj = raw instanceof File ? raw : raw?.file
      return {
        id: ++fileIdSeq,
        name: fileObj?.name || item.name || 'file',
        file: fileObj,
        kind: '',
        mime: (fileObj && fileObj.type) || '',
      }
    })
  }

  function removePendingFile(index) {
    pendingFiles.value.splice(index, 1)
  }

  function resetPending() {
    pendingFiles.value = []
    uploadResetKey.value += 1
  }

  return {
    pendingFiles,
    uploadResetKey,
    handleUploadRequest,
    onUploadChange,
    removePendingFile,
    resetPending,
  }
}
