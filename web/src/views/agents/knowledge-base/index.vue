<template>
  <AppPage :show-footer="false" scroll-in-parent class="!p-0">
    <div class="agent-kb-scroll">
      <div class="agent-kb-layout">
        <header class="agent-page-header">
          <div class="agent-kb-title-row">
            <h1 class="agent-page-title">{{ $t('views.agents.title_knowledge_base') }}</h1>
            <n-button quaternary size="small" @click="goHub">
              <TheIcon icon="mdi:home-outline" :size="16" class="agent-kb-btn-icon" />
              {{ $t('views.agents.kb_back_hub') }}
            </n-button>
          </div>
          <div v-if="agent" class="agent-kb-identity">
            <n-avatar
              round
              :size="48"
              :src="agentAvatar"
              object-fit="cover"
              class="agent-kb-identity-avatar"
            />
            <div class="agent-kb-identity-text">
              <div class="agent-kb-identity-name">
                <span class="agent-kb-identity-name-text">{{ agent.name }}</span>
                <n-tag
                  v-if="agent.is_published"
                  size="tiny"
                  type="success"
                  :bordered="false"
                  class="agent-kb-identity-tag"
                >
                  {{ $t('views.agents.shared_count', { n: agent.shared_count || 0 }) }}
                </n-tag>
              </div>
              <p class="agent-kb-identity-desc">
                {{ agent.description || $t('views.agents.text_no_description') }}
              </p>
              <n-tag v-if="agent.model_name" size="tiny" :bordered="false">
                {{ agent.model_name }}
              </n-tag>
            </div>
            <div class="agent-kb-identity-actions">
              <n-button size="small" @click="goChat">
                <template #icon>
                  <TheIcon icon="mdi:message-outline" :size="16" />
                </template>
                {{ $t('views.agents.kb_go_chat') }}
              </n-button>
              <n-button size="small" @click="goEdit">
                <template #icon>
                  <TheIcon icon="mdi:pencil-outline" :size="16" />
                </template>
                {{ $t('views.agents.kb_go_edit') }}
              </n-button>
            </div>
          </div>
        </header>

        <n-spin :show="pageLoading">
          <div class="agent-kb-section">
            <h2 class="agent-kb-h2">{{ $t('views.agents.kb_upload_title') }}</h2>
            <n-upload
              ref="uploadRef"
              class="agent-kb-drop"
              :show-file-list="false"
              :default-upload="false"
              multiple
              accept=".pdf,.doc,.docx,.xls,.xlsx,.txt,.md"
              @change="onUploadChange"
            >
              <n-upload-dragger>
                <div class="agent-kb-drop-inner">
                  <TheIcon icon="mdi:cloud-upload-outline" :size="36" class="agent-kb-drop-icon" />
                  <div class="agent-kb-drop-title">{{ $t('views.agents.kb_upload_drop') }}</div>
                  <div class="agent-kb-drop-formats">
                    <n-tag v-for="fmt in uploadFormats" :key="fmt" size="tiny" :bordered="false">
                      {{ fmt }}
                    </n-tag>
                  </div>
                  <div class="agent-kb-drop-hint">
                    {{ $t('views.agents.kb_upload_max_size', { mb: maxFileMb }) }}
                  </div>
                </div>
              </n-upload-dragger>
            </n-upload>
          </div>

          <div v-if="tasks.length" class="agent-kb-section">
            <div class="agent-kb-overview">
              <div class="agent-kb-overview-head">
                <h2 class="agent-kb-h2 agent-kb-overview-title">
                  {{ $t('views.agents.kb_process_title') }}
                </h2>
                <div class="agent-kb-overview-actions">
                  <n-button
                    v-if="hasFinishedTasks"
                    size="tiny"
                    quaternary
                    @click="clearFinishedTasks"
                  >
                    {{ $t('views.agents.kb_clear_finished') }}
                  </n-button>
                  <button
                    type="button"
                    class="agent-kb-detail-toggle"
                    @click="detailOpen = !detailOpen"
                  >
                    {{ $t('views.agents.kb_detail_toggle') }}
                    <TheIcon
                      :icon="detailOpen ? 'mdi:chevron-up' : 'mdi:chevron-down'"
                      :size="16"
                    />
                  </button>
                </div>
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
                    <div class="agent-kb-task-title">
                      <span :class="fileKindClass('', task.filename)">
                        <TheIcon :icon="fileKindIcon('', task.filename)" :size="18" />
                      </span>
                      <span class="agent-kb-task-name" :title="task.filename">{{
                        task.filename
                      }}</span>
                      <n-tag size="tiny" :type="taskStatusMeta(task).type" :bordered="false">
                        {{ taskStatusMeta(task).label }}
                      </n-tag>
                    </div>
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
              <span v-if="list.length" class="agent-kb-list-count">
                {{ $t('views.agents.kb_list_total', { count: list.length }) }}
              </span>
            </div>
            <div v-if="list.length" class="agent-kb-stats">
              <div class="agent-kb-stat">
                <div class="agent-kb-stat-value">{{ kbStats.docs }}</div>
                <div class="agent-kb-stat-label">{{ $t('views.agents.kb_stat_docs') }}</div>
              </div>
              <div class="agent-kb-stat">
                <div class="agent-kb-stat-value">{{ kbStats.chunks }}</div>
                <div class="agent-kb-stat-label">{{ $t('views.agents.kb_stat_chunks') }}</div>
              </div>
              <div class="agent-kb-stat agent-kb-stat--types">
                <div class="agent-kb-stat-types">
                  <n-tag
                    v-for="item in kbStats.types"
                    :key="item.type"
                    size="tiny"
                    :type="fileKindTagType(item.type, '')"
                    :bordered="false"
                  >
                    {{ item.type }} {{ item.count }}
                  </n-tag>
                </div>
                <div class="agent-kb-stat-label">{{ $t('views.agents.kb_stat_types') }}</div>
              </div>
            </div>
            <div v-if="list.length" class="agent-kb-list-toolbar">
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
              v-if="list.length"
              :columns="columns"
              :data="filteredList"
              :loading="tableLoading"
              :pagination="false"
              :bordered="true"
              size="small"
            />
            <div v-if="!list.length && !tableLoading" class="agent-kb-empty">
              <TheIcon icon="mdi:folder-open-outline" :size="40" class="agent-kb-empty-icon" />
              <div class="agent-kb-empty-title">{{ $t('views.agents.kb_empty') }}</div>
              <p class="agent-kb-empty-hint">{{ $t('views.agents.kb_empty_hint') }}</p>
              <n-button type="primary" @click="openFilePicker">
                {{ $t('views.agents.kb_empty_action') }}
              </n-button>
            </div>
            <div
              v-else-if="list.length && !filteredList.length && !tableLoading"
              class="agent-kb-empty agent-kb-empty--filter"
            >
              {{ $t('views.agents.kb_filter_empty') }}
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
                size: formatFileSize(pendingValidTotalSize),
              })
            }}
          </p>
          <div class="agent-kb-file-list">
            <div
              v-for="item in sortedPendingFiles"
              :key="`${item.name}_${item.size}`"
              class="agent-kb-file-row"
              :class="{ 'agent-kb-file-row-invalid': item.overLimit }"
            >
              <span :class="['agent-kb-file-icon', fileKindClass('', item.name)]">
                <TheIcon :icon="fileKindIcon('', item.name)" :size="20" />
              </span>
              <div class="agent-kb-file-main">
                <div class="agent-kb-file-name" :title="item.name">{{ item.name }}</div>
                <div class="agent-kb-file-meta">
                  <span class="agent-kb-file-ext">{{ item.ext.toUpperCase() || '-' }}</span>
                  <span>{{ formatFileSize(item.size) }}</span>
                  <span v-if="item.modifiedAt">{{ formatModified(item.modifiedAt) }}</span>
                  <span v-if="item.overLimit" class="agent-kb-file-over">
                    {{ $t('views.agents.kb_file_over_limit', { mb: maxFileMb }) }}
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
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import {
  NAvatar,
  NButton,
  NCollapseTransition,
  NDataTable,
  NInput,
  NModal,
  NPopconfirm,
  NProgress,
  NSelect,
  NSpin,
  NTag,
  NUpload,
  NUploadDragger,
  useMessage,
} from 'naive-ui'
import AppPage from '@/components/page/AppPage.vue'
import TheIcon from '@/components/icon/TheIcon.vue'
import api from '@/api'
import { DEFAULT_AVATAR } from '@/views/agents/composables/agentFormCommon.js'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const message = useMessage()

