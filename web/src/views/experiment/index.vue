<script setup>
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { NButton, NPopconfirm, useMessage } from 'naive-ui'
import AppPage from '@/components/page/AppPage.vue'
import TheIcon from '@/components/icon/TheIcon.vue'
import api from '@/api'

defineOptions({ name: '实验平台' })

const router = useRouter()
const message = useMessage()

const loading = ref(false)
const datasets = ref([])
const showCreate = ref(false)
const creating = ref(false)
const form = ref({ name: '', description: '' })

async function loadDatasets() {
  loading.value = true
  try {
    const res = await api.getExpDatasets()
    datasets.value = res.data || []
  } finally {
    loading.value = false
  }
}

onMounted(loadDatasets)

async function handleCreate() {
  if (!form.value.name.trim()) {
    message.warning('请输入数据集名称')
    return
  }
  creating.value = true
  try {
    await api.createExpDataset({
      name: form.value.name.trim(),
      description: form.value.description,
    })
    message.success('创建成功')
    showCreate.value = false
    form.value = { name: '', description: '' }
    await loadDatasets()
  } finally {
    creating.value = false
  }
}

async function handleDelete(ds) {
  await api.deleteExpDataset({ dataset_id: ds.id })
  message.success('已删除')
  await loadDatasets()
}

function goDataset(ds) {
  router.push(`/system/experiment/dataset/${ds.id}`)
}

function fmtTime(iso) {
  if (!iso) return '-'
  return String(iso).replace('T', ' ').slice(0, 16)
}
</script>

<template>
  <AppPage :show-footer="false">
    <div class="exp-layout">
      <header class="exp-header">
        <div class="exp-header-main">
          <h1 class="exp-title">
            <TheIcon icon="mdi:flask-outline" :size="24" class="exp-title-icon" />
            实验平台
          </h1>
          <p class="exp-subtitle">
            实验平台：RAG 检索策略消融对比 + 端到端问答测评（仅超级管理员）
          </p>
        </div>
        <div class="exp-header-actions">
          <n-button size="small" quaternary @click="loadDatasets">
            <template #icon><TheIcon icon="mdi:refresh" :size="16" /></template>
            刷新
          </n-button>
          <n-button size="small" type="primary" @click="showCreate = true">
            <template #icon><TheIcon icon="mdi:plus" :size="16" /></template>
            新建数据集
          </n-button>
        </div>
      </header>

      <n-alert type="info" :bordered="false" class="exp-tip">
        提示：可使用仓库脚本一键导入 CRUD_RAG 评测数据 ——
        <n-text code>python scripts/import_rag_test.py --task all</n-text>
      </n-alert>

      <n-spin :show="loading">
        <n-empty
          v-if="!loading && !datasets.length"
          description="暂无数据集，点击右上角新建"
          class="exp-empty"
        />
        <div v-else class="exp-grid">
          <div v-for="ds in datasets" :key="ds.id" class="exp-card" @click="goDataset(ds)">
            <div class="exp-card-head">
              <div class="exp-card-name">
                <TheIcon icon="mdi:database-outline" :size="18" class="exp-card-icon" />
                <span class="exp-card-name-text">{{ ds.name }}</span>
              </div>
              <n-popconfirm @positive-click="handleDelete(ds)">
                <template #trigger>
                  <n-button quaternary size="tiny" type="error" @click.stop>
                    <template #icon><TheIcon icon="mdi:trash-can-outline" :size="15" /></template>
                  </n-button>
                </template>
                删除将级联清理该数据集的向量、文档、问题与全部实验记录，确认删除「{{ ds.name }}」？
              </n-popconfirm>
            </div>
            <p class="exp-card-desc">{{ ds.description || '暂无描述' }}</p>
            <div class="exp-card-stats">
              <div class="exp-stat">
                <span class="exp-stat-num">{{ ds.doc_count }}</span>
                <span class="exp-stat-label">文档</span>
              </div>
              <div class="exp-stat">
                <span class="exp-stat-num">{{ ds.question_count }}</span>
                <span class="exp-stat-label">问题</span>
              </div>
              <div class="exp-stat-time">创建于 {{ fmtTime(ds.created_at) }}</div>
            </div>
          </div>
        </div>
      </n-spin>
    </div>

    <n-modal
      v-model:show="showCreate"
      preset="dialog"
      title="新建实验数据集"
      positive-text="创建"
      negative-text="取消"
      :style="{ width: 'min(520px, 92vw)' }"
      :loading="creating"
      @positive-click="handleCreate"
    >
      <n-form label-placement="top" class="exp-form" @submit.prevent>
        <n-form-item label="名称" required>
          <n-input v-model:value="form.name" placeholder="如 RAG_test-1doc" maxlength="128" />
        </n-form-item>
        <n-form-item label="描述">
          <n-input
            v-model:value="form.description"
            type="textarea"
            :rows="3"
            placeholder="数据集来源、用途说明"
          />
        </n-form-item>
      </n-form>
    </n-modal>
  </AppPage>
</template>

<style scoped>
.exp-layout {
  width: 100%;
  max-width: 1080px;
  padding-bottom: 24px;
  margin: 0 auto;
}
.exp-header {
  display: flex;
  gap: 16px;
  align-items: flex-start;
  justify-content: space-between;
  margin-bottom: 16px;
}
.exp-title {
  display: flex;
  gap: 8px;
  align-items: center;
  margin: 0;
  font-size: 22px;
  font-weight: 700;
  color: var(--n-text-color-2);
}
.exp-title-icon {
  color: var(--n-primary-color);
}
.exp-subtitle {
  margin: 6px 0 0;
  font-size: 13px;
  color: var(--n-text-color-3);
}
.exp-header-actions {
  display: flex;
  flex: none;
  gap: 8px;
}
.exp-tip {
  margin-bottom: 16px;
  font-size: 13px;
}
.exp-empty {
  padding: 60px 0;
}
.exp-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 14px;
}
.exp-card {
  padding: 16px;
  cursor: pointer;
  background: var(--n-color-embedded);
  border: 1px solid var(--n-border-color);
  border-radius: 10px;
  transition: box-shadow 0.2s, border-color 0.2s;
}
.exp-card:hover {
  border-color: var(--n-primary-color);
  box-shadow: 0 4px 14px rgba(0, 0, 0, 0.08);
}
.exp-card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.exp-card-name {
  display: flex;
  gap: 6px;
  align-items: center;
  min-width: 0;
  font-size: 15px;
  font-weight: 600;
  color: var(--n-text-color-2);
}
.exp-card-icon {
  flex: none;
  color: var(--n-primary-color);
}
.exp-card-name-text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.exp-card-desc {
  display: -webkit-box;
  height: 40px;
  margin: 10px 0;
  overflow: hidden;
  font-size: 12px;
  line-height: 20px;
  color: var(--n-text-color-3);
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}
.exp-card-stats {
  display: flex;
  gap: 20px;
  align-items: center;
}
.exp-stat {
  display: flex;
  flex-direction: column;
}
.exp-stat-num {
  font-size: 18px;
  font-weight: 700;
  color: var(--n-text-color-2);
}
.exp-stat-label {
  font-size: 11px;
  color: var(--n-text-color-3);
}
.exp-stat-time {
  margin-left: auto;
  font-size: 11px;
  color: var(--n-text-color-3);
}
.exp-form {
  margin-top: 8px;
}
</style>
