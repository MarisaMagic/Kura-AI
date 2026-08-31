import 'virtual:svg-icons-register'

/** 离线注册 iconify 图标，避免运行时请求 api.iconify.design（文件由 predev/prebuild 生成） */
import { addCollection } from '@iconify/vue'
import offlineIconCollections from '@/assets/js/offline-icons'

offlineIconCollections.forEach((collection) => addCollection(collection))

/** 重置样式 */
import '@/styles/reset.css'
/** 思源黑体（简体）：Noto Sans SC，与 Source Han Sans 同源 */
import '@fontsource/noto-sans-sc/400.css'
import '@fontsource/noto-sans-sc/500.css'
import '@fontsource/noto-sans-sc/700.css'
import 'uno.css'
import '@/styles/global.scss'

import { createApp } from 'vue'
import { setupRouter } from '@/router'
import { setupStore } from '@/store'
import App from './App.vue'
import { setupDirectives } from './directives'
import { useResize } from '@/utils'
import i18n from '~/i18n'

async function setupApp() {
  const app = createApp(App)

  setupStore(app)

  await setupRouter(app)
  setupDirectives(app)
  app.use(useResize)
  app.use(i18n)
  app.mount('#app')
}

setupApp()
