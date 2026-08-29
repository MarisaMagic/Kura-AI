import stackoverflowLight from 'highlight.js/styles/stackoverflow-light.css?url'
import stackoverflowDark from 'highlight.js/styles/stackoverflow-dark.css?url'

const LINK_ID = 'hljs-theme'

/** 按亮/暗色切换 highlight.js 样式表：StackOverflow Light / Dark。 */
export function applyHljsTheme(isDark) {
  let link = document.getElementById(LINK_ID)
  if (!link) {
    link = document.createElement('link')
    link.id = LINK_ID
    link.rel = 'stylesheet'
    document.head.appendChild(link)
  }
  link.href = isDark ? stackoverflowDark : stackoverflowLight
}
