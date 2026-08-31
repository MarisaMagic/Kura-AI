/**
 * 离线图标集合生成脚本（dev / build 前自动执行）。
 *
 * 背景：TheIcon / renderIcon 使用 @iconify/vue 的 <Icon>，未注册的图标会在运行时
 * 请求 https://api.iconify.design，离线 / 内网 / 国内网络下会导致图标缺失。
 * 本脚本扫描 src 中的图标名（prefix:name），从 @iconify/json 提取对应图标数据，
 * 生成 src/assets/js/offline-icons.js；main.js 通过 addCollection 注册后完全离线。
 */
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const WEB_ROOT = path.resolve(__dirname, '..')
const SRC_DIR = path.resolve(WEB_ROOT, 'src')
const OUTPUT = path.resolve(SRC_DIR, 'assets/js/offline-icons.js')
const ICONIFY_JSON_DIR = path.resolve(WEB_ROOT, 'node_modules/@iconify/json/json')

// 后端代码中的图标（菜单 / MCP 预置），前端扫描不到，需与后端保持同步：
// app/core/init_app.py（菜单 icon）、app/mcp_client/presets.py（iconify 格式 icon）
const BACKEND_ICONS = [
  'carbon:gui-management',
  'material-symbols:person-outline-rounded',
  'carbon:user-role',
  'material-symbols:list-alt-outline',
  'ant-design:api-outlined',
  'mingcute:department-line',
  'ph:clipboard-text-bold',
  'material-symbols:smart-toy-outline',
  'mdi:github',
  'simple-icons:huggingface',
]

// 匹配 'prefix:name' 形式的图标名（prefix 允许 simple-icons 这类连字符）
const ICON_NAME_RE = /['"`]([a-z][a-z0-9]*(?:-[a-z0-9]+)*:[a-z0-9]+(?:-[a-z0-9]+)*)['"`]/g
const SCAN_EXT = new Set(['.vue', '.js', '.ts'])

function* walk(dir) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name)
    if (entry.isDirectory()) {
      if (entry.name === 'node_modules' || entry.name.startsWith('.')) continue
      yield* walk(full)
    } else if (SCAN_EXT.has(path.extname(entry.name))) {
      yield full
    }
  }
}

function collectIconNames() {
  const names = new Set(BACKEND_ICONS)
  for (const file of walk(SRC_DIR)) {
    if (path.resolve(file) === OUTPUT) continue
    const text = fs.readFileSync(file, 'utf-8')
    for (const m of text.matchAll(ICON_NAME_RE)) names.add(m[1])
    // icons.js（IconPicker 数据源）使用 'mdi-xxx' 连字符格式
    if (file.endsWith(path.join('assets', 'js', 'icons.js'))) {
      for (const m of text.matchAll(/['"]([a-z0-9-]+)['"]/g)) {
        const raw = m[1]
        if (raw.startsWith('mdi-')) names.add(`mdi:${raw.slice(4)}`)
      }
    }
  }
  return names
}

function extractSubset(data, wantedNames) {
  const icons = {}
  const aliases = {}
  const resolve = (name) => {
    if (data.icons[name]) return { icons: [name], aliases: [] }
    const alias = data.aliases?.[name]
    if (!alias) return null
    // alias 可能链式指向另一个 alias
    const chain = [name]
    let parent = alias.parent
    while (!data.icons[parent] && data.aliases?.[parent]) {
      chain.push(parent)
      parent = data.aliases[parent].parent
    }
    if (!data.icons[parent]) return null
    return { icons: [parent], aliases: chain }
  }
  const missing = []
  for (const name of wantedNames) {
    const r = resolve(name)
    if (!r) {
      missing.push(name)
      continue
    }
    for (const n of r.icons) icons[n] = data.icons[n]
    for (const n of r.aliases) aliases[n] = data.aliases[n]
  }
  if (missing.length) console.warn(`[offline-icons] ${data.prefix} 缺少图标: ${missing.join(', ')}`)
  const subset = { prefix: data.prefix, icons }
  for (const [k, v] of Object.entries(data)) {
    if (k !== 'icons' && k !== 'aliases' && k !== 'prefix') subset[k] = v
  }
  if (Object.keys(aliases).length) subset.aliases = aliases
  return subset
}

function main() {
  if (!fs.existsSync(ICONIFY_JSON_DIR)) {
    console.error('[offline-icons] 未找到 @iconify/json，请先执行 pnpm install')
    process.exit(1)
  }
  const names = collectIconNames()
  const byPrefix = new Map()
  for (const full of names) {
    const idx = full.indexOf(':')
    const prefix = full.slice(0, idx)
    const name = full.slice(idx + 1)
    if (!byPrefix.has(prefix)) byPrefix.set(prefix, new Set())
    byPrefix.get(prefix).add(name)
  }

  const collections = []
  for (const [prefix, wanted] of [...byPrefix.entries()].sort()) {
    const file = path.join(ICONIFY_JSON_DIR, `${prefix}.json`)
    if (!fs.existsSync(file)) {
      console.warn(`[offline-icons] @iconify/json 中不存在集合: ${prefix}`)
      continue
    }
    const data = JSON.parse(fs.readFileSync(file, 'utf-8'))
    collections.push(extractSubset(data, wanted))
  }

  const total = collections.reduce((n, c) => n + Object.keys(c.icons).length, 0)
  const body = `// 本文件由 build/generate-offline-icons.mjs 自动生成，请勿手改
export default ${JSON.stringify(collections)}
`
  fs.mkdirSync(path.dirname(OUTPUT), { recursive: true })
  fs.writeFileSync(OUTPUT, body, 'utf-8')
  const kb = (Buffer.byteLength(body) / 1024).toFixed(1)
  console.log(`[offline-icons] 已生成 ${collections.length} 个集合、${total} 个图标（${kb} KB）`)
}

main()
