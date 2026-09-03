<template>
  <section class="agent-section">
    <header class="section-header mcp-section-header">
      <span>{{ $t('views.agents.mcp_section_title') }}</span>
      <div class="mcp-section-actions">
        <n-button size="small" quaternary type="primary" @click="openStore">
          <template #icon>
            <TheIcon icon="mdi:storefront-outline" :size="16" />
          </template>
          {{ $t('views.agents.mcp_add_from_store') }}
        </n-button>
        <n-button size="small" quaternary type="primary" @click="openEditor(null)">
          <template #icon>
            <TheIcon icon="mdi:plus" :size="16" />
          </template>
          {{ $t('views.agents.mcp_add_custom') }}
        </n-button>
      </div>
    </header>
    <div class="section-body">
      <div class="mcp-hint">{{ $t('views.agents.mcp_section_hint') }}</div>
      <n-spin :show="loading">
        <div v-if="!servers.length" class="mcp-empty">{{ $t('views.agents.mcp_empty') }}</div>
        <div v-for="srv in servers" :key="srv.id" class="mcp-server-card">
          <div class="mcp-server-main">
            <div class="mcp-server-title-row">
              <span class="mcp-server-name">{{ srv.name }}</span>
              <n-tag size="tiny" :bordered="false" class="mcp-transport-tag">{{
                srv.transport
              }}</n-tag>
              <n-tag v-if="!srv.enabled" size="tiny" type="warning" :bordered="false">
                {{ $t('views.agents.mcp_disabled_tag') }}
              </n-tag>
            </div>
            <div class="mcp-server-url" :title="srv.url">{{ srv.url }}</div>
            <div v-if="srv.header_keys?.length" class="mcp-server-headers">
              Headers: {{ srv.header_keys.join(', ') }}
            </div>
          </div>
          <div class="mcp-server-ops">
            <n-switch
              :value="srv.enabled"
              size="small"
              :loading="toggleLoadingId === srv.id"
              @update:value="(v) => onToggleEnabled(srv, v)"
            />
            <n-button
              size="tiny"
              quaternary
              :loading="testingId === srv.id"
              @click="onTestSaved(srv)"
            >
              {{ $t('views.agents.mcp_test_connection') }}
            </n-button>
            <n-button size="tiny" quaternary @click="openEditor(srv)">
              <template #icon>
                <TheIcon icon="material-symbols:edit-outline" :size="15" />
              </template>
            </n-button>
            <n-popconfirm @positive-click="onDelete(srv)">
              <template #trigger>
                <n-button size="tiny" quaternary type="error">
                  <template #icon>
                    <TheIcon icon="mdi:delete-outline" :size="15" />
                  </template>
                </n-button>
              </template>
              {{ $t('views.agents.mcp_delete_confirm') }}
            </n-popconfirm>
          </div>
        </div>
      </n-spin>
    </div>

    <!-- 新增/编辑弹窗 -->
    <n-modal
      v-model:show="editorVisible"
      preset="card"
      :title="editingId ? $t('views.agents.mcp_edit_title') : $t('views.agents.mcp_add_title')"
      class="mcp-editor-modal"
      :mask-closable="false"
    >
      <n-form label-placement="top" :show-require-mark="true">
        <n-form-item :label="$t('views.agents.mcp_form_name')" required>
          <n-input
            v-model:value="editorForm.name"
            :placeholder="$t('views.agents.mcp_form_name_ph')"
          />
        </n-form-item>
        <n-form-item :label="$t('views.agents.mcp_form_url')" required>
          <n-input v-model:value="editorForm.url" placeholder="https://example.com/mcp" />
        </n-form-item>
        <n-form-item :label="$t('views.agents.mcp_form_transport')">
          <n-radio-group v-model:value="editorForm.transport">
            <n-radio-button value="streamable_http">streamable_http</n-radio-button>
            <n-radio-button value="sse">sse</n-radio-button>
          </n-radio-group>
        </n-form-item>
        <n-form-item label="调用确认">
          <n-radio-group v-model:value="editorForm.confirm_policy">
            <n-radio-button value="auto">仅高风险</n-radio-button>
            <n-radio-button value="always">全部确认</n-radio-button>
            <n-radio-button value="never">不确认</n-radio-button>
          </n-radio-group>
        </n-form-item>
        <n-form-item :label="$t('views.agents.mcp_form_desc')">
          <n-input v-model:value="editorForm.description" type="textarea" :rows="2" />
        </n-form-item>
        <n-form-item :label="$t('views.agents.mcp_form_headers')">
          <div class="mcp-headers-editor">
            <div v-for="(h, idx) in editorForm.headersList" :key="idx" class="mcp-header-row">
              <n-input v-model:value="h.key" placeholder="Authorization" class="mcp-header-key" />
              <n-input
                v-model:value="h.value"
                type="password"
                show-password-on="click"
                placeholder="Bearer ..."
                class="mcp-header-value"
              />
              <n-button
                size="tiny"
                quaternary
                type="error"
                @click="editorForm.headersList.splice(idx, 1)"
              >
                <template #icon>
                  <TheIcon icon="mdi:close" :size="14" />
                </template>
              </n-button>
            </div>
            <n-button
              size="tiny"
              dashed
              block
              @click="editorForm.headersList.push({ key: '', value: '' })"
            >
              <template #icon>
                <TheIcon icon="mdi:plus" :size="14" />
              </template>
              {{ $t('views.agents.mcp_headers_add') }}
            </n-button>
            <div v-if="editingId && savedHeaderKeys.length" class="mcp-headers-saved-hint">
              {{ $t('views.agents.mcp_headers_saved_hint', { keys: savedHeaderKeys.join(', ') }) }}
            </div>
          </div>
        </n-form-item>
        <n-form-item :show-label="false">
          <n-checkbox v-model:checked="editorForm.enabled">
            {{ $t('views.agents.mcp_form_enabled') }}
          </n-checkbox>
        </n-form-item>
      </n-form>
      <div class="mcp-editor-footer">
        <n-button :loading="testingEditor" @click="onTestEditor">
          {{ $t('views.agents.mcp_test_connection') }}
        </n-button>
        <div class="mcp-editor-footer-right">
          <n-button @click="editorVisible = false">{{ $t('views.agents.mcp_cancel') }}</n-button>
          <n-button type="primary" :loading="saving" @click="onSave">
            {{ $t('views.agents.mcp_save') }}
          </n-button>
        </div>
      </div>
    </n-modal>

    <!-- 商店弹窗 -->
    <n-modal
      v-model:show="storeVisible"
      preset="card"
      :title="$t('views.agents.mcp_store_title')"
      class="mcp-store-modal"
    >
      <n-input
        v-model:value="storeSearch"
        clearable
        :placeholder="$t('views.agents.mcp_store_search_ph')"
        class="mcp-store-search"
      >
        <template #prefix>
          <TheIcon icon="mdi:magnify" :size="16" />
        </template>
      </n-input>
      <n-spin :show="presetsLoading">
        <div v-if="!filteredPresets.length && !presetsLoading" class="mcp-store-empty">
          {{ $t('views.agents.mcp_store_empty') }}
        </div>
        <div class="mcp-store-grid">
          <div v-for="p in filteredPresets" :key="p.key" class="mcp-store-card">
            <div class="mcp-store-card-head">
              <span class="mcp-store-icon">
                <TheIcon v-if="isIconifyIcon(p.icon)" :icon="p.icon" :size="22" />
                <img
                  v-else-if="p.icon && !brokenIcons.has(p.icon)"
                  :src="p.icon"
                  :alt="p.name"
                  class="mcp-store-icon-img"
                  loading="lazy"
                  @error="onIconError(p.icon)"
                />
                <TheIcon v-else icon="mdi:puzzle-outline" :size="22" />
              </span>
              <div class="mcp-store-name">{{ p.name }}</div>
            </div>
            <div class="mcp-store-desc">{{ p.description }}</div>
            <div class="mcp-store-url" :title="p.url">{{ p.url }}</div>
            <div class="mcp-store-footer">
              <span v-if="p.header_fields?.length" class="mcp-store-keyhint">
                {{ p.header_fields.map((f) => f.key).join(', ') }}
              </span>
              <n-button size="small" type="primary" secondary @click="onPickPreset(p)">
                {{ $t('views.agents.mcp_store_add') }}
              </n-button>
            </div>
          </div>
        </div>
      </n-spin>
    </n-modal>
  </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import {
  NButton,
  NCheckbox,
  NForm,
  NFormItem,
  NInput,
  NModal,
  NPopconfirm,
  NRadioButton,
  NRadioGroup,
  NSpin,
  NSwitch,
  NTag,
} from 'naive-ui'
import { useI18n } from 'vue-i18n'
import TheIcon from '@/components/icon/TheIcon.vue'
import api from '@/api'

