import { defineStore } from 'pinia'
import { useDark } from '@vueuse/core'
import { lStorage } from '@/utils'
import i18n from '~/i18n'

const currentLocale = lStorage.get('locale')
const currentCollapsed = lStorage.get('collapsed')
const { locale } = i18n.global

const isDark = useDark()
export const useAppStore = defineStore('app', {
  state() {
    return {
      reloadFlag: true,
      collapsed: currentCollapsed ?? false,
      fullScreen: true,
      /** keepAlive路由的key，重新赋值可重置keepAlive */
      aliveKeys: {},
      isDark,
      locale: currentLocale || 'en',
    }
  },
  actions: {
    async reloadPage() {
      $loadingBar.start()
      this.reloadFlag = false
      await nextTick()
      this.reloadFlag = true

      setTimeout(() => {
        document.documentElement.scrollTo({ left: 0, top: 0 })
        $loadingBar.finish()
      }, 100)
    },
    switchCollapsed() {
      this.collapsed = !this.collapsed
      lStorage.set('collapsed', this.collapsed)
    },
    setCollapsed(collapsed) {
      this.collapsed = collapsed
    },
    /** 恢复用户手动折叠偏好（响应式断点强制折叠不写入偏好） */
    restoreCollapsedPref() {
      this.collapsed = lStorage.get('collapsed') ?? false
    },
    setFullScreen(fullScreen) {
      this.fullScreen = fullScreen
    },
    setAliveKeys(key, val) {
      this.aliveKeys[key] = val
    },
    /** 设置暗黑模式 */
    setDark(isDark) {
      this.isDark = isDark
    },
    /** 切换/关闭 暗黑模式 */
    toggleDark() {
      this.isDark = !this.isDark
    },
    setLocale(newLocale) {
      this.locale = newLocale
      locale.value = newLocale
      lStorage.set('locale', newLocale)
    },
  },
})
