import MarkdownIt from 'markdown-it'
import texmath from 'markdown-it-texmath'
import katex from 'katex'
import 'katex/dist/katex.min.css'
import hljs from 'highlight.js'
import 'highlight.js/styles/github.css'
import DOMPurify from 'dompurify'

const md = new MarkdownIt({
  html: false,
  breaks: true,
  linkify: true,
  highlight(str, lang) {
    if (lang && hljs.getLanguage(lang)) {
      try {
        return `<pre class="hljs"><code>${hljs.highlight(str, { language: lang, ignoreIllegals: true }).value}</code></pre>`
      } catch {
        /* ignore */
      }
    }
    return `<pre class="hljs"><code>${md.utils.escapeHtml(str)}</code></pre>`
  },
}).use(texmath, { engine: katex, delimiters: 'dollars' })

/**
 * 将 Markdown + LaTeX（$...$ / $$...$$）转为可安全 v-html 的 HTML。
 */
export function renderAgentChatMarkdown(text) {
  if (text == null || text === '') return ''
  const raw = md.render(String(text))
  return DOMPurify.sanitize(raw, {
    USE_PROFILES: { html: true },
  })
}
