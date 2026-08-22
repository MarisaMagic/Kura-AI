/** 参与预览/试聊 diff 的表单字段 */

export const PREVIEW_CONFIG_FIELDS = [
  'name',
  'model_name',
  'base_url',
  'description',
  'system_prompt',
  'enable_web',
  'supports_vision',
  'opening_message',
  'temperature',
]

export function pickPreviewConfig(form) {
  if (!form) return {}
  const out = {}
  for (const k of PREVIEW_CONFIG_FIELDS) {
    out[k] = form[k]
  }
  return out
}

function normStr(v) {
  return String(v ?? '').trim()
}

function normNum(v, fallback = 0.1) {
  const n = typeof v === 'number' ? v : Number(v)
  return Number.isFinite(n) ? n : fallback
}

/** 规范化后再比较，避免无意义 diff */
export function previewConfigEqual(a, b) {
  if (!a || !b) return false
  for (const k of PREVIEW_CONFIG_FIELDS) {
    if (k === 'temperature') {
      if (normNum(a[k]) !== normNum(b[k])) return false
      continue
    }
    if (typeof a[k] === 'boolean' || typeof b[k] === 'boolean') {
      if (!!a[k] !== !!b[k]) return false
      continue
    }
    if (normStr(a[k]) !== normStr(b[k])) return false
  }
  return true
}

/**
 * 前端拼装 system prompt 预览（与后端 _compose_system_prompt 基础部分对齐，不含 KB 扩展）。
 */
export function composeSystemPromptPreview(form) {
  const parts = []
  const base = normStr(form?.system_prompt)
  if (base) {
    parts.push(base)
  } else {
    parts.push('You are a helpful assistant.')
  }
  if (form?.enable_web) {
    parts.push('用户已开启「联网」能力说明：当前未接入真实联网工具，请勿编造实时网页内容。')
  }
  return parts.join('\n\n')
}

export function editorPreviewSessionId(agentId) {
  if (agentId == null || agentId === '') return '__editor_preview_draft__'
  return `__editor_preview_${agentId}__`
}
