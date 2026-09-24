/**
 * 实验文档上传：共享上传内核（useUploadBatch）的适配层。
 * 交互与持久化策略见 `@/composables/useUploadBatch`（限并发、批量轮询、进度节流、断点恢复）。
 */
import api from '@/api'
import {
  MAX_UPLOAD_FILE_BYTES,
  MAX_UPLOAD_FILE_MB,
  makeFileKindClass,
  useUploadBatch,
} from '@/composables/useUploadBatch'

export {
  extFromName,
  fileKindIcon,
  fileKindKey,
  fileKindTagType,
  formatFileSize,
  formatModified,
  isTaskActive,
} from '@/composables/useUploadBatch'

export const MAX_FILE_BYTES = MAX_UPLOAD_FILE_BYTES
export const MAX_FILE_MB = MAX_UPLOAD_FILE_MB

export const fileKindClass = makeFileKindClass('exp-kind-')

const STORAGE_PREFIX = 'kura_ai_exp_upload_'

export function useExpUpload(datasetId, { onListChanged } = {}) {
  return useUploadBatch({
    scopeId: datasetId,
    storagePrefix: STORAGE_PREFIX,
    upload: async (file, onProgress) => {
      const fd = new FormData()
      fd.append('file', file)
      const res = await api.uploadExpDocument(datasetId, fd, onProgress, {
        noErrorMessage: true,
      })
      return res?.data?.task_id
    },
    statusBatch: async (taskIds) => {
      const res = await api.getExpUploadStatusBatch(taskIds)
      return res?.data?.items || {}
    },
    cancel: (taskId) => api.cancelExpUpload({ task_id: taskId }),
    onListChanged,
  })
}