const pageLoading = ref(true)
const tableLoading = ref(false)
const agentId = ref(Number(route.params.agentId) || 0)
const agent = ref(null)
const list = ref([])
const uploadRef = ref(null)
const uploadFormats = ['PDF', 'Word', 'Excel', 'TXT', 'MD']

const MAX_FILE_BYTES = 50 * 1024 * 1024 // 与后端 KB_UPLOAD_MAX_BYTES 默认值一致
const maxFileMb = Math.round(MAX_FILE_BYTES / (1024 * 1024))
const showConfirmModal = ref(false)
const pendingFiles = ref([])
let batchSeq = 0
const reportedBatches = new Set()
const detailOpen = ref(false)

const keyword = ref('')
const typeFilter = ref('all')

const agentAvatar = computed(() => agent.value?.avatar_url || DEFAULT_AVATAR)

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

const kbStats = computed(() => {
  const docs = list.value
  const chunks = docs.reduce((sum, doc) => sum + (Number(doc.chunk_count) || 0), 0)
  const byType = {}
  docs.forEach((doc) => {
    const type = doc.file_type || 'Text'
    byType[type] = (byType[type] || 0) + 1
  })
  const types = Object.entries(byType)
    .map(([type, count]) => ({ type, count }))
    .sort((a, b) => b.count - a.count)
  return { docs: docs.length, chunks, types }
})

