<script setup>
import { computed, h, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NButton, NPopconfirm, NProgress, NTag, useMessage } from 'naive-ui'
import AppPage from '@/components/page/AppPage.vue'
import TheIcon from '@/components/icon/TheIcon.vue'
import api from '@/api'
import RunConfigModal from './components/RunConfigModal.vue'
import QaRunModal from './components/QaRunModal.vue'
import {
  fileKindClass,
  fileKindIcon,
  fileKindTagType,
  formatFileSize,
  formatModified,
  isTaskActive,
  useExpUpload,
} from './composables/useExpUpload'

defineOptions({ name: '实验数据集' })

const route = useRoute()
const router = useRouter()
const message = useMessage()
const datasetId = Number(route.params.id)

const dataset = ref(null)
const tab = ref('docs')
const pageLoading = ref(true)

// ---------------------------------------------------------------- 上传（常驻 Tab 上方）
const {
  tasks,
  visibleTasks,
  hiddenTaskCount,
  pendingFiles,
  visiblePendingFiles,
  hiddenPendingCount,
  showConfirmModal,
  detailOpen,
  uploadRef,
  MAX_FILE_MB,
  validPendingFiles,
  pendingValidTotalSize,
  batchSummary,
  overallPercent,
  overallStatus,
  hasFinishedTasks,
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
} = useExpUpload(datasetId, {
  onListChanged: () => {
    loadDocuments()
  },
})

// ---------------------------------------------------------------- data
async function loadDetail() {
  const res = await api.getExpDatasetDetail({ dataset_id: datasetId })
  dataset.value = res.data
}

async function loadDocuments() {
  docsLoading.value = true
  try {
    const res = await api.getExpDocuments({ dataset_id: datasetId })
    documents.value = res.data?.documents || []
  } finally {
    docsLoading.value = false
  }
}

// ---------------------------------------------------------------- 文档列表
const documents = ref([])
const docsLoading = ref(false)
const docKeyword = ref('')
const docTypeFilter = ref('all')
const docPage = ref(1)
const docPageSize = ref(20)

const docTypeOptions = computed(() => {
  const types = [...new Set(documents.value.map((d) => d.file_type).filter(Boolean))].sort()
  return [{ label: '全部类型', value: 'all' }, ...types.map((t) => ({ label: t, value: t }))]
})

const filteredDocuments = computed(() => {
  const kw = docKeyword.value.trim().toLowerCase()
  return documents.value.filter((doc) => {
    if (docTypeFilter.value !== 'all' && doc.file_type !== docTypeFilter.value) return false
    if (kw && !(doc.display_filename || '').toLowerCase().includes(kw)) return false
    return true
  })
})

const docPagination = computed(() => ({
  page: docPage.value,
  pageSize: docPageSize.value,
  showSizePicker: true,
  pageSizes: [20, 50, 100],
  itemCount: filteredDocuments.value.length,
  prefix: ({ itemCount }) => `共 ${itemCount} 个文档`,
  onChange: (p) => {
    docPage.value = p
  },
  onUpdatePageSize: (s) => {
    docPageSize.value = s
    docPage.value = 1
  },
}))

const docStats = computed(() => {
  const docs = documents.value
  const chunks = docs.reduce((sum, d) => sum + (Number(d.chunk_count) || 0), 0)
  const byType = {}
  docs.forEach((d) => {
    const t = d.file_type || 'Text'
    byType[t] = (byType[t] || 0) + 1
  })
  const types = Object.entries(byType)
    .map(([type, count]) => ({ type, count }))
    .sort((a, b) => b.count - a.count)
  return { docs: docs.length, chunks, types }
})

function formatUpdatedAt(iso) {
  if (!iso) return '-'
  return String(iso).replace('T', ' ').slice(0, 16)
}

async function deleteDocument(row) {
  await api.deleteExpDocument({ dataset_id: datasetId, filename: row.display_filename })
  message.success('已删除')
  await Promise.all([loadDocuments(), loadDetail()])
}

const deletingDocs = ref(false)
const hasDocFilter = computed(() => docKeyword.value.trim() !== '' || docTypeFilter.value !== 'all')

async function batchDeleteDocuments() {
  const names = filteredDocuments.value.map((d) => d.display_filename)
  if (!names.length) return
  deletingDocs.value = true
  try {
    const res = await api.batchDeleteExpDocuments({ dataset_id: datasetId, filenames: names })
    message.success(res.msg || `已删除 ${res.data?.deleted ?? names.length} 个文档`)
    await Promise.all([loadDocuments(), loadDetail()])
  } finally {
    deletingDocs.value = false
  }
}

