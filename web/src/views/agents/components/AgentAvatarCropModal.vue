<template>
  <n-modal
    v-model:show="visible"
    preset="card"
    :title="$t('views.agents.avatar_crop_title')"
    :style="{ width: 'min(420px, 92vw)' }"
    :mask-closable="false"
    @after-leave="onAfterLeave"
  >
    <div class="agent-avatar-crop">
      <p class="agent-avatar-crop__hint">{{ $t('views.agents.avatar_crop_hint') }}</p>
      <div
        class="agent-avatar-crop__viewport"
        @pointerdown="onPointerDown"
        @pointermove="onPointerMove"
        @pointerup="onPointerUp"
        @pointercancel="onPointerUp"
      >
        <img
          v-if="imgSrc"
          ref="imgRef"
          :src="imgSrc"
          alt=""
          class="agent-avatar-crop__img"
          draggable="false"
          :style="imgStyle"
          @load="onImgLoad"
        />
      </div>
      <div class="agent-avatar-crop__zoom">
        <span class="agent-avatar-crop__zoom-label">{{ $t('views.agents.avatar_crop_zoom') }}</span>
        <n-slider
          v-model:value="userZoom"
          :min="1"
          :max="3"
          :step="0.02"
          :tooltip="false"
          class="agent-avatar-crop__slider"
        />
      </div>
      <div class="agent-avatar-crop__actions">
        <n-button @click="onCancel">{{ $t('views.agents.avatar_crop_cancel') }}</n-button>
        <n-button type="primary" :loading="exporting" :disabled="!ready" @click="onConfirm">
          {{ $t('views.agents.avatar_crop_confirm') }}
        </n-button>
      </div>
    </div>
  </n-modal>
</template>

<script setup>
import { computed, onUnmounted, ref, watch } from 'vue'
import { NButton, NModal, NSlider } from 'naive-ui'

const VIEW = 280
const OUTPUT = 512

const props = defineProps({
  show: { type: Boolean, default: false },
  file: { type: [File, Object], default: null },
})

const emit = defineEmits(['update:show', 'confirm'])

const visible = computed({
  get: () => props.show,
  set: (v) => emit('update:show', v),
})

const imgRef = ref(null)
const imgSrc = ref('')
const imgNaturalW = ref(0)
const imgNaturalH = ref(0)
const panX = ref(0)
const panY = ref(0)
const userZoom = ref(1)
const exporting = ref(false)
const dragging = ref(false)
let dragLastX = 0
let dragLastY = 0

const ready = computed(() => imgNaturalW.value > 0 && imgNaturalH.value > 0)

const baseScale = computed(() => {
  const nw = imgNaturalW.value
  const nh = imgNaturalH.value
  if (!nw || !nh) return 1
  return Math.max(VIEW / nw, VIEW / nh)
})

const displayScale = computed(() => baseScale.value * userZoom.value)

const imgW = computed(() => imgNaturalW.value * displayScale.value)
const imgH = computed(() => imgNaturalH.value * displayScale.value)

const imgStyle = computed(() => {
  const w = imgW.value
  const h = imgH.value
  const left = (VIEW - w) / 2 + panX.value
  const top = (VIEW - h) / 2 + panY.value
  return {
    position: 'absolute',
    left: `${left}px`,
    top: `${top}px`,
    width: `${w}px`,
    height: `${h}px`,
    userSelect: 'none',
    pointerEvents: 'none',
    touchAction: 'none',
  }
})

function clampPan() {
  const w = imgW.value
  const h = imgH.value
  if (!w || !h) return
  const minX = VIEW / 2 - w / 2
  const maxX = w / 2 - VIEW / 2
  const minY = VIEW / 2 - h / 2
  const maxY = h / 2 - VIEW / 2
  panX.value = Math.min(maxX, Math.max(minX, panX.value))
  panY.value = Math.min(maxY, Math.max(minY, panY.value))
}

