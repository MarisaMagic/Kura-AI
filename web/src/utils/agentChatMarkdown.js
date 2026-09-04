import MarkdownIt from 'markdown-it'
import hljs from 'highlight.js/lib/core'
import bash from 'highlight.js/lib/languages/bash'
import css from 'highlight.js/lib/languages/css'
import javascript from 'highlight.js/lib/languages/javascript'
import json from 'highlight.js/lib/languages/json'
import markdownLang from 'highlight.js/lib/languages/markdown'
import python from 'highlight.js/lib/languages/python'
import sql from 'highlight.js/lib/languages/sql'
import typescript from 'highlight.js/lib/languages/typescript'
import xml from 'highlight.js/lib/languages/xml'
import yaml from 'highlight.js/lib/languages/yaml'
import DOMPurify from 'dompurify'
import './agentChatMarkdown.css'

hljs.registerLanguage('bash', bash)
hljs.registerLanguage('css', css)
hljs.registerLanguage('javascript', javascript)
hljs.registerLanguage('js', javascript)
hljs.registerLanguage('json', json)
hljs.registerLanguage('markdown', markdownLang)
hljs.registerLanguage('md', markdownLang)
hljs.registerLanguage('python', python)
hljs.registerLanguage('py', python)
hljs.registerLanguage('sql', sql)
hljs.registerLanguage('typescript', typescript)
hljs.registerLanguage('ts', typescript)
hljs.registerLanguage('xml', xml)
hljs.registerLanguage('html', xml)
hljs.registerLanguage('yaml', yaml)
hljs.registerLanguage('yml', yaml)

function highlightBlock(mdInst, str, lang) {
  if (lang && hljs.getLanguage(lang)) {
    try {
      return `<pre class="hljs"><code>${
        hljs.highlight(str, { language: lang, ignoreIllegals: true }).value
      }</code></pre>`
    } catch {
      /* ignore */
    }
  }
  return `<pre class="hljs"><code>${mdInst.utils.escapeHtml(str)}</code></pre>`
}

function createMarkdownIt() {
  const inst = new MarkdownIt({
    html: false,
    breaks: true,
    linkify: true,
    highlight(str, lang) {
      return highlightBlock(inst, str, lang)
    },
  })
  return inst
}

const md = createMarkdownIt()
let mdMath = null
let katexLoading = null

function looksLikeTex(text) {
  return /\$\$[\s\S]+?\$\$|\$[^$\n]+\$/.test(String(text || ''))
}

async function ensureMathMarkdown() {
  if (mdMath) return mdMath
  if (!katexLoading) {
    katexLoading = Promise.all([
      import('katex'),
      import('markdown-it-texmath'),
      import('katex/dist/katex.min.css'),
    ]).then(([katexMod, texmathMod]) => {
      const katex = katexMod.default || katexMod
      const texmath = texmathMod.default || texmathMod
      mdMath = createMarkdownIt().use(texmath, { engine: katex, delimiters: 'dollars' })
      applyCitationAndImageRules(mdMath)
      return mdMath
    })
  }
  return katexLoading
}

// 正文中的知识库引用标注：[来源N] / [来源 3] / [来源1、2]。
// 放在 inline ruler 而非文本替换，代码块与行内代码不会被误解析。
const KCITE_RE = /^\[来源\s*([\d\s、,，]+)\]/