const docColumns = computed(() => [
  {
    title: '文件名',
    key: 'display_filename',
    ellipsis: { tooltip: true },
    render(row) {
      return h('div', { class: 'exp-file-cell' }, [
        h('span', { class: fileKindClass(row.file_type, row.display_filename) }, [
          h(TheIcon, { icon: fileKindIcon(row.file_type, row.display_filename), size: 18 }),
        ]),
        h(
          'span',
          { class: 'exp-file-cell-name', title: row.display_filename },
          row.display_filename
        ),
      ])
    },
  },
  {
    title: '类型',
    key: 'file_type',
    width: 100,
    render: (row) =>
      h(
        NTag,
        {
          size: 'small',
          type: fileKindTagType(row.file_type, row.display_filename),
          bordered: false,
        },
        { default: () => row.file_type || '-' }
      ),
  },
  { title: '分块数', key: 'chunk_count', width: 90, align: 'center' },
  {
    title: '更新时间',
    key: 'updated_at',
    width: 170,
    render: (row) => formatUpdatedAt(row.updated_at),
  },
  {
    title: '操作',
    key: 'actions',
    width: 90,
    align: 'center',
    render: (row) =>
      h(
        NPopconfirm,
        { onPositiveClick: () => deleteDocument(row) },
        {
          trigger: () =>
            h(
              NButton,
              { size: 'small', quaternary: true, type: 'error' },
              { icon: () => h(TheIcon, { icon: 'mdi:trash-can-outline', size: 16 }) }
            ),
          default: () => `确认删除「${row.display_filename}」？向量与元数据将一并清理`,
        }
      ),
  },
])

// ---------------------------------------------------------------- 问题集
const questions = ref({ total: 0, items: [] })
const qLoading = ref(false)
const qPage = ref(1)
const qPageSize = ref(20)
const qOodFilter = ref(null)
const importing = ref(false)
const replaceOnImport = ref(false)
const qUploadRef = ref(null)

async function loadQuestions() {
  qLoading.value = true
  try {
    const params = { dataset_id: datasetId, page: qPage.value, page_size: qPageSize.value }
    if (qOodFilter.value !== null) params.is_ood = qOodFilter.value
    const res = await api.getExpQuestions(params)
    questions.value = res.data || { total: 0, items: [] }
  } finally {
    qLoading.value = false
  }
}

async function onImportQuestions(options) {
  // 只取本次新选中的文件并立即清空组件，避免旧文件被再次提交造成重复导入
  const file = options.file?.file
  qUploadRef.value?.clear()
  if (!file) return
  const fd = new FormData()
  fd.append('file', file)
  importing.value = true
  try {
    const res = await api.importExpQuestions(datasetId, fd, replaceOnImport.value)
    const d = res.data || {}
    const parts = [`导入 ${d.imported} 个问题（OOD ${d.ood_imported} 条`]
    if (d.skipped) parts.push(`，跳过重复 ${d.skipped} 条`)
    parts.push('）')
    message.success(parts.join(''))
    if (d.unmatched_gold_keys?.length) {
      message.warning(`${d.unmatched_gold_keys.length} 个 gold 文件名未匹配到已上传文档`, {
        duration: 8000,
      })
    }
    await Promise.all([loadQuestions(), loadDetail()])
  } finally {
    importing.value = false
  }
}

async function deleteQuestion(row) {
  await api.deleteExpQuestion({ question_id: row.id })
  message.success('已删除')
  await Promise.all([loadQuestions(), loadDetail()])
}

// 清空跟随当前筛选：全部 / 库内题 / OOD 题
const clearQuestionsLabel = computed(() => {
  if (qOodFilter.value === null) return '清空全部题目'
  return qOodFilter.value ? '清空 OOD 题' : '清空库内题'
})

async function clearQuestions() {
  const params = { dataset_id: datasetId }
  if (qOodFilter.value !== null) params.is_ood = qOodFilter.value
  const res = await api.clearExpQuestions(params)
  message.success(res.msg || `已清空 ${res.data?.deleted ?? 0} 个问题`)
  await Promise.all([loadQuestions(), loadDetail()])
}

const qColumns = computed(() => [
  { title: '#', key: 'index', width: 70, align: 'center', render: (row) => row.index ?? row.id },
  { title: '问题', key: 'question', ellipsis: { tooltip: true } },
  {
    title: '目标文档',
    key: 'gold_file_keys',
    width: 240,
    ellipsis: { tooltip: true },
    render: (row) => (row.is_ood ? '—（库外题）' : (row.gold_file_keys || []).join(', ')),
  },
  {
    title: '分层',
    key: 'stratum',
    width: 90,
    align: 'center',
    render: (row) =>
      row.stratum ? h(NTag, { size: 'small', bordered: false }, () => row.stratum) : '-',
  },
  {
    title: 'OOD',
    key: 'is_ood',
    width: 80,
    align: 'center',
    render: (row) =>
      row.is_ood
        ? h(NTag, { size: 'small', type: 'warning', bordered: false }, () => '库外')
        : null,
  },
  {
    title: '操作',
    key: 'actions',
    width: 80,
    align: 'center',
    render: (row) =>
      h(
        NPopconfirm,
        { onPositiveClick: () => deleteQuestion(row) },
        {
          trigger: () =>
            h(
              NButton,
              { size: 'small', quaternary: true, type: 'error' },
              { icon: () => h(TheIcon, { icon: 'mdi:trash-can-outline', size: 16 }) }
            ),
          default: () => '确认删除该问题？',
        }
      ),
  },
])

