<template>
  <AppPage :show-footer="false" scroll-in-parent class="!p-0">
    <div class="agent-kb-scroll">
      <div class="agent-kb-layout">
        <header class="agent-page-header">
          <h1 class="agent-page-title">{{ $t('views.agents.title_knowledge_base') }}</h1>
          <div v-if="agentName" class="agent-kb-subtitle">{{ agentName }}</div>
        </header>

        <n-spin :show="pageLoading">
          <div class="agent-kb-section">
            <h2 class="agent-kb-h2">{{ $t('views.agents.kb_upload_title') }}</h2>
            <p class="agent-kb-hint">{{ $t('views.agents.kb_upload_hint') }}</p>
            <n-upload
              ref="uploadRef"
              :show-file-list="false"
              multiple
              accept=".pdf,.doc,.docx,.xls,.xlsx,.txt,.md"
              @change="onUploadChange"
            >
              <n-button type="primary">
                {{ $t('views.agents.kb_upload_title') }}
              </n-button>
            </n-upload>
          </div>

          <div v-if="tasks.length" class="agent-kb-section">
            <div class="agent-kb-overview">
              <div class="agent-kb-overview-head">
                <h2 class="agent-kb-h2 agent-kb-overview-title">
                  {{ $t('views.agents.kb_process_title') }}
                </h2>
                <button
                  type="button"
                  class="agent-kb-detail-toggle"
                  @click="detailOpen = !detailOpen"
                >
                  {{ $t('views.agents.kb_detail_toggle') }}
                  <TheIcon :icon="detailOpen ? 'mdi:chevron-up' : 'mdi:chevron-down'" :size="16" />
                </button>
              </div>
              <n-progress
                type="line"
                :percentage="overallPercent"
                :status="overallStatus"
                :height="10"
              />
              <div class="agent-kb-batch-summary">
                {{ $t('views.agents.kb_batch_summary', batchSummary) }}
              </div>
            </div>
            <n-collapse-transition :show="detailOpen">
              <div class="agent-kb-tasks">
                <div v-for="task in tasks" :key="task.key" class="agent-kb-task">
                  <div class="agent-kb-task-head">
                    <span class="agent-kb-task-name" :title="task.filename">{{
                      task.filename
                    }}</span>
                    <n-button
                      v-if="isTaskActive(task) && task.taskId"
                      size="tiny"
                      quaternary
                      type="error"
                      @click="cancelTask(task)"
                    >
                      {{ $t('views.agents.kb_task_cancel') }}
                    </n-button>
                  </div>
                  <n-progress
                    type="line"
                    :percentage="clampPercent(task.percent)"
                    :status="taskProgressStatus(task.status)"
                    :height="8"
                  />
                  <div
                    class="agent-kb-task-stage"
                    :class="{ 'agent-kb-task-stage-error': isTaskFailed(task.status) }"
                  >
                    {{ taskStageText(task) }}
                  </div>
                </div>
              </div>
            </n-collapse-transition>
          </div>

          <div class="agent-kb-section">
            <div class="agent-kb-list-head">
              <h2 class="agent-kb-h2 agent-kb-overview-title">
                {{ $t('views.agents.kb_list_title') }}
              </h2>
              <span class="agent-kb-list-count">
                {{ $t('views.agents.kb_list_total', { count: list.length }) }}
              </span>
            </div>
            <div class="agent-kb-list-toolbar">
              <n-input
                v-model:value="keyword"
                :placeholder="$t('views.agents.kb_search_placeholder')"
                clearable
                class="agent-kb-search"
              />
              <n-select
                v-model:value="typeFilter"
                :options="typeOptions"
                class="agent-kb-type-filter"
              />
            </div>
            <n-data-table
              :columns="columns"
              :data="filteredList"
              :loading="tableLoading"
              :pagination="false"
              :bordered="true"
              size="small"
            />
            <div v-if="!list.length && !tableLoading" class="agent-kb-empty">
              {{ $t('views.agents.kb_empty') }}
            </div>
          </div>
        </n-spin>

        <n-modal
          v-model:show="showConfirmModal"
          preset="card"
          :title="$t('views.agents.kb_confirm_title')"
          :style="{ width: 'min(560px, 92vw)' }"
          @update:show="onConfirmModalShowUpdate"
        >
          <p class="agent-kb-confirm-hint">
            {{
              $t('views.agents.kb_confirm_hint', {
                total: pendingFiles.length,
                valid: validPendingFiles.length,
              })
            }}
          </p>
          <div class="agent-kb-file-list">
            <div
              v-for="item in pendingFiles"
              :key="`${item.name}_${item.size}`"
              class="agent-kb-file-row"
              :class="{ 'agent-kb-file-row-invalid': item.overLimit }"
            >
              <TheIcon :icon="pendingFileIcon(item.ext)" :size="20" class="agent-kb-file-icon" />
              <div class="agent-kb-file-main">
                <div class="agent-kb-file-name" :title="item.name">{{ item.name }}</div>
                <div class="agent-kb-file-meta">
                  <span class="agent-kb-file-ext">{{ item.ext.toUpperCase() || '-' }}</span>
                  <span>{{ formatFileSize(item.size) }}</span>
                  <span v-if="item.modifiedAt">{{ formatModified(item.modifiedAt) }}</span>
                  <span v-if="item.overLimit" class="agent-kb-file-over">
                    {{ $t('views.agents.kb_file_over_limit', { mb: 50 }) }}
                  </span>
                </div>
              </div>
            </div>
          </div>
          <template #footer>
            <div class="agent-kb-confirm-footer">
              <n-button quaternary @click="cancelConfirm">
                {{ $t('views.agents.kb_confirm_cancel') }}
              </n-button>
              <n-button type="primary" :disabled="!validPendingFiles.length" @click="confirmUpload">
                {{ $t('views.agents.kb_confirm_ok') }}
                <template v-if="validPendingFiles.length">
                  （{{ validPendingFiles.length }}）</template
                >
              </n-button>
            </div>
          </template>
        </n-modal>
      </div>
    </div>
  </AppPage>
