/** 参与预览/试聊 diff 的表单字段 */

export const PREVIEW_CONFIG_FIELDS = [
  'name',
  'model_name',
  'base_url',
  'description',
  'system_prompt',
  'supports_vision',
  'opening_message',
  'temperature',
  'sub_model_name',
  'sub_base_url',
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

const WEB_SEARCH_DISCIPLINE =
  '联网搜索作答纪律：回答必须仅依据 web_search 工具的返回内容与多轮对话上下文；' +
  '凡引用搜索到的内容，必须以 [来源N] 标注（N 与工具返回中的编号一致），并保证引用的 URL 与工具返回逐字一致。' +
  '当工具返回明确提示搜索失败或无结果（TOOL_CALL_LIMIT_REACHED / WEB_SEARCH_NO_RESULTS / 联网搜索出错）' +
  '或本轮禁用（TOOL_DISABLED_THIS_TURN）时，' +
  '必须如实告知用户「联网搜索未找到相关内容」并可建议换个问法重试，' +
  '不得编造搜索结果、实时数据或来源链接；' +
  '注意区分搜索结论与你的一般常识推断，后者不得冒充联网检索结果。'

const KB_IMAGE_DISCIPLINE =
  '知识库检索结果中，每个图片 chunk 都会单独给出一行现成的 Markdown：`![说明](/api/v1/media/...?exp=...&sig=...)`。' +
  '展示图片时必须把那一行原样复制到回答中，括号内必须是以 /api/v1/media/ 开头并带 ?exp=&sig= 的地址。' +
  '禁止自行改写或拼接括号内内容：不得填入文档名、页码、`[i] ... (Page n)` 标题、stored_relpath、' +
  '不得改成 http(s) 绝对地址、image://、file://、kb_image://，也不得用 [1][2] 或序号代替。'

const KB_ANSWER_DISCIPLINE =
  '知识库作答纪律：回答必须仅依据知识库检索工具的返回内容与多轮对话上下文；' +
  '凡引用检索到的内容，必须以 [来源N] 标注（N 与工具返回中的编号一致）。' +
  '当工具返回明确提示知识库无相关资料（或检索未命中）或本轮禁用（TOOL_DISABLED_THIS_TURN）时，' +
  '必须如实告知用户「知识库中未找到相关资料」' +
  '并说明可补充资料后重试，不得编造知识库结论或凭想象作答；' +
  '若无确凿资料支撑，宁可说明「知识库中未找到相关资料」，也不要虚构。' +
  '注意区分知识库中的结论与你的一般常识推断，后者不得冒充知识库内容。'

/**
 * 前端拼装 system prompt 预览（与后端 _compose_system_prompt 对齐：人设 + 固定联网/知识库纪律）。
 */
export function composeSystemPromptPreview(form) {
  const parts = []
  const base = normStr(form?.system_prompt)
  if (base) {
    parts.push(base)
  } else {
    parts.push('You are a helpful assistant.')
  }
  parts.push(WEB_SEARCH_DISCIPLINE)
  parts.push(KB_IMAGE_DISCIPLINE)
  parts.push(KB_ANSWER_DISCIPLINE)
  return parts.join('\n\n')
}

export function editorPreviewSessionId(agentId) {
  if (agentId == null || agentId === '') return '__editor_preview_draft__'
  return `__editor_preview_${agentId}__`
}
