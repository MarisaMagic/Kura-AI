<template>
  <n-config-provider
    wh-full
    :locale="zhCN"
    :date-locale="dateZhCN"
    :theme="appStore.isDark ? darkTheme : undefined"
    :theme-overrides="naiveThemeOverrides"
  >
    <n-loading-bar-provider>
      <n-dialog-provider>
        <n-notification-provider>
          <n-message-provider>
            <slot></slot>
            <NaiveProviderContent />
          </n-message-provider>
        </n-notification-provider>
      </n-dialog-provider>
    </n-loading-bar-provider>
  </n-config-provider>
</template>

<script setup>
import { defineComponent, h, watch } from 'vue'
import {
  zhCN,
  dateZhCN,
  darkTheme,
  useLoadingBar,
  useDialog,
  useMessage,
  useNotification,
  useThemeVars,
} from 'naive-ui'
import { useCssVar } from '@vueuse/core'
import { kebabCase } from 'lodash-es'
import { setupMessage, setupDialog } from '@/utils'
import { applyHljsTheme } from '@/utils/hljsTheme'
import { naiveThemeOverrides } from '~/settings'
import { useAppStore } from '@/store'

const appStore = useAppStore()

watch(
  () => appStore.isDark,
  (dark) => applyHljsTheme(!!dark),
  { immediate: true }
)

function setupCssVar() {
  const common = naiveThemeOverrides.common
  for (const key in common) {
    useCssVar(`--${kebabCase(key)}`, document.documentElement).value = common[key] || ''
    if (key === 'primaryColor') window.localStorage.setItem('__THEME_COLOR__', common[key] || '')
  }
}

// --n-* 语义变量注入 :root：AgentEditorLayout 等纯 div 布局不在任何 Naive 组件内，
// 普通元素上的 var(--n-*) 会未定义（文字黑、边框消失）；组件自身定义更近，不受影响
function setupNaiveThemeVars() {
  const themeVars = useThemeVars()
  const mapping = {
    '--n-text-color': 'textColor1',
    '--n-text-color-2': 'textColor2',
    '--n-text-color-3': 'textColor3',
    '--n-divider-color': 'dividerColor',
    '--n-border-color': 'borderColor',
    '--n-color': 'bodyColor',
    '--n-color-embedded': 'actionColor',
    '--n-primary-color': 'primaryColor',
    '--n-warning-color': 'warningColor',
    '--n-error-color': 'errorColor',
  }
  const targets = {}
  for (const cssVar in mapping) {
    targets[cssVar] = useCssVar(cssVar, document.documentElement)
  }
  watch(
    themeVars,
    (tv) => {
      for (const cssVar in mapping) {
        targets[cssVar].value = tv[mapping[cssVar]] || ''
      }
    },
    { immediate: true, deep: true }
  )
}

// 挂载naive组件的方法至window, 以便在全局使用
function setupNaiveTools() {
  window.$loadingBar = useLoadingBar()
  window.$notification = useNotification()

  window.$message = setupMessage(useMessage())
  window.$dialog = setupDialog(useDialog())
}

const NaiveProviderContent = defineComponent({
  setup() {
    setupCssVar()
    setupNaiveThemeVars()
    setupNaiveTools()
  },
  render() {
    return h('div')
  },
})
</script>
