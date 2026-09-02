import dayjs from 'dayjs'

/**
 * * 此处定义的是全局常量，启动或打包后将添加到window中
 * https://vitejs.cn/config/#define
 */

// 项目构建时间
const _BUILD_TIME_ = JSON.stringify(dayjs().format('YYYY-MM-DD HH:mm:ss'))

export const viteDefine = {
  _BUILD_TIME_,
  // vue-i18n 默认 compileToFunction 会 new Function，生产 CSP 无 unsafe-eval 会白屏
  __INTLIFY_JIT_COMPILATION__: true,
}
