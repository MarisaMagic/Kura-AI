import { nextTick, ref, watchEffect } from 'vue'

/** 距底部小于该值视为仍在底部（过小会因亚像素/惯性滚动误判离开） */
const SCROLL_BOTTOM_THRESHOLD = 72

/** 滚轮/键盘输入后这段时间内的 scroll 事件视为用户意图（覆盖惯性滚动尾音） */
const USER_SCROLL_GRACE_MS = 200

/**
 * 对话列表 stick-to-bottom：仅根据用户滚动更新粘底标记，
 * 流式内容增长时只在粘底为 true 时自动滚底，避免打断上翻阅读。
 *
 * 粘底标记只由真实用户输入（滚轮 / 键盘 / 拖动滚动条）更新：
 * 程序化滚底与虚拟列表高度补偿产生的 scroll 事件一律忽略，
 * 否则它们会反复把 stickToBottom 翻回 true，形成「上滑又被拉回」的震荡。
 *
 * @param {import('vue').Ref<HTMLElement | null>} bodyScrollRef
 */
export function useChatStickToBottom(bodyScrollRef) {
  const stickToBottom = ref(true)
  const scrollAtBottom = ref(true)

  // —— 用户输入识别 ——
  let lastUserInputAt = 0
  let scrollbarDragging = false

  function noteUserInput() {
    lastUserInputAt = Date.now()
  }

  function isUserScrollEvent() {
    return scrollbarDragging || Date.now() - lastUserInputAt < USER_SCROLL_GRACE_MS
  }

  function resolveEl() {
    const v = bodyScrollRef.value
    if (!v) return null
    if (typeof v.getScrollEl === 'function') return v.getScrollEl()
    if (v instanceof HTMLElement) return v
    return v.$el instanceof HTMLElement ? v.$el : null
  }

  // 滚动元素随 phase（intro 外层 div ↔ chat 虚拟列表）切换，动态挂/卸输入监听
  watchEffect((onCleanup) => {
    const el = resolveEl()
    if (!el) return
    const onWheel = () => noteUserInput()
    // 原生滚动条不产生子元素，命中容器右侧滚动条区即视为拖动意图
    const onPointerDown = (e) => {
      if (e.target === el && e.offsetX >= el.clientWidth - 1) scrollbarDragging = true
    }
    const onPointerUp = () => {
      scrollbarDragging = false
    }
    const onKeyDown = () => noteUserInput()
    el.addEventListener('wheel', onWheel, { passive: true })
    el.addEventListener('pointerdown', onPointerDown)
    el.addEventListener('keydown', onKeyDown)
    window.addEventListener('pointerup', onPointerUp)
    onCleanup(() => {
      el.removeEventListener('wheel', onWheel)
      el.removeEventListener('pointerdown', onPointerDown)
      el.removeEventListener('keydown', onKeyDown)
      window.removeEventListener('pointerup', onPointerUp)
      scrollbarDragging = false
    })
  })

  function measureGap() {
    const el = resolveEl()
    if (!el) return 0
    return el.scrollHeight - el.scrollTop - el.clientHeight
  }

  function isNearBottom() {
    return measureGap() <= SCROLL_BOTTOM_THRESHOLD
  }

  /** 只更新按钮显隐，不改粘底意图（用于程序化滚底之后） */
  function updateScrollBottomState() {
    const el = resolveEl()
    if (!el) {
      scrollAtBottom.value = true
      return
    }
    scrollAtBottom.value = isNearBottom()
  }

  /** 用户滚动：同时更新粘底与「是否在底部」 */
  function onBodyScroll() {
    const el = resolveEl()
    if (!el) {
      scrollAtBottom.value = true
      stickToBottom.value = true
      return
    }
    if (!isUserScrollEvent()) return
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
      const el = resolveEl()
      if (!el) {
        nextTick(() => updateScrollBottomState())
        return
      }
      const top = el.scrollHeight
      // 已在底部（含虚拟列表高度补偿刚把视口推到位）时不重复写 scrollTop，
      // 避免与 vueuc 的 ResizeObserver 补偿同帧互相覆盖造成抖动
      if (el.scrollTop >= top - 1) {
        nextTick(() => updateScrollBottomState())
        return
      }
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
