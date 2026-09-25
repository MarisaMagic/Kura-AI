<script setup>
import { computed, h, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NTag, useMessage } from 'naive-ui'
import * as echarts from 'echarts/core'
import { BarChart, PieChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import AppPage from '@/components/page/AppPage.vue'
import TheIcon from '@/components/icon/TheIcon.vue'
import api from '@/api'

echarts.use([BarChart, PieChart, GridComponent, TooltipComponent, LegendComponent, CanvasRenderer])

defineOptions({ name: '问答测评结果' })

const route = useRoute()
const router = useRouter()
const message = useMessage()
const runId = Number(route.params.id)

const loading = ref(true)
const run = ref(null)
const job = ref({})
const summary = ref([])
const perQuestion = ref([])

const pieRef = ref(null)
const strataRef = ref(null)
const oodRef = ref(null)
let pieChart = null
let strataChart = null
let oodChart = null
let pollTimer = null

const isActive = computed(() => ['queued', 'running'].includes(run.value?.status))
const cfgIdx = computed(() => {
  const idx = run.value?.eval_config_idx
  return Number.isInteger(idx) ? idx : 0
})
const cfgName = computed(
  () => run.value?.configs?.[cfgIdx.value]?.name || `配置${cfgIdx.value + 1}`
)
const gen = computed(
  () => summary.value.find((s) => s.config_idx === cfgIdx.value)?.metrics?.generation || null
)
const answerCount = computed(() => run.value?.snapshot?.answer_count ?? null)

// ---------------------------------------------------------------- 数据加载
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

function fmtElapsed(seconds) {
  const s = Number(seconds)
  if (!Number.isFinite(s) || s < 0) return ''
  if (s < 60) return `${Math.floor(s)} 秒`
  return `${Math.floor(s / 60)} 分 ${Math.floor(s % 60)} 秒`
}

function pct(v) {
  return `${((v ?? 0) * 100).toFixed(1)}%`
}

// ---------------------------------------------------------------- 逐题分类
function rowOf(item) {
  return item.by_config?.[String(cfgIdx.value)] || null
}

function verdictOf(item) {
  const r = rowOf(item)
  if (!r || r.error) return { key: 'error', label: '检索失败', type: 'error' }
  const am = r.answer_metrics || {}
  if (am.gen_error || !Object.keys(am).length)
    return { key: 'gen_error', label: '生成失败', type: 'error' }
  if (item.question?.is_ood) {
    if (am.refused === true) return { key: 'refused', label: '已拒答', type: 'success' }
    if (am.refused === false) return { key: 'answered', label: '误答', type: 'error' }
    return { key: 'judge_error', label: '判分失败', type: 'warning' }
  }
  if (am.correctness == null) {
    return am.judge_error
      ? { key: 'judge_error', label: '判分失败', type: 'warning' }
      : { key: 'unknown', label: '未评测', type: 'default' }
  }
  if (am.correctness >= 1) return { key: 'correct', label: '正确', type: 'success' }
  if (am.correctness >= 0.5) return { key: 'partial', label: '部分正确（历史）', type: 'warning' }
  return { key: 'wrong', label: '错误', type: 'error' }
}

const hasLegacyPartial = computed(() => (gen.value?.legacy_partial_count || 0) > 0)

const verdictOptions = computed(() => {
  const options = [
    { label: '全部结论', value: 'all' },
    { label: '正确', value: 'correct' },
  ]
  if (hasLegacyPartial.value) options.push({ label: '部分正确（历史）', value: 'partial' })
  options.push(
    { label: '错误', value: 'wrong' },
    { label: '已拒答', value: 'refused' },
    { label: '误答', value: 'answered' },
    { label: '判分失败', value: 'judge_error' },
    { label: '生成失败', value: 'gen_error' }
  )
  return options
})

const verdictFilter = ref('all')
const stratumFilter = ref('all')
const oodOnly = ref(false)
const keyword = ref('')

const stratumOptions = computed(() => {
  const set = new Set(
    perQuestion.value
      .filter((q) => !q.question?.is_ood)
      .map((q) => q.question?.stratum || 'unknown')
  )
  return [
    { label: '全部分层', value: 'all' },
    ...[...set].sort().map((s) => ({ label: s, value: s })),
  ]
})

const filteredQuestions = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  return perQuestion.value.filter((q) => {
    if (oodOnly.value && !q.question?.is_ood) return false
    if (stratumFilter.value !== 'all') {
      const s = q.question?.stratum || 'unknown'
      if (s !== stratumFilter.value) return false
    }
    if (verdictFilter.value !== 'all' && verdictOf(q).key !== verdictFilter.value) return false
    if (kw && !(q.question?.question || '').toLowerCase().includes(kw)) return false
    return true
  })
})

