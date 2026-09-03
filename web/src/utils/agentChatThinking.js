/**
 * 思考区时间线：工具步骤（step）与过渡文案（text）按发生顺序交错。
 * 对话页与编辑器预览的 SSE reducer、历史回放共用。
 *
 * item 结构：
 *   { type: 'step', icon, label, detail }
 *   { type: 'text', text }
 */

/** 压掉过渡文本中 3+ 连续换行，避免 Markdown 空段叠出整行空白 */
export function normalizeThinkingText(text) {
  return String(text || '').replace(/\n{3,}/g, '\n\n')
}

/**
 * 将一条 SSE thinking_item 事件并入 items（返回新数组）。
 * @param {Array} items 当前 thinkingItems
 * @param {object} item 事件携带的 item
 * @param {boolean} append true 时 text 类型并入尾部已有 text 项（流式续写）
 */
export function applyThinkingItem(items, item, append = false) {
  const list = Array.isArray(items) ? [...items] : []
  if (!item || typeof item !== 'object') return list
  if (item.type === 'text') {
    const text = normalizeThinkingText(item.text)
    if (!text.trim()) return list
    const last = list[list.length - 1]
    if (append && last && last.type === 'text') {
      list[list.length - 1] = { ...last, text: normalizeThinkingText(last.text + text) }
      return list
    }
    list.push({ type: 'text', text })
    return list
  }
  if (item.type === 'step') {
    list.push({
      type: 'step',
      icon: item.icon || '▸',
      label: item.label || '',
      detail: item.detail || '',
    })
  }
  return list
}

/**
 * 历史行兼容：优先用落库的 thinking_items；缺失时按旧字段 rag_steps + thinking_text 合成
 * （旧数据无交错信息，步骤整体排前、思考排后）。
 */
export function buildThinkingItemsFromRow(row) {
  if (Array.isArray(row?.thinking_items) && row.thinking_items.length) {
    return row.thinking_items
      .map((it) => {
        if (!it || typeof it !== 'object') return null
        if (it.type === 'text') {
          const text = normalizeThinkingText(it.text)
          return text.trim() ? { type: 'text', text } : null
        }
        if (it.type === 'step') {
          return {
            type: 'step',
            icon: it.icon || '▸',
            label: it.label || '',
            detail: it.detail || '',
          }
        }
        return null
      })
      .filter(Boolean)
  }
  const items = []
  const steps = Array.isArray(row?.rag_steps) ? row.rag_steps : []
  for (const s of steps) {
    if (!s || typeof s !== 'object') continue
    items.push({ type: 'step', icon: s.icon || '▸', label: s.label || '', detail: s.detail || '' })
  }
  const text = normalizeThinkingText(row?.thinking_text)
  if (text.trim()) items.push({ type: 'text', text })
  return items
}

/** 思考胶囊在生成中展示最近一个工具步骤名（text 项不算步骤） */
export function lastThinkingStepLabel(m) {
  const items = Array.isArray(m?.thinkingItems) ? m.thinkingItems : []
  for (let i = items.length - 1; i >= 0; i--) {
    if (items[i]?.type === 'step' && items[i].label) return items[i].label
  }
  return ''
}

/**
 * 思考胶囊文案：排队提示 > 最近工具步骤 > 思考中 / 思考结束。
 * @param {object} m 消息行（含 pending / queuedWaiting / thinkingItems）
 * @param {Function} t vue-i18n 的 $t
 */
export function thinkingPillLabel(m, t) {
  if (m?.pending && m.queuedWaiting) {
    return t('views.agents.chat_feed_queued', { n: m.queuedWaiting })
  }
  if (m?.pending) return lastThinkingStepLabel(m) || t('views.agents.chat_feed_thinking')
  return t('views.agents.chat_feed_thinking_done')
}