const POLL_INTERVAL_MS = 500
const STORAGE_PREFIX = 'kura_ai_kb_upload_'
const TERMINAL_STATUSES = ['completed', 'failed', 'timeout', 'cancelled']
const tasks = ref([])
let taskSeq = 0

const batchSummary = computed(() => {
  const total = tasks.value.length
  const done = tasks.value.filter((task) => task.status === 'completed').length
  const active = tasks.value.filter((task) => isTaskActive(task)).length
  return { total, done, active, failed: total - done - active }
})

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

const hasFinishedTasks = computed(() =>
  tasks.value.some((task) => TERMINAL_STATUSES.includes(task.status))
)

const validPendingFiles = computed(() => pendingFiles.value.filter((item) => !item.overLimit))

const pendingValidTotalSize = computed(() =>
  validPendingFiles.value.reduce((sum, item) => sum + (Number(item.size) || 0), 0)
)

const sortedPendingFiles = computed(() =>
  [...pendingFiles.value].sort((a, b) => Number(b.overLimit) - Number(a.overLimit))
)

function extFromName(name) {
  return (
    String(name || '')
      .split('.')
      .pop() || ''
  ).toLowerCase()
}

function fileKindKey(fileType, name) {
  const type = String(fileType || '').toLowerCase()
  const ext = extFromName(name)
  if (type === 'pdf' || ext === 'pdf') return 'pdf'
  if (type === 'word' || ext === 'doc' || ext === 'docx') return 'word'
  if (type === 'excel' || ext === 'xls' || ext === 'xlsx') return 'excel'
  if (ext === 'md' || ext === 'markdown') return 'md'
  return 'text'
}

function fileKindIcon(fileType, name) {
  const key = fileKindKey(fileType, name)
  if (key === 'pdf') return 'mdi:file-pdf-box'
  if (key === 'word') return 'mdi:file-word-box'
  if (key === 'excel') return 'mdi:file-excel-box'
  if (key === 'md') return 'simple-icons:markdown'
  return 'mdi:file-document-outline'
}

