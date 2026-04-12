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
            <n-button type="primary" :loading="uploading">
              {{ $t('views.agents.kb_upload_title') }}
            </n-button>
          </n-upload>
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
import { h, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { NButton, NDataTable, NPopconfirm, NSpin, NUpload, useMessage } from 'naive-ui'
import AppPage from '@/components/page/AppPage.vue'
import api from '@/api'

const { t } = useI18n()
const route = useRoute()
const message = useMessage()

const pageLoading = ref(true)
const tableLoading = ref(false)
const uploading = ref(false)
const agentId = ref(Number(route.params.agentId) || 0)
const agentName = ref('')
const list = ref([])

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
  uploading.value = true
  const fd = new FormData()
  fd.append('file', f)
  try {
    await api.uploadKbDocument(agentId.value, fd)
    message.success(t('views.agents.kb_upload_ok'))
    await fetchList()
  } catch (e) {
    message.error(e?.response?.data?.msg || e?.message || 'upload failed')
  } finally {
    uploading.value = false
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
  } catch (e) {
    message.error(t('views.agents.chat_error_load_agent'))
  } finally {
    pageLoading.value = false
  }
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
</style>