function applyCitationAndImageRules(inst) {
  inst.inline.ruler.before('text', 'kura_citation', (state, silent) => {
    const match = KCITE_RE.exec(state.src.slice(state.pos))
    if (!match) return false
    if (!silent) {
      const token = state.push('kura_citation', '', 0)
      token.meta = { indices: match[1].split(/[\s、,，]+/).filter(Boolean) }
    }
    state.pos += match[0].length
    return true
  })

  inst.renderer.rules.kura_citation = (tokens, idx, _options, env) => {
    const indices = (tokens[idx].meta && tokens[idx].meta.indices) || []
    const sources = Array.isArray(env && env.kuraSources) ? env.kuraSources : []
    const byIndex = new Map(sources.map((s) => [String(s && s.index), s]))
    const esc = (v) => inst.utils.escapeHtml(String(v))
    const badges = indices.map((n) => {
      const src = byIndex.get(String(n))
      let title = src && (src.filename || src.title) ? String(src.filename || src.title) : ''
      if (title && src.page_number && src.page_number !== 'N/A') title += ` · P${src.page_number}`
      if (title && src.url) title += `\n${src.url}`
      return `<span class="kcite" data-kcite="${esc(n)}"${
        title ? ` title="${esc(title)}"` : ''
      }>${esc(n)}</span>`
    })
    return `<sup class="kcite-group">${badges.join('<span class="kcite-sep">,</span>')}</sup>`
  }

  const defaultImageRender =
    inst.renderer.rules.image ||
    ((tokens, idx, options, env, self) => self.renderToken(tokens, idx, options))
  inst.renderer.rules.image = (tokens, idx, options, env, self) => {
    tokens[idx].attrSet('referrerpolicy', 'no-referrer')
    // 流式正文里的图片惰性加载 + 异步解码，避免加载完成的回流行打断滚动位置
    tokens[idx].attrSet('loading', 'lazy')
    tokens[idx].attrSet('decoding', 'async')
    return defaultImageRender(tokens, idx, options, env, self)
  }
}

applyCitationAndImageRules(md)

// 知识库/头像等签名媒体：把 http(s)://host/api/v1/media/... 收成同源相对路径，
// 以通过 CSP img-src 'self'（绝对 http://127.0.0.1:9999 会被拦截）。
const MEDIA_PATH_RE =
  /\/api\/v1\/media\/(?:user_avatar|user_agents_avatar|user_agent_images)\/[^\s"'<>)]*/i
const ABS_MEDIA_URL_RE = new RegExp(`https?://[^/\\s"'<>)]+(${MEDIA_PATH_RE.source})`, 'gi')

export function toSameOriginMediaUrl(url) {
  const raw = String(url || '').trim()
  if (!raw) return raw
  const m = raw.match(new RegExp(`^https?://[^/]+(${MEDIA_PATH_RE.source})$`, 'i'))
  return m ? m[1] : raw
}

const BLOCKED_CHAT_HOSTS = new Set([
  'localhost',
  'metadata.google.internal',
  'metadata.google.com',
  'metadata.internal',
  'metadata.azure.internal',
  'instance-data.ec2.internal',
])

function isLiteralIpHost(host) {
  const h = String(host || '')
    .replace(/^\[|\]$/g, '')
    .toLowerCase()
  if (!h) return false
  if (/^\d+$/.test(h)) {
    const n = Number(h)
    return n >= 0 && n <= 0xffffffff
  }
  if (/^(\d{1,3}\.){3}\d{1,3}$/.test(h)) return true
  if (h.includes(':')) return true
  return false
}

function hasUnsafeChatUrlChars(raw) {
  return /[\s)"'<>]/.test(raw)
}

/** 同源签名媒体，或 https 且主机不是 localhost / 字面量 IP。 */
export function isSafeChatUrl(url) {
  const raw = String(url || '').trim()
  if (!raw || hasUnsafeChatUrlChars(raw)) return false
  if (new RegExp(`^${MEDIA_PATH_RE.source}$`, 'i').test(raw)) return true
  let parsed
  try {
    parsed = new URL(raw)
  } catch {
    return false
  }
  if (parsed.protocol !== 'https:') return false
  if (parsed.username || parsed.password) return false
  const host = (parsed.hostname || '').replace(/\.$/, '').toLowerCase()
  if (!host || BLOCKED_CHAT_HOSTS.has(host) || host.endsWith('.localhost')) return false
  if (isLiteralIpHost(host)) return false
  return true
}

function isSafeChatHref(url) {
  const raw = String(url || '').trim()
  if (!raw || hasUnsafeChatUrlChars(raw)) return false
  if (new RegExp(`^${MEDIA_PATH_RE.source}$`, 'i').test(raw)) return true
  let parsed
  try {
    parsed = new URL(raw)
  } catch {
    return false
  }
  if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:') return false
  if (parsed.username || parsed.password) return false
  const host = (parsed.hostname || '').replace(/\.$/, '').toLowerCase()
  if (!host || BLOCKED_CHAT_HOSTS.has(host) || host.endsWith('.localhost')) return false
  if (isLiteralIpHost(host)) {
    // 来源页允许公网字面量 IP，禁止回环/私网形态（与后端页面校验一致：只拦明显内网）
    if (
      host.includes(':') ||
      host === '127.0.0.1' ||
      host.startsWith('127.') ||
      host === '0.0.0.0'
    ) {
      return false
    }
    if (/^(10\.|192\.168\.|169\.254\.|172\.(1[6-9]|2\d|3[0-1])\.)/.test(host)) return false
    if (/^100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\./.test(host)) return false
  }
  return true
}

export function safeExternalHref(url) {
  const raw = String(url || '').trim()
  if (!raw) return ''
  const same = toSameOriginMediaUrl(raw)
  if (new RegExp(`^${MEDIA_PATH_RE.source}$`, 'i').test(same)) return same
  return isSafeChatHref(raw) ? raw : ''
}

function dropUnsafeUriHook(node, data) {
  if (data.attrName !== 'src' && data.attrName !== 'href') return
  const val = String(data.attrValue || '').trim()
  const tag = node.tagName
  if (data.attrName === 'src' && tag === 'IMG') {
    if (!isSafeChatUrl(val)) data.keepAttr = false
  }
  if (data.attrName === 'href' && tag === 'A') {
    if (val.startsWith('#')) return
    if (!isSafeChatHref(val)) data.keepAttr = false
  }
}

// 只认知识库签名媒体路径 /api/v1/media/...；外链 https 图（联网搜图）不当作坏链回退。
function firstMediaImageUrl(sources) {
  if (!Array.isArray(sources)) return null
  for (const s of sources) {
    const raw = s && s.image_url
    const u = toSameOriginMediaUrl(raw)
    if (u && new RegExp(`^${MEDIA_PATH_RE.source}$`, 'i').test(u)) return u
  }
  return null
}

// 修正常见坏图 Markdown：模型把文档标题/页码/stored_relpath 或没有签名的裸路径塞进 ![](...)
const MD_IMG_LINE = /^([ \t]*)!\[([^\]]*)\]\((.*)\)[ \t]*$/gm