function fileKindClass(fileType, name) {
  return `agent-kb-kind-${fileKindKey(fileType, name)}`
}

function fileKindTagType(fileType, name) {
  const key = fileKindKey(fileType, name)
  if (key === 'pdf') return 'error'
  if (key === 'word') return 'info'
  if (key === 'excel') return 'success'
  return 'default'
}

function formatFileSize(bytes) {
  const n = Number(bytes)
  if (!Number.isFinite(n) || n < 0) return '-'
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / (1024 * 1024)).toFixed(1)} MB`
}

function formatModified(ts) {
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return ''
  const pad = (v) => String(v).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(
    d.getHours()
  )}:${pad(d.getMinutes())}`
}

function formatUpdatedAt(iso) {
  if (!iso) return '-'
  return String(iso).replace('T', ' ').slice(0, 16)
}

function goHub() {
  router.push('/agent-hub')
}

function goChat() {
  if (!agentId.value) return
  router.push({ name: 'AgentChat', params: { agentId: String(agentId.value) } })
}

function goEdit() {
  if (!agentId.value) return
  router.push({ name: 'AgentEdit', params: { id: String(agentId.value) } })
}

function openFilePicker() {
  uploadRef.value?.openOpenFileDialog?.()
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
    status: 'uploading',
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

function taskStatusMeta(task) {
  if (task.status === 'completed') {
    return { type: 'success', label: t('views.agents.kb_task_status_completed') }
  }
  if (task.status === 'failed' || task.status === 'timeout') {
    return { type: 'error', label: t('views.agents.kb_task_status_failed') }
  }
  if (task.status === 'cancelled') {
    return { type: 'warning', label: t('views.agents.kb_task_status_cancelled') }
  }
  if (task.status === 'uploading') {
    return { type: 'info', label: t('views.agents.kb_task_status_uploading') }
  }
  if (task.status === 'queued') {
    return { type: 'info', label: t('views.agents.kb_task_status_queued') }
  }
  return { type: 'info', label: t('views.agents.kb_task_status_processing') }
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

function clearFinishedTasks() {
  tasks.value.forEach((task) => {
    if (!isTaskActive(task)) clearTimer(task)
  })
  tasks.value = tasks.value.filter((task) => isTaskActive(task))
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
    render(row) {
      return h('div', { class: 'agent-kb-file-cell' }, [
        h('span', { class: fileKindClass(row.file_type, row.display_filename) }, [
          h(TheIcon, {
            icon: fileKindIcon(row.file_type, row.display_filename),
            size: 18,
          }),
        ]),
        h(
          'span',
          { class: 'agent-kb-file-cell-name', title: row.display_filename },
          row.display_filename
        ),
      ])
    },
  },
  {
    title: () => t('views.agents.kb_col_type'),
    key: 'file_type',
    width: 110,
    render(row) {
      return h(
        NTag,
        {
          size: 'small',
          type: fileKindTagType(row.file_type, row.display_filename),
          bordered: false,
        },
        { default: () => row.file_type || '-' }
      )
    },
  },
  {
    title: () => t('views.agents.kb_col_chunks'),
    key: 'chunk_count',
    width: 110,
    render(row) {
      return t('views.agents.kb_chunks_value', { n: row.chunk_count ?? 0 })
    },
  },
  {
    title: () => t('views.agents.kb_col_updated'),
    key: 'updated_at',
    width: 170,
    render(row) {
      return formatUpdatedAt(row.updated_at)
    },
  },
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
  agent.value = res.data || null
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

function onUploadChange(options) {
  const f = options.file?.file
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
      overLimit: size > MAX_FILE_BYTES,
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
.agent-kb-title-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 16px;
}
.agent-page-title {
  margin: 0;
  font-size: clamp(26px, 2.8vw, 32px);
  font-weight: 700;
  line-height: 1.25;
  letter-spacing: 0.04em;
  color: var(--n-text-color);
}
.agent-kb-btn-icon {
  margin-right: 4px;
}
.agent-kb-identity {
  display: flex;
  align-items: flex-start;
  gap: 14px;
  padding: 14px 16px;
  border: 1px solid var(--n-border-color);
  border-radius: 10px;
  background: var(--n-color-embedded);
}
.agent-kb-identity-avatar {
  flex: none;
}
.agent-kb-identity-text {
  flex: 1;
  min-width: 0;
}
.agent-kb-identity-name {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  font-size: 16px;
  font-weight: 600;
  line-height: 1.35;
  color: var(--n-text-color);
}
.agent-kb-identity-name-text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.agent-kb-identity-tag {
  flex: none;
}
.agent-kb-identity-desc {
  margin: 6px 0 8px;
  font-size: 13px;
  line-height: 1.45;
  color: var(--n-text-color-2);
  overflow: hidden;
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  word-break: break-word;
}
.agent-kb-identity-actions {
  display: flex;
  flex-direction: column;
  gap: 6px;
  flex: none;
}
@media (max-width: 639px) {
  .agent-kb-title-row {
    flex-wrap: wrap;
  }
  .agent-kb-identity {
    flex-wrap: wrap;
  }
  .agent-kb-identity-actions {
    flex-direction: row;
    width: 100%;
    justify-content: flex-end;
  }
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
.agent-kb-drop {
  width: 100%;
}
.agent-kb-drop :deep(.n-upload-trigger) {
  width: 100%;
}
.agent-kb-drop :deep(.n-upload-dragger) {
  padding: 28px 20px;
  border-radius: 12px;
}
.agent-kb-drop-inner {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
}
.agent-kb-drop-icon {
  color: var(--n-primary-color);
  opacity: 0.9;
}
.agent-kb-drop-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--n-text-color);
}
.agent-kb-drop-formats {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 6px;
  margin-top: 2px;
}
.agent-kb-drop-hint {
  font-size: 12px;
  line-height: 1.5;
  color: var(--n-text-color-3);
}
.agent-kb-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding: 40px 16px;
  border: 1px dashed var(--n-border-color);
  border-radius: 12px;
  text-align: center;
}
.agent-kb-empty--filter {
  padding: 20px 16px;
  color: var(--n-text-color-3);
  font-size: 14px;
}
.agent-kb-empty-icon {
  color: var(--n-text-color-3);
}
.agent-kb-empty-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--n-text-color-2);
}
.agent-kb-empty-hint {
  margin: 0 0 8px;
  font-size: 13px;
  line-height: 1.5;
  color: var(--n-text-color-3);
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
.agent-kb-stats {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 14px;
}
.agent-kb-stat {
  min-width: 104px;
  padding: 10px 14px;
  border: 1px solid var(--n-border-color);
  border-radius: 10px;
  background: var(--n-color-embedded);
}
.agent-kb-stat--types {
  flex: 1;
  min-width: 160px;
}
.agent-kb-stat-value {
  font-size: 20px;
  font-weight: 700;
  line-height: 1.2;
  color: var(--n-text-color);
}
.agent-kb-stat-label {
  margin-top: 4px;
  font-size: 12px;
  color: var(--n-text-color-3);
}
.agent-kb-stat-types {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  min-height: 24px;
  align-items: center;
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
.agent-kb-task-title {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  flex: 1;
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
.agent-kb-overview-actions {
  display: flex;
  align-items: center;
  gap: 4px;
  flex: none;
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
:deep(.agent-kb-file-cell) {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
:deep(.agent-kb-file-cell-name) {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
:deep(.agent-kb-kind-pdf),
.agent-kb-kind-pdf {
  color: #e53935;
}
:deep(.agent-kb-kind-word),
.agent-kb-kind-word {
  color: #1e88e5;
}
:deep(.agent-kb-kind-excel),
.agent-kb-kind-excel {
  color: #43a047;
}
:deep(.agent-kb-kind-md),
.agent-kb-kind-md {
  color: #546e7a;
}
:deep(.agent-kb-kind-text),
.agent-kb-kind-text {
  color: var(--n-text-color-3);
}
</style>
