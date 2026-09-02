import { createHtmlPlugin } from 'vite-plugin-html'

export function configHtmlPlugin(viteEnv, isBuild) {
  const { VITE_TITLE } = viteEnv

  const htmlPlugin = createHtmlPlugin({
    minify: isBuild,
    inject: {
      data: {
        title: VITE_TITLE,
        cspScriptSrc: isBuild ? "'self'" : "'self' 'unsafe-eval'",
      },
    },
  })
  return htmlPlugin
}