function revokeSrc() {
  if (imgSrc.value && imgSrc.value.startsWith('blob:')) {
    URL.revokeObjectURL(imgSrc.value)
  }
  imgSrc.value = ''
}

watch(
  () => [props.show, props.file],
  () => {
    revokeSrc()
    imgNaturalW.value = 0
    imgNaturalH.value = 0
    panX.value = 0
    panY.value = 0
    userZoom.value = 1
    if (props.show && props.file) {
      imgSrc.value = URL.createObjectURL(props.file)
    }
  },
  { flush: 'sync' }
)

watch(userZoom, () => {
  clampPan()
})

function onImgLoad(e) {
  const el = e.target
  imgNaturalW.value = el.naturalWidth || 0
  imgNaturalH.value = el.naturalHeight || 0
  panX.value = 0
  panY.value = 0
  userZoom.value = 1
  clampPan()
}

function onPointerDown(ev) {
  if (!ready.value) return
  dragging.value = true
  dragLastX = ev.clientX
  dragLastY = ev.clientY
  try {
    ev.currentTarget?.setPointerCapture?.(ev.pointerId)
  } catch {
    /* ignore */
  }
}

function onPointerMove(ev) {
  if (!dragging.value) return
  const dx = ev.clientX - dragLastX
  const dy = ev.clientY - dragLastY
  dragLastX = ev.clientX
  dragLastY = ev.clientY
  panX.value += dx
  panY.value += dy
  clampPan()
}

function onPointerUp() {
  dragging.value = false
}

function onCancel() {
  visible.value = false
}

function onAfterLeave() {
  dragging.value = false
}

function onConfirm() {
  const img = imgRef.value
  if (!img || !ready.value) return
  exporting.value = true
  const w = imgW.value
  const h = imgH.value
  const il = (VIEW - w) / 2 + panX.value
  const it = (VIEW - h) / 2 + panY.value
  const k = OUTPUT / VIEW

  const canvas = document.createElement('canvas')
  canvas.width = OUTPUT
  canvas.height = OUTPUT
  const ctx = canvas.getContext('2d')
  if (!ctx) {
    exporting.value = false
    return
  }
  ctx.save()
  ctx.beginPath()
  ctx.arc(OUTPUT / 2, OUTPUT / 2, OUTPUT / 2, 0, Math.PI * 2)
  ctx.clip()
  ctx.drawImage(img, 0, 0, imgNaturalW.value, imgNaturalH.value, il * k, it * k, w * k, h * k)
  ctx.restore()

  canvas.toBlob(
    (blob) => {
      exporting.value = false
      if (!blob) return
      const out = new File([blob], 'avatar.png', { type: 'image/png' })
      emit('confirm', out)
      visible.value = false
    },
    'image/png',
    0.92
  )
}

onUnmounted(() => {
  revokeSrc()
})
</script>

<style scoped>
.agent-avatar-crop {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.agent-avatar-crop__hint {
  margin: 0;
  font-size: 13px;
  line-height: 1.5;
  color: var(--n-text-color-3);
}

.agent-avatar-crop__viewport {
  position: relative;
  width: 280px;
  height: 280px;
  margin: 0 auto;
  border-radius: 50%;
  overflow: hidden;
  background: var(--n-color-modal);
  box-shadow: inset 0 0 0 1px rgba(0, 0, 0, 0.08);
  cursor: grab;
  touch-action: none;
}

.agent-avatar-crop__viewport:active {
  cursor: grabbing;
}

html.dark .agent-avatar-crop__viewport {
  box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.12);
}

.agent-avatar-crop__img {
  display: block;
}

.agent-avatar-crop__zoom {
  display: flex;
  align-items: center;
  gap: 12px;
}

.agent-avatar-crop__zoom-label {
  flex-shrink: 0;
  font-size: 13px;
  color: var(--n-text-color-2);
}

.agent-avatar-crop__slider {
  flex: 1;
  min-width: 0;
}

.agent-avatar-crop__actions {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
  margin-top: 4px;
}
</style>