</template>

<script setup>
import { computed, h, onMounted, onUnmounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import {
  NButton,
  NCollapseTransition,
  NDataTable,
  NInput,
  NModal,
  NPopconfirm,
  NProgress,
  NSelect,
  NSpin,
  NUpload,
  useMessage,
} from 'naive-ui'
import AppPage from '@/components/page/AppPage.vue'
import TheIcon from '@/components/icon/TheIcon.vue'
import api from '@/api'

const { t } = useI18n()
const route = useRoute()
const message = useMessage()

const pageLoading = ref(true)
const tableLoading = ref(false)
const agentId = ref(Number(route.params.agentId) || 0)
const agentName = ref('')
const list = ref([])
const uploadRef = ref(null)

// 上传确认弹窗：选中文件先进入待确认列表，确认后才真正发起上传
const MAX_FILE_BYTES = 50 * 1024 * 1024 // 与后端 KB_UPLOAD_MAX_BYTES 默认值一致
const showConfirmModal = ref(false)
const pendingFiles = ref([])
let batchSeq = 0
const reportedBatches = new Set()
const detailOpen = ref(false)

// 文档列表搜索/筛选（纯前端过滤，列表本身全量拉取）
const keyword = ref('')
const typeFilter = ref('all')

const typeOptions = computed(() => [
  { label: t('views.agents.kb_filter_type_all'), value: 'all' },
  { label: 'PDF', value: 'PDF' },
  { label: 'Word', value: 'Word' },
  { label: 'Excel', value: 'Excel' },
  { label: 'Text', value: 'Text' },
])

const filteredList = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  return list.value.filter((doc) => {
    if (typeFilter.value !== 'all' && doc.file_type !== typeFilter.value) return false
    if (kw && !(doc.display_filename || '').toLowerCase().includes(kw)) return false
    return true
  })
})

// 上传任务面板：上传传输 + 处理进度轮询，支持多任务并行显示
const POLL_INTERVAL_MS = 500
const STORAGE_PREFIX = 'kura_ai_kb_upload_'
const TERMINAL_STATUSES = ['completed', 'failed', 'timeout', 'cancelled']
const tasks = ref([])
let taskSeq = 0

// 批次汇总：total/done/active/failed，由任务数组实时计算
const batchSummary = computed(() => {
  const total = tasks.value.length
  const done = tasks.value.filter((task) => task.status === 'completed').length
  const active = tasks.value.filter((task) => isTaskActive(task)).length
  return { total, done, active, failed: total - done - active }
})

