<script setup>
import { computed, ref, watch } from 'vue'
import TheIcon from '@/components/icon/TheIcon.vue'
import api from '@/api'

defineOptions({ name: 'QaRunModal' })

const props = defineProps({
  show: { type: Boolean, default: false },
  datasetId: { type: Number, required: true },
  questionCount: { type: Number, default: 0 },
  answerCount: { type: Number, default: 0 },
})
const emit = defineEmits(['update:show', 'created'])

const STRATEGIES = [
  {
    key: 'dense',
    label: '单稠密向量',
    desc: 'dense-only 语义检索',
    cfg: { retrieval_mode: 'dense', fusion: 'rrf', rerank: false },
  },
  {
    key: 'sparse',
    label: '单稀疏向量',
    desc: 'BM25 关键词检索',
    cfg: { retrieval_mode: 'sparse', fusion: 'rrf', rerank: false },
  },
  {
    key: 'hybrid_rrf',
    label: '混合 + RRF',
    desc: 'dense + BM25 排名融合',
    cfg: { retrieval_mode: 'hybrid', fusion: 'rrf', rerank: false },
  },
  {
    key: 'hybrid_rrf_rerank',
    label: '混合 + RRF + Rerank',
    desc: '融合后经重排模型精排（推荐）',
    cfg: { retrieval_mode: 'hybrid', fusion: 'rrf', rerank: true },
  },
  {
    key: 'hybrid_weighted_rerank',
    label: '混合 + 加权 + Rerank',
    desc: '加权融合后精排',
    cfg: { retrieval_mode: 'hybrid', fusion: 'weighted', rerank: true },
  },
  {
    key: 'dense_rerank',
    label: '稠密 + Rerank',
    desc: 'dense-only 精排',
    cfg: { retrieval_mode: 'dense', fusion: 'rrf', rerank: true },
  },
]

const strategy = ref('hybrid_rrf_rerank')
const topK = ref(5)
const questionLimit = ref(10)
const includeOod = ref(true)
const runName = ref('')
const submitting = ref(false)

const LIMIT_PRESETS = [10, 50, 100, 200, 300, 500]

const limitOptions = computed(() => {
  const total = props.questionCount || 0
  const options = LIMIT_PRESETS.filter((n) => total >= n).map((n) => ({
    label: `前 ${n} 题`,
    value: n,
  }))
  options.push({ label: total ? `全部题目（${total}）` : '全部题目', value: 0 })
  return options
})

const selected = computed(() => STRATEGIES.find((s) => s.key === strategy.value) || STRATEGIES[0])

const estCalls = computed(() => {
  const n = questionLimit.value || props.questionCount
  return {
    embedding: n,
    rerank: selected.value.cfg.rerank ? n : 0,
    milvus: n,
    generate: n,
    judge: n * 2,
  }
})

watch(
  () => props.show,
  (v) => {
    if (v) {
      runName.value = ''
      submitting.value = false
      if (
        questionLimit.value > 0 &&
        props.questionCount &&
        questionLimit.value > props.questionCount
      ) {
        questionLimit.value = props.questionCount
      }
    }
  }
)

async function submit() {
  submitting.value = true
  try {
    const res = await api.createExpRun({
      dataset_id: props.datasetId,
      name: runName.value.trim(),
      kind: 'qa',
      configs: [
        {
          ...selected.value.cfg,
          name: selected.value.label,
          top_k: topK.value,
          rrf_k: 60,
          candidate_multiplier: 3,
          weighted_params: [0.7, 0.3],
        },
      ],
      question_limit: questionLimit.value || 0,
      include_ood: includeOod.value,
    })
    emit('created', res.data)
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <n-modal
    :show="show"
    preset="card"
    title="新建问答测评"
    :style="{ width: 'min(680px, 92vw)' }"
    :bordered="false"
    @update:show="emit('update:show', $event)"
  >
    <n-form label-placement="left" label-width="86" @submit.prevent>
      <n-form-item label="任务名称">
        <n-input
          v-model:value="runName"
          placeholder="留空自动生成（数据集名 · 问答测评 #N）"
          maxlength="128"
        />
      </n-form-item>

      <n-form-item label="检索策略">
        <n-select
          v-model:value="strategy"
          size="small"
          :options="STRATEGIES.map((s) => ({ label: s.label, value: s.key }))"
          class="exp-qa-strategy"
        />
        <span class="exp-qa-desc">{{ selected.desc }}</span>
      </n-form-item>

      <n-form-item label="检索参数">
        <div class="exp-qa-params">
          <div class="exp-qa-param">
            <span class="exp-qa-param-label">Top-K 文档数</span>
            <n-input-number v-model:value="topK" size="small" :min="1" :max="50" />
          </div>
        </div>
      </n-form-item>

      <n-form-item label="评测题数">
        <div class="exp-qa-params">
          <n-select
            v-model:value="questionLimit"
            size="small"
            :options="limitOptions"
            class="exp-qa-limit"
          />
          <n-checkbox v-model:checked="includeOod">包含 OOD 库外拒答题</n-checkbox>
        </div>
      </n-form-item>

      <n-alert v-if="answerCount === 0" type="warning" :bordered="false" class="exp-qa-tip">
        当前问题集没有参考答案：答案正确率 / EM / 字符 F1 将不可用，仍可评测忠实度与 OOD 拒答。
      </n-alert>
      <n-alert v-else type="info" :bordered="false" class="exp-qa-tip">
        参考答案覆盖 {{ answerCount }} 题；生成与判分使用服务端 EXP_EVAL_* 模型（默认 qwen-plus）。
      </n-alert>

      <n-alert type="warning" :bordered="false" class="exp-qa-tip">
        预估外部调用：embedding {{ estCalls.embedding }} 次，rerank {{ estCalls.rerank }} 次，Milvus
        检索 {{ estCalls.milvus }} 次，LLM 生成 {{ estCalls.generate }} 次 + 判分约
        {{ estCalls.judge }} 次（均为计费调用，请按需控制题数）
      </n-alert>
    </n-form>

    <template #footer>
      <div class="exp-qa-footer">
        <n-button quaternary @click="emit('update:show', false)">
          <template #icon><TheIcon icon="mdi:close" :size="16" /></template>
          取消
        </n-button>
        <n-button type="primary" :loading="submitting" @click="submit">
          <template #icon><TheIcon icon="mdi:play-circle-outline" :size="16" /></template>
          启动测评
        </n-button>
      </div>
    </template>
  </n-modal>
</template>

<style scoped>
.exp-qa-strategy {
  width: 220px;
}
.exp-qa-desc {
  margin-left: 10px;
  font-size: 12px;
  color: var(--n-text-color-3);
}
.exp-qa-params {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
  align-items: center;
}
.exp-qa-param {
  display: flex;
  gap: 6px;
  align-items: center;
  font-size: 13px;
  color: var(--n-text-color-2);
}
.exp-qa-param :deep(.n-input-number) {
  width: 120px;
}
.exp-qa-limit {
  width: 150px;
}
.exp-qa-tip {
  margin-top: 10px;
  font-size: 12px;
}
.exp-qa-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
</style>