const props = defineProps({
  agentId: { type: [Number, String], required: true },
})

const { t } = useI18n()

const servers = ref([])
const loading = ref(false)
const toggleLoadingId = ref(null)
const testingId = ref(null)

const editorVisible = ref(false)
const editingId = ref(null)
const saving = ref(false)
const testingEditor = ref(false)
const savedHeaderKeys = ref([])
const editorForm = ref(emptyEditorForm())

const storeVisible = ref(false)
const presets = ref([])
const presetsLoading = ref(false)
const storeSearch = ref('')
const brokenIcons = ref(new Set())

const filteredPresets = computed(() => {
  const kw = String(storeSearch.value || '')
    .trim()
    .toLowerCase()
  if (!kw) return presets.value
  return presets.value.filter((p) =>
    [p.name, p.description, p.url, p.key].some((s) =>
      String(s || '')
        .toLowerCase()
        .includes(kw)
    )
  )
})

// icon 为 iconify 名称（如 mdi:github）时走 TheIcon；http(s) URL 按图片渲染
function isIconifyIcon(icon) {
  return typeof icon === 'string' && !/^https?:\/\//i.test(icon) && icon.includes(':')
}

function onIconError(iconUrl) {
  brokenIcons.value = new Set([...brokenIcons.value, iconUrl])
}