// 总任务进度：各任务百分比的平均值；状态按终态结果聚合
const overallPercent = computed(() => {
  if (!tasks.value.length) return 0
  const sum = tasks.value.reduce((acc, task) => acc + clampPercent(task.percent), 0)
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

const validPendingFiles = computed(() => pendingFiles.value.filter((item) => !item.overLimit))

function formatFileSize(bytes) {
  const n = Number(bytes)
  if (!Number.isFinite(n) || n < 0) return '-'
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / (1024 * 1024)).toFixed(1)} MB`
}

function pendingFileIcon(ext) {
  if (ext === 'pdf') return 'mdi:file-pdf-box'
  if (ext === 'md') return 'simple-icons:markdown'
  return 'mdi:file-document-outline'
}

function formatModified(ts) {
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return ''
  const pad = (v) => String(v).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(
    d.getHours()
  )}:${pad(d.getMinutes())}`
}

function storageKey() {
  return `${STORAGE_PREFIX}${agentId.value}`
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
    status: 'uploading', // uploading | queued | processing | completed | failed | timeout | cancelled
    stage: 'uploading',
    percent: 0,
    done: null,
    total: null,
    error: '',
    result: null,
    timer: null,
    pollErrors: 0,
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

function isTaskActive(task) {
  return ['uploading', 'queued', 'processing', 'running'].includes(task.status)
}

function isTaskFailed(status) {
  return status === 'failed' || status === 'timeout'
}

function clampPercent(percent) {
  const n = Number(percent)
  if (!Number.isFinite(n)) return 0
  return Math.min(100, Math.max(0, Math.round(n)))
}

function taskProgressStatus(status) {
  if (status === 'completed') return 'success'
  if (status === 'failed' || status === 'timeout') return 'error'
  if (status === 'cancelled') return 'warning'
  if (status === 'uploading' || status === 'queued') return 'info'
  return 'default'
}

function taskStageText(task) {
  if (task.status === 'uploading') return t('views.agents.kb_stage_uploading')
  if (task.status === 'completed') {
    return task.result?.unchanged
      ? t('views.agents.kb_upload_unchanged')
      : t('views.agents.kb_stage_done')
  }
  if (task.status === 'timeout') return task.error || t('views.agents.kb_upload_timeout_title')
  if (task.status === 'cancelled') return t('views.agents.kb_upload_cancelled_title')
  if (task.status === 'failed') return task.error || t('views.agents.kb_upload_failed_title')
  if (task.status === 'queued') return t('views.agents.kb_stage_queued')
  const stageTexts = {
    parsing: t('views.agents.kb_stage_parsing'),
    chunking: t('views.agents.kb_stage_chunking'),
    embedding: t('views.agents.kb_stage_embedding'),
    writing: t('views.agents.kb_stage_writing'),
  }
  let label = stageTexts[task.stage] || t('views.agents.kb_stage_processing')
  if (task.stage === 'embedding' && task.done != null && task.total != null) {
    label = `${label}（${task.done}/${task.total}）`
  }
  return label
}

function startPolling(task) {
  clearTimer(task)
  const tick = async () => {
    clearTimer(task)
    if (!task.taskId) return
    try {
      const res = await api.getKbUploadStatus({ task_id: task.taskId }, { noErrorMessage: true })
      const meta = res?.data
      if (!meta || !meta.status) throw new Error('empty status')
      task.pollErrors = 0
      task.status = meta.status === 'running' ? 'processing' : meta.status
      task.stage = meta.stage || task.stage
      task.percent = Number(meta.percent ?? task.percent ?? 0)
      task.done = meta.done ?? null
      task.total = meta.total ?? null
      task.error = meta.error || ''
      task.result = meta.result || null
      if (TERMINAL_STATUSES.includes(task.status)) {
        finishTask(task)
        return
      }
      task.timer = setTimeout(tick, POLL_INTERVAL_MS)
    } catch (e) {
      // 错误分流：404=任务确定不存在立即终局；401/403=鉴权失效交给登出流程；其余按网络抖动容忍
      const httpStatus = Number(e?.code || 0)
      if (httpStatus === 404) {
        task.status = 'failed'
        task.error = t('views.agents.kb_upload_gone')
        finishTask(task)
        return
      }
      if (httpStatus === 401 || httpStatus === 403) {
        clearTimer(task)
        persistActiveTasks()
        return
      }
      // 网络抖动/超时：连续多次失败才判中断；期间刷新列表兜底核对文档是否其实已入库
      task.pollErrors += 1
      if (task.pollErrors === 2) fetchList()
      if (task.pollErrors > 3) {
        task.status = 'failed'
        task.error = t('views.agents.kb_upload_status_lost')
        finishTask(task)
        return
      }
      task.timer = setTimeout(tick, 1000)
    }
  }
  task.timer = setTimeout(tick, POLL_INTERVAL_MS)
}

// 每批上传只在全部结束时弹一次汇总消息（reportedBatches 防重）；恢复的任务（batchId=null）静默收尾
function maybeReportBatch(batchId) {
  if (batchId == null || reportedBatches.has(batchId)) return
  const batchTasks = tasks.value.filter((task) => task.batchId === batchId)
  if (!batchTasks.length) return
  if (!batchTasks.every((task) => TERMINAL_STATUSES.includes(task.status))) return
  reportedBatches.add(batchId)
  const total = batchTasks.length
  const done = batchTasks.filter((task) => task.status === 'completed').length
  const failed = total - done
  if (failed === 0) {
    message.success(t('views.agents.kb_batch_done_all', { total }))
  } else {
    message.warning(t('views.agents.kb_batch_done_partial', { total, done, failed }), {
      duration: 8000,
    })
  }
}

function finishTask(task) {
  clearTimer(task)
  persistActiveTasks()
  fetchList()
  maybeReportBatch(task.batchId)
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
    startPolling(task)
  })
}

