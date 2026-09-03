import vue from '@vitejs/plugin-vue'

/**
 * * unocss插件，原子css
 * https://github.com/antfu/unocss
 */
import Unocss from 'unocss/vite'

// rollup打包分析插件
import visualizer from 'rollup-plugin-visualizer'
// 压缩
import viteCompression from 'vite-plugin-compression'

import { configHtmlPlugin } from './html'
import unplugin from './unplugin'

export function createVitePlugins(viteEnv, isBuild) {
  const plugins = [vue(), ...unplugin, configHtmlPlugin(viteEnv, isBuild), Unocss()]

  if (viteEnv.VITE_USE_COMPRESS) {
    plugins.push(viteCompression({ algorithm: viteEnv.VITE_COMPRESS_TYPE || 'gzip' }))
  }

  if (isBuild) {
    const openVisualizer = process.env.CI !== 'true' && process.env.DOCKER_BUILD !== 'true'
    plugins.push(
      visualizer({
        open: openVisualizer,
        gzipSize: true,
        brotliSize: true,
      })
    )
  }

  return plugins
}