function emptyEditorForm() {
  return {
    name: '',
    url: '',
    transport: 'streamable_http',
    description: '',
    enabled: true,
    confirm_policy: 'auto',
    headersList: [],
  }
}

function headersListToDict(list) {
  const out = {}
  for (const h of list || []) {
    const k = (h.key || '').trim()
    if (k) out[k] = h.value ?? ''
  }
  return out
}

async function loadServers() {
  if (!props.agentId) return
  loading.value = true
  try {
    const res = await api.getAgentMcpServers({ agent_id: props.agentId })
    servers.value = res?.data?.servers || []
  } catch (e) {
    window.$message?.error(e?.message || String(e))
  } finally {
    loading.value = false
  }
}

function openEditor(srv) {
  if (srv) {
    editingId.value = srv.id
    savedHeaderKeys.value = srv.header_keys || []
    editorForm.value = {
      name: srv.name,
      url: srv.url,
      transport: srv.transport || 'streamable_http',
      description: srv.description || '',
      enabled: !!srv.enabled,
      confirm_policy: srv.confirm_policy || 'auto',
      headersList: [],
    }
  } else {
    editingId.value = null
    savedHeaderKeys.value = []
    editorForm.value = emptyEditorForm()
  }
  editorVisible.value = true
}

async function openStore() {
  storeVisible.value = true
  if (presets.value.length) return
  presetsLoading.value = true
  try {
    const res = await api.getAgentMcpPresets()
    presets.value = res?.data?.presets || []
  } catch (e) {
    window.$message?.error(e?.message || String(e))
  } finally {
    presetsLoading.value = false
  }
}

function onPickPreset(p) {
  storeVisible.value = false
  editingId.value = null
  savedHeaderKeys.value = []
  editorForm.value = {
    name: p.name,
    url: p.url,
    transport: p.transport || 'streamable_http',
    description: p.description || '',
    enabled: true,
    confirm_policy: p.confirm_policy || 'auto',
    headersList: (p.header_fields || []).map((f) => ({ key: f.key, value: '' })),
  }
  editorVisible.value = true
}

async function onSave() {
  const f = editorForm.value
  if (!f.name.trim() || !f.url.trim()) {
    window.$message?.warning(t('views.agents.mcp_form_required'))
    return
  }
  saving.value = true
  try {
    if (editingId.value) {
      const payload = {
        name: f.name.trim(),
        url: f.url.trim(),
        transport: f.transport,
        description: f.description,
        enabled: f.enabled,
        confirm_policy: f.confirm_policy,
      }
      // 编辑时仅在用户填写了请求头时覆盖；留空则保留已保存值
      if (f.headersList.length) payload.headers = headersListToDict(f.headersList)
      await api.updateAgentMcpServer(props.agentId, editingId.value, payload)
    } else {
      await api.createAgentMcpServer(props.agentId, {
        name: f.name.trim(),
        url: f.url.trim(),
        transport: f.transport,
        description: f.description,
        enabled: f.enabled,
        confirm_policy: f.confirm_policy,
        headers: headersListToDict(f.headersList),
      })
    }
    window.$message?.success(t('views.agents.mcp_save_success'))
    editorVisible.value = false
    await loadServers()
  } catch (e) {
    window.$message?.error(e?.message || String(e))
  } finally {
    saving.value = false
  }
}

async function onToggleEnabled(srv, val) {
  toggleLoadingId.value = srv.id
  try {
    await api.updateAgentMcpServer(props.agentId, srv.id, { enabled: val })
    srv.enabled = val
  } catch (e) {
    window.$message?.error(e?.message || String(e))
  } finally {
    toggleLoadingId.value = null
  }
}

async function onDelete(srv) {
  try {
    await api.deleteAgentMcpServer({ agent_id: props.agentId, server_id: srv.id })
    window.$message?.success(t('views.agents.mcp_delete_success'))
    await loadServers()
  } catch (e) {
    window.$message?.error(e?.message || String(e))
  }
}

function reportTestResult(data) {
  if (data?.ok) {
    window.$message?.success(t('views.agents.mcp_test_success', { n: data.tool_count ?? 0 }))
  } else {
    window.$message?.error(
      `${t('views.agents.mcp_test_failed')}：${data?.error || 'unknown error'}`
    )
  }
}