export function rewriteMediaUrlsInText(text, sources) {
  if (text == null || text === '') return text
  let out = String(text).replace(ABS_MEDIA_URL_RE, (_, path) => path)
  const fallback = firstMediaImageUrl(sources)
  if (!fallback) return out
  const hasValidImg = new RegExp(`!\\[[^\\]]*\\]\\(\\s*${MEDIA_PATH_RE.source}\\s*\\)`, 'i').test(
    out
  )
  if (hasValidImg) return out
  let replaced = false
  out = out.replace(MD_IMG_LINE, (line, indent, alt, inner) => {
    if (replaced) return line
    const target = String(inner || '').trim()
    if (new RegExp(`^${MEDIA_PATH_RE.source}$`, 'i').test(target)) return line
    // 只修知识库坏链；不要把 https:// 外链图改写成 /api/v1/media/
    if (/^https:\/\//i.test(target)) return line
    replaced = true
    return `${indent}![${alt || '知识库图片'}](${fallback})`
  })
  return out
}

/**
 * 将 Markdown + LaTeX（$...$ / $$...$$）转为可安全 v-html 的 HTML。
 * sources（可选）：消息来源数组，与正文 [来源N] 编号对应，用于引用胶囊的 hover 提示。
 */
export function renderAgentChatMarkdown(text, sources) {
  if (text == null || text === '') return ''
  const kuraSources = Array.isArray(sources)
    ? sources.map((s) => {
        if (!s || typeof s !== 'object') return s
        const next = { ...s }
        if (next.image_url) next.image_url = toSameOriginMediaUrl(next.image_url)
        if (next.url) next.url = toSameOriginMediaUrl(next.url)
        return next
      })
    : sources
  const rewritten = rewriteMediaUrlsInText(String(text), kuraSources)
  const engine = looksLikeTex(rewritten) && mdMath ? mdMath : md
  if (looksLikeTex(rewritten) && !mdMath) {
    ensureMathMarkdown()
  }
  const raw = engine.render(rewritten, { kuraSources })
  DOMPurify.addHook('uponSanitizeAttribute', dropUnsafeUriHook)
  try {
    return DOMPurify.sanitize(raw, {
      USE_PROFILES: { html: true },
      ADD_ATTR: ['referrerpolicy', 'loading', 'decoding'],
    })
  } finally {
    DOMPurify.removeHook('uponSanitizeAttribute')
  }
}