// ---------------------------------------------------------------- 实验运行
const runsRetrieval = ref([])
const runsQa = ref([])
const runsLoading = ref(false)
const runTab = ref('retrieval')
const showRunModal = ref(false)
const showQaModal = ref(false)
let runsTimer = null

async function loadRuns({ silent = false } = {}) {
  if (!silent) runsLoading.value = true
  try {
    const [retrievalRes, qaRes] = await Promise.all([
      api.getExpRuns({ dataset_id: datasetId, kind: 'retrieval' }),
      api.getExpRuns({ dataset_id: datasetId, kind: 'qa' }),
    ])
    runsRetrieval.value = retrievalRes.data || []
    runsQa.value = qaRes.data || []
    const hasActive = [...runsRetrieval.value, ...runsQa.value].some((r) =>
      ['queued', 'running'].includes(r.status)
    )
    if (hasActive && !runsTimer) runsTimer = setInterval(() => loadRuns({ silent: true }), 3000)
    if (!hasActive && runsTimer) {
      clearInterval(runsTimer)
      runsTimer = null
    }
  } finally {
    if (!silent) runsLoading.value = false
  }
}

const runStatusMap = {
  queued: { label: '排队中', type: 'default' },
  running: { label: '运行中', type: 'info' },
  completed: { label: '已完成', type: 'success' },
  cancelled: { label: '已取消', type: 'warning' },
  failed: { label: '失败', type: 'error' },
}

async function cancelRun(row) {
  await api.cancelExpRun({ run_id: row.id })
  message.success('已请求取消')
  await loadRuns({ silent: true })
}

async function deleteRun(row) {
  await api.deleteExpRun({ run_id: row.id })
  message.success('已删除')
  await loadRuns()
}

function renderRunProgress(row) {
  const s = runStatusMap[row.status] || { label: row.status, type: 'default' }
  const tag = h(NTag, { size: 'small', type: s.type, bordered: false }, () => s.label)
  if (!['queued', 'running'].includes(row.status)) return tag
  const percent = Number(row.progress?.percent ?? 0)
  const parts = [
    h('div', { class: 'exp-run-progress-line' }, [
      tag,
      h(NProgress, {
        type: 'line',
        percentage: percent,
        height: 6,
        showIndicator: false,
        class: 'exp-run-progress-bar',
      }),
      h('span', { class: 'exp-run-progress-pct' }, `${percent}%`),
    ]),
  ]
  const stage = row.progress?.stage
  if (stage) {
    const elapsed = row.progress?.elapsed_seconds
    parts.push(
      h(
        'div',
        { class: 'exp-run-progress-stage' },
        elapsed != null ? `${stage} · 已用 ${formatElapsed(elapsed)}` : stage
      )
    )
  }
  return h('div', { class: 'exp-run-progress-cell' }, parts)
}

function formatElapsed(seconds) {
  const s = Number(seconds)
  if (!Number.isFinite(s) || s < 0) return '-'
  if (s < 60) return `${Math.floor(s)} 秒`
  return `${Math.floor(s / 60)} 分 ${Math.floor(s % 60)} 秒`
}

function runActions(row, kind) {
  const btns = []
  if (row.status === 'completed' || row.status === 'cancelled') {
    btns.push(
      h(
        NButton,
        {
          size: 'small',
          quaternary: true,
          type: 'primary',
          onClick: () =>
            router.push(
              kind === 'qa'
                ? `/system/experiment/qa-run/${row.id}`
                : `/system/experiment/run/${row.id}`
            ),
        },
        {
          icon: () => h(TheIcon, { icon: 'mdi:chart-box-outline', size: 16 }),
          default: () => '结果',
        }
      )
    )
  }
  if (['queued', 'running'].includes(row.status)) {
    btns.push(
      h(
        NButton,
        { size: 'small', quaternary: true, onClick: () => cancelRun(row) },
        {
          icon: () => h(TheIcon, { icon: 'mdi:stop-circle-outline', size: 16 }),
          default: () => '取消',
        }
      )
    )
  } else {
    btns.push(
      h(
        NPopconfirm,
        { onPositiveClick: () => deleteRun(row) },
        {
          trigger: () =>
            h(
              NButton,
              { size: 'small', quaternary: true, type: 'error' },
              {
                icon: () => h(TheIcon, { icon: 'mdi:trash-can-outline', size: 16 }),
                default: () => '删除',
              }
            ),
          default: () => '确认删除该运行及全部结果？',
        }
      )
    )
  }
  return h('div', { class: 'exp-row-actions' }, btns)
}

function buildRunColumns(kind) {
  const cols = []
  if (kind === 'qa') {
    cols.push({
      title: '策略',
      key: 'strategy',
      width: 150,
      ellipsis: { tooltip: true },
      render: (row) => row.configs?.[0]?.name || '-',
    })
  } else {
    cols.push({
      title: '配置',
      key: 'configs',
      width: 90,
      align: 'center',
      render: (row) => `${(row.configs || []).length} 组`,
    })
  }
  cols.push(
    { title: '名称', key: 'name', ellipsis: { tooltip: true } },
    {
      title: '题数',
      key: 'question_limit',
      width: 110,
      align: 'center',
      render: (row) => `${row.question_limit || '全部'}${row.include_ood ? ' +OOD' : ''}`,
    },
    { title: '进度', key: 'progress', width: 230, render: renderRunProgress },
    {
      title: '创建时间',
      key: 'created_at',
      width: 160,
      render: (row) => formatUpdatedAt(row.created_at),
    },
    {
      title: '操作',
      key: 'actions',
      width: 190,
      align: 'center',
      render: (row) => runActions(row, kind),
    }
  )
  return cols
}

