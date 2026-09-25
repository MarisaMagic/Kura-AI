/**
 * 批量文档上传内核（智能体知识库 / 实验数据集共用）。
 *
 * 大批量（数百至上万文件）下的关键约束：
 * 1. 上传限并发（UPLOAD_CONCURRENCY）：其余任务显示「排队中」，避免瞬间占满浏览器同源连接
 *    （HTTP/1.1 仅 6）与后端事件循环。
 * 2. 进度按批合并轮询（一次最多 POLL_BATCH_SIZE 个 task_id），把 N 任务 2N QPS 降到个位数；
 *    页面隐藏时进一步降频，移动端后台不再风暴式轮询。
 * 3. 上传进度事件节流：同一任务 1% 内且 250ms 内不重复写响应式，避免高频重渲染卡死主线程。
 * 4. 列表仅渲染前 MAX_RENDERED 行（任务明细 / 待上传清单），极端数量下仍保持可交互。
 * 5. 活动任务写 sessionStorage，页面重新挂载后 restoreTasks() 续跑；
 *    有活动任务时 beforeunload 二次确认，降低误触离开导致未上传文件丢失的概率。
 */
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'

export const UPLOAD_CONCURRENCY = 4
export const TERMINAL_STATUSES = ['completed', 'failed', 'timeout', 'cancelled']
export const MAX_UPLOAD_FILE_BYTES = 50 * 1024 * 1024
export const MAX_UPLOAD_FILE_MB = Math.round(MAX_UPLOAD_FILE_BYTES / (1024 * 1024))

const POLL_INTERVAL_MS = 1000
const POLL_INTERVAL_HIDDEN_MS = 5000
const POLL_BATCH_SIZE = 100
const POLL_ERROR_LIMIT = 5
const UPLOAD_MAX_ATTEMPTS = 3
const UPLOAD_RETRY_BASE_MS = 800
const LIST_REFRESH_DEBOUNCE_MS = 2500
const PERCENT_MIN_DELTA = 1
const PERCENT_MIN_INTERVAL_MS = 250
const MAX_RENDERED_ROWS = 300

/** texts 值可为字符串或返回字符串的函数（函数形式保证 i18n 切换语言后仍实时生效） */
const DEFAULT_TEXTS = {
  statusUploading: '正在上传',
  statusQueued: '排队中',
  statusProcessing: '处理中',
  statusCompleted: '已完成',
  statusFailed: '失败',
  statusTimeout: '超时',
  statusCancelled: '已取消',
  stageUploading: '正在上传文件…',
  stageUnchanged: '内容未变化，跳过重建',
  stageDone: '解析、向量化并入库完成',
  stageCancelled: '已取消处理',
  stageTimeoutFallback: '处理超时已中止',
  stageFailedFallback: '处理失败',
  stageQueueUpload: '排队等待上传…',
  stageQueueUploadWithPos: (n) => `排队等待上传（第 ${n} 位）…`,
  stageQueueProcess: '等待处理…',
  stageParsing: '解析文档',
  stageChunking: '分块',
  stageEmbedding: '向量化',
  stageWriting: '写入知识库',
  stageProcessing: '处理中',
  batchAllDone: (total) => `本批 ${total} 个文档全部处理完成`,
  batchPartial: ({ total, done, failed, throttled }) => {
    const hint = throttled ? `（其中 ${throttled} 个为嵌入服务限流，稍后重传即可）` : ''
    return `本批 ${total} 个文档：成功 ${done}，失败 ${failed}${hint}`
  },
  cancelRequested: '已请求取消处理',
  cancelFailed: '取消失败',
  statusLost: '状态查询失败，请稍后刷新列表确认',
  taskGone: '任务不存在或已过期（服务可能已重启）',
  uploadFailed: '上传失败',
  uploadNoTaskId: '上传失败：服务未返回任务 ID',
  uploadRetryHint: (attempt, max) => `连接中断，正在重试（${attempt}/${max}）`,
  uploadBusy: '服务繁忙，正在排队重试…',
}

function rt(value, ...args) {
  return typeof value === 'function' ? value(...args) : value
}