async function onTestSaved(srv) {
  testingId.value = srv.id
  try {
    const res = await api.testAgentMcpServer(props.agentId, {
      transport: srv.transport,
      url: srv.url,
      server_id: srv.id,
    })
    reportTestResult(res?.data)
  } catch (e) {
    window.$message?.error(e?.message || String(e))
  } finally {
    testingId.value = null
  }
}

async function onTestEditor() {
  const f = editorForm.value
  if (!f.url.trim()) {
    window.$message?.warning(t('views.agents.mcp_form_required'))
    return
  }
  testingEditor.value = true
  try {
    const payload = { transport: f.transport, url: f.url.trim() }
    const dict = headersListToDict(f.headersList)
    if (Object.keys(dict).length) payload.headers = dict
    if (editingId.value) payload.server_id = editingId.value
    const res = await api.testAgentMcpServer(props.agentId, payload)
    reportTestResult(res?.data)
  } catch (e) {
    window.$message?.error(e?.message || String(e))
  } finally {
    testingEditor.value = false
  }
}

onMounted(loadServers)
</script>

<style scoped>
.agent-section {
  margin-bottom: 20px;
}

.section-header {
  margin-bottom: 14px;
  padding: 8px 12px 8px 12px;
  font-size: 17px;
  font-weight: 600;
  line-height: 1.45;
  letter-spacing: 0.04em;
  color: var(--n-text-color);
  border-left: 4px solid var(--n-primary-color);
  background: #ececef;
  border-radius: 8px;
}

html.dark .section-header {
  background: rgba(255, 255, 255, 0.08);
}

.mcp-section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.mcp-section-actions {
  display: inline-flex;
  gap: 4px;
}

.mcp-hint {
  font-size: 12px;
  color: var(--n-text-color-3);
  margin-bottom: 10px;
  line-height: 1.5;
}

.mcp-empty {
  font-size: 13px;
  color: var(--n-text-color-3);
  text-align: center;
  padding: 18px 0;
  border: 1px dashed var(--n-border-color);
  border-radius: 8px;
}

.mcp-server-card {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 10px 12px;
  border: 1px solid var(--n-border-color);
  border-radius: 8px;
  margin-bottom: 8px;
}

.mcp-server-main {
  min-width: 0;
  flex: 1;
}

.mcp-server-title-row {
  display: flex;
  align-items: center;
  gap: 6px;
}

.mcp-server-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--n-text-color);
}

.mcp-transport-tag {
  font-family: monospace;
}

.mcp-server-url {
  font-size: 12px;
  color: var(--n-text-color-3);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  margin-top: 2px;
}

.mcp-server-headers {
  font-size: 11px;
  color: var(--n-text-color-3);
  margin-top: 2px;
}

.mcp-server-ops {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
}

.mcp-headers-editor {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.mcp-header-row {
  display: flex;
  gap: 6px;
  align-items: center;
}

.mcp-header-key {
  width: 180px;
  flex-shrink: 0;
}

.mcp-header-value {
  flex: 1;
}

.mcp-headers-saved-hint {
  font-size: 12px;
  color: var(--n-text-color-3);
}

.mcp-editor-footer {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-top: 8px;
}

.mcp-editor-footer-right {
  display: inline-flex;
  gap: 8px;
}

.mcp-store-search {
  margin-bottom: 12px;
}

.mcp-store-empty {
  font-size: 13px;
  color: var(--n-text-color-3);
  text-align: center;
  padding: 24px 0;
}

.mcp-store-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 12px;
}

.mcp-store-card {
  border: 1px solid var(--n-border-color);
  border-radius: 8px;
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.mcp-store-card-head {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.mcp-store-icon {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  border-radius: 8px;
  border: 1px solid var(--n-border-color);
  background: var(--n-color-embedded);
  color: var(--n-text-color-2);
  overflow: hidden;
}

/* favicon 多为深色 logo：衬白色底，保证暗色主题下可辨认 */
.mcp-store-icon-img {
  width: 22px;
  height: 22px;
  object-fit: contain;
  display: block;
  background: #ffffff;
  border-radius: 5px;
  padding: 2px;
  box-sizing: border-box;
}

.mcp-store-name {
  font-size: 14px;
  font-weight: 600;
}

.mcp-store-desc {
  font-size: 12px;
  color: var(--n-text-color-3);
  line-height: 1.5;
  flex: 1;
}

.mcp-store-url {
  font-size: 12.5px;
  color: var(--n-text-color-3);
  font-family: monospace;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.mcp-store-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.mcp-store-keyhint {
  font-size: 12px;
  color: var(--n-warning-color);
  font-family: monospace;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>

<style>
.mcp-editor-modal {
  max-width: 560px;
}

.mcp-store-modal {
  max-width: 720px;
}
</style>