const inKbRows = computed(() => perQuestion.value.filter((q) => !q.question?.is_ood))
const oodRows = computed(() => perQuestion.value.filter((q) => q.question?.is_ood))

// ---------------------------------------------------------------- 指标卡片
const cards = computed(() => {
  const g = gen.value
  if (!g) return []
  const f3 = (v) => (v === null || v === undefined ? '—' : Number(v).toFixed(3))
  const correctRate = g.correct_rate ?? g.pass_exact_rate
  const n = g.count || 0
  const correctCount = correctRate == null ? null : Math.round(correctRate * n)
  const wrongCount = correctCount == null ? null : n - correctCount
  const legacyNote = hasLegacyPartial.value ? ` · 历史部分正确 ${g.legacy_partial_count} 题` : ''
  return [
    {
      label: '答案正确率',
      value: correctRate == null ? '—' : pct(correctRate),
      sub:
        correctCount == null
          ? `n=${n}${legacyNote}`
          : `正确 ${correctCount} 题 · 错误 ${wrongCount} 题 · n=${n}${legacyNote}`,
    },
    {
      label: '忠实度',
      value: g.faithfulness_mean == null ? '—' : pct(g.faithfulness_mean),
      sub: `幻觉率 ${g.hallucination_rate == null ? '—' : pct(g.hallucination_rate)}`,
    },
    { label: 'EM', value: f3(g.em), sub: '归一化精确匹配' },
    { label: '字符级 F1', value: f3(g.f1), sub: '中文按字符' },
    {
      label: '数值题命中',
      value: g.numeric?.count ? pct(g.numeric.accuracy) : '—',
      sub: `n=${g.numeric?.count || 0} · 容差 1%`,
    },
    {
      label: 'OOD 拒答正确率',
      value: g.ood?.count ? pct(g.ood.correct_refusal_rate) : '—',
      sub: `误答率 ${g.ood?.count ? pct(g.ood.false_answer_rate) : '—'} · n=${g.ood?.count || 0}`,
    },
    { label: '平均生成延迟', value: `${g.avg_answer_latency_ms ?? 0} ms`, sub: '仅 LLM 生成调用' },
    {
      label: '评测覆盖',
      value: `${g.count} 题`,
      sub: `生成失败 ${g.gen_error_count} · 判分失败 ${g.judge_error_count}`,
    },
  ]
})