/** 与后端 app/utils/document_types.py 的 CODE_EXTS 对齐（前端仅用于图标/样式） */
const CODE_EXTS = new Set([
  'py',
  'pyi',
  'pyw',
  'js',
  'jsx',
  'mjs',
  'cjs',
  'ts',
  'tsx',
  'java',
  'kt',
  'kts',
  'scala',
  'groovy',
  'go',
  'rs',
  'c',
  'h',
  'cc',
  'cpp',
  'cxx',
  'hpp',
  'hh',
  'hxx',
  'cs',
  'm',
  'mm',
  'rb',
  'php',
  'swift',
  'lua',
  'pl',
  'pm',
  'r',
  'dart',
  'vue',
  'svelte',
  'sql',
  'proto',
  'sh',
  'bash',
  'zsh',
  'fish',
  'ps1',
  'bat',
  'cmd',
  'yaml',
  'yml',
  'toml',
  'ini',
  'cfg',
  'conf',
  'json',
  'jsonc',
  'xml',
  'html',
  'htm',
  'css',
  'scss',
  'less',
  'gradle',
  'tf',
])
const SPECIAL_CODE_NAMES = new Set([
  'dockerfile',
  'makefile',
  'gnumakefile',
  'cmakelists.txt',
  'jenkinsfile',
])

export function extFromName(name) {
  return (
    String(name || '')
      .split('.')
      .pop() || ''
  ).toLowerCase()
}

export function fileKindKey(fileType, name) {
  const type = String(fileType || '').toLowerCase()
  const ext = extFromName(name)
  if (type === 'pdf' || ext === 'pdf') return 'pdf'
  if (type === 'word' || ext === 'doc' || ext === 'docx') return 'word'
  if (type === 'excel' || ext === 'xls' || ext === 'xlsx' || ext === 'csv') return 'excel'
  if (ext === 'md' || ext === 'markdown') return 'md'
  if (
    type === 'code' ||
    CODE_EXTS.has(ext) ||
    SPECIAL_CODE_NAMES.has(String(name || '').toLowerCase())
  )
    return 'code'
  return 'text'
}

export function fileKindIcon(fileType, name) {
  const key = fileKindKey(fileType, name)
  if (key === 'pdf') return 'mdi:file-pdf-box'
  if (key === 'word') return 'mdi:file-word-box'
  if (key === 'excel') return 'mdi:file-excel-box'
  if (key === 'md') return 'simple-icons:markdown'
  if (key === 'code') return 'mdi:file-code-outline'
  return 'mdi:file-document-outline'
}

/** 各业务页面自适应类名前缀（如 exp-kind-*、agent-kb-kind-*），保持既有 CSS 生效 */
export function makeFileKindClass(prefix) {
  return (fileType, name) => `${prefix}${fileKindKey(fileType, name)}`
}

export function fileKindTagType(fileType, name) {
  const key = fileKindKey(fileType, name)
  if (key === 'pdf') return 'error'
  if (key === 'word') return 'info'
  if (key === 'excel') return 'success'
  if (key === 'code') return 'warning'
  return 'default'
}

export function formatFileSize(bytes) {
  const n = Number(bytes)
  if (!Number.isFinite(n) || n < 0) return '-'
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / (1024 * 1024)).toFixed(1)} MB`
}

export function formatModified(ts) {
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return ''
  const pad = (v) => String(v).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(
    d.getHours()
  )}:${pad(d.getMinutes())}`
}

export function isTaskActive(task) {
  return ['uploading', 'queued', 'processing', 'running'].includes(task.status)
}

export function isTaskFailed(status) {
  return status === 'failed' || status === 'timeout'
}

export function clampPercent(percent) {
  const n = Number(percent)
  if (!Number.isFinite(n)) return 0
  return Math.min(100, Math.max(0, Math.round(n)))
}

export function taskProgressStatus(status) {
  if (status === 'completed') return 'success'
  if (status === 'failed' || status === 'timeout') return 'error'
  if (status === 'cancelled') return 'warning'
  if (status === 'uploading' || status === 'queued') return 'info'
  return 'default'
}

/**
 * 批量上传 composable。
 * @param {object} options
 * @param {string|number} options.scopeId 作用域 ID（智能体 ID / 数据集 ID），用于 sessionStorage key
 * @param {string} options.storagePrefix sessionStorage 前缀（各业务自定，避免互相覆盖）
 * @param {number} [options.maxFileBytes] 单文件大小上限
 * @param {(file: File, onProgress: Function) => Promise<string>} options.upload 上传适配器，返回 task_id
 * @param {(taskIds: string[]) => Promise<Record<string, object>>} options.statusBatch 批量状态适配器
 * @param {(taskId: string) => Promise<any>} options.cancel 取消适配器
 * @param {Function} [options.onListChanged] 列表刷新回调（已做去抖）
 * @param {object} [options.texts] 文案覆盖（字符串或函数；函数形式可保持 i18n 实时性）
 * @param {number} [options.concurrency] 上传并发度
 */