const columns = [
  {
    title: () => t('views.agents.kb_col_filename'),
    key: 'display_filename',
    ellipsis: { tooltip: true },
  },
  { title: () => t('views.agents.kb_col_type'), key: 'file_type', width: 100 },
  { title: () => t('views.agents.kb_col_chunks'), key: 'chunk_count', width: 100 },
  { title: () => t('views.agents.kb_col_updated'), key: 'updated_at', width: 180 },
  {
    title: '',
    key: 'actions',
    width: 100,
    render(row) {
      return h(
        NPopconfirm,
        {
          onPositiveClick: () => handleDelete(row.display_filename),
        },
        {
          trigger: () =>
            h(
              NButton,
              { size: 'small', quaternary: true, type: 'error' },
              { default: () => t('common.buttons.delete') }
            ),
          default: () => t('views.agents.kb_confirm_delete'),
        }
      )
    },
  },
]

async function loadAgent() {
  const res = await api.getUserAgent({ agent_id: agentId.value })
  agentName.value = res.data?.name || ''
}

async function fetchList() {
  tableLoading.value = true
  try {
    const res = await api.getKbDocuments({ agent_id: agentId.value })
    const docs = res.data?.documents ?? []
    list.value = Array.isArray(docs) ? docs : []
  } finally {
    tableLoading.value = false
  }
}

// 选中文件先进入待确认列表并打开确认弹窗；弹窗打开期间追加的选择直接并入列表
function onUploadChange(options) {
  const f = options.file?.file
  // 清理组件内部 fileList，避免历史选择累积影响后续选择行为
  uploadRef.value?.clear()
  if (!f) return
  const name = f.name || ''
  const size = Number(f.size) || 0
  if (!pendingFiles.value.some((p) => p.name === name && p.size === size)) {
    pendingFiles.value.push({
      file: f,
      name,
      ext: (name.split('.').pop() || '').toLowerCase(),
      size,
      modifiedAt: f.lastModified || null,
      overLimit: size > MAX_FILE_BYTES,
    })
  }
  showConfirmModal.value = true
}

function cancelConfirm() {
  pendingFiles.value = []
  showConfirmModal.value = false
}

// 点遮罩/右上角关闭视为取消
function onConfirmModalShowUpdate(v) {
  if (!v) cancelConfirm()
}

function confirmUpload() {
  const items = validPendingFiles.value
  if (!items.length) return
  const batchId = ++batchSeq
  pendingFiles.value = []
  showConfirmModal.value = false
  items.forEach((item) => {
    const task = makeTask({ filename: item.name, batchId })
    uploadOne(task, item.file)
  })
}