// ---------------------------------------------------------------- 图表
function renderCharts() {
  const g = gen.value
  if (!g) return

  // 1. 库内题正确率分布
  const buckets = { correct: 0, partial: 0, wrong: 0, judge_error: 0, gen_error: 0 }
  for (const q of inKbRows.value) {
    const key = verdictOf(q).key
    if (key in buckets) buckets[key] += 1
  }
  const pieData = [
    { name: '正确', value: buckets.correct, itemStyle: { color: '#18a058' } },
    { name: '部分正确（历史）', value: buckets.partial, itemStyle: { color: '#f0a020' } },
    { name: '错误', value: buckets.wrong, itemStyle: { color: '#d03050' } },
    { name: '判分失败', value: buckets.judge_error, itemStyle: { color: '#909399' } },
    { name: '生成失败', value: buckets.gen_error, itemStyle: { color: '#606266' } },
  ].filter((d) => d.value > 0)
  if (pieRef.value && pieData.length) {
    pieChart = pieChart || echarts.init(pieRef.value)
    pieChart.setOption({
      tooltip: { trigger: 'item', formatter: '{b}: {c} 题（{d}%）' },
      legend: { bottom: 0 },
      series: [
        {
          type: 'pie',
          radius: ['42%', '68%'],
          center: ['50%', '44%'],
          avoidLabelOverlap: true,
          label: { formatter: '{b}\n{c}' },
          data: pieData,
        },
      ],
    })
    pieChart.resize()
  }

  // 2. 分层正确率
  const strata = g.by_stratum || {}
  const strataKeys = Object.keys(strata)
  if (strataRef.value && strataKeys.length) {
    strataChart = strataChart || echarts.init(strataRef.value)
    strataChart.setOption({
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
      legend: { top: 0 },
      grid: { left: 48, right: 16, top: 36, bottom: 36 },
      xAxis: { type: 'category', data: strataKeys },
      yAxis: { type: 'value', max: 1 },
      series: [
        {
          name: '正确率',
          type: 'bar',
          data: strataKeys.map((k) => strata[k]?.correct_rate ?? strata[k]?.correctness_mean ?? 0),
          itemStyle: { color: '#2080f0' },
          barMaxWidth: 36,
          label: {
            show: true,
            position: 'top',
            fontSize: 11,
            formatter: (p) => {
              const info = strata[strataKeys[p.dataIndex]] || {}
              const v = info.correct_rate ?? info.correctness_mean
              return v == null ? '—' : `${(v * 100).toFixed(0)}%（n=${info.count}）`
            },
          },
        },
        {
          name: '字符 F1',
          type: 'bar',
          data: strataKeys.map((k) => strata[k]?.f1 ?? 0),
          itemStyle: { color: '#18a058' },
          barMaxWidth: 36,
        },
      ],
    })
    strataChart.resize()
  }

  // 3. OOD 拒答分布
  const oodBuckets = { refused: 0, answered: 0, judge_error: 0, gen_error: 0 }
  for (const q of oodRows.value) {
    const key = verdictOf(q).key
    if (key in oodBuckets) oodBuckets[key] += 1
  }
  const oodData = [
    { name: '已拒答', value: oodBuckets.refused, itemStyle: { color: '#18a058' } },
    { name: '误答', value: oodBuckets.answered, itemStyle: { color: '#d03050' } },
    { name: '判分失败', value: oodBuckets.judge_error, itemStyle: { color: '#909399' } },
    { name: '生成失败', value: oodBuckets.gen_error, itemStyle: { color: '#606266' } },
  ].filter((d) => d.value > 0)
  if (oodRef.value && oodData.length) {
    oodChart = oodChart || echarts.init(oodRef.value)
    oodChart.setOption({
      tooltip: { trigger: 'item', formatter: '{b}: {c} 题（{d}%）' },
      legend: { bottom: 0 },
      series: [
        {
          type: 'pie',
          radius: ['42%', '68%'],
          center: ['50%', '44%'],
          label: { formatter: '{b}\n{c}' },
          data: oodData,
        },
      ],
    })
    oodChart.resize()
  }
}

// ---------------------------------------------------------------- 逐题表
const columns = computed(() => [
  { type: 'expand', renderExpand },
  {
    title: '#',
    key: 'qid',
    width: 64,
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
    title: '分层',
    key: 'stratum',
    width: 90,
    align: 'center',
    render: (row) =>
      row.question?.is_ood
        ? h(NTag, { size: 'small', type: 'warning', bordered: false }, () => 'OOD')
        : row.question?.stratum || '-',
  },
  {
    title: '结论',
    key: 'verdict',
    width: 100,
    align: 'center',
    render: (row) => {
      const v = verdictOf(row)
      return h(NTag, { size: 'small', type: v.type, bordered: false }, () => v.label)
    },
  },
  {
    title: '正确率',
    key: 'correctness',
    width: 84,
    align: 'center',
    render: (row) => {
      const am = rowOf(row)?.answer_metrics || {}
      if (row.question?.is_ood) return '—'
      return am.correctness == null ? '—' : am.correctness.toFixed(2)
    },
  },
  {
    title: '忠实度',
    key: 'faithfulness',
    width: 84,
    align: 'center',
    render: (row) => {
      const am = rowOf(row)?.answer_metrics || {}
      return am.faithfulness == null ? '—' : am.faithfulness.toFixed(2)
    },
  },
  {
    title: 'EM / F1',
    key: 'em_f1',
    width: 110,
    align: 'center',
    render: (row) => {
      const am = rowOf(row)?.answer_metrics || {}
      if (am.em == null && am.f1 == null) return '—'
      return `${am.em == null ? '—' : am.em.toFixed(1)} / ${am.f1 == null ? '—' : am.f1.toFixed(2)}`
    },
  },
  {
    title: '生成延迟',
    key: 'answer_latency',
    width: 100,
    align: 'center',
    render: (row) => {
      const r = rowOf(row)
      return r ? `${r.answer_latency_ms || 0} ms` : '—'
    },
  },
])

