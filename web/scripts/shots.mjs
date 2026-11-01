// 一次性生成几张界面截图（Playwright + dist，不需要 Windows / pywebview）。
// 用法：node scripts/shots.mjs <url> <outDir>
import { mkdirSync } from 'node:fs'
import { chromium } from 'playwright'

const url = process.argv[2] || 'http://127.0.0.1:8095/index.html'
const outDir = process.argv[3] || 'shots'
const exe = process.env.CHROMIUM || '/home/admin/.cache/ms-playwright/chromium-1194/chrome-linux/chrome'
mkdirSync(outDir, { recursive: true })

const browser = await chromium.launch({
  executablePath: exe,
  args: ['--no-sandbox', '--force-device-scale-factor=1'],
})
const page = await browser.newPage({ viewport: { width: 1440, height: 860 }, deviceScaleFactor: 1 })
const errors = []
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()) })
page.on('pageerror', (e) => errors.push(String(e)))

await page.goto(url, { waitUntil: 'networkidle' })
// 浏览器模式里 App 会在 whenReady(700) 之后灌一份演示状态，等它灌完再覆盖
await page.waitForTimeout(1400)

const set = (store, patch) => page.evaluate(
  ([s, p]) => { window.__pe[s].setState(p) },
  [store, patch],
)

const PACK = {
  path: 'D:\\Minecraft\\PCL\\.minecraft\\versions\\Pulses-Easier',
  name: 'Pulses-Easier',
  version: '1.20.1',
  loader: 'Fabric',
  mcVersion: '1.20.1',
  fileCount: 186,
  updatePack: { path: 'Pulses-Easier-2026.11.1.zip', size: 41234567, signature: 'ok', changelog: true },
}

const STRATEGIES = [
  { name: 'mods', display: 'mods', isFile: false, strategies: ['智能剔除', '完全匹配', '替换重名', '跳过重名'], current: '完全匹配', checked: true, changed: 3 },
  { name: 'config', display: 'config', isFile: false, strategies: ['智能剔除', '完全匹配', '替换重名', '跳过重名'], current: '智能剔除', checked: true, changed: 1 },
  { name: 'resourcepacks', display: 'resourcepacks', isFile: false, strategies: ['完全匹配', '替换重名', '跳过重名'], current: '完全匹配', checked: true, changed: 1 },
  { name: 'shaderpacks', display: 'shaderpacks', isFile: false, strategies: ['完全匹配', '替换重名', '跳过重名'], current: '完全匹配', checked: false, changed: 0 },
  { name: 'options.txt', display: 'options.txt', isFile: true, strategies: ['完全匹配', '保留键位', '替换重名', '跳过重名'], current: '保留键位', checked: true, changed: 1 },
]

const GROUPS = [
  {
    top: 'mods', counts: { added: 1, modified: 1, deleted: 0 }, skipped: 0, smartExcluded: 0,
    items: [
      { path: 'mods/sodium-0.6.5.jar', kind: 'modified' },
      { path: 'mods/iris-1.7.1.jar', kind: 'added' },
      { path: 'mods/lithium-0.12.1.jar', kind: 'modified', skipped: true },
    ],
  },
  {
    top: 'config', counts: { added: 0, modified: 1, deleted: 0 }, skipped: 0, smartExcluded: 2,
    items: [{ path: 'config/mine.toml', kind: 'modified' }],
  },
  {
    top: 'resourcepacks', counts: { added: 0, modified: 0, deleted: 1 }, skipped: 0, smartExcluded: 0,
    items: [{ path: 'resourcepacks/old-pack.zip', kind: 'deleted' }],
  },
  { top: 'shaderpacks', counts: { added: 0, modified: 0, deleted: 0 }, skipped: 0, smartExcluded: 0, items: [] },
]