const retrievalRunColumns = computed(() => buildRunColumns('retrieval'))
const qaRunColumns = computed(() => buildRunColumns('qa'))

function onRunCreated() {
  showRunModal.value = false
  message.success('实验已启动')
  loadRuns()
}

function onQaRunCreated() {
  showQaModal.value = false
  message.success('问答测评已启动')
  runTab.value = 'qa'
  loadRuns()
}

// ---------------------------------------------------------------- misc
function goBack() {
  router.push('/system/experiment')
}

onMounted(async () => {
  try {
    await Promise.all([loadDetail(), loadDocuments(), loadQuestions(), loadRuns()])
    restoreTasks()
  } finally {
    pageLoading.value = false
  }
})

onUnmounted(() => {
  if (runsTimer) clearInterval(runsTimer)
})
</script>

<template>
  <AppPage :show-footer="false">
    <div class="exp-layout">
      <header class="exp-page-header">
        <div class="exp-title-row">
          <n-button quaternary size="small" @click="goBack">
            <template #icon><TheIcon icon="mdi:arrow-left" :size="16" /></template>
            返回实验平台
          </n-button>
          <div class="exp-title-main">
            <h1 class="exp-page-title">{{ dataset?.name || '实验数据集' }}</h1>
            <p class="exp-page-desc">{{ dataset?.description || 'RAG 检索消融评测数据集' }}</p>
          </div>
          <div class="exp-title-tags">
            <n-tag size="small" :bordered="false">{{ dataset?.doc_count || 0 }} 文档</n-tag>
            <n-tag size="small" :bordered="false">{{ dataset?.question_count || 0 }} 问题</n-tag>
            <n-button size="small" quaternary @click="loadDocuments">
              <template #icon><TheIcon icon="mdi:refresh" :size="16" /></template>
              刷新
            </n-button>
          </div>
        </div>
        <n-alert
          v-if="dataset?.unmatched_gold_keys?.length"
          type="warning"
          :bordered="false"
          class="exp-alert"
        >
          {{ dataset.unmatched_gold_keys.length }} 个 gold
          文件名未匹配到已上传文档（命中评测会偏低）：
          {{ dataset.unmatched_gold_keys.slice(0, 5).join(', ') }}
          <span v-if="dataset.unmatched_gold_keys.length > 5">…</span>
        </n-alert>
      </header>

      <n-spin :show="pageLoading">
        <!-- 上传区（常驻） -->
        <section class="exp-section">
          <h2 class="exp-h2">上传实验文档</h2>
          <n-upload
            ref="uploadRef"
            class="exp-drop"
            :show-file-list="false"
            :default-upload="false"
            multiple
            accept=".pdf,.docx,.xlsx,.txt,.md,.markdown,.csv,.py,.js,.ts,.tsx,.jsx,.java,.go,.rs,.c,.h,.cpp,.cs,.rb,.php,.swift,.kt,.scala,.sh,.ps1,.sql,.yaml,.yml,.toml,.ini,.json,.xml,.html,.css,.vue"
            @change="onUploadChange"
          >
            <n-upload-dragger>
              <div class="exp-drop-inner">
                <TheIcon icon="mdi:cloud-upload-outline" :size="36" class="exp-drop-icon" />
                <div class="exp-drop-title">拖拽或点击批量上传实验文档</div>
                <div class="exp-drop-formats">
                  <n-tag
                    v-for="fmt in ['PDF', 'Word', 'Excel', 'TXT', 'MD']"
                    :key="fmt"
                    size="tiny"
                    :bordered="false"
                  >
                    {{ fmt }}
                  </n-tag>
                </div>
                <div class="exp-drop-hint">
                  单文件上限 {{ MAX_FILE_MB }}MB；文件名将作为命中判定的 file_key，请与问题集 gold
                  保持一致
                </div>
              </div>
            </n-upload-dragger>
          </n-upload>
        </section>

        <!-- 处理进度（常驻，可展开 + 限高） -->
        <section v-if="tasks.length" class="exp-section">
          <div class="exp-overview">
            <div class="exp-overview-head">
              <h2 class="exp-h2 exp-overview-title">处理进度</h2>
              <div class="exp-overview-actions">
                <n-button
                  v-if="hasFinishedTasks"
                  size="tiny"
                  quaternary
                  @click="clearFinishedTasks"
                >
                  <template #icon><TheIcon icon="mdi:broom" :size="14" /></template>
                  清除已完成
                </n-button>
                <button type="button" class="exp-detail-toggle" @click="detailOpen = !detailOpen">
                  展开明细
                  <TheIcon :icon="detailOpen ? 'mdi:chevron-up' : 'mdi:chevron-down'" :size="16" />
                </button>
              </div>
            </div>
            <n-progress
              type="line"
              :percentage="overallPercent"
              :status="overallStatus"
              :height="10"
            />
            <div class="exp-batch-summary">
              共 {{ batchSummary.total }} 个任务 · 完成 {{ batchSummary.done }} · 进行中
              {{ batchSummary.active }} · 失败 {{ batchSummary.failed }}
            </div>
          </div>
          <n-collapse-transition :show="detailOpen">
            <div class="exp-tasks">
              <div v-for="task in visibleTasks" :key="task.key" class="exp-task">
                <div class="exp-task-head">
                  <div class="exp-task-title">
                    <span :class="fileKindClass('', task.filename)">
                      <TheIcon :icon="fileKindIcon('', task.filename)" :size="18" />
                    </span>
                    <span class="exp-task-name" :title="task.filename">{{ task.filename }}</span>
                    <n-tag size="tiny" :type="taskStatusMeta(task).type" :bordered="false">
                      {{ taskStatusMeta(task).label }}
                    </n-tag>
                  </div>
                  <n-button
                    v-if="isTaskActive(task)"
                    size="tiny"
                    quaternary
                    type="error"
                    @click="cancelTask(task)"
                  >
                    <template #icon
                      ><TheIcon icon="mdi:close-circle-outline" :size="14"
                    /></template>
                    取消
                  </n-button>
                </div>
                <n-progress
                  type="line"
                  :percentage="clampPercent(task.percent)"
                  :status="taskProgressStatus(task.status)"
                  :height="8"
                />
                <div
                  class="exp-task-stage"
                  :class="{ 'exp-task-stage-error': ['failed', 'timeout'].includes(task.status) }"
                >
                  {{ taskStageText(task) }}
                </div>
              </div>
              <div v-if="hiddenTaskCount" class="exp-task-more">
                仅展示前 300 个任务，其余 {{ hiddenTaskCount }} 个未显示（不影响后台处理）
              </div>
            </div>
          </n-collapse-transition>
        </section>

        <!-- 内容 Tabs -->
        <n-tabs v-model:value="tab" type="line" animated>
          <!-- 文档 -->
          <n-tab-pane name="docs" tab="文档列表">
            <div v-if="documents.length" class="exp-stats">
              <div class="exp-stat">
                <div class="exp-stat-value">{{ docStats.docs }}</div>
                <div class="exp-stat-label">文档</div>
              </div>
              <div class="exp-stat">
                <div class="exp-stat-value">{{ docStats.chunks }}</div>
                <div class="exp-stat-label">分块</div>
              </div>
              <div class="exp-stat exp-stat--types">
                <div class="exp-stat-types">
                  <n-tag
                    v-for="item in docStats.types"
                    :key="item.type"
                    size="tiny"
                    :type="fileKindTagType(item.type, '')"
                    :bordered="false"
                  >
                    {{ item.type }} {{ item.count }}
                  </n-tag>
                </div>
                <div class="exp-stat-label">类型分布</div>
              </div>
            </div>

            <div v-if="documents.length" class="exp-list-toolbar">
              <n-input
                v-model:value="docKeyword"
                placeholder="搜索文件名"
                clearable
                class="exp-search"
                @update:value="docPage = 1"
              >
                <template #prefix><TheIcon icon="mdi:magnify" :size="16" /></template>
              </n-input>
              <n-select
                v-model:value="docTypeFilter"
                :options="docTypeOptions"
                class="exp-type-filter"
                @update:value="docPage = 1"
              />
              <div class="exp-toolbar-right">
                <n-popconfirm @positive-click="batchDeleteDocuments">
                  <template #trigger>
                    <n-button size="small" quaternary type="error" :loading="deletingDocs">
                      <template #icon
                        ><TheIcon icon="mdi:delete-sweep-outline" :size="16"
                      /></template>
                      {{
                        hasDocFilter
                          ? `删除筛选结果（${filteredDocuments.length}）`
                          : `清空全部文档（${documents.length}）`
                      }}
                    </n-button>
                  </template>
                  <template v-if="hasDocFilter">
                    确认删除当前筛选出的
                    {{ filteredDocuments.length }} 个文档？向量与元数据将一并清理
                  </template>
                  <template v-else>
                    确认清空全部 {{ documents.length }} 个文档？向量与元数据将一并清理
                  </template>
                </n-popconfirm>
              </div>
            </div>

            <n-data-table
              v-if="documents.length"
              :columns="docColumns"
              :data="filteredDocuments"
              :loading="docsLoading"
              :pagination="docPagination"
              :bordered="true"
              size="small"
            />
            <div v-if="!documents.length && !docsLoading" class="exp-empty">
              <TheIcon icon="mdi:folder-open-outline" :size="40" class="exp-empty-icon" />
              <div class="exp-empty-title">暂无文档</div>
              <p class="exp-empty-hint">
                拖拽或点击上方区域上传实验文档，文件名需与问题集 gold 一致
              </p>
              <n-button type="primary" @click="openFilePicker">
                <template #icon><TheIcon icon="mdi:cloud-upload-outline" :size="16" /></template>
                上传文档
              </n-button>
            </div>
            <div
              v-else-if="documents.length && !filteredDocuments.length && !docsLoading"
              class="exp-empty exp-empty--filter"
            >
              没有符合筛选条件的文档
            </div>
          </n-tab-pane>

          <!-- 问题集 -->
          <n-tab-pane name="questions" tab="问题集">
            <div class="exp-list-toolbar">
              <n-upload
                ref="qUploadRef"
                :show-file-list="false"
                :default-upload="false"
                accept=".json,.jsonl"
                @change="onImportQuestions"
              >
                <n-button size="small" type="primary" :loading="importing">
                  <template #icon><TheIcon icon="mdi:upload-outline" :size="16" /></template>
                  导入问题集
                </n-button>
              </n-upload>
              <n-checkbox v-model:checked="replaceOnImport">导入前清空旧问题</n-checkbox>
              <n-select
                v-model:value="qOodFilter"
                size="small"
                class="exp-type-filter"
                :options="[
                  { label: '全部题目', value: null },
                  { label: '库内题', value: false },
                  { label: 'OOD 库外题', value: true },
                ]"
                @update:value="
                  () => {
                    qPage = 1
                    loadQuestions()
                  }
                "
              />
              <div v-if="questions.total" class="exp-toolbar-right">
                <n-popconfirm @positive-click="clearQuestions">
                  <template #trigger>
                    <n-button size="small" quaternary type="error">
                      <template #icon
                        ><TheIcon icon="mdi:delete-sweep-outline" :size="16"
                      /></template>
                      {{ clearQuestionsLabel }}（{{ questions.total }}）
                    </n-button>
                  </template>
                  确认{{ clearQuestionsLabel }}（共
                  {{ questions.total }} 条）？仅删除问题记录，不影响文档。
                </n-popconfirm>
              </div>
            </div>
            <n-alert type="info" :bordered="false" class="exp-alert">
              兼容 RAG_test 格式：dataset.json（file_key/file_keys/documents 自动识别为 gold）与
              ood_questions.json（document_in_pack=false 自动标记为 OOD）。
            </n-alert>
            <n-data-table
              :columns="qColumns"
              :data="questions.items"
              :loading="qLoading"
              :bordered="true"
              size="small"
              :row-key="(r) => r.id"
              remote
              :pagination="{
                page: qPage,
                pageSize: qPageSize,
                itemCount: questions.total,
                showSizePicker: true,
                pageSizes: [20, 50, 100],
                prefix: ({ itemCount }) => `共 ${itemCount} 题`,
                onChange: (p) => {
                  qPage = p
                  loadQuestions()
                },
                onUpdatePageSize: (s) => {
                  qPageSize = s
                  qPage = 1
                  loadQuestions()
                },
              }"
            />
          </n-tab-pane>

          <!-- 实验运行 -->
          <n-tab-pane name="runs" tab="实验运行">
            <n-tabs v-model:value="runTab" type="segment" size="small" class="exp-run-subtabs">
              <n-tab-pane name="retrieval" :tab="`检索消融（${runsRetrieval.length}）`">
                <div class="exp-list-toolbar">
                  <n-button type="primary" size="small" @click="showRunModal = true">
                    <template #icon><TheIcon icon="mdi:play-circle-outline" :size="16" /></template>
                    新建消融实验
                  </n-button>
                  <n-button size="small" quaternary @click="loadRuns()">
                    <template #icon><TheIcon icon="mdi:refresh" :size="16" /></template>
                    刷新
                  </n-button>
                </div>
                <n-data-table
                  :columns="retrievalRunColumns"
                  :data="runsRetrieval"
                  :loading="runsLoading"
                  :bordered="true"
                  size="small"
                  :row-key="(r) => r.id"
                />
              </n-tab-pane>
              <n-tab-pane name="qa" :tab="`问答测评（${runsQa.length}）`">
                <div class="exp-list-toolbar">
                  <n-button type="primary" size="small" @click="showQaModal = true">
                    <template #icon
                      ><TheIcon icon="mdi:comment-question-outline" :size="16"
                    /></template>
                    新建问答测评
                  </n-button>
                  <n-button size="small" quaternary @click="loadRuns()">
                    <template #icon><TheIcon icon="mdi:refresh" :size="16" /></template>
                    刷新
                  </n-button>
                </div>
                <n-data-table
                  :columns="qaRunColumns"
                  :data="runsQa"
                  :loading="runsLoading"
                  :bordered="true"
                  size="small"
                  :row-key="(r) => r.id"
                />
              </n-tab-pane>
            </n-tabs>
          </n-tab-pane>
        </n-tabs>
      </n-spin>
    </div>

    <!-- 上传确认弹窗（知识库同款） -->
    <n-modal
      v-model:show="showConfirmModal"
      preset="card"
      title="确认上传"
      :style="{ width: 'min(560px, 92vw)' }"
      @update:show="onConfirmModalShowUpdate"
    >
      <p class="exp-confirm-hint">
        共 {{ pendingFiles.length }} 个文件，可上传 {{ validPendingFiles.length }} 个，合计
        {{ formatFileSize(pendingValidTotalSize) }}
      </p>
      <div class="exp-confirm-list">
        <div
          v-for="item in visiblePendingFiles"
          :key="`${item.name}_${item.size}`"
          class="exp-confirm-row"
          :class="{ 'exp-confirm-row-invalid': item.overLimit }"
        >
          <span :class="['exp-confirm-icon', fileKindClass('', item.name)]">
            <TheIcon :icon="fileKindIcon('', item.name)" :size="20" />
          </span>
          <div class="exp-confirm-main">
            <div class="exp-confirm-name" :title="item.name">{{ item.name }}</div>
            <div class="exp-confirm-meta">
              <span class="exp-confirm-ext">{{ item.ext.toUpperCase() || '-' }}</span>
              <span>{{ formatFileSize(item.size) }}</span>
              <span v-if="item.modifiedAt">{{ formatModified(item.modifiedAt) }}</span>
              <span v-if="item.overLimit" class="exp-confirm-over">
                超过 {{ MAX_FILE_MB }}MB 上限
              </span>
            </div>
          </div>
        </div>
        <div v-if="hiddenPendingCount" class="exp-file-more">
          仅展示前 300 个文件，其余 {{ hiddenPendingCount }} 个未显示（仍会按顺序上传）
        </div>
      </div>
      <template #footer>
        <div class="exp-confirm-footer">
          <n-button quaternary @click="cancelConfirm">取消</n-button>
          <n-button type="primary" :disabled="!validPendingFiles.length" @click="confirmUpload">
            <template #icon><TheIcon icon="mdi:cloud-upload-outline" :size="16" /></template>
            开始上传<template v-if="validPendingFiles.length"
              >（{{ validPendingFiles.length }}）</template
            >
          </n-button>
        </div>
      </template>
    </n-modal>

    <RunConfigModal
      v-model:show="showRunModal"
      :dataset-id="datasetId"
      :question-count="dataset?.question_count || 0"
      @created="onRunCreated"
    />

    <QaRunModal
      v-model:show="showQaModal"
      :dataset-id="datasetId"
      :question-count="dataset?.question_count || 0"
      :answer-count="dataset?.answer_count || 0"
      @created="onQaRunCreated"
    />
  </AppPage>
