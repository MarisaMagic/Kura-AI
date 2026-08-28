<template>
  <AppPage :show-footer="false" scroll-in-parent class="!p-0">
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
            :show-file-list="false"
            :max="1"
            accept=".pdf,.doc,.docx,.xls,.xlsx"
            @change="onUploadChange"
          >
            <n-button type="primary">
              {{ $t('views.agents.kb_upload_title') }}
            </n-button>
          </n-upload>
        </div>

        <div v-if="tasks.length" class="agent-kb-section">
          <h2 class="agent-kb-h2">{{ $t('views.agents.kb_process_title') }}</h2>
          <div class="agent-kb-tasks">
            <div v-for="task in tasks" :key="task.key" class="agent-kb-task">
              <div class="agent-kb-task-head">
                <span class="agent-kb-task-name" :title="task.filename">{{ task.filename }}</span>
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
        </div>

        <div class="agent-kb-section">
          <h2 class="agent-kb-h2">{{ $t('views.agents.kb_list_title') }}</h2>
          <n-data-table
            :columns="columns"
            :data="list"
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
    </div>
  </AppPage>
</template>

<script setup>
import { h, onMounted, onUnmounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { NButton, NDataTable, NPopconfirm, NProgress, NSpin, NUpload, useMessage } from 'naive-ui'
import AppPage from '@/components/page/AppPage.vue'
import api from '@/api'

const { t } = useI18n()
const route = useRoute()
const message = useMessage()

const pageLoading = ref(true)
const tableLoading = ref(false)
const agentId = ref(Number(route.params.agentId) || 0)
const agentName = ref('')
const list = ref([])

// 上传任务面板：上传传输 + 处理进度轮询，支持多任务并行显示
const POLL_INTERVAL_MS = 500
const STORAGE_PREFIX = 'kura_ai_kb_upload_'
const TERMINAL_STATUSES = ['completed', 'failed', 'timeout', 'cancelled']
const tasks = ref([])
let taskSeq = 0

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

function makeTask({ filename = '', taskId = null } = {}) {
  const task = reactive({
    key: `task_${++taskSeq}`,
    taskId,
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

function finishTask(task) {
  clearTimer(task)
  persistActiveTasks()
  fetchList()
  if (task.status === 'completed') {
    if (task.result?.unchanged) message.success(t('views.agents.kb_upload_unchanged'))
    else message.success(t('views.agents.kb_upload_ok'))
  } else if (task.status === 'timeout') {
    message.error(t('views.agents.kb_upload_timeout', { filename: task.filename }), {
      duration: 8000,
    })
  } else if (task.status === 'cancelled') {
    message.info(t('views.agents.kb_upload_cancelled', { filename: task.filename }))
  } else if (task.status === 'failed') {
    message.error(
      t('views.agents.kb_upload_failed', { filename: task.filename, reason: task.error }),
      { duration: 8000 },
    )
  }
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
  { title: () => t('views.agents.kb_col_filename'), key: 'display_filename', ellipsis: { tooltip: true } },
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
              { default: () => t('common.buttons.delete') },
            ),
          default: () => t('views.agents.kb_confirm_delete'),
        },
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

async function onUploadChange(options) {
  const f = options.file?.file
  if (!f) return
  const task = makeTask({ filename: f.name || '' })
  const fd = new FormData()
  fd.append('file', f)
  try {
    const res = await api.uploadKbDocument(
      agentId.value,
      fd,
      (ev) => {
        if (ev?.total) {
          task.percent = Math.min(100, Math.round((ev.loaded / ev.total) * 100))
        }
      },
      { noErrorMessage: true },
    )
    const taskId = res?.data?.task_id
    if (!taskId) throw new Error('no task id')
    task.taskId = taskId
    task.status = 'queued'
    task.percent = 0
    persistActiveTasks()
    startPolling(task)
  } catch (e) {
    // 传输或受理失败：任务卡标红；拦截器已静默（noErrorMessage），由本处统一弹出提示
    task.status = 'failed'
    task.error = e?.message || t('views.agents.kb_upload_failed_title')
    message.error(t('views.agents.kb_upload_failed', { filename: task.filename, reason: task.error }), {
      duration: 8000,
    })
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
</style>