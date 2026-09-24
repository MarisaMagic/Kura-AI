<script setup>
import { computed, h, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NTag, useMessage } from 'naive-ui'
import * as echarts from 'echarts/core'
import { BarChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import AppPage from '@/components/page/AppPage.vue'
import TheIcon from '@/components/icon/TheIcon.vue'
import api from '@/api'

echarts.use([BarChart, GridComponent, TooltipComponent, LegendComponent, CanvasRenderer])

defineOptions({ name: '实验结果' })

const route = useRoute()
const router = useRouter()
const message = useMessage()
const runId = Number(route.params.id)

const loading = ref(true)
const run = ref(null)
const job = ref({})
const summary = ref([])
const perQuestion = ref([])
const chartRef = ref(null)
const oodChartRef = ref(null)
let chart = null
let oodChart = null
let pollTimer = null

const isActive = computed(() => ['queued', 'running'].includes(run.value?.status))

async function loadAll() {
  const [statusRes, resultsRes] = await Promise.all([
    api.getExpRunStatus({ run_id: runId }),
    api.getExpRunResults({ run_id: runId }),
  ])
  run.value = statusRes.data?.run || null
  job.value = statusRes.data?.job || {}
  summary.value = resultsRes.data?.summary || []
  perQuestion.value = resultsRes.data?.per_question || []
  if (isActive.value) {
    if (!pollTimer) pollTimer = setInterval(loadAll, 3000)
  } else {
    stopPolling()
    await nextTick()
    renderCharts()
  }
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

function renderCharts() {
  if (!summary.value.length) return
  const labels = summary.value.map((s) => s.config?.name || `配置${s.config_idx + 1}`)
  if (chartRef.value) {
    chart = chart || echarts.init(chartRef.value)
    chart.setOption({
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
      legend: { data: ['Hit@k', 'MRR', 'Recall@k'] },
      grid: { left: 40, right: 16, top: 40, bottom: 60 },
      xAxis: {
        type: 'category',
        data: labels,
        axisLabel: { interval: 0, rotate: labels.length > 4 ? 20 : 0, fontSize: 11 },
      },
      yAxis: { type: 'value', max: 1 },
      series: [
        {
          name: 'Hit@k',
          type: 'bar',
          data: summary.value.map((s) => s.metrics.hit_rate),
          itemStyle: { color: '#2080f0' },
          barMaxWidth: 28,
        },
        {
          name: 'MRR',
          type: 'bar',
          data: summary.value.map((s) => s.metrics.mrr),
          itemStyle: { color: '#18a058' },
          barMaxWidth: 28,
        },
        {
          name: 'Recall@k',
          type: 'bar',
          data: summary.value.map((s) => s.metrics.recall),
          itemStyle: { color: '#f0a020' },
          barMaxWidth: 28,
        },
      ],
    })
    chart.resize()
  }
  const oodRows = summary.value.filter((s) => s.metrics.ood?.count)
  if (oodChartRef.value && oodRows.length) {
    oodChart = oodChart || echarts.init(oodChartRef.value)
    oodChart.setOption({
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
      legend: { data: ['门控拒答率'] },
      grid: { left: 40, right: 16, top: 40, bottom: 60 },
      xAxis: {
        type: 'category',
        data: oodRows.map((s) => s.config?.name || `配置${s.config_idx + 1}`),
        axisLabel: { interval: 0, rotate: 20, fontSize: 11 },
      },
      yAxis: { type: 'value', max: 1 },
      series: [
        {
          name: '门控拒答率',
          type: 'bar',
          data: oodRows.map((s) => s.metrics.ood.gated_rate ?? 0),
          itemStyle: { color: '#d03050' },
          barMaxWidth: 28,
          label: { show: true, position: 'top', fontSize: 10 },
        },
      ],
    })
    oodChart.resize()
  }
}

async function cancelRun() {
  await api.cancelExpRun({ run_id: runId })
  message.success('已请求取消')
  await loadAll()
}

// ---------------------------------------------------------------- 汇总表
const summaryColumns = computed(() => {
  const cols = [
    {
      title: '配置',
      key: 'name',
      render: (row) => row.config?.name || `配置${row.config_idx + 1}`,
    },
    {
      title: '模式',
      key: 'mode',
      align: 'center',
      render: (row) => row.config?.retrieval_mode || '-',
    },
    {
      title: '融合',
      key: 'fusion',
      align: 'center',
      render: (row) => (row.config?.retrieval_mode === 'hybrid' ? row.config?.fusion || '-' : '—'),
    },
    {
      title: 'Rerank',
      key: 'rerank',
      align: 'center',
      render: (row) =>
        row.config?.rerank
          ? h(NTag, { size: 'small', type: 'success', bordered: false }, () => '开')
          : h(NTag, { size: 'small', bordered: false }, () => '关'),
    },
    { title: '题数', key: 'n', align: 'center', render: (row) => row.metrics.question_count },
    {
      title: 'Hit@k',
      key: 'hit_rate',
      align: 'center',
      sorter: (a, b) => a.metrics.hit_rate - b.metrics.hit_rate,
      defaultSortOrder: 'descend',
      render: (row) => renderBest(row.metrics.hit_rate, 'hit_rate', pct(row.metrics.hit_rate)),
    },
    {
      title: 'MRR',
      key: 'mrr',
      align: 'center',
      sorter: (a, b) => a.metrics.mrr - b.metrics.mrr,
      render: (row) => renderBest(row.metrics.mrr, 'mrr', row.metrics.mrr.toFixed(4)),
    },
    {
      title: 'Recall@k',
      key: 'recall',
      align: 'center',
      sorter: (a, b) => a.metrics.recall - b.metrics.recall,
      render: (row) => renderBest(row.metrics.recall, 'recall', row.metrics.recall.toFixed(4)),
    },
    {
      title: '平均命中排名',
      key: 'avg_rank',
      align: 'center',
      render: (row) => row.metrics.avg_hit_rank || '-',
    },
    {
      title: '平均延迟',
      key: 'latency',
      align: 'center',
      render: (row) => `${row.metrics.avg_latency_ms} ms`,
    },
    {
      title: 'OOD 门控拒答',
      key: 'ood',
      align: 'center',
      render: (row) =>
        row.metrics.ood?.count
          ? `${row.metrics.ood.gated_count}/${row.metrics.ood.count}（${pct(
              row.metrics.ood.gated_rate
            )}）`
          : '—',
    },
    {
      title: '失败',
      key: 'errors',
      align: 'center',
      render: (row) =>
        row.metrics.error_count
          ? h(
              NTag,
              { size: 'small', type: 'error', bordered: false },
              () => row.metrics.error_count
            )
          : '0',
    },
  ]
  return cols
})

function pct(v) {
  return `${((v ?? 0) * 100).toFixed(1)}%`
}

function bestOf(key) {
  return Math.max(...summary.value.map((s) => s.metrics[key] ?? 0), 0)
}

function renderBest(value, key, text) {
  const best = bestOf(key)
  const isBest = summary.value.length > 1 && value > 0 && value >= best - 1e-9
  return isBest ? h('span', { class: 'exp-best' }, [`★ ${text}`]) : h('span', text)
}

// ---------------------------------------------------------------- 逐题表
const qColumns = computed(() => [
  { type: 'expand', renderExpand: renderExpand },
  {
    title: '#',
    key: 'qid',
    width: 70,
    align: 'center',
    render: (row) => row.question?.index ?? row.question?.id ?? '-',
  },
  {
    title: '问题',
    key: 'question',
    ellipsis: { tooltip: true },
    render: (row) => row.question?.question || '',
  },
  {
    title: 'gold',
    key: 'gold',
    width: 220,
    ellipsis: { tooltip: true },
    render: (row) =>
      row.question?.is_ood ? '—（OOD）' : (row.question?.gold_file_keys || []).join(', '),
  },
  ...summary.value.map((s) => ({
    title: s.config?.name || `配置${s.config_idx + 1}`,
    key: `cfg_${s.config_idx}`,
    width: 110,
    align: 'center',
    render: (row) => {
      const r = row.by_config?.[String(s.config_idx)]
      if (!r) return '-'
      if (r.error) return h(NTag, { size: 'small', type: 'error', bordered: false }, () => '失败')
      if (row.question?.is_ood) {
        return r.rerank_below_min
          ? h(NTag, { size: 'small', type: 'success', bordered: false }, () => '已拒答')
          : h(
              NTag,
              { size: 'small', type: 'warning', bordered: false },
              () => `top1 ${r.top1_score?.toFixed(2)}`
            )
      }
      return r.hit
        ? h(NTag, { size: 'small', type: 'success', bordered: false }, () => `命中 @${r.hit_rank}`)
        : h(NTag, { size: 'small', type: 'error', bordered: false }, () => '未中')
    },
  })),
])

function renderExpand(row) {
  const panes = summary.value.map((s) => {
    const r = row.by_config?.[String(s.config_idx)]
    const cfgName = s.config?.name || `配置${s.config_idx + 1}`
    if (!r) return h('div', { class: 'exp-expand-pane' }, [h('b', cfgName), h('span', '无结果')])
    const gold = new Set(row.question?.gold_file_keys || [])
    const items = (r.retrieved || []).map((d) =>
      h('li', { class: ['exp-retrieved-item', gold.has(d.filename) ? 'gold' : ''] }, [
        h('span', { class: 'exp-retrieved-rank' }, `#${d.rank}`),
        h('span', { class: 'exp-retrieved-fn' }, d.filename),
        h('span', { class: 'exp-retrieved-score' }, d.score?.toFixed(4)),
        h('div', { class: 'exp-retrieved-snippet' }, d.snippet || ''),
      ])
    )
    return h('div', { class: 'exp-expand-pane' }, [
      h('div', { class: 'exp-expand-pane-head' }, [
        h('b', cfgName),
        r.error
          ? h('span', { class: 'exp-expand-error' }, r.error)
          : h(
              'span',
              { class: 'exp-expand-meta' },
              `${r.latency_ms}ms${
                r.max_rerank_score != null
                  ? ` · rerank top ${Number(r.max_rerank_score).toFixed(3)}`
                  : ''
              }`
            ),
      ]),
      items.length
        ? h('ul', { class: 'exp-retrieved-list' }, items)
        : h('span', { class: 'exp-expand-empty' }, '（无检索结果）'),
    ])
  })
  const answer = row.question?.answer
    ? h('div', { class: 'exp-answer' }, [h('b', '参考答案：'), row.question.answer])
    : null
  return h('div', { class: 'exp-expand' }, [answer, h('div', { class: 'exp-expand-grid' }, panes)])
}

function goBack() {
  const dsId = run.value?.dataset_id
  if (dsId) router.push(`/system/experiment/dataset/${dsId}`)
  else router.push('/system/experiment')
}

const statusText = computed(() => {
  const s = run.value?.status
  if (s === 'running')
    return `运行中 ${job.value?.percent ?? 0}%（${job.value?.done ?? 0}/${
      job.value?.total ?? '?'
    } 题）`
  if (s === 'queued') return '排队中…'
  if (s === 'completed') return '已完成'
  if (s === 'cancelled') return '已取消'
  if (s === 'failed') return `失败：${run.value?.error || '未知原因'}`
  return s || ''
})

onMounted(async () => {
  try {
    await loadAll()
  } finally {
    loading.value = false
  }
  window.addEventListener('resize', resizeCharts)
})

function resizeCharts() {
  chart?.resize()
  oodChart?.resize()
}

onUnmounted(() => {
  stopPolling()
  window.removeEventListener('resize', resizeCharts)
  chart?.dispose()
  oodChart?.dispose()
})
</script>

<template>
  <AppPage :show-footer="false">
    <div class="exp-result">
      <header class="exp-result-header">
        <n-button quaternary size="small" @click="goBack">
          <template #icon><TheIcon icon="mdi:arrow-left" :size="16" /></template>
          返回数据集
        </n-button>
        <div class="exp-result-title">
          <h1>{{ run?.name || '实验结果' }}</h1>
          <p>
            <span :class="['exp-status', run?.status]">{{ statusText }}</span>
            <template v-if="run">
              · Top-K {{ run.configs?.[0]?.top_k }} · 题数限制 {{ run.question_limit || '全部'
              }}{{ run.include_ood ? ' +OOD' : '' }}
            </template>
          </p>
        </div>
        <div class="exp-result-actions">
          <n-button v-if="isActive" size="small" type="warning" @click="cancelRun">
            <template #icon><TheIcon icon="mdi:stop-circle-outline" :size="16" /></template>
            取消运行
          </n-button>
          <n-button v-else size="small" quaternary @click="loadAll">
            <template #icon><TheIcon icon="mdi:refresh" :size="16" /></template>
            刷新
          </n-button>
        </div>
      </header>

      <n-spin :show="loading">
        <template v-if="isActive">
          <div class="exp-running">
            <n-progress type="line" :percentage="job?.percent || 0" :height="12" processing />
            <p>正在逐题评测，页面会自动刷新…（{{ job?.done || 0 }}/{{ job?.total || '?' }} 题）</p>
          </div>
        </template>

        <template v-else-if="summary.length">
          <section class="exp-section">
            <h2>指标对比</h2>
            <div ref="chartRef" class="exp-chart" />
          </section>

          <section v-if="summary.some((s) => s.metrics.ood?.count)" class="exp-section">
            <h2>OOD 库外题拒答表现</h2>
            <p class="exp-section-tip">
              门控拒答 = rerank 最高分低于 RERANK_MIN_SCORE 阈值（仅 rerank 配置有该信号）；未开
              rerank 的配置可参考 top1 分数人工判断误命中风险
            </p>
            <div ref="oodChartRef" class="exp-chart exp-chart-sm" />
          </section>

          <section class="exp-section">
            <h2>汇总（★ 为各指标最优）</h2>
            <n-data-table
              :columns="summaryColumns"
              :data="summary"
              :bordered="false"
              size="small"
              :row-key="(r) => r.config_idx"
            />
          </section>

          <section class="exp-section">
            <h2>逐题明细（点击展开查看各配置检索结果）</h2>
            <n-data-table
              :columns="qColumns"
              :data="perQuestion"
              :bordered="false"
              size="small"
              :row-key="(r) => r.question?.id"
              :max-height="560"
              :scroll-x="900"
            />
          </section>
        </template>

        <n-empty v-else description="暂无结果数据" class="exp-empty" />
      </n-spin>
    </div>
  </AppPage>
</template>

<style scoped>
.exp-result {
  display: flex;
  flex-direction: column;
  gap: 14px;
  width: 100%;
  max-width: 1080px;
  padding-bottom: 24px;
  margin: 0 auto;
}
.exp-result-header {
  display: flex;
  gap: 12px;
  align-items: flex-start;
}
.exp-result-title {
  flex: 1;
  min-width: 0;
}
.exp-result-title h1 {
  margin: 0;
  font-size: 22px;
  font-weight: 700;
  line-height: 1.3;
  color: var(--n-text-color-2);
}
.exp-result-title p {
  margin: 4px 0 0;
  font-size: 12px;
  color: var(--n-text-color-3);
}
.exp-result-actions {
  display: flex;
  flex: none;
  gap: 8px;
}
.exp-status.running,
.exp-status.queued {
  color: #2080f0;
}
.exp-status.completed {
  color: #18a058;
}
.exp-status.failed {
  color: #d03050;
}
.exp-status.cancelled {
  color: #f0a020;
}
.exp-running {
  padding: 40px 60px;
  text-align: center;
}
.exp-running p {
  margin-top: 10px;
  font-size: 13px;
  color: var(--n-text-color-3);
}
.exp-section h2 {
  margin: 0 0 10px;
  font-size: 15px;
  font-weight: 600;
  color: var(--n-text-color-2);
}
.exp-section-tip {
  margin: -6px 0 8px;
  font-size: 12px;
  color: var(--n-text-color-3);
}
.exp-chart {
  width: 100%;
  height: 320px;
}
.exp-chart-sm {
  height: 240px;
}
.exp-empty {
  padding: 60px 0;
}
:deep(.exp-best) {
  font-weight: 700;
  color: #18a058;
}
:deep(.exp-expand) {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 6px 10px;
}
:deep(.exp-answer) {
  padding: 8px 10px;
  font-size: 12px;
  background: rgba(128, 128, 128, 0.08);
  border-radius: 6px;
}
:deep(.exp-expand-grid) {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
  gap: 10px;
}
:deep(.exp-expand-pane) {
  padding: 8px 10px;
  border: 1px solid rgba(128, 128, 128, 0.15);
  border-radius: 8px;
}
:deep(.exp-expand-pane-head) {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  margin-bottom: 6px;
  font-size: 13px;
}
:deep(.exp-expand-meta) {
  font-size: 11px;
  opacity: 0.6;
}
:deep(.exp-expand-error) {
  font-size: 11px;
  color: #d03050;
}
:deep(.exp-expand-empty) {
  font-size: 12px;
  opacity: 0.5;
}
:deep(.exp-retrieved-list) {
  padding: 0;
  margin: 0;
  list-style: none;
}
:deep(.exp-retrieved-item) {
  display: grid;
  grid-template-columns: 34px 1fr 60px;
  gap: 6px;
  align-items: baseline;
  padding: 4px 6px;
  font-size: 12px;
  border-radius: 4px;
}
:deep(.exp-retrieved-item.gold) {
  background: rgba(24, 160, 88, 0.12);
  font-weight: 600;
}
:deep(.exp-retrieved-rank) {
  opacity: 0.6;
}
:deep(.exp-retrieved-fn) {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
:deep(.exp-retrieved-score) {
  text-align: right;
  opacity: 0.7;
}
:deep(.exp-retrieved-snippet) {
  grid-column: 1 / -1;
  font-weight: 400;
  font-size: 11px;
  line-height: 1.5;
  opacity: 0.55;
  display: -webkit-box;
  overflow: hidden;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}
</style>