export function useUploadBatch(options = {}) {
  const {
    scopeId,
    storagePrefix,
    maxFileBytes = MAX_UPLOAD_FILE_BYTES,
    upload,
    statusBatch,
    cancel,
    onListChanged,
    texts: textOverrides = {},
    concurrency = UPLOAD_CONCURRENCY,
  } = options

  const texts = { ...DEFAULT_TEXTS, ...textOverrides }
  const maxFileMb = Math.round(Number(maxFileBytes) / (1024 * 1024))

  const tasks = ref([])
  const pendingFiles = ref([])
  const showConfirmModal = ref(false)
  const detailOpen = ref(false)
  const uploadRef = ref(null)
  let taskSeq = 0
  let batchSeq = 0
  const reportedBatches = new Set()

  const validPendingFiles = computed(() => pendingFiles.value.filter((item) => !item.overLimit))
  const pendingValidTotalSize = computed(() =>
    validPendingFiles.value.reduce((sum, item) => sum + (Number(item.size) || 0), 0)
  )
  const sortedPendingFiles = computed(() =>
    [...pendingFiles.value].sort((a, b) => Number(b.overLimit) - Number(a.overLimit))
  )
  const visiblePendingFiles = computed(() => sortedPendingFiles.value.slice(0, MAX_RENDERED_ROWS))
  const hiddenPendingCount = computed(() =>
    Math.max(0, sortedPendingFiles.value.length - MAX_RENDERED_ROWS)
  )

  const batchSummary = computed(() => {
    const total = tasks.value.length
    const done = tasks.value.filter((task) => task.status === 'completed').length
    const active = tasks.value.filter(isTaskActive).length
    return { total, done, active, failed: total - done - active }
  })

  const overallPercent = computed(() => {
    if (!tasks.value.length) return 0
    let sum = 0
    for (const task of tasks.value) sum += clampPercent(task.percent)
    return Math.round(sum / tasks.value.length)
  })

  const overallStatus = computed(() => {
    if (!tasks.value.length) return 'default'
    const allTerminal = tasks.value.every((task) => TERMINAL_STATUSES.includes(task.status))
    if (!allTerminal) return 'default'
    if (tasks.value.some((task) => isTaskFailed(task.status))) return 'error'
    if (tasks.value.some((task) => task.status === 'cancelled')) return 'warning'
    return 'success'
  })

  const hasFinishedTasks = computed(() =>
    tasks.value.some((task) => TERMINAL_STATUSES.includes(task.status))
  )

  /** 明细仅渲染前 MAX_RENDERED_ROWS 行（活动任务优先），避免上千行 DOM 拖垮主线程 */
  const visibleTasks = computed(() => {
    if (tasks.value.length <= MAX_RENDERED_ROWS) return tasks.value
    const active = tasks.value.filter(isTaskActive)
    const finished = tasks.value.filter((task) => !isTaskActive(task)).slice(-MAX_RENDERED_ROWS)
    return [...active, ...finished].slice(0, MAX_RENDERED_ROWS)
  })
  const hiddenTaskCount = computed(() =>
    Math.max(0, tasks.value.length - visibleTasks.value.length)
  )

  function taskStatusMeta(task) {
    if (task.status === 'completed') return { type: 'success', label: rt(texts.statusCompleted) }
    if (task.status === 'failed' || task.status === 'timeout') {
      return { type: 'error', label: rt(texts.statusFailed) }
    }
    if (task.status === 'cancelled') return { type: 'warning', label: rt(texts.statusCancelled) }
    if (task.status === 'uploading') return { type: 'info', label: rt(texts.statusUploading) }
    if (task.status === 'queued') return { type: 'info', label: rt(texts.statusQueued) }
    return { type: 'info', label: rt(texts.statusProcessing) }
  }

  function taskStageText(task) {
    if (task.status === 'uploading') return rt(texts.stageUploading)
    if (task.status === 'completed') {
      return task.result?.unchanged ? rt(texts.stageUnchanged) : rt(texts.stageDone)
    }
    if (task.status === 'timeout') return task.error || rt(texts.stageTimeoutFallback)
    if (task.status === 'cancelled') return rt(texts.stageCancelled)
    if (task.status === 'failed') return task.error || rt(texts.stageFailedFallback)
    if (task.status === 'queued' && !task.taskId) {
      return task.queuePosition
        ? rt(texts.stageQueueUploadWithPos, task.queuePosition)
        : rt(texts.stageQueueUpload)
    }
    if (task.status === 'queued') return rt(texts.stageQueueProcess)
    const stageTexts = {
      parsing: rt(texts.stageParsing),
      chunking: rt(texts.stageChunking),
      embedding: rt(texts.stageEmbedding),
      writing: rt(texts.stageWriting),
    }
    let label = stageTexts[task.stage] || rt(texts.stageProcessing)
    if (task.stage === 'embedding' && task.done != null && task.total != null) {
      label = `${label}（${task.done}/${task.total}）`
    }
    return label
  }

  function storageKey() {
    return `${storagePrefix}${scopeId}`
  }

  function persistActiveTasks() {
    const active = tasks.value
      .filter((task) => task.taskId && !TERMINAL_STATUSES.includes(task.status))
      .map((task) => ({ task_id: task.taskId, filename: task.filename }))
    try {
      if (active.length) sessionStorage.setItem(storageKey(), JSON.stringify(active))
      else sessionStorage.removeItem(storageKey())
    } catch (e) {
      /* sessionStorage 不可用时忽略 */
    }
  }

  function makeTask({ filename = '', taskId = null, batchId = null } = {}) {
    const task = reactive({
      key: `task_${++taskSeq}`,
      taskId,
      batchId,
      filename,
      status: 'uploading',
      stage: 'uploading',
      percent: 0,
      done: null,
      total: null,
      error: '',
      errorType: '',
      result: null,
      queuePosition: null,
      timer: null,
      pollErrors: 0,
      lastPercentAt: 0,
    })
    tasks.value.push(task)
    return task
  }

  function clearTimer(task) {
    if (task.timer) {
      clearTimeout(task.timer)
      task.timer = null
    }
  }

  // 列表刷新去抖：上千任务逐个完成时不至于每次都重新拉全量文档列表
  let listChangedTimer = null

  function notifyListChanged() {
    if (listChangedTimer) return
    listChangedTimer = setTimeout(() => {
      listChangedTimer = null
      onListChanged?.()
    }, LIST_REFRESH_DEBOUNCE_MS)
  }

  // ---------------------------------------------------------------- 上传调度（限并发）

  const uploadQueue = []
  let activeUploads = 0

  function enqueueUpload(task, file) {
    task.status = 'queued'
    task.stage = 'uploading'
    uploadQueue.push({ task, file })
    pumpUploads()
  }

  function pumpUploads() {
    while (activeUploads < concurrency && uploadQueue.length) {
      const next = uploadQueue.shift()
      activeUploads += 1
      uploadOne(next.task, next.file).finally(() => {
        activeUploads -= 1
        pumpUploads()
      })
    }
    updateQueueHint()
  }

  function updateQueueHint() {
    uploadQueue.forEach((item, i) => {
      if (item.task.status === 'queued' && !item.task.taskId) {
        item.task.queuePosition = i + 1
      }
    })
  }

  // ---------------------------------------------------------------- 轮询（批量合并）

  let pollTimer = null
  let polling = false

  function pollableTasks() {
    return tasks.value.filter((t) => t.taskId && !TERMINAL_STATUSES.includes(t.status))
  }

  function nextInterval() {
    return typeof document !== 'undefined' && document.hidden
      ? POLL_INTERVAL_HIDDEN_MS
      : POLL_INTERVAL_MS
  }

  function ensurePolling() {
    if (pollTimer || polling) return
    pollTimer = setTimeout(runPoll, nextInterval())
  }

  function stopPollingIfIdle() {
    if (pollTimer && !pollableTasks().length) {
      clearTimeout(pollTimer)
      pollTimer = null
    }
  }

  async function runPoll() {
    pollTimer = null
    polling = true
    try {
      const pending = pollableTasks()
      if (!pending.length) return
      const chunks = []
      for (let i = 0; i < pending.length; i += POLL_BATCH_SIZE) {
        chunks.push(pending.slice(i, i + POLL_BATCH_SIZE))
      }
      await Promise.all(chunks.map(pollChunk))
    } finally {
      polling = false
      if (pollableTasks().length) ensurePolling()
      else stopPollingIfIdle()
    }
  }

  async function pollChunk(chunk) {
    let items = null
    try {
      const ids = chunk.map((t) => t.taskId)
      items = (await statusBatch(ids)) || {}
      // 响应契约校验：返回非空却与本批 task_id 完全对不上，说明接口约定异常
      // （如服务端返回了错误的 key），按 chunk 级查询失败处理，避免误报「任务不存在」。
      if (Object.keys(items).length && !ids.some((id) => items[id])) {
        console.warn(
          '[upload] 批量状态响应 key 与 task_id 不匹配，按查询失败处理：',
          Object.keys(items).slice(0, 3)
        )
        throw Object.assign(new Error('status items key mismatch'), { code: 0 })
      }
    } catch (e) {
      const httpStatus = Number(e?.code || 0)
      chunk.forEach((task) => {
        if (httpStatus === 404) {
          task.status = 'failed'
          task.error = rt(texts.taskGone)
          finishTask(task)
          return
        }
        task.pollErrors += 1
        if (task.pollErrors > POLL_ERROR_LIMIT) {
          task.status = 'failed'
          task.error = rt(texts.statusLost)
          finishTask(task)
        }
      })
      notifyListChanged()
      return
    }
    chunk.forEach((task) => {
      const meta = items?.[task.taskId]
      if (!meta || !meta.status) {
        task.pollErrors += 1
        if (task.pollErrors > POLL_ERROR_LIMIT) {
          task.status = 'failed'
          task.error = rt(texts.taskGone)
          finishTask(task)
        }
        return
      }
      task.pollErrors = 0
      task.status = meta.status === 'running' ? 'processing' : meta.status
      task.stage = meta.stage || task.stage
      task.percent = Number(meta.percent ?? task.percent ?? 0)
      task.done = meta.done ?? null
      task.total = meta.total ?? null
      task.error = meta.error || ''
      task.errorType = meta.error_type || ''
      task.result = meta.result || null
      if (TERMINAL_STATUSES.includes(task.status)) finishTask(task)
    })
    notifyListChanged()
  }

  function finishTask(task) {
    clearTimer(task)
    persistActiveTasks()
    notifyListChanged()
    if (task.batchId == null || reportedBatches.has(task.batchId)) return
    const batchTasks = tasks.value.filter((t) => t.batchId === task.batchId)
    if (!batchTasks.length) return
    if (!batchTasks.every((t) => TERMINAL_STATUSES.includes(t.status))) return
    reportedBatches.add(task.batchId)
    const total = batchTasks.length
    const done = batchTasks.filter((t) => t.status === 'completed').length
    const failed = total - done
    const throttled = batchTasks.filter((t) => t.errorType === 'throttled').length
    if (failed === 0) {
      window.$message?.success(rt(texts.batchAllDone, total))
    } else {
      window.$message?.warning(rt(texts.batchPartial, { total, done, failed, throttled }), {
        duration: 8000,
      })
    }
  }

  function clearFinishedTasks() {
    tasks.value.forEach((task) => {
      if (!isTaskActive(task)) clearTimer(task)
    })
    tasks.value = tasks.value.filter(isTaskActive)
    stopPollingIfIdle()
  }

  function restoreTasks() {
    let saved = []
    try {
      saved = JSON.parse(sessionStorage.getItem(storageKey()) || '[]')
    } catch (e) {
      saved = []
    }
    saved.forEach((item) => {
      if (!item?.task_id) return
      const task = makeTask({ filename: item.filename || '', taskId: item.task_id })
      task.status = 'queued'
    })
    if (tasks.value.length) ensurePolling()
  }

  function onUploadChange(optionsPayload) {
    const f = optionsPayload.file?.file
    uploadRef.value?.clear()
    if (!f) return
    const name = f.name || ''
    const size = Number(f.size) || 0
    if (!pendingFiles.value.some((p) => p.name === name && p.size === size)) {
      pendingFiles.value.push({
        file: f,
        name,
        ext: extFromName(name),
        size,
        modifiedAt: f.lastModified || null,
        overLimit: size > maxFileBytes,
      })
    }
    showConfirmModal.value = true
  }

  function cancelConfirm() {
    pendingFiles.value = []
    showConfirmModal.value = false
  }

  function onConfirmModalShowUpdate(v) {
    if (!v) cancelConfirm()
  }

  // 传输阶段失败自动重试：浏览器 ERR_NETWORK / 连接被重置时等待后重发，避免直接判失败
  function isNetworkError(e) {
    const code = String(e?.error?.code || e?.code || '')
    return (
      code.startsWith('ERR_NETWORK') || code === 'ERR_EMPTY_RESPONSE' || code === 'ECONNABORTED'
    )
  }

  // 服务端背压（队列已满 429）：等待后重试，大批量上传自动降速而不是直接失败
  function isBusyError(e) {
    return Number(e?.code) === 429
  }

  function retryDelayMs(e, attempt) {
    return (isBusyError(e) ? 3000 : UPLOAD_RETRY_BASE_MS) * attempt
  }

  function onUploadProgress(task, ev) {
    if (!ev?.total) return
    const percent = Math.min(100, Math.round((ev.loaded / ev.total) * 100))
    if (percent === task.percent) return
    const now = Date.now()
    if (
      percent - task.percent < PERCENT_MIN_DELTA &&
      now - (task.lastPercentAt || 0) < PERCENT_MIN_INTERVAL_MS
    ) {
      return
    }
    task.percent = percent
    task.lastPercentAt = now
  }

  async function uploadOne(task, file) {
    clearTimer(task)
    for (let attempt = 1; ; attempt += 1) {
      task.status = 'uploading'
      task.stage = 'uploading'
      task.queuePosition = null
      try {
        const taskId = await upload(file, (ev) => onUploadProgress(task, ev))
        if (!taskId) throw new Error(rt(texts.uploadNoTaskId))
        task.taskId = taskId
        task.status = 'queued'
        task.error = ''
        task.percent = 0
        persistActiveTasks()
        ensurePolling()
        return
      } catch (e) {
        if (attempt < UPLOAD_MAX_ATTEMPTS && (isNetworkError(e) || isBusyError(e))) {
          task.error = isBusyError(e)
            ? e?.message || rt(texts.uploadBusy)
            : rt(texts.uploadRetryHint, attempt, UPLOAD_MAX_ATTEMPTS - 1)
          await new Promise((r) => setTimeout(r, retryDelayMs(e, attempt)))
          continue
        }
        task.status = 'failed'
        task.error = task.error || e?.message || rt(texts.uploadFailed)
        finishTask(task)
        return
      }
    }
  }

  function confirmUpload() {
    const items = validPendingFiles.value
    if (!items.length) return
    const batchId = ++batchSeq
    pendingFiles.value = []
    showConfirmModal.value = false
    items.forEach((item) => {
      const task = makeTask({ filename: item.name, batchId })
      enqueueUpload(task, item.file)
    })
  }

  async function cancelTask(task) {
    if (!task.taskId) {
      const idx = uploadQueue.findIndex((item) => item.task === task)
      if (idx >= 0) uploadQueue.splice(idx, 1)
      task.status = 'cancelled'
      task.percent = 0
      updateQueueHint()
      finishTask(task)
      return
    }
    try {
      await cancel(task.taskId)
      task.status = 'cancelled'
      task.percent = 0
      finishTask(task)
      window.$message?.info(rt(texts.cancelRequested))
    } catch (e) {
      window.$message?.error(e?.message || rt(texts.cancelFailed))
    }
  }

  function openFilePicker() {
    uploadRef.value?.openOpenFileDialog?.()
  }

  function hasActiveTasks() {
    return tasks.value.some(isTaskActive)
  }

  function handleBeforeUnload(e) {
    if (!hasActiveTasks()) return
    e.preventDefault()
    e.returnValue = ''
  }

  function handleVisibilityChange() {
    if (!document.hidden) ensurePolling()
  }

  onMounted(() => {
    window.addEventListener('beforeunload', handleBeforeUnload)
    document.addEventListener('visibilitychange', handleVisibilityChange)
  })

  onUnmounted(() => {
    window.removeEventListener('beforeunload', handleBeforeUnload)
    document.removeEventListener('visibilitychange', handleVisibilityChange)
    tasks.value.forEach(clearTimer)
    if (pollTimer) {
      clearTimeout(pollTimer)
      pollTimer = null
    }
    if (listChangedTimer) {
      clearTimeout(listChangedTimer)
      listChangedTimer = null
    }
  })

  return {
    tasks,
    visibleTasks,
    hiddenTaskCount,
    pendingFiles,
    visiblePendingFiles,
    hiddenPendingCount,
    showConfirmModal,
    detailOpen,
    uploadRef,
    maxFileMb,
    validPendingFiles,
    pendingValidTotalSize,
    sortedPendingFiles,
    batchSummary,
    overallPercent,
    overallStatus,
    hasFinishedTasks,
    isTaskActive,
    isTaskFailed,
    clampPercent,
    taskProgressStatus,
    taskStatusMeta,
    taskStageText,
    clearFinishedTasks,
    restoreTasks,
    onUploadChange,
    cancelConfirm,
    onConfirmModalShowUpdate,
    confirmUpload,
    cancelTask,
    openFilePicker,
  }
}