function renderExpand(row) {
  const r = rowOf(row)
  const q = row.question || {}
  const am = r?.answer_metrics || {}
  const blocks = []
  if (q.answer) {
    blocks.push(h('div', { class: 'qa-ref' }, [h('b', '参考答案：'), q.answer]))
  }
  if (am.gen_error) {
    blocks.push(h('div', { class: 'qa-error' }, `生成失败：${am.gen_error}`))
  } else if (r?.answer) {
    blocks.push(h('div', { class: 'qa-answer' }, [h('b', '生成答案：'), r.answer]))
  } else {
    blocks.push(h('div', { class: 'qa-empty' }, '（无生成答案）'))
  }
  if (am.correctness_reason) {
    blocks.push(h('div', { class: 'qa-reason' }, `判分理由：${am.correctness_reason}`))
  }
  // 判分结构化明细：人工复核 judge 是否漏检/误判
  if (am.correctness_missing?.length) {
    blocks.push(
      h('div', { class: 'qa-reason qa-gap' }, `缺失要点：${am.correctness_missing.join('；')}`)
    )
  }
  if (am.correctness_conflicts?.length) {
    blocks.push(
      h('div', { class: 'qa-reason qa-conflict' }, `冲突：${am.correctness_conflicts.join('；')}`)
    )
  }
  if (am.correctness_covered?.length) {
    blocks.push(
      h('div', { class: 'qa-reason qa-ok' }, `已覆盖：${am.correctness_covered.join('；')}`)
    )
  }
  if (am.unsupported?.length) {
    blocks.push(h('div', { class: 'qa-reason' }, `未支持论断：${am.unsupported.join('；')}`))
  }
  if (am.judge_error) {
    blocks.push(h('div', { class: 'qa-error' }, `判分失败：${am.judge_error}`))
  }
  const gold = new Set(q.gold_file_keys || [])
  const retrieved = (r?.retrieved || []).map((d) =>
    h('li', { class: ['qa-doc-item', gold.has(d.filename) ? 'gold' : ''] }, [
      h('span', { class: 'qa-doc-rank' }, `#${d.rank}`),
      h('span', { class: 'qa-doc-name' }, d.filename),
      h('span', { class: 'qa-doc-score' }, d.score?.toFixed(4)),
      h('div', { class: 'qa-doc-snippet' }, d.snippet || ''),
    ])
  )
  blocks.push(
    h('div', { class: 'qa-retrieval' }, [
      h(
        'div',
        { class: 'qa-retrieval-head' },
        `检索：${q.is_ood ? '—（OOD）' : r?.hit ? `命中 @${r.hit_rank}` : '未命中'} · ${
          r?.latency_ms ?? 0
        } ms`
      ),
      retrieved.length
        ? h('ul', { class: 'qa-doc-list' }, retrieved)
        : h('span', { class: 'qa-empty' }, '（无检索结果）'),
    ])
  )
  return h('div', { class: 'qa-expand' }, blocks)
}

// ---------------------------------------------------------------- 其他
async function cancelRun() {
  await api.cancelExpRun({ run_id: runId })
  message.success('已请求取消')
  await loadAll()
}

function goBack() {
  const dsId = run.value?.dataset_id
  if (dsId) router.push(`/system/experiment/dataset/${dsId}`)
  else router.push('/system/experiment')
}