</template>

<style scoped>
.exp-layout {
  width: 100%;
  max-width: 1080px;
  padding-bottom: 24px;
  margin: 0 auto;
}
.exp-page-header {
  margin-bottom: 20px;
}
.exp-title-row {
  display: flex;
  align-items: flex-start;
  gap: 12px;
}
.exp-title-main {
  flex: 1;
  min-width: 0;
}
.exp-page-title {
  margin: 0;
  font-size: 22px;
  font-weight: 700;
  line-height: 1.3;
  color: var(--n-text-color-2);
}
.exp-page-desc {
  margin: 4px 0 0;
  font-size: 13px;
  color: var(--n-text-color-3);
}
.exp-title-tags {
  display: flex;
  flex: none;
  gap: 8px;
  align-items: center;
}
.exp-alert {
  margin-top: 12px;
  font-size: 12px;
}
.exp-section {
  margin-bottom: 24px;
}
.exp-h2 {
  margin: 0 0 8px;
  font-size: 16px;
  font-weight: 600;
  color: var(--n-text-color-2);
}
.exp-drop {
  width: 100%;
}
.exp-drop :deep(.n-upload-trigger) {
  width: 100%;
}
.exp-drop :deep(.n-upload-dragger) {
  padding: 24px 20px;
  border-radius: 12px;
}
.exp-drop-inner {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
}
.exp-drop-icon {
  color: var(--n-primary-color);
  opacity: 0.9;
}
.exp-drop-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--n-text-color-2);
}
.exp-drop-formats {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 6px;
}
.exp-drop-hint {
  font-size: 12px;
  line-height: 1.5;
  color: var(--n-text-color-3);
}
.exp-overview {
  padding: 14px 16px;
  margin-bottom: 12px;
  background: var(--n-color-embedded);
  border: 1px solid var(--n-border-color);
  border-radius: 10px;
}
.exp-overview-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
}
.exp-overview-title {
  margin: 0;
}
.exp-overview-actions {
  display: flex;
  flex: none;
  gap: 4px;
  align-items: center;
}
.exp-detail-toggle {
  display: inline-flex;
  gap: 4px;
  align-items: center;
  padding: 2px 8px;
  font-size: 13px;
  line-height: 1.6;
  color: var(--n-text-color-3);
  cursor: pointer;
  background: transparent;
  border: none;
  border-radius: 6px;
}
.exp-detail-toggle:hover {
  color: var(--n-text-color-2);
  background: rgba(128, 128, 128, 0.12);
}
.exp-batch-summary {
  margin: 8px 0 0;
  font-size: 13px;
  color: var(--n-text-color-3);
}
.exp-tasks {
  display: flex;
  flex-direction: column;
  gap: 12px;
  max-height: 490px;
  padding-right: 4px;
  overflow-y: auto;
  scrollbar-width: thin;
}
.exp-task {
  padding: 12px 14px;
  background: var(--n-color-embedded);
  border: 1px solid var(--n-border-color);
  border-radius: 8px;
}
.exp-task-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 8px;
}
.exp-task-title {
  display: flex;
  flex: 1;
  gap: 8px;
  align-items: center;
  min-width: 0;
}
.exp-task-name {
  overflow: hidden;
  font-size: 14px;
  font-weight: 500;
  color: var(--n-text-color-2);
  text-overflow: ellipsis;
  white-space: nowrap;
}
.exp-task-stage {
  margin-top: 6px;
  overflow: hidden;
  font-size: 12px;
  color: var(--n-text-color-3);
  text-overflow: ellipsis;
  white-space: nowrap;
}
.exp-task-stage-error {
  color: var(--n-error-color);
}
.exp-task-more,
.exp-file-more {
  padding: 8px 4px;
  font-size: 12px;
  line-height: 1.5;
  text-align: center;
  color: var(--n-text-color-3);
}
.exp-stats {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 14px;
}
.exp-stat {
  min-width: 104px;
  padding: 10px 14px;
  background: var(--n-color-embedded);
  border: 1px solid var(--n-border-color);
  border-radius: 10px;
}
.exp-stat--types {
  flex: 1;
  min-width: 160px;
}
.exp-stat-value {
  font-size: 20px;
  font-weight: 700;
  line-height: 1.2;
  color: var(--n-text-color-2);
}
.exp-stat-label {
  margin-top: 4px;
  font-size: 12px;
  color: var(--n-text-color-3);
}
.exp-stat-types {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
  min-height: 24px;
}
.exp-list-toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
  margin-bottom: 12px;
}
.exp-search {
  flex: 1;
  max-width: 320px;
}
.exp-type-filter {
  flex: none;
  width: 150px;
}
.exp-toolbar-right {
  display: flex;
  gap: 4px;
  margin-left: auto;
}
.exp-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding: 40px 16px;
  text-align: center;
  border: 1px dashed var(--n-border-color);
  border-radius: 12px;
}
.exp-empty--filter {
  padding: 20px 16px;
  font-size: 14px;
  color: var(--n-text-color-3);
  border: none;
}
.exp-empty-icon {
  color: var(--n-text-color-3);
}
.exp-empty-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--n-text-color-2);
}
.exp-empty-hint {
  margin: 0 0 8px;
  font-size: 13px;
  line-height: 1.5;
  color: var(--n-text-color-3);
}
.exp-confirm-hint {
  margin: 0 0 12px;
  font-size: 13px;
  line-height: 1.5;
  color: var(--n-text-color-3);
}
.exp-confirm-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: min(46vh, 400px);
  overflow-y: auto;
}
.exp-confirm-row {
  display: flex;
  gap: 10px;
  align-items: center;
  padding: 8px 10px;
  border: 1px solid var(--n-border-color);
  border-radius: 8px;
}
.exp-confirm-row-invalid {
  border-color: var(--n-error-color);
}
.exp-confirm-icon {
  flex: none;
}
.exp-confirm-main {
  flex: 1;
  min-width: 0;
}
.exp-confirm-name {
  overflow: hidden;
  font-size: 14px;
  font-weight: 500;
  color: var(--n-text-color-2);
  text-overflow: ellipsis;
  white-space: nowrap;
}
.exp-confirm-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 2px;
  font-size: 12px;
  color: var(--n-text-color-3);
}
.exp-confirm-ext {
  font-weight: 600;
}
.exp-confirm-over {
  font-weight: 600;
  color: var(--n-error-color);
}
.exp-confirm-footer {
  display: flex;
  gap: 12px;
  justify-content: flex-end;
}
:deep(.exp-file-cell) {
  display: flex;
  gap: 8px;
  align-items: center;
  min-width: 0;
}
:deep(.exp-file-cell-name) {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
:deep(.exp-run-progress-cell) {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
:deep(.exp-run-progress-line) {
  display: flex;
  gap: 8px;
  align-items: center;
}
:deep(.exp-run-progress-bar) {
  flex: 1;
  min-width: 60px;
}
:deep(.exp-run-progress-pct) {
  flex: none;
  font-size: 12px;
  color: var(--n-text-color-3);
}
:deep(.exp-run-progress-stage) {
  overflow: hidden;
  font-size: 11px;
  color: var(--n-text-color-3);
  text-overflow: ellipsis;
  white-space: nowrap;
}
.exp-run-subtabs {
  margin-top: 4px;
}
:deep(.exp-row-actions) {
  display: flex;
  gap: 2px;
  justify-content: center;
}
:deep(.exp-kind-pdf) {
  color: #e53935;
}
:deep(.exp-kind-word) {
  color: #1e88e5;
}
:deep(.exp-kind-excel) {
  color: #43a047;
}
:deep(.exp-kind-md) {
  color: #546e7a;
}
:deep(.exp-kind-code) {
  color: #8e24aa;
}
:deep(.exp-kind-text) {
  color: var(--n-text-color-3);
}
</style>
