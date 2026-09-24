<script setup>
import { computed, ref, watch } from 'vue'
import { useMessage } from 'naive-ui'
import TheIcon from '@/components/icon/TheIcon.vue'
import api from '@/api'

defineOptions({ name: 'RunConfigModal' })

const props = defineProps({
  show: { type: Boolean, default: false },
  datasetId: { type: Number, required: true },
  questionCount: { type: Number, default: 0 },
})
const emit = defineEmits(['update:show', 'created'])
const message = useMessage()

const PRESETS = [
  {
    key: 'dense',
    label: '单稠密向量',
    desc: 'dense-only，语义相似',
    cfg: { retrieval_mode: 'dense', fusion: 'rrf', rerank: false },
  },
  {
    key: 'sparse',
    label: '单稀疏向量',
    desc: 'BM25 关键词匹配',
    cfg: { retrieval_mode: 'sparse', fusion: 'rrf', rerank: false },
  },
  {
    key: 'hybrid_rrf',
    label: '混合 + RRF',
    desc: 'dense + BM25，RRF 排名融合',
    cfg: { retrieval_mode: 'hybrid', fusion: 'rrf', rerank: false },
  },
  {
    key: 'hybrid_rrf_rerank',
    label: '混合 + RRF + Rerank',
    desc: '融合后经重排模型精排',
    cfg: { retrieval_mode: 'hybrid', fusion: 'rrf', rerank: true },
  },
  {
    key: 'hybrid_weighted',
    label: '混合 + 加权融合',
    desc: 'dense + BM25，分数加权',
    cfg: { retrieval_mode: 'hybrid', fusion: 'weighted', rerank: false },
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
  {
    key: 'sparse_rerank',
    label: '稀疏 + Rerank',
    desc: 'BM25 精排',
    cfg: { retrieval_mode: 'sparse', fusion: 'rrf', rerank: true },
  },
]

const checked = ref(['hybrid_rrf', 'hybrid_rrf_rerank'])
const topK = ref(5)
const rrfK = ref(60)
const candidateMultiplier = ref(3)
const denseWeight = ref(0.7)
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
  options.push({
    label: total ? `全部题目（${total}）` : '全部题目',
    value: 0,
  })
  return options
})

const estCalls = computed(() => {
  const n = questionLimit.value || props.questionCount
  const withRerank = PRESETS.filter((p) => checked.value.includes(p.key) && p.cfg.rerank).length
  return { embed: n, rerank: n * withRerank, total: checked.value.length * n }
})

watch(
  () => props.show,
  (v) => {
    if (v) {
      runName.value = ''
      submitting.value = false
      // 数据集题数可能小于当前选项（如全量集切到小样本），避免静默截断
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
  if (!checked.value.length) {
    message.warning('请至少勾选一个实验配置')
    return
  }
  const configs = PRESETS.filter((p) => checked.value.includes(p.key)).map((p) => ({
    ...p.cfg,
    name: p.label,
    top_k: topK.value,
    rrf_k: rrfK.value,
    candidate_multiplier: candidateMultiplier.value,
    weighted_params: [denseWeight.value, Number((1 - denseWeight.value).toFixed(2))],
  }))
  submitting.value = true
  try {
    const res = await api.createExpRun({
      dataset_id: props.datasetId,
      name: runName.value.trim(),
      configs,
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
    title="新建消融实验"
    :style="{ width: 'min(680px, 92vw)' }"
    :bordered="false"
    @update:show="emit('update:show', $event)"
  >
    <n-form label-placement="left" label-width="86" @submit.prevent>
      <n-form-item label="实验名称">
        <n-input v-model:value="runName" placeholder="留空自动生成" maxlength="128" />
      </n-form-item>

      <n-form-item label="对比配置">
        <n-checkbox-group v-model:value="checked" class="exp-preset-group">
          <div class="exp-preset-grid">
            <div v-for="p in PRESETS" :key="p.key" class="exp-preset-item">
              <n-checkbox :value="p.key" :label="p.label" />
              <span class="exp-preset-desc">{{ p.desc }}</span>
            </div>
          </div>
        </n-checkbox-group>
      </n-form-item>

      <n-form-item label="检索参数">
        <div class="exp-params">
          <div class="exp-param">
            <span class="exp-param-label">Top-K 文档数</span>
            <n-input-number v-model:value="topK" size="small" :min="1" :max="50" />
          </div>
          <div class="exp-param">
            <span class="exp-param-label">RRF k</span>
            <n-input-number v-model:value="rrfK" size="small" :min="1" :max="500" />
          </div>
          <div class="exp-param">
            <span class="exp-param-label">候选倍数</span>
            <n-input-number v-model:value="candidateMultiplier" size="small" :min="1" :max="10" />
          </div>
          <div class="exp-param">
            <span class="exp-param-label">稠密腿权重</span>
            <n-input-number
              v-model:value="denseWeight"
              size="small"
              :min="0"
              :max="1"
              :step="0.1"
            />
          </div>
        </div>
      </n-form-item>

      <n-form-item label="评测题数">
        <div class="exp-params">
          <n-select
            v-model:value="questionLimit"
            size="small"
            :options="limitOptions"
            class="exp-limit-select"
          />
          <n-checkbox v-model:checked="includeOod">包含 OOD 库外拒答题</n-checkbox>
        </div>
      </n-form-item>

      <n-alert type="warning" :bordered="false" class="exp-cost-tip">
        预估外部调用：embedding {{ estCalls.embed }} 次，rerank {{ estCalls.rerank }} 次，Milvus
        检索 {{ estCalls.total }} 次（均为 DashScope 计费调用，请按需控制题数）
      </n-alert>
    </n-form>

    <template #footer>
      <div class="exp-modal-footer">
        <n-button quaternary @click="emit('update:show', false)">
          <template #icon><TheIcon icon="mdi:close" :size="16" /></template>
          取消
        </n-button>
        <n-button type="primary" :loading="submitting" @click="submit">
          <template #icon><TheIcon icon="mdi:play-circle-outline" :size="16" /></template>
          启动实验
        </n-button>
      </div>
    </template>
  </n-modal>
</template>

<style scoped>
.exp-preset-group {
  width: 100%;
}
.exp-preset-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 2px 16px;
  width: 100%;
}
.exp-preset-item {
  display: flex;
  gap: 8px;
  align-items: baseline;
  padding: 4px 0;
}
.exp-preset-desc {
  overflow: hidden;
  font-size: 12px;
  color: var(--n-text-color-3);
  text-overflow: ellipsis;
  white-space: nowrap;
}
.exp-params {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
  align-items: center;
}
.exp-param {
  display: flex;
  gap: 6px;
  align-items: center;
  font-size: 13px;
  color: var(--n-text-color-2);
}
.exp-param-label {
  white-space: nowrap;
}
.exp-param :deep(.n-input-number) {
  width: 120px;
}
.exp-limit-select {
  width: 150px;
}
.exp-cost-tip {
  font-size: 12px;
}
.exp-modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
</style>
