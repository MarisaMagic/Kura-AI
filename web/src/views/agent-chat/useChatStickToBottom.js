import { nextTick, ref } from 'vue'

/** 距底部小于该值视为仍在底部（过小会因亚像素/惯性滚动误判离开） */
const SCROLL_BOTTOM_THRESHOLD = 72

/**
 * 对话列表 stick-to-bottom：仅根据用户滚动更新粘底标记，
 * 流式内容增长时只在粘底为 true 时自动滚底，避免打断上翻阅读。
 *
 * @param {import('vue').Ref<HTMLElement | null>} bodyScrollRef
 */
export function useChatStickToBottom(bodyScrollRef) {
  const stickToBottom = ref(true)
  const scrollAtBottom = ref(true)

  function measureGap() {
    const el = bodyScrollRef.value
    if (!el) return 0
    return el.scrollHeight - el.scrollTop - el.clientHeight
  }

  function isNearBottom() {
    return measureGap() <= SCROLL_BOTTOM_THRESHOLD
  }

  /** 只更新按钮显隐，不改粘底意图（用于程序化滚底之后） */
  function updateScrollBottomState() {
    const el = bodyScrollRef.value
    if (!el) {
      scrollAtBottom.value = true
      return
    }
    scrollAtBottom.value = isNearBottom()
  }

  /** 用户滚动：同时更新粘底与「是否在底部」 */
  function onBodyScroll() {
    const el = bodyScrollRef.value
    if (!el) {
      scrollAtBottom.value = true
      stickToBottom.value = true
      return
    }
    const atBottom = isNearBottom()
    scrollAtBottom.value = atBottom
    stickToBottom.value = atBottom
  }

  /**
   * @param {boolean} [smooth]
   * @param {{ force?: boolean }} [options] force 时无视粘底并重新开启跟随
   */
  function scrollBodyToBottom(smooth = false, { force = false } = {}) {
    if (force) stickToBottom.value = true
    nextTick(() => {
      if (!force && !stickToBottom.value) return
      const el = bodyScrollRef.value
      if (!el) {
        nextTick(() => updateScrollBottomState())
        return
      }
      const top = el.scrollHeight
      if (smooth) {
        el.scrollTo({ top, behavior: 'smooth' })
        window.setTimeout(() => updateScrollBottomState(), 500)
      } else {
        el.scrollTop = top
        nextTick(() => updateScrollBottomState())
      }
    })
  }

  return {
    stickToBottom,
    scrollAtBottom,
    onBodyScroll,
    scrollBodyToBottom,
    updateScrollBottomState,
  }
}
