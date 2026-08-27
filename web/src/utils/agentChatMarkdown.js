import MarkdownIt from 'markdown-it'
import texmath from 'markdown-it-texmath'
import katex from 'katex'
import 'katex/dist/katex.min.css'
import hljs from 'highlight.js'
import 'highlight.js/styles/github.css'
import DOMPurify from 'dompurify'
import './agentChatMarkdown.css'

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

// 正文中的知识库引用标注：[来源N] / [来源 3] / [来源1、2]。
// 放在 inline ruler 而非文本替换，代码块与行内代码不会被误解析。
const KCITE_RE = /^\[来源\s*([\d\s、,，]+)\]/

md.inline.ruler.before('text', 'kura_citation', (state, silent) => {
  const match = KCITE_RE.exec(state.src.slice(state.pos))
  if (!match) return false
  if (!silent) {
    const token = state.push('kura_citation', '', 0)
    token.meta = { indices: match[1].split(/[\s、,，]+/).filter(Boolean) }
  }
  state.pos += match[0].length
  return true
})

md.renderer.rules.kura_citation = (tokens, idx, _options, env) => {
  const indices = (tokens[idx].meta && tokens[idx].meta.indices) || []
  const sources = Array.isArray(env && env.kuraSources) ? env.kuraSources : []
  const byIndex = new Map(sources.map((s) => [String(s && s.index), s]))
  const esc = (v) => md.utils.escapeHtml(String(v))
  const badges = indices.map((n) => {
    const src = byIndex.get(String(n))
    let title = src && src.filename ? String(src.filename) : ''
    if (title && src.page_number && src.page_number !== 'N/A') title += ` · P${src.page_number}`
    return `<span class="kcite" data-kcite="${esc(n)}"${
      title ? ` title="${esc(title)}"` : ''
    }>${esc(n)}</span>`
  })
  return `<sup class="kcite-group">${badges.join('<span class="kcite-sep">,</span>')}</sup>`
}

/**
 * 将 Markdown + LaTeX（$...$ / $$...$$）转为可安全 v-html 的 HTML。
 * sources（可选）：消息来源数组，与正文 [来源N] 编号对应，用于引用胶囊的 hover 提示。
 */
export function renderAgentChatMarkdown(text, sources) {
  if (text == null || text === '') return ''
  const raw = md.render(String(text), { kuraSources: sources })
  return DOMPurify.sanitize(raw, {
    USE_PROFILES: { html: true },
  })
}
