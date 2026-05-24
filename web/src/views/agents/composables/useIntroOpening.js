import { nextTick, ref } from 'vue'

const INTRO_OPENING_START_DELAY_MS = 450
const INTRO_OPENING_CHAR_DELAY_MS = 72

/** 开场白打字机预览（不入库） */
export function useIntroOpening(getOpeningText) {
  const displayed = ref('')
  const running = ref(false)
  let generation = 0

  function stop() {
    generation += 1
    displayed.value = ''
    running.value = false
  }

  async function start() {
    const full = String(getOpeningText() || '').trim()
    if (!full) {
      stop()
      return
    }

    generation += 1
    const gen = generation
    displayed.value = ''
    running.value = true

    await nextTick()
    await new Promise((r) => setTimeout(r, INTRO_OPENING_START_DELAY_MS))
    if (gen !== generation) return

    for (let i = 0; i < full.length; i += 1) {
      if (gen !== generation) return
      displayed.value += full[i]
      await new Promise((r) => setTimeout(r, INTRO_OPENING_CHAR_DELAY_MS))
    }
    if (gen !== generation) return
    running.value = false
  }

  return { displayed, running, stop, start }
}