async function uploadOne(task, file) {
  const fd = new FormData()
  fd.append('file', file)
  try {
    const res = await api.uploadKbDocument(
      agentId.value,
      fd,
      (ev) => {
        if (ev?.total) {
          task.percent = Math.min(100, Math.round((ev.loaded / ev.total) * 100))
        }
      },
      { noErrorMessage: true }
    )
    const taskId = res?.data?.task_id
    if (!taskId) throw new Error('no task id')
    task.taskId = taskId
    task.status = 'queued'
    task.percent = 0
    persistActiveTasks()
    startPolling(task)
  } catch (e) {
    // 传输或受理失败：任务卡标红（原因见详情列表），批次汇总消息由 maybeReportBatch 统一弹出
    task.status = 'failed'
    task.error = e?.message || t('views.agents.kb_upload_failed_title')
    maybeReportBatch(task.batchId)
  }
}

async function cancelTask(task) {
  if (!task.taskId) return
  try {
    await api.cancelKbUploadTask({ task_id: task.taskId }, { noErrorMessage: true })
    task.status = 'cancelled'
    task.percent = 0
    clearTimer(task)
    persistActiveTasks()
    message.info(t('views.agents.kb_upload_cancel_requested'))
  } catch (e) {
    message.error(e?.message || t('views.agents.kb_upload_cancel_failed'))
  }
}

async function handleDelete(displayFilename) {
  try {
    await api.deleteKbDocument({
      agent_id: agentId.value,
      filename: displayFilename,
    })
    message.success(t('views.agents.kb_delete_ok'))
    await fetchList()
  } catch (e) {
    message.error(e?.response?.data?.msg || e?.message || 'delete failed')
  }
}

onMounted(async () => {
  pageLoading.value = true
  try {
    await loadAgent()
    await fetchList()
    restoreTasks()
  } catch (e) {
    message.error(t('views.agents.chat_error_load_agent'))
  } finally {
    pageLoading.value = false
  }
})

onUnmounted(() => {
  tasks.value.forEach(clearTimer)
})
</script>