const statusText = computed(() => {
  const s = run.value?.status
  if (s === 'running') return `运行中 ${job.value?.percent ?? 0}%`
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
  pieChart?.resize()
  strataChart?.resize()
  oodChart?.resize()
}

onUnmounted(() => {
  stopPolling()
  window.removeEventListener('resize', resizeCharts)
  pieChart?.dispose()
  strataChart?.dispose()
  oodChart?.dispose()
})
</script>

<template>
  <AppPage :show-footer="false">
    <div class="qa-result">
      <header class="qa-header">
        <n-button quaternary size="small" @click="goBack">
          <template #icon><TheIcon icon="mdi:arrow-left" :size="16" /></template>
          返回数据集
        </n-button>
        <div class="qa-title">
          <h1>{{ run?.name || '问答测评结果' }}</h1>
          <p>
            <span :class="['qa-status', run?.status]">{{ statusText }}</span>
            <template v-if="run">
              · 策略 {{ cfgName }} · Top-K {{ run.configs?.[0]?.top_k }} · 题数限制
              {{ run.question_limit || '全部' }}{{ run.include_ood ? ' +OOD' : '' }}
              <template v-if="run.snapshot?.eval_prompt_version">
                · 评测口径 {{ run.snapshot.eval_prompt_version }}
              </template>
            </template>
          </p>
        </div>
        <div class="qa-actions">
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
          <div class="qa-running">
            <n-progress type="line" :percentage="job?.percent || 0" :height="12" processing />
            <p class="qa-running-stage">{{ job?.stage || '正在准备…' }}</p>
            <p>
              已完成 {{ job?.done || 0 }}/{{ job?.total || '?' }} 题<template
                v-if="job?.elapsed_seconds != null"
              >
                · 已用 {{ fmtElapsed(job.elapsed_seconds) }}</template
              >，页面每 3 秒自动刷新
            </p>
          </div>
        </template>

        <template v-else-if="gen">
          <n-alert v-if="answerCount === 0" type="warning" :bordered="false" class="qa-alert">
            问题集无参考答案：正确率 / EM / 字符 F1 不可用（忠实度与 OOD 拒答仍有效）
          </n-alert>
          <n-alert
            v-else-if="gen.judge_error_count || gen.gen_error_count"
            type="warning"
            :bordered="false"
            class="qa-alert"
          >
            生成失败 {{ gen.gen_error_count }} 题，判分失败
            {{ gen.judge_error_count }} 题（均不计入均值）
          </n-alert>

          <section class="qa-section">
            <h2>核心指标</h2>
            <div class="qa-cards">
              <div v-for="c in cards" :key="c.label" class="qa-card">
                <span class="qa-card-label">{{ c.label }}</span>
                <span class="qa-card-value">{{ c.value }}</span>
                <span class="qa-card-sub">{{ c.sub }}</span>
              </div>
            </div>
          </section>

          <section class="qa-section qa-charts">
            <div class="qa-chart-box">
              <h3>库内题正确率分布</h3>
              <div ref="pieRef" class="qa-chart" />
            </div>
            <div class="qa-chart-box">
              <h3>分层表现</h3>
              <div ref="strataRef" class="qa-chart" />
            </div>
            <div v-if="oodRows.length" class="qa-chart-box">
              <h3>OOD 拒答分布</h3>
              <div ref="oodRef" class="qa-chart" />
            </div>
          </section>

          <section class="qa-section">
            <div class="qa-toolbar">
              <h2>逐题明细</h2>
              <div class="qa-filters">
                <n-input
                  v-model:value="keyword"
                  size="small"
                  placeholder="搜索问题"
                  clearable
                  class="qa-search"
                >
                  <template #prefix><TheIcon icon="mdi:magnify" :size="15" /></template>
                </n-input>
                <n-select
                  v-model:value="verdictFilter"
                  size="small"
                  :options="verdictOptions"
                  class="qa-filter"
                />
                <n-select
                  v-model:value="stratumFilter"
                  size="small"
                  :options="stratumOptions"
                  class="qa-filter"
                />
                <n-checkbox v-model:checked="oodOnly">仅 OOD</n-checkbox>
              </div>
            </div>
            <n-data-table
              :columns="columns"
              :data="filteredQuestions"
              :bordered="false"
              size="small"
              :row-key="(r) => r.question?.id"
              :max-height="600"
              :scroll-x="980"
            />
          </section>
        </template>

        <n-empty v-else description="暂无结果数据" class="qa-empty-page" />
      </n-spin>
    </div>
  </AppPage>
</template>

<style scoped>
.qa-result {
  display: flex;
  flex-direction: column;
  gap: 14px;
  width: 100%;
  max-width: 1180px;
  padding-bottom: 24px;
  margin: 0 auto;
}
.qa-header {
  display: flex;
  gap: 12px;
  align-items: flex-start;
}
.qa-title {
  flex: 1;
  min-width: 0;
}
.qa-title h1 {
  margin: 0;
  font-size: 22px;
  font-weight: 700;
  line-height: 1.3;
  color: var(--n-text-color-2);
}
.qa-title p {
  margin: 4px 0 0;
  font-size: 12px;
  color: var(--n-text-color-3);
}
.qa-actions {
  display: flex;
  flex: none;
  gap: 8px;
}
.qa-status.running,
.qa-status.queued {
  color: #2080f0;
}
.qa-status.completed {
  color: #18a058;
}
.qa-status.failed {
  color: #d03050;
}
.qa-status.cancelled {
  color: #f0a020;
}
.qa-running {
  padding: 40px 60px;
  text-align: center;
}
.qa-running-stage {
  margin-top: 10px;
  font-size: 14px;
  font-weight: 600;
  color: var(--n-text-color-2);
}
.qa-running p {
  margin-top: 10px;
  font-size: 13px;
  color: var(--n-text-color-3);
}
.qa-alert {
  margin-bottom: 14px;
  font-size: 13px;
}
.qa-section {
  margin-bottom: 18px;
}
.qa-section h2 {
  margin: 0 0 10px;
  font-size: 15px;
  font-weight: 600;
  color: var(--n-text-color-2);
}
.qa-cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 10px;
}
.qa-card {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 12px 14px;
  background: rgba(128, 128, 128, 0.06);
  border: 1px solid rgba(128, 128, 128, 0.12);
  border-radius: 10px;
}
.qa-card-label {
  font-size: 12px;
  color: var(--n-text-color-3);
}
.qa-card-value {
  font-size: 22px;
  font-weight: 700;
  line-height: 1.3;
  color: var(--n-text-color-2);
}
.qa-card-sub {
  font-size: 11px;
  line-height: 1.5;
  color: var(--n-text-color-3);
}
.qa-charts {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
  gap: 12px;
}
.qa-chart-box {
  padding: 12px 14px;
  background: rgba(128, 128, 128, 0.04);
  border: 1px solid rgba(128, 128, 128, 0.12);
  border-radius: 10px;
}
.qa-chart-box h3 {
  margin: 0;
  font-size: 13px;
  font-weight: 600;
  color: var(--n-text-color-2);
}
.qa-chart {
  width: 100%;
  height: 260px;
}
.qa-toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
  margin-bottom: 10px;
}
.qa-toolbar h2 {
  margin: 0;
}
.qa-filters {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: center;
  margin-left: auto;
}
.qa-search {
  width: 220px;
}
.qa-filter {
  width: 140px;
}
.qa-empty-page {
  padding: 60px 0;
}
:deep(.qa-expand) {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 6px 10px;
}
:deep(.qa-ref) {
  padding: 8px 10px;
  font-size: 12px;
  background: rgba(128, 128, 128, 0.08);
  border-radius: 6px;
}
:deep(.qa-answer) {
  padding: 8px 10px;
  font-size: 12px;
  line-height: 1.6;
  background: rgba(32, 128, 240, 0.08);
  border-radius: 6px;
}
:deep(.qa-reason) {
  font-size: 12px;
  line-height: 1.6;
  color: var(--n-text-color-3);
}
:deep(.qa-gap) {
  color: #f0a020;
}
:deep(.qa-conflict) {
  color: #d03050;
}
:deep(.qa-ok) {
  color: #18a058;
}
:deep(.qa-error) {
  font-size: 12px;
  color: #d03050;
}
:deep(.qa-empty) {
  font-size: 12px;
  opacity: 0.55;
}
:deep(.qa-retrieval) {
  padding-top: 6px;
  border-top: 1px dashed rgba(128, 128, 128, 0.2);
}
:deep(.qa-retrieval-head) {
  font-size: 12px;
  color: var(--n-text-color-3);
}
:deep(.qa-doc-list) {
  padding: 0;
  margin: 6px 0 0;
  list-style: none;
}
:deep(.qa-doc-item) {
  display: grid;
  grid-template-columns: 34px 1fr 60px;
  gap: 6px;
  align-items: baseline;
  padding: 3px 6px;
  font-size: 12px;
  border-radius: 4px;
}
:deep(.qa-doc-item.gold) {
  font-weight: 600;
  background: rgba(24, 160, 88, 0.12);
}
:deep(.qa-doc-rank) {
  opacity: 0.6;
}
:deep(.qa-doc-name) {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
:deep(.qa-doc-score) {
  text-align: right;
  opacity: 0.7;
}
:deep(.qa-doc-snippet) {
  grid-column: 1 / -1;
  overflow: hidden;
  font-size: 11px;
  line-height: 1.5;
  opacity: 0.55;
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}
</style>