async function base(patch = {}) {
  await set('useApp', {
    connected: false,
    booting: false,
    state: {
      version: '0.10.5',
      role: 'player',
      pack: PACK,
      recent: [
        { name: 'Pulses-Easier', path: PACK.path },
        { name: '1.20.1-Forge', path: 'D:\\Minecraft\\PCL\\.minecraft\\versions\\1.20.1-Forge' },
      ],
      settings: {},
      busy: false,
      job: null,
    },
    ...patch,
  })
  await set('usePlayer', {
    phase: 'idle',
    btn: 'confirm',
    hint: '比对完成：需要下载 3 个文件（含 1 项保留键位）',
    hintLevel: 'info',
    progress: null,
    strategies: STRATEGIES,
    groups: GROUPS,
    counts: { added: 1, modified: 2, deleted: 1 },
    pendingCount: 0,
    pendingMode: null,
    slots: [],
    slotsOpen: false,
    log: [
      { id: 1, level: 'INFO', text: '已导入更新包：Pulses-Easier-2026.11.1.zip' },
      { id: 2, level: 'INFO', text: '正在比对 186 个文件…' },
      { id: 3, level: 'SUCCESS', text: '比对完成：+1 ~2 -1，跳过 1，智能剔除 2' },
    ],
  })
  await set('useUi', { sheet: null, covering: false })
  const dbg = await page.evaluate(() => ({
    pack: window.__pe.useApp.getState().state?.pack?.name,
    groups: window.__pe.usePlayer.getState().groups?.length,
    counts: window.__pe.usePlayer.getState().counts,
  }))
  console.log('  [state]', JSON.stringify(dbg))
}

async function shot(name) {
  await page.waitForTimeout(900)
  const head = (await page.evaluate(() => document.body.innerText)).slice(0, 80).replace(/\n/g, ' | ')
  const hasCounts = await page.evaluate(() => document.body.innerText.includes('+1'))
  console.log(`  [${name}] +1=${hasCounts} ${head}`)
  await page.screenshot({ path: `${outDir}/${name}.png`, animations: 'disabled', caret: 'hide' })
  console.log('saved', `${outDir}/${name}.png`)
}

// 01 主界面（已扫描）
await base()
await shot('01-main')

// 02 下载中（进度 + 槽位）
await base()
await set('usePlayer', {
  phase: 'download',
  btn: 'busy',
  hint: '正在下载 3 个文件 · sodium-0.6.5.jar',
  progress: 46,
  slotsOpen: true,
  slots: [
    { name: 'sodium-0.6.5.jar', percent: 72, speed: '3.4 MB/s' },
    { name: 'iris-1.7.1.jar', percent: 31, speed: '2.1 MB/s' },
    { name: 'options.txt', percent: 100, speed: '已校验' },
  ],
})
await shot('02-download')

// 03 开发者端
await base({
  state: {
    version: '0.10.5', role: 'developer', pack: null, recent: [], settings: {}, busy: false, job: null,
    dev: {
      phase: 'idle', source: { path: 'D:\\packs\\Pulses-Easier.zip', name: 'Pulses-Easier.zip' },
      name: 'Pulses-Easier', version: '2026.11.1', files: 186,
      strategies: [
        { name: 'mods', current: '完全匹配', excluded: false, isFile: false },
        { name: 'config', current: '智能剔除', excluded: false, isFile: false },
        { name: 'options.txt', current: '保留键位', excluded: true, isFile: true },
      ],
      changelog: '## 2026.11.1\n\n- 更新 sodium / iris\n- 修了两处配置',
      options: { include_hashes: true, tamper_proof: true, precompute_overrides: true },
      outPath: 'D:\\packs\\Pulses-Easier-2026.11.1.eapack', error: null, progress: {},
    },
  },
})
await shot('03-developer')

// 04 首选项
await base()
await set('useUi', { sheet: 'preferences', covering: false })
await shot('04-preferences')

// 05 白名单
await base()
await set('useUi', { sheet: 'whitelist', covering: false })
await shot('05-whitelist')

// 06 受阻（待补入）
await base()
await set('usePlayer', { hint: '有 2 个文件没下下来，需要补入', hintLevel: 'warn', pendingCount: 2, pendingMode: 'complete' })
await shot('06-blocked')

await browser.close()
if (errors.length) {
  console.error('console errors:\n' + errors.join('\n'))
  process.exit(1)
}
console.log('done')