<style scoped>
.agent-kb-scroll {
  flex: 1;
  min-height: 0;
  width: 100%;
  overflow-y: auto;
  -webkit-overflow-scrolling: touch;
  scrollbar-width: thin;
  scrollbar-color: rgba(15, 23, 42, 0.14) transparent;
}
html.dark .agent-kb-scroll {
  scrollbar-color: rgba(255, 255, 255, 0.12) transparent;
}
.agent-kb-scroll::-webkit-scrollbar {
  width: 8px;
}
.agent-kb-scroll::-webkit-scrollbar-button {
  display: none;
  width: 0;
  height: 0;
}
.agent-kb-scroll::-webkit-scrollbar-track {
  background: transparent;
}
.agent-kb-scroll::-webkit-scrollbar-thumb {
  background-color: rgba(15, 23, 42, 0.14);
  border-radius: 100px;
  border: 2px solid transparent;
  background-clip: padding-box;
}
.agent-kb-scroll::-webkit-scrollbar-thumb:hover {
  background-color: rgba(15, 23, 42, 0.22);
}
html.dark .agent-kb-scroll::-webkit-scrollbar-thumb {
  background-color: rgba(255, 255, 255, 0.12);
}
html.dark .agent-kb-scroll::-webkit-scrollbar-thumb:hover {
  background-color: rgba(255, 255, 255, 0.2);
}
.agent-kb-layout {
  padding: 16px 24px 32px;
  max-width: 960px;
  margin: 0 auto;
}
.agent-page-header {
  margin-bottom: 24px;
}
.agent-page-title {
  margin: 0 0 8px;
  font-size: clamp(26px, 2.8vw, 32px);
  font-weight: 700;
  line-height: 1.25;
  letter-spacing: 0.04em;
  color: var(--n-text-color);
}
.agent-kb-subtitle {
  color: var(--n-text-color-3);
  font-size: 15px;
  line-height: 1.4;
}
.agent-kb-section {
  margin-bottom: 32px;
}
.agent-kb-h2 {
  font-size: 18px;
  font-weight: 600;
  line-height: 1.35;
  margin: 0 0 8px;
  color: var(--n-text-color);
}
.agent-kb-hint {
  color: var(--n-text-color-3);
  font-size: 14px;
  line-height: 1.5;
  margin: 0 0 12px;
}
.agent-kb-empty {
  color: var(--n-text-color-3);
  font-size: 14px;
  padding: 16px 0;
}
.agent-kb-tasks {
  display: flex;
  flex-direction: column;
  gap: 12px;
  max-height: 490px;
  overflow-y: auto;
  padding-right: 4px;
  scrollbar-width: thin;
  scrollbar-color: rgba(15, 23, 42, 0.14) transparent;
}
html.dark .agent-kb-tasks {
  scrollbar-color: rgba(255, 255, 255, 0.12) transparent;
}
.agent-kb-tasks::-webkit-scrollbar {
  width: 8px;
}
.agent-kb-tasks::-webkit-scrollbar-button {
  display: none;
  width: 0;
  height: 0;
}
.agent-kb-tasks::-webkit-scrollbar-track {
  background: transparent;
}
.agent-kb-tasks::-webkit-scrollbar-thumb {
  background-color: rgba(15, 23, 42, 0.14);
  border-radius: 100px;
  border: 2px solid transparent;
  background-clip: padding-box;
}
html.dark .agent-kb-tasks::-webkit-scrollbar-thumb {
  background-color: rgba(255, 255, 255, 0.12);
}
.agent-kb-list-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 8px;
}
.agent-kb-list-count {
  font-size: 13px;
  color: var(--n-text-color-3);
}
.agent-kb-list-toolbar {
  display: flex;
  gap: 12px;
  margin-bottom: 12px;
}
.agent-kb-search {
  flex: 1;
  max-width: 320px;
}
.agent-kb-type-filter {
  width: 140px;
  flex: none;
}
.agent-kb-batch-summary {
  margin: 0 0 12px;
  font-size: 13px;
  line-height: 1.5;
  color: var(--n-text-color-3);
}
.agent-kb-task {
  padding: 12px 14px;
  border: 1px solid var(--n-border-color);
  border-radius: 8px;
  background: var(--n-color-embedded);
}
.agent-kb-task-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 8px;
}
.agent-kb-task-name {
  font-size: 14px;
  font-weight: 500;
  color: var(--n-text-color);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.agent-kb-task-stage {
  margin-top: 6px;
  font-size: 12px;
  color: var(--n-text-color-3);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.agent-kb-task-stage-error {
  color: var(--n-error-color);
}
.agent-kb-overview {
  padding: 14px 16px;
  border: 1px solid var(--n-border-color);
  border-radius: 10px;
  background: var(--n-color-embedded);
  margin-bottom: 14px;
}
.agent-kb-overview-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
}
.agent-kb-overview-title {
  margin: 0;
}
.agent-kb-detail-toggle {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  border: none;
  border-radius: 6px;
  background: transparent;
  font-size: 13px;
  line-height: 1.6;
  color: var(--n-text-color-3);
  cursor: pointer;
}
.agent-kb-detail-toggle:hover {
  color: var(--n-text-color);
  background: rgba(128, 128, 128, 0.12);
}
.agent-kb-overview .agent-kb-batch-summary {
  margin: 8px 0 0;
}
.agent-kb-confirm-hint {
  margin: 0 0 12px;
  font-size: 13px;
  line-height: 1.5;
  color: var(--n-text-color-3);
}
.agent-kb-file-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: min(46vh, 400px);
  overflow-y: auto;
}
.agent-kb-file-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  border: 1px solid var(--n-border-color);
  border-radius: 8px;
}
.agent-kb-file-row-invalid {
  border-color: var(--n-error-color);
}
.agent-kb-file-icon {
  flex: none;
  color: var(--n-text-color-3);
}
.agent-kb-file-main {
  flex: 1;
  min-width: 0;
}
.agent-kb-file-name {
  font-size: 14px;
  font-weight: 500;
  color: var(--n-text-color);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.agent-kb-file-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 2px;
  font-size: 12px;
  color: var(--n-text-color-3);
}
.agent-kb-file-ext {
  font-weight: 600;
}
.agent-kb-file-over {
  color: var(--n-error-color);
  font-weight: 600;
}
.agent-kb-confirm-footer {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
}
</style>
